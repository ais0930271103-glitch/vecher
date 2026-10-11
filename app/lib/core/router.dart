import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/auth/city_screen.dart';
import '../features/chat/chat_screen.dart';
import '../features/auth/code_screen.dart';
import '../features/auth/consent_screen.dart';
import '../features/auth/email_screen.dart';
import '../features/auth/name_screen.dart';
import '../features/solo/solo_screen.dart';
import '../features/today/today_screen.dart';
import '../features/us/dates_screen.dart';
import '../features/us/person_screens.dart';
import '../features/us/reminders_screen.dart';
import '../features/us/settings_screen.dart';
import '../features/us/us_screen.dart';
import '../features/welcome/splash_screen.dart';
import '../features/welcome/welcome_screen.dart';
import '../features/wishes/matches_screen.dart';
import '../features/wishes/plan_screen.dart';
import '../features/wishes/swipe_screen.dart';
import '../features/wishes/wishes_screen.dart';
import 'api.dart';
import 'push.dart';
import 'theme.dart';

final _rootKey = GlobalKey<NavigatorState>();

final router = GoRouter(
  navigatorKey: _rootKey,
  initialLocation: '/',
  routes: [
    GoRoute(path: '/', builder: (_, __) => const SplashScreen()),
    GoRoute(path: '/welcome', builder: (_, __) => const WelcomeScreen()),
    GoRoute(path: '/login', builder: (_, __) => const EmailScreen()),
    GoRoute(path: '/name', builder: (_, __) => const NameScreen()),
    GoRoute(path: '/consent', builder: (_, __) => const ConsentScreen()),
    GoRoute(path: '/city', builder: (_, __) => const CityScreen()),
    GoRoute(path: '/chat', builder: (_, state) => ChatScreen(attach: state.extra as Map?)),
    // Ссылка-приглашение: запоминаем код и идём обычным путём (вход → согласие → имя → «Сегодня»).
    GoRoute(path: '/i/:code', builder: (_, state) => _InviteLink(code: state.pathParameters['code']!)),
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
                path: 'plan/:cardId',
                parentNavigatorKey: _rootKey,
                builder: (_, state) => PlanScreen(cardId: int.parse(state.pathParameters['cardId']!)),
              ),
              GoRoute(
                path: 'matches',
                parentNavigatorKey: _rootKey,
                builder: (_, __) => const MatchesScreen(),
              ),
            ],
          ),
        ]),
        StatefulShellBranch(routes: [
          GoRoute(path: '/solo', builder: (_, __) => const SoloScreen()),
        ]),
        StatefulShellBranch(routes: [
          GoRoute(
            path: '/us',
            builder: (_, __) => const UsScreen(),
            routes: [
              GoRoute(path: 'partner', parentNavigatorKey: _rootKey, builder: (_, __) => const PartnerMemoScreen()),
              GoRoute(path: 'me', parentNavigatorKey: _rootKey, builder: (_, __) => const AboutMeScreen()),
              GoRoute(path: 'dates', parentNavigatorKey: _rootKey, builder: (_, __) => const DatesScreen()),
              GoRoute(path: 'reminders', parentNavigatorKey: _rootKey, builder: (_, __) => const RemindersScreen()),
              GoRoute(path: 'settings', parentNavigatorKey: _rootKey, builder: (_, __) => const SettingsScreen()),
            ],
          ),
        ]),
      ],
    ),
  ],
);

class _HomeShell extends ConsumerStatefulWidget {
  const _HomeShell({required this.shell});
  final StatefulNavigationShell shell;

  @override
  ConsumerState<_HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends ConsumerState<_HomeShell> {
  StatefulNavigationShell get shell => widget.shell;

  @override
  void initState() {
    super.initState();
    // Человек вошёл — подключаем push (разрешение спросит система).
    initPush(ref.read(dioProvider));
    // Значок непрочитанных на «Чат» — раз в 10 секунд (мгновенно — с сервером).
    _unreadPoll = Timer.periodic(const Duration(seconds: 10), (_) => ref.invalidate(chatUnreadProvider));
  }

  Timer? _unreadPoll;

  @override
  void dispose() {
    _unreadPoll?.cancel();
    super.dispose();
  }

  // В меню 5 пунктов: «Чат» посередине открывает переписку на весь экран (поверх вкладок).
  static const _tabs = <(IconData, String, int?)>[
    (Icons.nightlight_outlined, 'Сегодня', 0),
    (Icons.style_outlined, 'Вместе', 1),
    (Icons.chat_bubble_outline, 'Чат', null),
    (Icons.person_outline, 'Я', 2),
    (Icons.favorite_border, 'Пара', 3),
  ];

  @override
  Widget build(BuildContext context) {
    // Нижнее меню как в макетах: тёмная полоса, золотой активный пункт, без «пилюли».
    return Scaffold(
      body: shell,
      bottomNavigationBar: Container(
        decoration: const BoxDecoration(
          color: VColors.nav,
          border: Border(top: BorderSide(color: Color(0x0FFFFFFF))),
        ),
        child: SafeArea(
          top: false,
          child: SizedBox(
            height: 64,
            child: Row(children: [
              for (final t in _tabs)
                Expanded(
                  child: _NavItem(
                    icon: t.$1,
                    label: t.$2,
                    selected: t.$3 == shell.currentIndex,
                    badge: t.$3 == null ? ref.watch(chatUnreadProvider).valueOrNull ?? 0 : 0,
                    onTap: () {
                      final branch = t.$3;
                      if (branch == null) {
                        context.push('/chat');
                      } else {
                        shell.goBranch(branch, initialLocation: branch == shell.currentIndex);
                      }
                    },
                  ),
                ),
            ]),
          ),
        ),
      ),
    );
  }
}

class _InviteLink extends StatefulWidget {
  const _InviteLink({required this.code});
  final String code;

  @override
  State<_InviteLink> createState() => _InviteLinkState();
}

class _InviteLinkState extends State<_InviteLink> {
  @override
  void initState() {
    super.initState();
    savePendingInvite(widget.code).then((_) {
      if (mounted) context.go('/');
    });
  }

  @override
  Widget build(BuildContext context) => const Scaffold(body: Center(child: CircularProgressIndicator()));
}

class _NavItem extends StatelessWidget {
  const _NavItem({required this.icon, required this.label, required this.selected, required this.onTap, this.badge = 0});
  final IconData icon;
  final String label;
  final bool selected;
  final int badge;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final color = selected ? VColors.gold : VColors.navInactive;
    return InkWell(
      onTap: onTap,
      child: Semantics(
        selected: selected,
        button: true,
        child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
          Stack(clipBehavior: Clip.none, children: [
            Icon(icon, size: 24, color: color),
            if (badge > 0)
              Positioned(
                right: -10,
                top: -6,
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                  decoration: BoxDecoration(color: VColors.coral, borderRadius: BorderRadius.circular(9)),
                  child: Text(badge > 9 ? '9+' : '$badge',
                      style: const TextStyle(color: VColors.ink, fontSize: 10, fontWeight: FontWeight.w700)),
                ),
              ),
          ]),
          const SizedBox(height: 4),
          Text(label,
              style: TextStyle(
                fontSize: 11,
                fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
                color: color,
              )),
        ]),
      ),
    );
  }
}
