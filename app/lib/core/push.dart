import 'dart:async';

import 'package:dio/dio.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../features/calls/call_navigation.dart';
import 'chat_presence.dart';
import 'router.dart';
import 'top_banner.dart';

final scaffoldMessengerKey = GlobalKey<ScaffoldMessengerState>();

bool _started = false;
int _generation = 0;
String? _token;
Future<void>? _initialization;
final List<StreamSubscription<dynamic>> _subscriptions = [];
final Set<Future<dynamic>> _registrations = {};

/// One set of listeners per authenticated session, including after logout/login.
Future<void> initPush(Dio dio) {
  if (_started || kIsWeb) return _initialization ?? Future<void>.value();
  _started = true;
  final generation = ++_generation;
  return _initialization = _start(dio, generation);
}

bool _current(int generation) => _started && generation == _generation;

Future<void> _cancelListeners() async {
  final listeners = List<StreamSubscription<dynamic>>.of(_subscriptions);
  _subscriptions.clear();
  await Future.wait(listeners.map((s) => s.cancel()));
}

Future<void> _start(Dio dio, int generation) async {
  try {
    await Firebase.initializeApp();
    if (!_current(generation)) return;
    final fm = FirebaseMessaging.instance;
    final perm = await fm.requestPermission();
    if (!_current(generation)) return;
    if (perm.authorizationStatus == AuthorizationStatus.denied) {
      _started = false;
      return;
    }

    Future<void> register(String token) async {
      if (!_current(generation)) return;
      _token = token;
      final request = dio.post(
        '/push/register',
        data: {
          'token': token,
          'platform': defaultTargetPlatform == TargetPlatform.iOS
              ? 'ios'
              : 'android',
        },
      );
      _registrations.add(request);
      try {
        await request;
      } finally {
        _registrations.remove(request);
      }
    }

    final token = await fm.getToken();
    if (!_current(generation)) return;
    if (token != null) await register(token);
    if (!_current(generation)) return;
    _subscriptions.add(
      fm.onTokenRefresh.listen(
        (t) => unawaited(register(t).catchError((_) {})),
      ),
    );
    _subscriptions.add(
      FirebaseMessaging.onMessage.listen((m) {
        if (!_current(generation)) return;
        final body = m.notification?.body;
        if (body == null ||
            WidgetsBinding.instance.lifecycleState != AppLifecycleState.resumed) {
          return;
        }
        if (m.data['open'] == 'chat' && chatOnScreen) return;
        if (callOnScreen) return;
        showTopBanner(
          router.routerDelegate.navigatorKey.currentState?.overlay,
          title: m.notification?.title == 'Вечер'
              ? null
              : m.notification?.title,
          text: body,
          onTap: () => _open(m.data),
        );
      }),
    );
    _subscriptions.add(
      FirebaseMessaging.onMessageOpenedApp.listen((m) {
        if (_current(generation)) _open(m.data);
      }),
    );
    final initial = await fm.getInitialMessage();
    if (_current(generation) && initial != null) _open(initial.data);
  } catch (_) {
    // No Firebase on this platform or no network: foreground polling still works.
    if (_current(generation)) {
      _started = false;
      await _cancelListeners();
    }
    debugPrint('Push пока недоступен');
  }
}

Future<void> stopPush(Dio dio) async {
  _started = false;
  ++_generation;
  await _cancelListeners();
  // Finish an in-flight registration before unlinking it from this account.
  await _initialization;
  await Future.wait(
    _registrations.toList().map((request) async {
      try {
        await request;
      } catch (_) {}
    }),
  );
  _initialization = null;
  final token = _token;
  _token = null;
  hideTopBanner();
  if (token == null) return;
  try {
    await dio.post(
      '/push/unregister',
      data: {
        'token': token,
        'platform': defaultTargetPlatform == TargetPlatform.iOS
            ? 'ios'
            : 'android',
      },
    );
  } catch (_) {}
}

void _open(Map<String, dynamic> data) {
  switch (data['open']) {
    case 'matches':
      router.go('/wishes/matches');
    case 'plan':
      final id = data['card_id'];
      router.go(id == null ? '/wishes/matches' : '/wishes/plan/$id');
    case 'reminders':
      router.go('/us/reminders');
    case 'dates':
      router.go('/us/dates');
    case 'chat':
      if (chatOnScreen) return;
      router.go('/today');
      router.push('/chat');
    case 'call':
      final id = data['call_id'];
      final context = router.routerDelegate.navigatorKey.currentContext;
      if (id is String && context != null) {
        unawaited(openIncomingVideoCall(context, id));
      }
    default:
      router.go('/today');
  }
}
