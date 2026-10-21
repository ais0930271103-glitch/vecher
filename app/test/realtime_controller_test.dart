import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/realtime_controller.dart';
import 'package:vecher_app/core/realtime_polling.dart';

import 'support/fake_realtime.dart';

void main() {
  test(
    'session requires secure endpoint, own channel shape and unexpired UTC grant',
    () {
      final now = DateTime.utc(2030);
      final data = {
        'enabled': true,
        'url': 'wss://api.example.test/connection/websocket',
        'channel': testRealtimeChannel,
        'token': 'not-printed',
        'expires_at': now.add(const Duration(minutes: 5)).toIso8601String(),
      };
      expect(RealtimeSession.parse(data, now: now).enabled, isTrue);
      expect(RealtimeSession.parse({'enabled': false}).enabled, isFalse);
      for (final invalid in [
        {...data, 'url': 'ws://api.example.test/connection/websocket'},
        {...data, 'url': 'https://api.example.test/connection/websocket'},
        {...data, 'url': 'wss://user:secret@api.example.test/socket'},
        {...data, 'url': 'wss://api.example.test/socket?token=secret'},
        {...data, 'channel': 'pair:other-account'},
        {...data, 'channel': 'events:'},
        {...data, 'expires_at': now.toIso8601String()},
        {...data, 'token': ''},
        {...data, 'enabled': 'true'},
      ]) {
        expect(
          () => RealtimeSession.parse(invalid, now: now),
          throwsFormatException,
        );
      }
      final local = {...data, 'url': 'ws://10.0.2.2:8000/connection/websocket'};
      expect(
        () => RealtimeSession.parse(local, now: now),
        throwsFormatException,
      );
      expect(
        RealtimeSession.parse(local, now: now, allowLocalWs: true).enabled,
        isTrue,
      );
    },
  );

  testWidgets(
    'late session response cannot connect after stop or account change',
    (tester) async {
      final first = Completer<RealtimeSession>();
      var calls = 0;
      final fake = FakeRealtime(
        load: () =>
            ++calls == 1 ? first.future : Future.value(testRealtimeSession),
      );
      addTearDown(fake.controller.dispose);
      fake.controller.activate('old-account');
      fake.controller.deactivate();
      fake.controller.activate('new-account');
      await tester.pump();
      expect(fake.transports, hasLength(1));
      first.complete(testRealtimeSession);
      await tester.pump();
      expect(fake.transports, hasLength(1));
      expect(fake.transport.connects, 1);
      fake.controller.deactivate();
      expect(fake.transport.closed, isTrue);
    },
  );

  testWidgets(
    'REST resync on start and each reconnect; polling waits for subscription',
    (tester) async {
      final fake = FakeRealtime();
      addTearDown(fake.controller.dispose);
      fake.controller.activate('account');
      await tester.pump(const Duration(milliseconds: 200));
      expect(fake.controller.connected, isFalse);
      expect(fake.updates, [
        {'all'},
      ]);
      fake.transport.subscribed();
      expect(fake.controller.connected, isTrue);
      await tester.pump(const Duration(milliseconds: 200));
      expect(fake.updates.last, {'all'});
      final count = fake.updates.length;
      fake.transport.stateEvents.add(RealtimeLinkState.connecting);
      expect(fake.controller.connected, isFalse);
      fake.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      expect(fake.controller.connected, isTrue);
      expect(fake.updates.length, count + 1);
      expect(fake.updates.last, {'all'});
    },
  );

  testWidgets(
    'events coalesce, deduplicate, ignore foreign channels and unknown data',
    (tester) async {
      final fake = FakeRealtime();
      addTearDown(fake.controller.dispose);
      fake.controller.activate('account');
      await tester.pump();
      fake.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      fake.updates.clear();
      fake.transport.publish(1, ['chat']);
      fake.transport.publish(1, ['plans']);
      fake.transport.publish(2, ['plans', 'future-scope']);
      fake.transport.publish(3, ['us'], channel: 'events:another-account');
      fake.transport.publish(4, ['solo'], version: 2);
      fake.transport.publish(5, ['future-scope']);
      fake.transport.publicationEvents.add(
        const RealtimePublication(testRealtimeChannel, [255]),
      );
      fake.transport.publicationEvents.add(
        RealtimePublication(testRealtimeChannel, List.filled(4097, 32)),
      );
      await tester.pump(const Duration(milliseconds: 150));
      expect(fake.updates, [
        {'chat', 'plans'},
      ]);
    },
  );

  testWidgets(
    'continuous events cannot postpone invalidation forever; stop cancels queued event',
    (tester) async {
      final fake = FakeRealtime();
      addTearDown(fake.controller.dispose);
      fake.controller.activate('account');
      await tester.pump(const Duration(milliseconds: 200));
      fake.updates.clear();
      for (var id = 0; id < 3; id++) {
        fake.transport.publish(id, ['chat']);
        await tester.pump(const Duration(milliseconds: 60));
      }
      expect(fake.updates, [
        {'chat'},
      ]);
      fake.transport.publish(9, ['us']);
      fake.controller.deactivate();
      await tester.pump(const Duration(milliseconds: 200));
      expect(fake.updates, [
        {'chat'},
      ]);
      expect(fake.controller.connected, isFalse);
    },
  );

  testWidgets(
    'token refresh obtains a fresh grant and rejects old generation completion',
    (tester) async {
      final refresh = Completer<RealtimeSession>();
      var calls = 0;
      final fake = FakeRealtime(
        load: () async {
          calls++;
          if (calls == 1) return testRealtimeSession;
          if (calls == 2) {
            return const RealtimeSession(
              url: 'wss://example.test/connection/websocket',
              token: 'fresh',
              channel: testRealtimeChannel,
            );
          }
          return refresh.future;
        },
      );
      addTearDown(fake.controller.dispose);
      fake.controller.activate('account');
      await tester.pump();
      expect(await fake.transport.refreshToken(), 'fresh');
      final pending = fake.transport.refreshToken();
      final assertion = expectLater(
        pending,
        throwsA(isA<RealtimeSessionEnded>()),
      );
      fake.controller.deactivate();
      refresh.complete(testRealtimeSession);
      await assertion;
      expect(fake.transport.closed, isTrue);
      expect(fake.loads, 3);
    },
  );

  testWidgets(
    'changed refresh channel closes old transport without returning a new account token',
    (tester) async {
      var calls = 0;
      final fake = FakeRealtime(
        load: () async => ++calls == 1
            ? testRealtimeSession
            : const RealtimeSession(
                url: 'wss://example.test/connection/websocket',
                token: 'other-account-secret',
                channel: 'events:22222222-2222-4222-8222-222222222222',
              ),
      );
      addTearDown(fake.controller.dispose);
      fake.controller.activate('account');
      await tester.pump();
      fake.transport.subscribed();
      await expectLater(
        fake.transport.refreshToken(),
        throwsA(isA<RealtimeSessionEnded>()),
      );
      expect(fake.transport.closed, isTrue);
      expect(fake.controller.connected, isFalse);
      fake.controller.dispose();
    },
  );

  testWidgets(
    'old server disabled mode resyncs once and retains fallback without rapid retries',
    (tester) async {
      final fake = FakeRealtime(
        load: () async => const RealtimeSession.disabled(),
      );
      addTearDown(fake.controller.dispose);
      fake.controller.activate('account');
      await tester.pump(const Duration(seconds: 10));
      expect(fake.transports, isEmpty);
      expect(fake.controller.connected, isFalse);
      expect(fake.updates, [
        {'all'},
      ]);
      expect(fake.loads, 1);
      fake.controller.dispose();
    },
  );

  testWidgets('failed session retries in foreground; stopping cancels retry', (
    tester,
  ) async {
    final fake = FakeRealtime(load: () async => throw StateError('offline'));
    addTearDown(fake.controller.dispose);
    fake.controller.activate('account');
    await tester.pump();
    await tester.pump(const Duration(seconds: 16));
    expect(fake.loads, 2);
    fake.controller.deactivate();
    await tester.pump(const Duration(minutes: 1));
    expect(fake.loads, 2);
  });

  testWidgets(
    'missing server-side subscription returns to fallback and recreates transport',
    (tester) async {
      final fake = FakeRealtime();
      addTearDown(fake.controller.dispose);
      fake.controller.activate('account');
      await tester.pump();
      final first = fake.transport;
      await tester.pump(const Duration(seconds: 31));
      expect(first.closed, isTrue);
      expect(fake.controller.connected, isFalse);
      await tester.pump(const Duration(seconds: 15));
      expect(fake.transports, hasLength(2));
      fake.controller.dispose();
    },
  );

  testWidgets(
    'polling slows only after subscription and immediately falls back on disconnect',
    (tester) async {
      final fake = FakeRealtime();
      addTearDown(fake.controller.dispose);
      fake.controller.activate('account');
      await tester.pump();
      var polls = 0;
      var allowed = true;
      final poll = RealtimePoll(
        controller: fake.controller,
        interval: const Duration(seconds: 4),
        allowed: () => allowed,
        now: tester.binding.clock.now,
        onPoll: () => polls++,
      );
      addTearDown(poll.cancel);
      await tester.pump(const Duration(seconds: 4));
      expect(polls, 1);
      fake.transport.subscribed();
      await tester.pump(const Duration(seconds: 20));
      expect(polls, 1);
      await tester.pump(const Duration(seconds: 40));
      expect(polls, 2);
      fake.transport.stateEvents.add(RealtimeLinkState.connecting);
      await tester.pump(const Duration(seconds: 4));
      expect(polls, 3);
      allowed = false;
      await tester.pump(const Duration(seconds: 4));
      expect(polls, 3);
      poll.cancel();
      fake.controller.dispose();
    },
  );
}
