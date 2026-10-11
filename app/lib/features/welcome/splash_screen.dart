import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/theme.dart';
import 'moon.dart';

/// Короткий сплэш (меньше секунды): решаем, куда вести — ко входу или в «Сегодня».
class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen> {
  @override
  void initState() {
    super.initState();
    _route();
  }

  Future<void> _route() async {
    final token = await readToken();
    await Future<void>.delayed(const Duration(milliseconds: 700));
    if (!mounted) return;
    context.go(token == null ? '/welcome' : '/today');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const MoonMark(size: 140),
          const SizedBox(height: 16),
          Text('Вечер', style: display(48)),
        ]),
      ),
    );
  }
}
