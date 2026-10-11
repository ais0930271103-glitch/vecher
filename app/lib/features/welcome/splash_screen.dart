import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/onboarding.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

/// Короткий сплэш (меньше секунды): решаем, куда вести — ко входу или в «Сегодня».
class SplashScreen extends ConsumerStatefulWidget {
  const SplashScreen({super.key});

  @override
  ConsumerState<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends ConsumerState<SplashScreen> {
  @override
  void initState() {
    super.initState();
    _route();
  }

  Future<void> _route() async {
    final token = await readToken();
    var target = token == null ? '/welcome' : '/today';
    if (token != null) {
      // Сначала согласие на обработку данных (и заново, если его текст обновился), потом имя.
      try {
        final me = (await ref.read(dioProvider).get('/auth/me')).data as Map;
        target = nextOnboardingStep(me);
      } catch (_) {
        // Нет связи или сессия истекла — экран «Сегодня» сам покажет, что не так.
      }
    }
    await Future<void>.delayed(const Duration(milliseconds: 500));
    if (!mounted) return;
    context.go(target);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: NightBackground(
        child: Center(
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            const Emblem(size: 180),
            const SizedBox(height: 16),
            Text('Вечер', style: display(48)),
          ]),
        ),
      ),
    );
  }
}
