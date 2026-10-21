import 'dart:async';

import 'package:centrifuge/centrifuge.dart' as centrifuge;
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/realtime_controller.dart';
import 'package:vecher_app/core/realtime_transport.dart';

import 'support/fake_realtime.dart';

void main() {
  test(
    'SDK adapter accepts only the granted server-side channel and closes all listeners',
    () async {
      final client = _Client();
      final transport = CentrifugoTransport(
        testRealtimeSession,
        () async => 'fresh',
        createClient: (_, __) => client,
      );
      final states = <RealtimeLinkState>[];
      final publications = <RealtimePublication>[];
      transport.states.listen(states.add);
      transport.publications.listen(publications.add);
      await transport.connect();
      client.serverSubscriptions.add(_Subscribed('events:foreign'));
      expect(states, [RealtimeLinkState.connecting]);
      client.serverSubscriptions.add(_Subscribed(testRealtimeChannel));
      expect(states.last, RealtimeLinkState.subscribed);
      client.messages.add(_Publication('events:foreign', [1]));
      client.messages.add(_Publication(testRealtimeChannel, [2]));
      expect(publications, hasLength(1));
      expect(publications.single.data, [2]);
      await transport.close();
      expect(client.closed, isTrue);
      final count = states.length;
      client.serverSubscriptions.add(_Subscribed(testRealtimeChannel));
      client.messages.add(_Publication(testRealtimeChannel, [3]));
      expect(states.length, count);
      expect(publications, hasLength(1));
    },
  );

  test(
    'SDK refresh uses API callback, keeps TLS checks, and treats stale session as unauthorized',
    () async {
      late centrifuge.ClientConfig config;
      var ended = false;
      var refreshes = 0;
      final transport = CentrifugoTransport(
        testRealtimeSession,
        () async {
          refreshes++;
          if (ended) throw const RealtimeSessionEnded();
          return 'fresh-$refreshes';
        },
        createClient: (url, value) {
          expect(url, testRealtimeSession.url);
          config = value;
          return _Client();
        },
      );
      expect(config.token, testRealtimeSession.token);
      expect(config.tlsSkipVerify, isFalse);
      expect(
        await config.getToken!(centrifuge.ConnectionTokenEvent()),
        'fresh-1',
      );
      ended = true;
      await expectLater(
        config.getToken!(centrifuge.ConnectionTokenEvent()),
        throwsA(isA<centrifuge.UnauthorizedException>()),
      );
      await transport.close();
    },
  );
}

class _Client implements centrifuge.Client {
  final attempts = StreamController<centrifuge.ConnectingEvent>.broadcast(
    sync: true,
  );
  final disconnects = StreamController<centrifuge.DisconnectedEvent>.broadcast(
    sync: true,
  );
  final serverSubscriptions =
      StreamController<centrifuge.ServerSubscribedEvent>.broadcast(sync: true);
  final subscribingEvents =
      StreamController<centrifuge.ServerSubscribingEvent>.broadcast(sync: true);
  final unsubscriptions =
      StreamController<centrifuge.ServerUnsubscribedEvent>.broadcast(
        sync: true,
      );
  final messages =
      StreamController<centrifuge.ServerPublicationEvent>.broadcast(sync: true);
  final errors = StreamController<centrifuge.ErrorEvent>.broadcast(sync: true);
  bool closed = false;

  @override
  Stream<centrifuge.ConnectingEvent> get connecting => attempts.stream;
  @override
  Stream<centrifuge.DisconnectedEvent> get disconnected => disconnects.stream;
  @override
  Stream<centrifuge.ServerSubscribedEvent> get subscribed =>
      serverSubscriptions.stream;
  @override
  Stream<centrifuge.ServerSubscribingEvent> get subscribing =>
      subscribingEvents.stream;
  @override
  Stream<centrifuge.ServerUnsubscribedEvent> get unsubscribed =>
      unsubscriptions.stream;
  @override
  Stream<centrifuge.ServerPublicationEvent> get publication => messages.stream;
  @override
  Stream<centrifuge.ErrorEvent> get error => errors.stream;
  @override
  Future<void> connect() async =>
      attempts.add(centrifuge.ConnectingEvent(0, 'test'));
  @override
  Future<void> close() async => closed = true;
  @override
  dynamic noSuchMethod(Invocation invocation) =>
      throw StateError('Unexpected client method ${invocation.memberName}');
}

class _Subscribed implements centrifuge.ServerSubscribedEvent {
  _Subscribed(this.channel);
  @override
  final String channel;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class _Publication implements centrifuge.ServerPublicationEvent {
  _Publication(this.channel, this.data);
  @override
  final String channel;
  @override
  final List<int> data;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}
