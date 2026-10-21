import 'dart:async';

import 'package:centrifuge/centrifuge.dart' as centrifuge;

import 'realtime_controller.dart';

/// Server-side subscription only. There is intentionally no subscribe/publish API.
class CentrifugoTransport implements RealtimeTransport {
  CentrifugoTransport(
    RealtimeSession session,
    Future<String> Function() refreshToken, {
    centrifuge.Client Function(String, centrifuge.ClientConfig)? createClient,
  }) : _client = (createClient ?? centrifuge.createClient)(
         session.url,
         centrifuge.ClientConfig(
           token: session.token,
           name: 'vecher-flutter',
           getToken: (_) async {
             try {
               return await refreshToken();
             } on RealtimeSessionEnded {
               throw centrifuge.UnauthorizedException();
             }
           },
         ),
       ) {
    _subscriptions.addAll([
      _client.connecting.listen((_) => _state(RealtimeLinkState.connecting)),
      _client.disconnected.listen(
        (_) => _state(RealtimeLinkState.disconnected),
      ),
      _client.subscribed.listen((event) {
        if (event.channel == session.channel) {
          _state(RealtimeLinkState.subscribed);
        }
      }),
      _client.subscribing.listen((event) {
        if (event.channel == session.channel) {
          _state(RealtimeLinkState.connecting);
        }
      }),
      _client.unsubscribed.listen((event) {
        if (event.channel == session.channel) {
          _state(RealtimeLinkState.disconnected);
        }
      }),
      _client.publication.listen((event) {
        if (!_closed && event.channel == session.channel) {
          _publications.add(RealtimePublication(event.channel, event.data));
        }
      }),
      // SDK manages transient reconnect/refresh errors. Never expose JWT-bearing details.
      _client.error.listen((_) {}),
    ]);
  }

  final centrifuge.Client _client;
  final _states = StreamController<RealtimeLinkState>.broadcast(sync: true);
  final _publications = StreamController<RealtimePublication>.broadcast(
    sync: true,
  );
  final _subscriptions = <StreamSubscription<dynamic>>[];
  bool _closed = false;

  @override
  Stream<RealtimeLinkState> get states => _states.stream;
  @override
  Stream<RealtimePublication> get publications => _publications.stream;

  void _state(RealtimeLinkState state) {
    if (!_closed) _states.add(state);
  }

  @override
  Future<void> connect() => _client.connect();

  @override
  Future<void> close() async {
    if (_closed) return;
    _closed = true;
    for (final subscription in _subscriptions) {
      await subscription.cancel();
    }
    await _client.close();
    await _states.close();
    await _publications.close();
  }
}
