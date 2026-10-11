import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import 'moon.dart';

class WelcomeScreen extends StatelessWidget {
  const WelcomeScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Container(
        decoration: const BoxDecoration(
          gradient: RadialGradient(
            center: Alignment(0, -0.45),
            radius: 1.1,
            colors: [Color(0xFF262A5C), Color(0xFF12143A), VColors.bgDeep],
            stops: [0, 0.45, 1],
          ),
        ),
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(24, 48, 24, 24),
            child: Column(children: [
              const Spacer(),
              const MoonMark(size: 180),
              const SizedBox(height: 20),
              Text('Вечер', style: display(54)),
              const SizedBox(height: 6),
              const Text(
                'БЛИЖЕ К ГЛАВНОМУ',
                style: TextStyle(letterSpacing: 5, fontSize: 12, color: Color(0xFFE2B57C)),
              ),
              const Spacer(flex: 2),
              FilledButton(
                onPressed: () => context.go('/login'),
                child: const Text('Начать'),
              ),
              const SizedBox(height: 8),
              TextButton(
                onPressed: () => context.go('/login'),
                child: const Text('У меня есть приглашение'),
              ),
              const SizedBox(height: 8),
              const Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                Icon(Icons.lock_outline, size: 14, color: VColors.muted),
                SizedBox(width: 6),
                Text('Данные хранятся в России',
                    style: TextStyle(fontSize: 12, color: VColors.muted)),
              ]),
            ]),
          ),
        ),
      ),
    );
  }
}
