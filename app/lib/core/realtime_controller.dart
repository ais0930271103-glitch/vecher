import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';

const realtimeScopes = {
  'today',
  'wishes',
  'plans',
  'chat',
  'us',
  'solo',
  'pair',
  'all',
};
final _uuid = RegExp(
  r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$',
);

class RealtimeSession {
  const RealtimeSession.disabled()
    : enabled = false,
      url = '',
      token = '',
      channel = '';

  const RealtimeSession({
    required this.url,
    required this.token,
    required this.channel,
  }) : enabled = true;

  final bool enabled;
  final String url;
  final String token;
  final String channel;

  factory RealtimeSession.parse(
    Object? value, {
    bool allowLocalWs = false,
    DateTime? now,
  }) {
    if (value is! Map || value['enabled'] is! bool) {
      throw const FormatException('Invalid realtime session');
    }
    if (value['enabled'] == false) return const RealtimeSession.disabled();
    final url = value['url'];
    final token = value['token'];
    final channel = value['channel'];
    final expiresAt = value['expires_at'];
    if (url is! String ||
        token is! String ||
        channel is! String ||
        expiresAt is! String) {
      throw const FormatException('Incomplete realtime session');
    }
    final uri = Uri.tryParse(url);
    final expires = DateTime.tryParse(expiresAt);
    final local =
        uri != null &&
        {'localhost', '127.0.0.1', '::1', '10.0.2.2'}.contains(uri.host);
    if (uri == null ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        !(uri.scheme == 'wss' ||
            (allowLocalWs && local && uri.scheme == 'ws')) ||
        !channel.startsWith('events:') ||
        !_uuid.hasMatch(channel.substring(7)) ||
        token.isEmpty ||
        token.length > 16384 ||
        expires == null ||
        !expires.isUtc ||
        !expires.isAfter(now ?? DateTime.now())) {
      throw const FormatException('Invalid realtime session');
    }
    return RealtimeSession(url: url, token: token, channel: channel);
  }
}

enum RealtimeLinkState { connecting, subscribed, disconnected }

class RealtimePublication {
  const RealtimePublication(this.channel, this.data);
  final String channel;
  final List<int> data;
}

/// Only the official client adapter knows the wire protocol.
abstract interface class RealtimeTransport {
  Stream<RealtimeLinkState> get states;
  Stream<RealtimePublication> get publications;
  Future<void> connect();
  Future<void> close();
}

class RealtimeSessionEnded implements Exception {
  const RealtimeSessionEnded();
}

typedef RealtimeTransportFactory =
    RealtimeTransport Function(
      RealtimeSession session,
      Future<String> Function() refreshToken,
    );

/// One foreground account session. Publications only invalidate REST data.
/// Every asynchronous boundary checks the generation; stopping is synchronous.
class RealtimeController extends ChangeNotifier {
  RealtimeController({
    required Future<RealtimeSession> Function() loadSession,
    required RealtimeTransportFactory createTransport,
    this.debounce = const Duration(milliseconds: 150),
    this.retryDelay = const Duration(seconds: 15),
    this.disabledRetryDelay = const Duration(minutes: 5),
    this.subscriptionTimeout = const Duration(seconds: 30),
  }) : _loadSession = loadSession,
       _createTransport = createTransport;

  final Future<RealtimeSession> Function() _loadSession;
  final RealtimeTransportFactory _createTransport;
  final Duration debounce, retryDelay, disabledRetryDelay, subscriptionTimeout;
  final _invalidations = StreamController<Set<String>>.broadcast(sync: true);
  final _pending = <String>{};
  final _seen =
      <
        String
      >{}; // Dart sets preserve insertion order for bounded deduplication.
  StreamSubscription<RealtimeLinkState>? _stateSubscription;
  StreamSubscription<RealtimePublication>? _publicationSubscription;
  RealtimeTransport? _transport;
  Timer? _retry, _flush, _subscriptionWatchdog;
  String? _identity;
  int _generation = 0;
  bool _active = false, _connected = false, _disposed = false;

  bool get connected => _connected;
  bool get active => _active;
  Stream<Set<String>> get invalidations => _invalidations.stream;

  void activate(String identity) {
    if (_disposed || identity.isEmpty || (_active && identity == _identity)) {
      return;
    }
    deactivate();
    _identity = identity;
    _active = true;
    // REST resync also happens when the old server has no realtime endpoint.
    _queue({'all'});
    unawaited(_start());
  }

  void deactivate() {
    _active = false;
    _identity = null;
    _generation++;
    _retry?.cancel();
    _flush?.cancel();
    _retry = _flush = null;
    _pending.clear();
    _seen.clear();
    _detachTransport();
    _setConnected(false);
  }

  bool _current(int generation) =>
      !_disposed && _active && generation == _generation;

  Future<void> _start() async {
    if (!_active || _disposed) return;
    final generation = ++_generation;
    _retry?.cancel();
    _detachTransport();
    _setConnected(false);
    try {
      final session = await _loadSession();
      if (!_current(generation)) return;
      if (!session.enabled) {
        _retryLater(generation, disabledRetryDelay);
        return;
      }
      final transport = _createTransport(
        session,
        () => _refreshToken(generation, session),
      );
      _transport = transport;
      _stateSubscription = transport.states.listen((state) {
        if (!_current(generation)) return;
        if (state == RealtimeLinkState.subscribed) {
          _subscriptionWatchdog?.cancel();
          _setConnected(true);
          // A missed event or unrecoverable history is harmless: REST is authoritative.
          _queue({'all'});
        } else {
          _setConnected(false);
          if (state == RealtimeLinkState.disconnected) {
            _retryLater(generation, retryDelay);
          } else {
            _watchSubscription(generation);
          }
        }
      }, onError: (_) => _retryLater(generation, retryDelay));
      _publicationSubscription = transport.publications.listen((publication) {
        if (_current(generation) && publication.channel == session.channel) {
          _receive(publication.data);
        }
      }, onError: (_) => _retryLater(generation, retryDelay));
      _watchSubscription(generation);
      await transport.connect();
    } catch (_) {
      // Deliberately do not log exceptions: HTTP/socket errors may contain JWTs.
      _retryLater(generation, retryDelay);
    }
  }

  Future<String> _refreshToken(int generation, RealtimeSession previous) async {
    if (!_current(generation)) throw const RealtimeSessionEnded();
    final next = await _loadSession();
    if (!_current(generation)) throw const RealtimeSessionEnded();
    if (!next.enabled ||
        next.channel != previous.channel ||
        next.url != previous.url) {
      _retryLater(generation, next.enabled ? retryDelay : disabledRetryDelay);
      throw const RealtimeSessionEnded();
    }
    return next.token;
  }

  void _watchSubscription(int generation) {
    _subscriptionWatchdog?.cancel();
    _subscriptionWatchdog = Timer(
      subscriptionTimeout,
      () => _retryLater(generation, retryDelay),
    );
  }

  void _retryLater(int generation, Duration delay) {
    if (!_current(generation)) return;
    final waitingGeneration = ++_generation;
    _detachTransport();
    _setConnected(false);
    _retry?.cancel();
    _retry = Timer(delay, () {
      if (_current(waitingGeneration)) unawaited(_start());
    });
  }

  void _detachTransport() {
    _subscriptionWatchdog?.cancel();
    _subscriptionWatchdog = null;
    final stateSubscription = _stateSubscription;
    final publicationSubscription = _publicationSubscription;
    final transport = _transport;
    _stateSubscription = null;
    _publicationSubscription = null;
    _transport = null;
    if (stateSubscription != null) unawaited(stateSubscription.cancel());
    if (publicationSubscription != null) {
      unawaited(publicationSubscription.cancel());
    }
    if (transport != null) unawaited(transport.close().catchError((_) {}));
  }

  void _setConnected(bool value) {
    if (_connected == value) return;
    _connected = value;
    if (!_disposed) notifyListeners();
  }

  void _receive(List<int> bytes) {
    if (bytes.length > 4096) return;
    try {
      final data = jsonDecode(utf8.decode(bytes));
      if (data is! Map ||
          data['version'] is! int ||
          data['version'] != 1 ||
          data['type'] != 'invalidate') {
        return;
      }
      final id = data['event_id'];
      final scopes = data['scopes'];
      if (id is! String ||
          !_uuid.hasMatch(id) ||
          scopes is! List ||
          scopes.length > 16) {
        return;
      }
      final known = scopes
          .whereType<String>()
          .where(realtimeScopes.contains)
          .toSet();
      if (known.isEmpty || !_seen.add(id)) return;
      if (_seen.length > 256) _seen.remove(_seen.first);
      _queue(known);
    } catch (_) {
      // Unknown versions or malformed payloads cannot affect application data.
    }
  }

  void _queue(Set<String> scopes) {
    if (!_active || _disposed) return;
    _pending.addAll(scopes);
    // Fixed window, not a trailing debounce: continuous traffic cannot starve UI updates.
    _flush ??= Timer(debounce, () {
      _flush = null;
      if (!_active || _disposed || _pending.isEmpty) return;
      final scopes = Set<String>.unmodifiable(_pending);
      _pending.clear();
      _invalidations.add(scopes);
    });
  }

  @override
  void dispose() {
    if (_disposed) return;
    deactivate();
    _disposed = true;
    unawaited(_invalidations.close());
    super.dispose();
  }
}
