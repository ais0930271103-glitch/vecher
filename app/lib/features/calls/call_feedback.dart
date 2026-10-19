import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'call_controller.dart' show CallPhase;

enum CallFeedbackTone { incoming, outgoing }

/// Injectable so cancellation and native acknowledgements can be tested without
/// playing sound. [owner] is an increasing local nonce, never a server call ID.
abstract class CallFeedbackPlayer {
  Future<void> play({
    required int owner,
    required int generation,
    required CallFeedbackTone tone,
    required Duration duration,
  });

  Future<void> stop({required int owner, required int generation});
}

class NativeCallFeedbackPlayer implements CallFeedbackPlayer {
  const NativeCallFeedbackPlayer();

  static const _channel = MethodChannel('vecher/call_feedback');
  bool get _supported =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  @override
  Future<void> play({
    required int owner,
    required int generation,
    required CallFeedbackTone tone,
    required Duration duration,
  }) async {
    // Other platforms keep working without an unimplemented native call UI.
    if (!_supported) return;
    await _channel.invokeMethod<bool>('play', {
      'owner': owner,
      'generation': generation,
      'incoming': tone == CallFeedbackTone.incoming,
      'durationMs': duration.inMilliseconds,
    });
  }

  @override
  Future<void> stop({required int owner, required int generation}) async {
    if (!_supported) return;
    await _channel.invokeMethod<void>('stop', {
      'owner': owner,
      'generation': generation,
    });
  }
}

/// Sounds only for an unanswered, visible, unlocked call. A stop before media
/// permanently silences this instance; background/PIN changes use [sync].
class CallFeedback {
  CallFeedback({CallFeedbackPlayer? player, DateTime Function()? now})
    : _player = player ?? const NativeCallFeedbackPlayer(),
      _now = now ?? DateTime.now,
      _owner = _nextOwner();

  static int _lastOwner = 0;
  static int _nextOwner() {
    final now = DateTime.now().microsecondsSinceEpoch;
    _lastOwner = now > _lastOwner ? now : _lastOwner + 1;
    return _lastOwner;
  }

  final CallFeedbackPlayer _player;
  final DateTime Function() _now;
  final int _owner;
  Future<void> _queue = Future<void>.value();
  Future<void>? _disposing;
  CallFeedbackTone? _desired;
  DateTime? _deadline;
  Timer? _expiryTimer;
  int _generation = 0;
  bool _expired = false;
  bool _stopped = false;
  bool _disposed = false;

  Future<void> sync({
    required CallPhase phase,
    required bool incoming,
    required bool accepting,
    required bool foreground,
    required bool privacyLocked,
    required bool fresh,
    required DateTime? expiresAt,
  }) {
    if (_disposed || _stopped || _expired) return _request(null);
    if (phase != CallPhase.ringing && phase != CallPhase.loading) return stop();
    if (phase == CallPhase.ringing && expiresAt != null) {
      final maximum = _now().add(const Duration(seconds: 60));
      final deadline = expiresAt.isBefore(maximum) ? expiresAt : maximum;
      // Repeated polls and a background/resume cycle never extend ringing.
      if (_deadline == null || deadline.isBefore(_deadline!)) {
        _deadline = deadline;
        _expiryTimer?.cancel();
        final remaining = deadline.difference(_now());
        if (remaining <= Duration.zero) {
          _expired = true;
        } else {
          _expiryTimer = Timer(remaining, () {
            _expired = true;
            unawaited(_request(null));
          });
        }
      }
    }
    final audible =
        phase == CallPhase.ringing &&
        !accepting &&
        foreground &&
        !privacyLocked &&
        fresh &&
        _deadline != null &&
        !_expired;
    return _request(
      audible
          ? (incoming ? CallFeedbackTone.incoming : CallFeedbackTone.outgoing)
          : null,
    );
  }

  Future<void> _request(CallFeedbackTone? tone, {bool force = false}) {
    if (!force && tone == _desired) return _queue;
    _desired = tone;
    final generation = ++_generation;
    _queue = _queue
        .then((_) async {
          if (tone == null) {
            // Stops remain ordered even if a later state asks for another sound.
            await _player.stop(owner: _owner, generation: generation);
            return;
          }
          if (_disposed || _stopped || _expired || generation != _generation) {
            return;
          }
          final remaining = _deadline!.difference(_now());
          if (remaining <= Duration.zero) {
            _expired = true;
            _desired = null;
            await _player.stop(owner: _owner, generation: generation);
            return;
          }
          await _player.play(
            owner: _owner,
            generation: generation,
            tone: tone,
            duration: remaining,
          );
        })
        .catchError((Object _) {
          // A missing sound API, silent mode or an unavailable player must not
          // prevent accepting a call. Native failures release their own resources.
        });
    return _queue;
  }

  /// Await this before media initialization: the reply follows native stop and
  /// abandonAudioFocus, including any earlier play that was still in flight.
  Future<void> stop() {
    _stopped = true;
    _expiryTimer?.cancel();
    return _request(null, force: true);
  }

  Future<void> dispose() {
    if (_disposing != null) return _disposing!;
    _disposed = true;
    return _disposing = stop();
  }
}
