import 'dart:async';
import 'dart:convert';

import 'package:vecher_app/core/realtime_controller.dart';

const testRealtimeChannel = 'events:11111111-1111-4111-8111-111111111111';
const testRealtimeSession = RealtimeSession(
  url: 'wss://example.test/connection/websocket',
  token: 'test-connection-token',
  channel: testRealtimeChannel,
);

class FakeRealtimeTransport implements RealtimeTransport {
  FakeRealtimeTransport(this.refreshToken);
  final Future<String> Function() refreshToken;
  final stateEvents = StreamController<RealtimeLinkState>.broadcast(sync: true);
  final publicationEvents = StreamController<RealtimePublication>.broadcast(
    sync: true,
  );
  bool closed = false;
  int connects = 0;

  @override
  Stream<RealtimeLinkState> get states => stateEvents.stream;
  @override
  Stream<RealtimePublication> get publications => publicationEvents.stream;
  @override
  Future<void> connect() async {
    connects++;
    stateEvents.add(RealtimeLinkState.connecting);
  }

  @override
  Future<void> close() async => closed = true;

  void subscribed() => stateEvents.add(RealtimeLinkState.subscribed);

  void publish(
    int id,
    List<String> scopes, {
    String channel = testRealtimeChannel,
    int version = 1,
  }) {
    publicationEvents.add(
      RealtimePublication(
        channel,
        utf8.encode(
          jsonEncode({
            'version': version,
            'type': 'invalidate',
            'event_id':
                '00000000-0000-4000-8000-${id.toRadixString(16).padLeft(12, '0')}',
            'scopes': scopes,
          }),
        ),
      ),
    );
  }
}

class FakeRealtime {
  FakeRealtime({Future<RealtimeSession> Function()? load}) {
    controller = RealtimeController(
      loadSession: () {
        loads++;
        return load?.call() ?? Future.value(testRealtimeSession);
      },
      createTransport: (_, refresh) {
        final transport = FakeRealtimeTransport(refresh);
        transports.add(transport);
        return transport;
      },
    );
    controller.invalidations.listen(updates.add);
  }

  late final RealtimeController controller;
  final transports = <FakeRealtimeTransport>[];
  final updates = <Set<String>>[];
  int loads = 0;
  FakeRealtimeTransport get transport => transports.last;
}
