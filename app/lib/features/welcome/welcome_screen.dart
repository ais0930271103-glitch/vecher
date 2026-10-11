import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import '../../core/ui.dart';

/// Первый экран — как в макете «Вход»: эмблема, орбиты, звёзды, золотая кнопка.
class WelcomeScreen extends StatelessWidget {
  const WelcomeScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: NightBackground(
        child: Stack(children: [
          const Positioned.fill(child: _Sky()),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(32, 56, 32, 24),
              child: Column(children: [
                const Spacer(),
                const Emblem(size: 250),
                const SizedBox(height: 18),
                Text('Вечер', style: display(54)),
                const SizedBox(height: 6),
                const Text('БЛИЖЕ К ГЛАВНОМУ',
                    style: TextStyle(letterSpacing: 5, fontSize: 12, color: VColors.label)),
                const Spacer(flex: 2),
                GoldButton(onPressed: () => context.go('/login'), child: const Text('Начать')),
                const SizedBox(height: 14),
                TextButton(
                  style: TextButton.styleFrom(foregroundColor: VColors.link),
                  onPressed: () => context.go('/login'),
                  child: const Text('У меня есть приглашение', style: TextStyle(fontSize: 15)),
                ),
                const SizedBox(height: 14),
                const Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                  Icon(Icons.lock_outline, size: 14, color: VColors.muted),
                  SizedBox(width: 8),
                  Text('Приватно · данные хранятся в России',
                      style: TextStyle(fontSize: 12, color: VColors.muted)),
                ]),
              ]),
            ),
          ),
        ]),
      ),
    );
  }
}

/// Орбиты и звёзды за эмблемой.
class _Sky extends StatelessWidget {
  const _Sky();

  @override
  Widget build(BuildContext context) => IgnorePointer(child: CustomPaint(painter: _SkyPainter()));
}

class _SkyPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width, h = size.height;
    void orbit(double cx, double cy, double rw, double rh, Color c) {
      canvas.save();
      canvas.translate(cx, cy);
      canvas.rotate(-0.21);
      canvas.drawOval(
        Rect.fromCenter(center: Offset.zero, width: rw, height: rh),
        Paint()
          ..style = PaintingStyle.stroke
          ..strokeWidth = 1
          ..color = c,
      );
      canvas.restore();
    }

    orbit(w / 2, h * 0.37, w * 1.3, h * 0.2, const Color(0x59F3C98B));
    orbit(w / 2, h * 0.37, w * 1.15, h * 0.155, const Color(0x599B7FE0));

    void star(double x, double y, double r, Color c) =>
        canvas.drawCircle(Offset(x * w, y * h), r, Paint()..color = c);
    star(0.16, 0.11, 1.5, VColors.gold);
    star(0.82, 0.18, 1, Colors.white);
    star(0.74, 0.56, 1.5, VColors.lavender);
    star(0.10, 0.62, 1, Colors.white);
    star(0.90, 0.40, 1, const Color(0x99FFFFFF));
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
