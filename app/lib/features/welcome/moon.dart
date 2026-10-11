import 'dart:math' as math;

import 'package:flutter/material.dart';

/// Фирменный знак: полумесяц со звёздочкой (упрощённая иконка без силуэтов).
class MoonMark extends StatelessWidget {
  const MoonMark({super.key, this.size = 120});
  final double size;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: 'Вечер',
      child: SizedBox(width: size, height: size, child: CustomPaint(painter: _MoonPainter())),
    );
  }
}

class _MoonPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final s = size.width / 120;
    final outer = Path()..addOval(Rect.fromCircle(center: Offset(56 * s, 64 * s), radius: 46 * s));
    final inner = Path()..addOval(Rect.fromCircle(center: Offset(76 * s, 50 * s), radius: 38 * s));
    final crescent = Path.combine(PathOperation.difference, outer, inner);

    canvas.drawPath(
      crescent,
      Paint()
        ..color = const Color(0x55F3B46E)
        ..maskFilter = MaskFilter.blur(BlurStyle.normal, 18 * s),
    );
    canvas.drawPath(
      crescent,
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFFFFE6BF), Color(0xFFE9A963)],
        ).createShader(Offset.zero & size),
    );
    _star(canvas, Offset(88 * s, 34 * s), 9 * s);
    _star(canvas, Offset(101 * s, 52 * s), 4.5 * s);
  }

  void _star(Canvas canvas, Offset c, double r) {
    final p = Path();
    for (var i = 0; i < 8; i++) {
      final angle = i * math.pi / 4 - math.pi / 2;
      final rad = i.isEven ? r : r * 0.28;
      final pt = c + Offset(math.cos(angle) * rad, math.sin(angle) * rad);
      if (i == 0) {
        p.moveTo(pt.dx, pt.dy);
      } else {
        p.lineTo(pt.dx, pt.dy);
      }
    }
    p.close();
    canvas.drawPath(p, Paint()..color = const Color(0xFFF6DDB4));
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
