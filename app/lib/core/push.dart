import 'package:dio/dio.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'router.dart';

/// Сообщения «в приложении» (когда push приходит, пока «Вечер» открыт).
final scaffoldMessengerKey = GlobalKey<ScaffoldMessengerState>();

bool _started = false;
String? _token;

/// Подключает push: разрешение, токен устройства → сервер, обработка нажатий.
/// Безопасно вызывать много раз и на платформах без Firebase (браузер) — тогда просто ничего не делает.
Future<void> initPush(Dio dio) async {
  if (_started || kIsWeb) return;
  _started = true;
  try {
    await Firebase.initializeApp();
    final fm = FirebaseMessaging.instance;
    final perm = await fm.requestPermission();
    debugPrint('Push: разрешение — ${perm.authorizationStatus}');
    if (perm.authorizationStatus == AuthorizationStatus.denied) {
      _started = false; // разрешат позже в настройках — попробуем снова
      return;
    }

    Future<void> register(String token) async {
      _token = token;
      await dio.post('/push/register', data: {'token': token, 'platform': 'android'});
    }

    final token = await fm.getToken();
    debugPrint('Push: токен ${token == null ? 'не получен' : 'получен'}');
    if (token != null) {
      await register(token);
      debugPrint('Push: телефон зарегистрирован на сервере');
    }
    fm.onTokenRefresh.listen((t) => register(t).catchError((_) {}));

    // Пока приложение открыто — показываем коротко внизу экрана.
    FirebaseMessaging.onMessage.listen((m) {
      final body = m.notification?.body;
      if (body == null) return;
      scaffoldMessengerKey.currentState?.showSnackBar(SnackBar(
        content: Text(body),
        action: SnackBarAction(label: 'Открыть', onPressed: () => _open(m.data)),
      ));
    });
    // Нажали на уведомление — открываем нужный экран.
    FirebaseMessaging.onMessageOpenedApp.listen((m) => _open(m.data));
    final initial = await fm.getInitialMessage();
    if (initial != null) _open(initial.data);
  } catch (e) {
    // Firebase не настроен (нет google-services.json) — приложение работает без push.
    debugPrint('Push недоступен: $e');
    _started = false;
  }
}

/// При выходе из аккаунта — отвязать телефон, чтобы уведомления сюда больше не шли.
Future<void> stopPush(Dio dio) async {
  final t = _token;
  _started = false;
  if (t == null) return;
  try {
    await dio.post('/push/unregister', data: {'token': t, 'platform': 'android'});
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
    default:
      router.go('/today');
  }
}
