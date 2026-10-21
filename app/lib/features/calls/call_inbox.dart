import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import 'call_api.dart';
import 'call_navigation.dart';

/// Foreground fallback when push is delayed or unavailable. No camera is opened here.
class CallInbox extends ConsumerStatefulWidget {
  const CallInbox({super.key, required this.child});
  final Widget child;

  @override
  ConsumerState<CallInbox> createState() => _CallInboxState();
}

class _CallInboxState extends ConsumerState<CallInbox>
    with WidgetsBindingObserver {
  Timer? _timer;
  bool _checking = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _timer = Timer.periodic(
      const Duration(seconds: 5),
      (_) => unawaited(_check()),
    );
    WidgetsBinding.instance.addPostFrameCallback((_) => unawaited(_check()));
  }

  Future<void> _check() async {
    if (!mounted ||
        _checking ||
        callOnScreen ||
        WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed) {
      return;
    }
    _checking = true;
    try {
      final call = await DioCallApi(ref.read(dioProvider)).active();
      if (!mounted ||
          callOnScreen ||
          WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed) {
        return;
      }
      if (call != null && call.ringing && !call.mine) {
        unawaited(openIncomingVideoCall(context, call.id));
      }
    } catch (_) {
      // Old API, a solo user or a temporary network failure must not disturb navigation.
    } finally {
      _checking = false;
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) unawaited(_check());
  }

  @override
  void dispose() {
    _timer?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
