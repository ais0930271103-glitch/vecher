import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/push.dart';
import 'core/router.dart';
import 'core/theme.dart';

void main() {
  runZonedGuarded(() {
    WidgetsFlutterBinding.ensureInitialized();

    // Ошибки отрисовки пишем в консоль `flutter run` целиком, а не только в DevTools.
    FlutterError.onError = (details) {
      FlutterError.presentError(details);
      debugPrint('VECHER UI ERROR: ${details.exceptionAsString()}\n${details.stack}');
    };

    // Вместо белого/серого экрана показываем понятную плашку с текстом ошибки.
    ErrorWidget.builder = (details) => _ErrorPanel(message: details.exceptionAsString());

    runApp(const ProviderScope(child: VecherApp()));
  }, (error, stack) {
    debugPrint('VECHER ERROR: $error\n$stack');
  });
}

class VecherApp extends StatelessWidget {
  const VecherApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp.router(
      title: 'Вечер',
      debugShowCheckedModeBanner: false,
      theme: buildTheme(),
      routerConfig: router,
      scaffoldMessengerKey: scaffoldMessengerKey,
      // Русский интерфейс системных элементов: календарь, выбор времени, меню копирования.
      locale: const Locale('ru'),
      supportedLocales: const [Locale('ru')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
    );
  }
}

class _ErrorPanel extends StatelessWidget {
  const _ErrorPanel({required this.message});
  final String message;

  @override
  Widget build(BuildContext context) {
    return Directionality(
      textDirection: TextDirection.ltr,
      child: Container(
        color: const Color(0xFF121220),
        padding: const EdgeInsets.all(16),
        alignment: Alignment.topLeft,
        child: Text(
          kReleaseMode ? 'Что-то пошло не так. Потяни вниз, чтобы обновить.' : 'Ошибка экрана:\n$message',
          style: const TextStyle(color: Color(0xFFF5A894), fontSize: 14, decoration: TextDecoration.none),
        ),
      ),
    );
  }
}
