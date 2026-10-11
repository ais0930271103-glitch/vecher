import 'package:go_router/go_router.dart';

import '../features/auth/code_screen.dart';
import '../features/auth/email_screen.dart';
import '../features/today/today_screen.dart';
import '../features/welcome/splash_screen.dart';
import '../features/welcome/welcome_screen.dart';

final router = GoRouter(
  initialLocation: '/',
  routes: [
    GoRoute(path: '/', builder: (_, __) => const SplashScreen()),
    GoRoute(path: '/welcome', builder: (_, __) => const WelcomeScreen()),
    GoRoute(path: '/login', builder: (_, __) => const EmailScreen()),
    GoRoute(
      path: '/code',
      builder: (_, state) => CodeScreen(email: state.extra as String? ?? ''),
    ),
    GoRoute(path: '/today', builder: (_, __) => const TodayScreen()),
  ],
);
