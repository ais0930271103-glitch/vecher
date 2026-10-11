import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/theme.dart';

/// Колоды и счётчик совпадений: GET /wishes/decks.
final decksProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/wishes/decks');
  return Map<String, dynamic>.from(r.data as Map);
});

const deckIcons = <String, IconData>{
  'dates': Icons.local_bar_outlined,
  'home': Icons.weekend_outlined,
  'travel': Icons.flight_takeoff,
  'romance': Icons.favorite_outline,
};

class WishesScreen extends ConsumerWidget {
  const WishesScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final decks = ref.watch(decksProvider);
    return Scaffold(
      appBar: AppBar(title: Text('Вместе', style: display(24))),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () => ref.refresh(decksProvider.future),
          child: ListView(padding: const EdgeInsets.all(20), children: [
            const Text(
              'Листайте карточки каждый сам. Партнёр увидит только то, чего вы хотите оба, '
              '— ваше «да» без взаимности останется тайной.',
              style: TextStyle(color: VColors.soft, fontSize: 15, height: 1.4),
            ),
            const SizedBox(height: 20),
            decks.when(
              loading: () => const Padding(
                padding: EdgeInsets.only(top: 60),
                child: Center(child: CircularProgressIndicator()),
              ),
              error: (e, _) => Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894))),
              data: (d) {
                final list = (d['decks'] as List).cast<Map>();
                return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  _MatchesTile(total: d['matches'] as int, fresh: d['new_matches'] as int),
                  const SizedBox(height: 18),
                  for (final deck in list) ...[
                    _DeckTile(deck: Map<String, dynamic>.from(deck)),
                    const SizedBox(height: 12),
                  ],
                ]);
              },
            ),
          ]),
        ),
      ),
    );
  }
}

class _MatchesTile extends ConsumerWidget {
  const _MatchesTile({required this.total, required this.fresh});
  final int total;
  final int fresh;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(24),
        onTap: () async {
          await context.push('/wishes/matches');
          ref.invalidate(decksProvider);
        },
        child: Ink(
          padding: const EdgeInsets.all(20),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(24),
            gradient: VColors.cardGradient,
            border: Border.all(color: const Color(0x4DF3C98B)),
          ),
          child: Row(children: [
            const Icon(Icons.auto_awesome, color: VColors.gold, size: 30),
            const SizedBox(width: 14),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                const Text('Совпадения', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
                const SizedBox(height: 4),
                Text(total == 0 ? 'Пока нет — листайте колоды вдвоём' : 'Вы оба хотите: $total',
                    style: const TextStyle(color: VColors.soft)),
              ]),
            ),
            if (fresh > 0)
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                decoration: BoxDecoration(color: VColors.gold, borderRadius: BorderRadius.circular(12)),
                child: Text('+$fresh',
                    style: const TextStyle(color: VColors.ink, fontWeight: FontWeight.w700)),
              )
            else
              const Icon(Icons.chevron_right, color: VColors.muted),
          ]),
        ),
      ),
    );
  }
}

class _DeckTile extends ConsumerWidget {
  const _DeckTile({required this.deck});
  final Map<String, dynamic> deck;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final total = deck['total'] as int;
    final done = deck['done'] as int;
    final finished = total > 0 && done >= total;
    return Material(
      color: VColors.card,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(20),
        side: const BorderSide(color: Color(0x14FFFFFF)),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(20),
        onTap: () async {
          await context.push('/wishes/deck/${deck['id']}', extra: deck['title'] as String);
          ref.invalidate(decksProvider);
        },
        child: Padding(
          padding: const EdgeInsets.all(18),
          child: Row(children: [
            Container(
              width: 48,
              height: 48,
              decoration: BoxDecoration(color: VColors.card2, borderRadius: BorderRadius.circular(14)),
              child: Icon(deckIcons[deck['id']] ?? Icons.style_outlined, color: VColors.lavender),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(deck['title'] as String,
                    style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600)),
                const SizedBox(height: 2),
                Text(deck['subtitle'] as String, style: const TextStyle(color: VColors.muted, fontSize: 13)),
                const SizedBox(height: 10),
                ClipRRect(
                  borderRadius: BorderRadius.circular(4),
                  child: LinearProgressIndicator(
                    value: total == 0 ? 0 : done / total,
                    minHeight: 6,
                    backgroundColor: const Color(0x1FFFFFFF),
                    color: VColors.gold,
                  ),
                ),
              ]),
            ),
            const SizedBox(width: 12),
            Text(finished ? 'Готово' : '$done/$total',
                style: TextStyle(color: finished ? VColors.gold : VColors.soft, fontSize: 13)),
          ]),
        ),
      ),
    );
  }
}
