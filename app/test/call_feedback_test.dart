import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/features/calls/call_controller.dart';
import 'package:vecher_app/features/calls/call_feedback.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  for (final incoming in [true, false]) {
    test(
      '${incoming ? 'incoming ringtone' : 'outgoing ringback'} is not restarted by polling',
      () async {
        final player = _Player();
        final feedback = _feedback(player);
        for (var i = 0; i < 5; i++) {
          await _ring(feedback, incoming: incoming);
        }
        expect(player.plays, hasLength(1));
        expect(
          player.plays.single.tone,
          incoming ? CallFeedbackTone.incoming : CallFeedbackTone.outgoing,
        );
        expect(
          player.plays.single.duration,
          lessThanOrEqualTo(const Duration(seconds: 60)),
        );
      },
    );
  }

  test(
    'stop invalidates a queued play before it reaches the native channel',
    () async {
      final player = _Player();
      final feedback = _feedback(player);
      final ringing = _ring(feedback);
      final stopping = feedback.stop();
      await Future.wait([ringing, stopping]);
      expect(player.plays, isEmpty);
      expect(player.events, ['stop-enter', 'stop-done']);
      await _ring(feedback);
      expect(player.plays, isEmpty);
    },
  );

  test(
    'stop waits for in-flight play and native focus release acknowledgement',
    () async {
      final player = _Player()
        ..playGate = Completer<void>()
        ..stopGate = Completer<void>();
      final feedback = _feedback(player);
      final ringing = _ring(feedback);
      await player.playEntered.future;
      var stopped = false;
      final stopping = feedback.stop().then((_) => stopped = true);
      expect(stopped, isFalse);
      expect(player.events, ['play-enter']);
      player.playGate!.complete();
      await player.stopEntered.future;
      expect(stopped, isFalse);
      expect(player.events, ['play-enter', 'play-done', 'stop-enter']);
      player.stopGate!.complete();
      await Future.wait([ringing, stopping]);
      expect(stopped, isTrue);
      expect(player.audible, isFalse);
    },
  );

  test('dispose closes a late player and is idempotent', () async {
    final player = _Player()..playGate = Completer<void>();
    final feedback = _feedback(player);
    final ringing = _ring(feedback);
    await player.playEntered.future;
    final disposing = feedback.dispose();
    expect(identical(disposing, feedback.dispose()), isTrue);
    player.playGate!.complete();
    await Future.wait([ringing, disposing]);
    await _ring(feedback);
    expect(player.plays, hasLength(1));
    expect(player.audible, isFalse);
    expect(player.events.last, 'stop-done');
  });

  test(
    'background and PIN silence ringing until a fresh visible state',
    () async {
      final player = _Player();
      final feedback = _feedback(player);
      await _ring(feedback, incoming: true);
      await _ring(feedback, incoming: true, foreground: false, fresh: false);
      expect(player.audible, isFalse);
      await _ring(feedback, incoming: true, fresh: false);
      expect(player.plays, hasLength(1));
      await _ring(feedback, incoming: true, privacyLocked: true);
      expect(player.audible, isFalse);
      await _ring(feedback, incoming: true);
      expect(player.plays, hasLength(2));
      expect(player.audible, isTrue);
    },
  );

  test(
    'pressing accept immediately silences an unanswered incoming call',
    () async {
      final player = _Player();
      final feedback = _feedback(player);
      await _ring(feedback, incoming: true);
      await _ring(feedback, incoming: true, accepting: true);
      expect(player.audible, isFalse);
      expect(player.events.last, 'stop-done');
    },
  );

  for (final phase in [
    CallPhase.connecting,
    CallPhase.connected,
    CallPhase.reconnecting,
    CallPhase.ended,
    CallPhase.failed,
  ]) {
    test(
      '$phase stops sound and cannot restart the old ringing state',
      () async {
        final player = _Player();
        final feedback = _feedback(player);
        await _ring(feedback);
        await _ring(feedback, phase: phase);
        await _ring(feedback);
        expect(player.audible, isFalse);
        expect(player.plays, hasLength(1));
      },
    );
  }

  testWidgets('60-second cap survives repeated polls and foreground changes', (
    tester,
  ) async {
    var now = DateTime.utc(2026, 10, 19, 12);
    final player = _Player();
    // Dispose inside this fake-time zone: an outer asynchronous addTearDown
    // cannot drain a queue whose microtasks belong to WidgetTest's stopped clock.
    final feedback = CallFeedback(player: player, now: () => now);
    final expires = now.add(const Duration(minutes: 5));
    try {
      await _ring(feedback, expiresAt: expires);
      now = now.add(const Duration(seconds: 50));
      await tester.pump(const Duration(seconds: 50));
      await _ring(
        feedback,
        expiresAt: expires,
        foreground: false,
        fresh: false,
      );
      await _ring(feedback, expiresAt: expires);
      expect(player.plays.last.duration, const Duration(seconds: 10));
      now = now.add(const Duration(seconds: 10));
      await tester.pump(const Duration(seconds: 10));
      expect(player.audible, isFalse);
      await _ring(feedback, expiresAt: expires);
      expect(player.plays, hasLength(2));
      expect(player.audible, isFalse);
    } finally {
      await feedback.dispose();
    }
  });

  test('expired server call is never sounded', () async {
    final player = _Player();
    final feedback = _feedback(player);
    await _ring(
      feedback,
      expiresAt: DateTime.now().subtract(const Duration(seconds: 1)),
    );
    expect(player.plays, isEmpty);
  });

  test('an unavailable sound player does not prevent call cleanup', () async {
    final player = _Player()..failPlay = true;
    final feedback = _feedback(player);
    await _ring(feedback);
    await feedback.stop();
    expect(player.events.last, 'stop-done');
    expect(player.audible, isFalse);
  });

  test(
    'each screen has a newer owner so native cleanup cannot stop a newer call',
    () async {
      final player = _Player();
      final previous = _feedback(player);
      final next = _feedback(player);
      await _ring(previous);
      await _ring(next);
      expect(player.plays[1].owner, greaterThan(player.plays[0].owner));
    },
  );

  test(
    'iOS feedback is a graceful no-op without a native call integration',
    () async {
      debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
      addTearDown(() => debugDefaultTargetPlatformOverride = null);
      const channel = MethodChannel('vecher/call_feedback');
      final calls = <MethodCall>[];
      final messenger =
          TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
      messenger.setMockMethodCallHandler(channel, (call) async {
        calls.add(call);
        return null;
      });
      addTearDown(() => messenger.setMockMethodCallHandler(channel, null));
      const player = NativeCallFeedbackPlayer();
      await player.play(
        owner: 1,
        generation: 1,
        tone: CallFeedbackTone.incoming,
        duration: const Duration(seconds: 1),
      );
      await player.stop(owner: 1, generation: 2);
      expect(calls, isEmpty);
    },
  );
}

CallFeedback _feedback(_Player player, {DateTime Function()? now}) {
  final value = CallFeedback(player: player, now: now);
  addTearDown(value.dispose);
  return value;
}

Future<void> _ring(
  CallFeedback value, {
  CallPhase phase = CallPhase.ringing,
  bool incoming = false,
  bool accepting = false,
  bool foreground = true,
  bool privacyLocked = false,
  bool fresh = true,
  DateTime? expiresAt,
}) => value.sync(
  phase: phase,
  incoming: incoming,
  accepting: accepting,
  foreground: foreground,
  privacyLocked: privacyLocked,
  fresh: fresh,
  expiresAt: expiresAt ?? DateTime.now().add(const Duration(minutes: 5)),
);

class _Play {
  _Play(this.owner, this.tone, this.duration);
  final int owner;
  final CallFeedbackTone tone;
  final Duration duration;
}

class _Player implements CallFeedbackPlayer {
  Completer<void>? playGate;
  Completer<void>? stopGate;
  final playEntered = Completer<void>();
  final stopEntered = Completer<void>();
  final events = <String>[];
  final plays = <_Play>[];
  bool audible = false;
  bool failPlay = false;

  @override
  Future<void> play({
    required int owner,
    required int generation,
    required CallFeedbackTone tone,
    required Duration duration,
  }) async {
    plays.add(_Play(owner, tone, duration));
    events.add('play-enter');
    if (!playEntered.isCompleted) playEntered.complete();
    if (playGate != null) await playGate!.future;
    if (failPlay) throw PlatformException(code: 'sound_unavailable');
    audible = true;
    events.add('play-done');
  }

  @override
  Future<void> stop({required int owner, required int generation}) async {
    events.add('stop-enter');
    if (!stopEntered.isCompleted) stopEntered.complete();
    if (stopGate != null) await stopGate!.future;
    audible = false;
    events.add('stop-done');
  }
}
