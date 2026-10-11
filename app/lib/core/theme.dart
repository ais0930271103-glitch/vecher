import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

/// Цвета из макетов «Вечера».
class VColors {
  static const bg = Color(0xFF0E1029);
  static const bgDeep = Color(0xFF0B0C24);
  static const card = Color(0xFF171A3B);
  static const card2 = Color(0xFF1D2048);
  static const text = Color(0xFFF4EEE6);
  static const muted = Color(0xFFA9A6C4);
  static const soft = Color(0xFFD6D1EA);
  static const gold = Color(0xFFF3C98B);
  static const gold2 = Color(0xFFE3A55E);
  static const goldText = Color(0xFFF6DDB4);
  static const lavender = Color(0xFFB9A3F0);
  static const ink = Color(0xFF1A1433); // текст на золотой кнопке
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
      surface: VColors.card,
      onSurface: VColors.text,
    ),
    textTheme: text,
    appBarTheme: const AppBarTheme(backgroundColor: Colors.transparent, elevation: 0),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: const Color(0xFF12143A),
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
