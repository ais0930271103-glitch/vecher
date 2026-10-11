import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// Палитра «Сумерки»: тёплый сливово-графитовый фон без синевы, мягкое золото и пыльная роза.
/// Все основные цвета — здесь; чтобы сменить настроение, правьте этот класс.
class VColors {
  static const bg = Color(0xFF16131A);
  static const bgDeep = Color(0xFF110E14);
  static const card = Color(0xFF211C24);
  static const card2 = Color(0xFF2A242E);
  static const text = Color(0xFFF4EEE6);
  static const muted = Color(0xFFADA2A4);
  static const soft = Color(0xFFE0D6D2);
  static const gold = Color(0xFFF3C98B);
  static const gold2 = Color(0xFFE3A55E);
  static const goldText = Color(0xFFF6DDB4);
  static const lavender = Color(0xFFCDB3C4);
  static const ink = Color(0xFF2B1F1A); // текст на золотой кнопке
}

TextStyle display(double size, {Color color = VColors.goldText}) =>
    GoogleFonts.comfortaa(fontSize: size, color: color, fontWeight: FontWeight.w400, height: 1.2);

ThemeData buildTheme() {
  final base = ThemeData(brightness: Brightness.dark, useMaterial3: true);
  final text = GoogleFonts.manropeTextTheme(base.textTheme).apply(
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
      surfaceContainerHighest: Color(0xFF332C36),
      outline: Color(0x33FFFFFF),
      outlineVariant: Color(0x1FFFFFFF),
      secondaryContainer: Color(0x40CDB3C4),
      onSecondaryContainer: VColors.text,
    ),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: VColors.card2,
      contentTextStyle: TextStyle(color: VColors.text),
      behavior: SnackBarBehavior.floating,
    ),
    dialogTheme: const DialogThemeData(backgroundColor: VColors.card),
    textTheme: text,
    appBarTheme: const AppBarTheme(backgroundColor: Colors.transparent, elevation: 0),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: const Color(0xFF1C1820),
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
        textStyle: GoogleFonts.manrope(fontSize: 17, fontWeight: FontWeight.w700),
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
