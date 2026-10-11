import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../features/auth/code_screen.dart';
import '../features/auth/email_screen.dart';
import '../features/today/today_screen.dart';
import '../features/welcome/splash_screen.dart';
import '../features/welcome/welcome_screen.dart';
import '../features/wishes/matches_screen.dart';
import '../features/wishes/swipe_screen.dart';
import '../features/wishes/wishes_screen.dart';
import 'theme.dart';

final _rootKey = GlobalKey<NavigatorState>();

final router = GoRouter(
  navigatorKey: _rootKey,
  initialLocation: '/',
  routes: [
    GoRoute(path: '/', builder: (_, __) => const SplashScreen()),
    GoRoute(path: '/welcome', builder: (_, __) => const WelcomeScreen()),
    GoRoute(path: '/login', builder: (_, __) => const EmailScreen()),
    GoRoute(
      path: '/code',
      builder: (_, state) => CodeScreen(email: state.extra as String? ?? ''),
    ),
    // Основные разделы с нижним меню.
    StatefulShellRoute.indexedStack(
      builder: (_, __, shell) => _HomeShell(shell: shell),
      branches: [
        StatefulShellBranch(routes: [
          GoRoute(path: '/today', builder: (_, __) => const TodayScreen()),
        ]),
        StatefulShellBranch(routes: [
          GoRoute(
            path: '/wishes',
            builder: (_, __) => const WishesScreen(),
            routes: [
              // Свайпер и совпадения открываются на весь экран, без нижнего меню.
              GoRoute(
                path: 'deck/:id',
                parentNavigatorKey: _rootKey,
                builder: (_, state) => SwipeScreen(
                  deckId: state.pathParameters['id']!,
                  title: state.extra as String? ?? 'Желания',
                ),
              ),
              GoRoute(
                path: 'matches',
                parentNavigatorKey: _rootKey,
                builder: (_, __) => const MatchesScreen(),
              ),
            ],
          ),
        ]),
      ],
    ),
  ],
);

class _HomeShell extends StatelessWidget {
  const _HomeShell({required this.shell});
  final StatefulNavigationShell shell;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: shell,
      bottomNavigationBar: NavigationBar(
        backgroundColor: VColors.bgDeep,
        indicatorColor: const Color(0x33F3C98B),
        selectedIndex: shell.currentIndex,
        onDestinationSelected: (i) => shell.goBranch(i, initialLocation: i == shell.currentIndex),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.nightlight_outlined),
            selectedIcon: Icon(Icons.nightlight, color: VColors.gold),
            label: 'Сегодня',
          ),
          NavigationDestination(
            icon: Icon(Icons.favorite_border),
            selectedIcon: Icon(Icons.favorite, color: VColors.gold),
            label: 'Желания',
          ),
        ],
      ),
    );
  }
}
