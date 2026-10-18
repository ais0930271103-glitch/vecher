import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

final Set<Object> _callScreens = {};
bool _opening = false;
Future<void> _cleanup = Future<void>.value();

void waitForCallCleanup(Future<void> cleanup) {
  _cleanup = cleanup.catchError((_) {});
}

bool get callOnScreen => _opening || _callScreens.isNotEmpty;

void setCallScreenPresent(Object owner, bool present) {
  if (present) {
    _callScreens.add(owner);
  } else {
    _callScreens.remove(owner);
  }
}

Future<void> openVideoCall(BuildContext context) => _open(context, '/call');

Future<void> openIncomingVideoCall(BuildContext context, String id) async {
  if (!RegExp(r'^[a-zA-Z0-9_-]{1,64}$').hasMatch(id)) return;
  await _open(context, '/call/${Uri.encodeComponent(id)}');
}

Future<void> _open(BuildContext context, String path) async {
  if (callOnScreen || !context.mounted) return;
  _opening = true;
  try {
    await _cleanup;
    if (!context.mounted) return;
    await context.push<void>(path);
  } finally {
    _opening = false;
  }
}
