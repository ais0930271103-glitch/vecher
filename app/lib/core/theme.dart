import 'package:flutter/material.dart';

/// Цвета из макетов «Вечера».
class VColors {
  static const bg = Color(0xFF121220);
  static const bgDeep = Color(0xFF0D0D18);
  static const card = Color(0xFF1C1B2B);
  static const card2 = Color(0xFF232233);
  static const text = Color(0xFFF4EEE6);
  static const muted = Color(0xFFA9A5B5);
  static const soft = Color(0xFFD8D2DE);
  static const gold = Color(0xFFF3C98B);
  static const gold2 = Color(0xFFE3A55E);
  static const goldText = Color(0xFFF6DDB4);
  static const lavender = Color(0xFFB9A3F0);
  static const ink = Color(0xFF1A1433); // текст на золотой кнопке

  // Из утверждённых макетов «Вечер — ключевые экраны».
  static const nav = Color(0xFF0E0E19);
  static const navInactive = Color(0xFF8F8A9E);
  static const chip = Color(0xFF212031);
  static const text2 = Color(0xFFB8B3C4); // вторичный текст в карточках
  static const link = Color(0xFFD0CAD9); // второстепенные кнопки-ссылки
  static const lavenderText = Color(0xFFE4DAFF);
  static const avatar = Color(0xFF3A2E5E);
  static const segment = Color(0xFF33294A); // выбранная вкладка-переключатель
  static const coral = Color(0xFFF5A894);
  static const label = Color(0xFFE2B57C); // подписи КАПСОМ

  static const goldGradient = LinearGradient(
    begin: Alignment.topCenter,
    end: Alignment.bottomCenter,
    colors: [Color(0xFFF6D29A), Color(0xFFE3A55E)],
  );
  static const cardGradient = LinearGradient(
    begin: Alignment(-0.6, -1),
    end: Alignment(0.6, 1),
    colors: [Color(0xFF2A2438), Color(0xFF1B1A2A)],
    stops: [0, 0.6],
  );
}

TextStyle display(double size, {Color color = VColors.goldText}) => TextStyle(
  fontFamily: 'Comfortaa',
  fontSize: size,
  color: color,
  fontWeight: FontWeight.w400,
  height: 1.2,
);

ThemeData buildTheme() {
  final base = ThemeData(brightness: Brightness.dark, useMaterial3: true);
  // Bundled static fonts keep first launch independent of Google Fonts/CDN.
  final text = base.textTheme.apply(
    fontFamily: 'Manrope',
    bodyColor: VColors.text,
    displayColor: VColors.text,
  );
  return base.copyWith(
    scaffoldBackgroundColor: VColors.bg,
    colorScheme: const ColorScheme.dark(
      primary: VColors.gold,
      onPrimary: VColors.ink,
      secondary: VColors.lavender,
      onSecondary: VColors.ink,
      surface: VColors.card,
      onSurface: VColors.text,
      onSurfaceVariant: VColors.muted,
      surfaceContainerLowest: VColors.bgDeep,
      surfaceContainerLow: VColors.bg,
      surfaceContainer: VColors.card,
      surfaceContainerHigh: VColors.card2,
      surfaceContainerHighest: Color(0xFF2A2938),
      outline: Color(0x33FFFFFF),
      outlineVariant: Color(0x1FFFFFFF),
      secondaryContainer: VColors.segment,
      onSecondaryContainer: VColors.goldText,
    ),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: VColors.card2,
      contentTextStyle: TextStyle(color: VColors.text),
      behavior: SnackBarBehavior.floating,
    ),
    dialogTheme: const DialogThemeData(backgroundColor: VColors.card),
    textTheme: text,
    appBarTheme: const AppBarTheme(
      backgroundColor: Colors.transparent,
      elevation: 0,
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: const Color(0xFF16151F),
      labelStyle: const TextStyle(color: VColors.muted),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: Color(0x24FFFFFF)),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: Color(0x24FFFFFF)),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: VColors.gold),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: VColors.gold,
        foregroundColor: VColors.ink,
        minimumSize: const Size.fromHeight(56),
        shape: const StadiumBorder(),
        textStyle: const TextStyle(
          fontFamily: 'Manrope',
          fontSize: 17,
          fontWeight: FontWeight.w700,
        ),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: VColors.soft,
        minimumSize: const Size(44, 44),
      ),
    ),
  );
}
