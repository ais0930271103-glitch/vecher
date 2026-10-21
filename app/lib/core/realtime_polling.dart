import 'dart:async';

import 'package:flutter/widgets.dart';

import 'privacy.dart';
import 'realtime_controller.dart';
import 'session_changes.dart';

bool get canRefreshInForeground =>
    (WidgetsBinding.instance.lifecycleState ?? AppLifecycleState.resumed) ==
        AppLifecycleState.resumed &&
    PinGate.isReady &&
    !PinGate.isLocked &&
    !sessionChanges.isChanging;

/// Keep the existing short poll when realtime is unavailable. A slow REST
/// reconciliation still covers scheduled changes and lost publication attempts.
class RealtimePoll {
  RealtimePoll({
    required RealtimeController controller,
    required Duration interval,
    required VoidCallback onPoll,
    bool Function()? allowed,
    Duration reconcileInterval = const Duration(seconds: 60),
    DateTime Function()? now,
  }) {
    final clock = now ?? DateTime.now;
    var last = clock();
    _timer = Timer.periodic(interval, (_) {
      if (!(allowed ?? () => canRefreshInForeground)()) return;
      final current = clock();
      if (controller.connected &&
          current.difference(last) < reconcileInterval) {
        return;
      }
      last = current;
      onPoll();
    });
  }

  late final Timer _timer;
  void cancel() => _timer.cancel();
}
