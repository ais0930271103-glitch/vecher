import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/theme.dart';
import 'wishes_screen.dart' show deckIcons;

/// Взаимные желания: GET /wishes/matches (открытие снимает пометку «новое»).
final matchesProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final r = await ref.read(dioProvider).get('/wishes/matches');
  return (r.data as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
});

class MatchesScreen extends ConsumerWidget {
  const MatchesScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final matches = ref.watch(matchesProvider);
    return Scaffold(
      appBar: AppBar(title: Text('Совпадения', style: display(22))),
      body: SafeArea(
        child: matches.when(
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(
            child: Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894))),
          ),
          data: (list) {
            if (list.isEmpty) return const _Empty();
            final both = list.where((m) => m['kind'] == 'yes').toList();
            final maybe = list.where((m) => m['kind'] != 'yes').toList();
            return RefreshIndicator(
              onRefresh: () => ref.refresh(matchesProvider.future),
              child: ListView(padding: const EdgeInsets.all(20), children: [
                if (both.isNotEmpty) ...[
                  const _Section(title: 'Вы оба хотите', icon: Icons.favorite),
                  for (final m in both) _MatchTile(m: m),
                  const SizedBox(height: 18),
                ],
                if (maybe.isNotEmpty) ...[
                  const _Section(title: 'Можно обсудить', icon: Icons.chat_bubble_outline),
                  for (final m in maybe) _MatchTile(m: m),
                ],
              ]),
            );
          },
        ),
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section({required this.title, required this.icon});
  final String title;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Row(children: [
        Icon(icon, size: 18, color: VColors.gold),
        const SizedBox(width: 8),
        Text(title.toUpperCase(),
            style: const TextStyle(letterSpacing: 1.4, fontSize: 12, color: Color(0xFFE2B57C))),
      ]),
    );
  }
}

class _MatchTile extends ConsumerWidget {
  const _MatchTile({required this.m});
  final Map<String, dynamic> m;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final fresh = m['new'] == true;
    final scheduled = m['scheduled_at'] == null ? null : DateTime.parse(m['scheduled_at'] as String);
    final status = scheduled != null
        ? 'Запланировано: ${formatWhen(scheduled)}'
        : m['plan_open'] == true
            ? 'План открыт — выберите вариант'
            : 'Открыть план вечера →';
    return GestureDetector(
      onTap: () async {
        await context.push('/wishes/plan/${m['card_id']}');
        ref.invalidate(matchesProvider);
      },
      child: Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: VColors.card,
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: fresh ? const Color(0x80F3C98B) : const Color(0x14FFFFFF)),
      ),
      child: Row(children: [
        Icon(deckIcons[m['deck']] ?? Icons.style_outlined, color: VColors.lavender),
        const SizedBox(width: 14),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(m['text'] as String, style: const TextStyle(fontSize: 16, height: 1.35)),
            const SizedBox(height: 4),
            Text(m['deck_title'] as String, style: const TextStyle(fontSize: 12, color: VColors.muted)),
            const SizedBox(height: 6),
            Text(status,
                style: TextStyle(fontSize: 13, color: scheduled != null ? VColors.gold : VColors.lavender)),
          ]),
        ),
        if (fresh)
          Container(
            margin: const EdgeInsets.only(left: 8),
            padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(color: VColors.gold, borderRadius: BorderRadius.circular(10)),
            child: const Text('новое',
                style: TextStyle(color: VColors.ink, fontSize: 11, fontWeight: FontWeight.w700)),
          ),
      ]),
      ),
    );
  }
}

class _Empty extends StatelessWidget {
  const _Empty();

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(32),
      child: Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const Icon(Icons.auto_awesome, color: VColors.gold, size: 44),
          const SizedBox(height: 14),
          Text('Пока пусто', style: display(24)),
          const SizedBox(height: 8),
          const Text(
            'Совпадения появятся, когда вы оба ответите «да» или «может быть» на одну карточку.',
            textAlign: TextAlign.center,
            style: TextStyle(color: VColors.soft),
          ),
        ]),
      ),
    );
  }
}
