import 'dart:async';
import 'dart:collection';
import 'dart:math';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';

import '../../core/api.dart' show errorText;
import 'call_api.dart';

enum CallPhase {
  loading,
  ringing,
  connecting,
  connected,
  reconnecting,
  ended,
  failed,
}

/// Injectable media boundary: the state machine is tested without opening a camera.
abstract class CallPeer {
  void Function(Map<String, dynamic> candidate)? onLocalIce;
  void Function(String state)? onConnection;
  Future<void> initialize(List<Map<String, dynamic>> iceServers);
  Future<Map<String, dynamic>> offer({bool iceRestart = false});
  Future<Map<String, dynamic>> answer(Map<String, dynamic> offer);
  Future<void> setAnswer(Map<String, dynamic> answer);
  Future<void> addIce(Map<String, dynamic> candidate);
  Future<void> close();
}

class CallMediaException implements Exception {
  const CallMediaException(this.message);
  final String message;
}

class _OutgoingSignal {
  _OutgoingSignal(this.kind, this.data, this.clientId);
  final String kind;
  final Map<String, dynamic> data;
  final String clientId;
}

class CallController extends ChangeNotifier {
  CallController({
    required this.api,
    required this.peer,
    this.autoPoll = true,
  }) {
    peer.onLocalIce = _localIce;
    peer.onConnection = _connectionChanged;
  }

  final CallApi api;
  final CallPeer peer;
  final bool autoPoll;
  final String _signalPrefix = List.generate(
    16,
    (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0'),
  ).join();
  final Queue<_OutgoingSignal> _outgoing = Queue();
  final List<Map<String, dynamic>> _earlyLocalIce = [];
  final List<Map<String, dynamic>> _earlyRemoteIce = [];
  CallSession? session;
  CallConfiguration? _configuration;
  CallPhase phase = CallPhase.loading;
  String? error;
  String? networkNotice;
  DateTime? connectedAt;
  int _cursor = 0;
  int _signalNumber = 0;
  int _networkFailures = 0;
  int _restartAttempts = 0;
  bool _disposed = false;
  bool _polling = false;
  bool _sending = false;
  bool _remoteReady = false;
  bool _publishIce = false;
  bool _accepting = false;
  bool _negotiating = false;
  Future<void>? _mediaFuture;
  Timer? _pollTimer;
  Timer? _connectionDeadline;
  Timer? _restartTimer;

  bool get finished => phase == CallPhase.ended || phase == CallPhase.failed;
  bool get accepting => _accepting;
  bool get incoming => session?.mine == false;
  bool get _alive => !_disposed && !finished;

  String get statusText {
    if (error != null) return error!;
    return switch (phase) {
      CallPhase.loading => 'Готовим звонок…',
      CallPhase.ringing =>
        incoming ? 'Входящий видеозвонок' : 'Вызываем партнёра…',
      CallPhase.connecting => 'Соединяем…',
      CallPhase.connected => 'Вы на связи',
      CallPhase.reconnecting => 'Восстанавливаем связь…',
      CallPhase.ended => session?.endText ?? 'Звонок завершён',
      CallPhase.failed => 'Не удалось соединиться',
    };
  }

  void _changed() {
    if (!_disposed) notifyListeners();
  }

  Future<void> start({String? callId}) async {
    try {
      _configuration = await api.configuration();
      if (!_alive) return;
      if (!_configuration!.enabled) {
        throw const CallMediaException(
          'Видеозвонки ещё не настроены на сервере. Попробуй позже.',
        );
      }
      final value = callId == null ? await api.start() : await api.get(callId);
      if (!_alive) {
        if (callId == null && !value.ended) unawaited(_bestEffortEnd(value.id));
        return;
      }
      session = value;
      if (value.ended) {
        _finish(CallPhase.ended);
        return;
      }
      phase = value.active ? CallPhase.connecting : CallPhase.ringing;
      _changed();
      if (autoPoll) {
        _pollTimer = Timer.periodic(
          const Duration(seconds: 1),
          (_) => unawaited(poll()),
        );
      }
      // Camera/microphone are never requested for an unanswered call.
      if (value.active) await _ensureMedia();
      if (_alive) await poll();
    } catch (e) {
      _fail(e);
    }
  }

  Future<void> accept() async {
    final call = session;
    if (!_alive || call == null || !incoming || !call.ringing || _accepting) {
      return;
    }
    _accepting = true;
    _changed();
    try {
      final value = await api.accept(call.id);
      if (!_alive) return;
      session = value;
      phase = CallPhase.connecting;
      _changed();
      await _ensureMedia();
      if (_alive) await poll();
    } catch (e) {
      _fail(e);
    } finally {
      _accepting = false;
      _changed();
    }
  }

  Future<void> decline() async {
    final id = session?.id;
    _finish(CallPhase.ended);
    if (id != null) {
      try {
        await api.decline(id).timeout(const Duration(seconds: 5));
      } catch (_) {
        // The server expires unanswered calls even if the phone lost its network.
      }
    }
  }

  Future<void> hangUp() async {
    final id = session?.id;
    final needsEnd = !finished;
    _finish(CallPhase.ended);
    if (id != null && needsEnd) await _bestEffortEnd(id);
  }

  Future<void> _bestEffortEnd(String id) async {
    try {
      await api.end(id).timeout(const Duration(seconds: 5));
    } catch (_) {}
  }

  Future<void> _ensureMedia() => _mediaFuture ??= _initializeMedia();

  Future<void> _initializeMedia() async {
    try {
      await peer.initialize(_configuration!.iceServers);
      if (!_alive) {
        await peer.close();
        return;
      }
      _armConnectionDeadline();
      if (session!.mine) await _makeOffer();
    } catch (e) {
      if (e is CallMediaException) rethrow;
      throw const CallMediaException(
        'Не удалось запустить камеру и звук. Попробуй позвонить ещё раз.',
      );
    }
  }

  Future<void> _makeOffer({bool iceRestart = false}) async {
    if (!_alive || _negotiating) return;
    _negotiating = true;
    _publishIce = false;
    _remoteReady = false;
    try {
      final description = await peer.offer(iceRestart: iceRestart);
      if (!_alive) return;
      _queue('offer', description);
      _flushEarlyLocalIce();
    } finally {
      _negotiating = false;
    }
  }

  void _localIce(Map<String, dynamic> candidate) {
    if (!_alive) return;
    if (_publishIce) {
      _queue('ice', candidate);
    } else {
      _earlyLocalIce.add(candidate);
    }
  }

  void _flushEarlyLocalIce() {
    _publishIce = true;
    for (final candidate in _earlyLocalIce) {
      _queue('ice', candidate);
    }
    _earlyLocalIce.clear();
  }

  void _queue(String kind, Map<String, dynamic> data) {
    if (!_alive) return;
    _outgoing.add(
      _OutgoingSignal(kind, data, '$_signalPrefix-${_signalNumber++}'),
    );
    unawaited(_flushOutgoing());
  }

  Future<void> _flushOutgoing() async {
    if (!_alive || _sending || session == null) return;
    _sending = true;
    try {
      while (_alive && _outgoing.isNotEmpty) {
        final signal = _outgoing.first;
        try {
          await api.sendSignal(
            session!.id,
            signal.kind,
            signal.data,
            signal.clientId,
          );
          if (_outgoing.isNotEmpty && identical(_outgoing.first, signal)) {
            _outgoing.removeFirst();
          }
        } catch (e) {
          final status = e is DioException ? e.response?.statusCode : null;
          if (status != null &&
              status >= 400 &&
              status < 500 &&
              status != 408) {
            _fail(e);
          }
          // Keep the same client_id when retrying after an ambiguous HTTP timeout.
          break;
        }
      }
    } finally {
      _sending = false;
    }
  }

  /// One bounded polling cycle; it also serves as the server heartbeat.
  Future<void> poll() async {
    if (!_alive || _polling || session == null) return;
    _polling = true;
    try {
      final batch = await api.signals(session!.id, _cursor);
      if (!_alive) return;
      session = batch.call;
      _networkFailures = 0;
      networkNotice = null;
      if (batch.call.ended) {
        _finish(CallPhase.ended);
        return;
      }
      if (batch.call.active) {
        if (phase == CallPhase.ringing) phase = CallPhase.connecting;
        await _ensureMedia();
        if (!_alive) return;
        for (final signal in batch.signals) {
          if (signal.seq <= _cursor) continue;
          try {
            await _receive(signal);
          } catch (_) {
            throw const CallMediaException(
              'Не удалось установить соединение. Попробуй позвонить ещё раз.',
            );
          }
          if (!_alive) return;
          _cursor = signal.seq;
        }
      }
      _cursor = max(_cursor, batch.cursor);
      await _flushOutgoing();
      _changed();
    } catch (e) {
      if (!_alive) return;
      if (e is CallMediaException ||
          e is DioException &&
              [401, 403, 404, 410].contains(e.response?.statusCode)) {
        _fail(e);
      } else {
        _networkFailures++;
        // A callee can accept just before the ringing deadline. Always trust a
        // successful server response first; the local deadline is offline-only.
        if (_networkFailures >= 3 &&
            DateTime.now().toUtc().isAfter(
              session!.expiresAt.add(const Duration(seconds: 15)),
            )) {
          _fail(
            const CallMediaException(
              'Связь с сервером прервалась. Попробуй позвонить ещё раз.',
            ),
          );
          return;
        }
        if (_networkFailures >= 3) {
          networkNotice = 'Слабая связь с сервером. Пробуем снова…';
          _changed();
        }
      }
    } finally {
      _polling = false;
    }
  }

  Future<void> _receive(CallSignal signal) async {
    switch (signal.kind) {
      case 'offer':
        if (session!.mine) return;
        _publishIce = false;
        _remoteReady = false;
        final answer = await peer.answer(signal.data);
        if (!_alive) return;
        _remoteReady = true;
        _queue('answer', answer);
        _flushEarlyLocalIce();
        await _flushRemoteIce();
      case 'answer':
        if (!session!.mine) return;
        await peer.setAnswer(signal.data);
        if (!_alive) return;
        _remoteReady = true;
        await _flushRemoteIce();
      case 'ice':
        if (_remoteReady) {
          await _addRemoteIce(signal.data);
        } else {
          _earlyRemoteIce.add(signal.data);
        }
    }
  }

  Future<void> _addRemoteIce(Map<String, dynamic> data) async {
    if (!_alive) return;
    try {
      await peer.addIce(data);
    } catch (_) {
      // A candidate from the old ICE generation can arrive during a network change.
      // Other candidates and the connection timeout determine whether recovery worked.
    }
  }

  Future<void> _flushRemoteIce() async {
    final pending = List<Map<String, dynamic>>.of(_earlyRemoteIce);
    _earlyRemoteIce.clear();
    for (final candidate in pending) {
      if (!_alive) return;
      await _addRemoteIce(candidate);
    }
  }

  void _connectionChanged(String state) {
    if (!_alive) return;
    if (state == 'connected') {
      phase = CallPhase.connected;
      connectedAt ??= DateTime.now();
      _connectionDeadline?.cancel();
      _restartTimer?.cancel();
      _restartAttempts = 0;
    } else if (state == 'disconnected' || state == 'failed') {
      phase = CallPhase.reconnecting;
      _armConnectionDeadline();
      if (session?.mine == true &&
          _restartAttempts < 2 &&
          !(_restartTimer?.isActive ?? false)) {
        _restartTimer = Timer(const Duration(seconds: 2), () async {
          if (!_alive || phase == CallPhase.connected) return;
          _restartAttempts++;
          try {
            await _makeOffer(iceRestart: true);
          } catch (e) {
            _fail(e);
          }
        });
      }
    }
    _changed();
  }

  void _armConnectionDeadline() {
    if (_connectionDeadline?.isActive ?? false) return;
    _connectionDeadline = Timer(const Duration(seconds: 45), () {
      if (_alive && phase != CallPhase.connected) {
        _fail(
          const CallMediaException(
            'Не удалось установить связь. Проверь интернет и позвони ещё раз.',
          ),
        );
      }
    });
  }

  void _fail(Object e) {
    if (!_alive) return;
    error = e is CallMediaException ? e.message : errorText(e);
    final id = session?.id;
    _finish(CallPhase.failed);
    if (id != null) unawaited(_bestEffortEnd(id));
  }

  void _finish(CallPhase finalPhase) {
    if (_disposed || finished) return;
    phase = finalPhase;
    _cancelTimers();
    _outgoing.clear();
    _earlyLocalIce.clear();
    _earlyRemoteIce.clear();
    unawaited(peer.close());
    _changed();
  }

  void _cancelTimers() {
    _pollTimer?.cancel();
    _connectionDeadline?.cancel();
    _restartTimer?.cancel();
  }

  @override
  void dispose() {
    if (_disposed) return;
    final needsEnd = !finished;
    _disposed = true;
    _cancelTimers();
    _outgoing.clear();
    _earlyLocalIce.clear();
    _earlyRemoteIce.clear();
    peer.onLocalIce = null;
    peer.onConnection = null;
    unawaited(peer.close());
    if (needsEnd && session != null) unawaited(_bestEffortEnd(session!.id));
    super.dispose();
  }
}
