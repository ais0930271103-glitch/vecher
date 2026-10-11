import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../auth/consent_screen.dart';
import '../../core/api.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

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
    // Согласие на интимное спрашиваем здесь — когда человек впервые открывает карточки.
    final needConsent = ref.watch(meProvider).valueOrNull?['special_ok'] == false;
    return Scaffold(
      appBar: AppBar(title: Text('Вместе', style: display(24))),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () => ref.refresh(decksProvider.future),
          child: ListView(padding: const EdgeInsets.all(20), children: [
            const Text(
              'Листайте карточки каждый сам. Партнёр увидит только то, чего вы хотите оба, '
              '— твоё «да» без взаимности останется тайной.',
              style: TextStyle(color: VColors.soft, fontSize: 15, height: 1.4),
            ),
            const SizedBox(height: 20),
            if (needConsent)
              const SpecialConsentCard(
                title: 'Карточки — про близость',
                text: 'Твои ответы видишь только ты, а партнёр — лишь совпадения. '
                    'Чтобы сохранять их, нужно твоё согласие.',
              )
            else ...[
              if (decks.valueOrNull?['level'] != null)
                _LevelPicker(level: Map<String, dynamic>.from(decks.valueOrNull!['level'] as Map)),
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
            ],
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
            Column(crossAxisAlignment: CrossAxisAlignment.end, children: [
              Text(finished ? 'Готово' : '$done/$total',
                  style: TextStyle(color: finished ? VColors.gold : VColors.soft, fontSize: 13)),
              if ((deck['locked'] as int? ?? 0) > 0) ...[
                const SizedBox(height: 4),
                Row(mainAxisSize: MainAxisSize.min, children: [
                  const Icon(Icons.lock_outline, size: 12, color: VColors.muted),
                  const SizedBox(width: 3),
                  Text('+${deck['locked']}', style: const TextStyle(color: VColors.muted, fontSize: 12)),
                ]),
              ],
            ]),
          ]),
        ),
      ),
    );
  }
}

// ---------- Уровни «Тепло · Остро · Жарко» ----------

const levelTitles = {'warm': 'Тепло', 'spicy': 'Остро', 'hot': 'Жарко'};
const _levelFlames = {'warm': 1, 'spicy': 2, 'hot': 3};
const _levelColors = {
  'warm': (VColors.goldText, Color(0xFF2E2A26)),
  'spicy': (VColors.coral, Color(0xFF3A2430)),
  'hot': (Color(0xFFF09595), Color(0xFF43202A)),
};

/// Значок уровня: огоньки и название (на карточке в свайпере).
class LevelBadge extends StatelessWidget {
  const LevelBadge({super.key, required this.level});
  final String level;

  @override
  Widget build(BuildContext context) {
    final (fg, bg) = _levelColors[level] ?? _levelColors['warm']!;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(10)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        for (var i = 0; i < (_levelFlames[level] ?? 1); i++)
          Icon(Icons.local_fire_department, size: 13, color: fg),
        const SizedBox(width: 4),
        Text(levelTitles[level] ?? '', style: TextStyle(fontSize: 12, color: fg)),
      ]),
    );
  }
}

/// «Мой уровень»: каждый выбирает сам, паре открывается меньший из двух.
/// Чей выбор осторожнее — не показываем: формулировки нейтральные, без давления.
class _LevelPicker extends ConsumerStatefulWidget {
  const _LevelPicker({required this.level});
  final Map<String, dynamic> level;

  @override
  ConsumerState<_LevelPicker> createState() => _LevelPickerState();
}

class _LevelPickerState extends ConsumerState<_LevelPicker> {
  String? _pending;

  Future<void> _set(String level) async {
    if (_pending != null || level == widget.level['mine']) return;
    setState(() => _pending = level);
    try {
      await ref.read(dioProvider).put('/wishes/level', data: {'level': level});
      ref.invalidate(decksProvider);
    } catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    } finally {
      if (mounted) setState(() => _pending = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final mine = _pending ?? widget.level['mine'] as String;
    final effective = widget.level['effective'] as String;
    final nightOpen = widget.level['night_open'] == true;
    final shown = effective == 'hot' ? 'spicy' : effective; // «Жарко» в «Вечере» — это «Ночь»
    String note;
    if (nightOpen) {
      note = 'Вам открыто всё в «Вечере» — и режим «Ночь» для вас двоих.';
    } else if (mine == 'hot' && effective == 'spicy') {
      note = 'Вам открыто: «Остро». «Ночь» предложим, когда вы оба будете готовы.';
    } else if ((_levelFlames[mine] ?? 1) > (_levelFlames[effective] ?? 1)) {
      note = 'Вам открыто: «${levelTitles[shown]}». Карточки посмелее откроются, когда вы оба будете готовы.';
    } else {
      note = 'Вам открыто: «${levelTitles[shown]}». Уровень можно поменять в любой момент.';
    }
    return Padding(
      padding: const EdgeInsets.only(bottom: 18),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const SectionLabel('Мой уровень', color: VColors.label),
        const SizedBox(height: 8),
        Container(
          padding: const EdgeInsets.all(4),
          decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(20)),
          child: Row(children: [
            for (final l in const ['warm', 'spicy', 'hot'])
              Expanded(
                child: Material(
                  color: l == mine ? VColors.segment : Colors.transparent,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(16),
                    side: BorderSide(color: l == mine ? VColors.gold : Colors.transparent),
                  ),
                  child: InkWell(
                    borderRadius: BorderRadius.circular(16),
                    onTap: () => _set(l),
                    child: SizedBox(
                      height: 52,
                      child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
                        Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                          for (var i = 0; i < _levelFlames[l]!; i++)
                            Icon(Icons.local_fire_department, size: 14,
                                color: l == mine ? VColors.gold : VColors.navInactive),
                        ]),
                        const SizedBox(height: 2),
                        Text(levelTitles[l]!,
                            style: TextStyle(
                              fontSize: 14,
                              fontWeight: l == mine ? FontWeight.w600 : FontWeight.w400,
                              color: l == mine ? VColors.goldText : VColors.link,
                            )),
                      ]),
                    ),
                  ),
                ),
              ),
          ]),
        ),
        const SizedBox(height: 8),
        Text(note, style: const TextStyle(color: VColors.muted, fontSize: 13, height: 1.4)),
        if (mine == 'hot' && !nightOpen) ...[
          const SizedBox(height: 4),
          const Text('«Жарко» — это отдельное приложение «Вечер. Ночь» только для вас двоих.',
              style: TextStyle(color: VColors.muted, fontSize: 13, height: 1.4)),
        ],
        if (nightOpen) ...[
          const SizedBox(height: 12),
          Container(
            padding: const EdgeInsets.all(18),
            decoration: glowBox(radius: 20),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                const Icon(Icons.nightlight_round, color: VColors.gold),
                const SizedBox(width: 8),
                Text('Вечер. Ночь', style: display(20)),
              ]),
              const SizedBox(height: 8),
              const Text(
                'Вы оба выбрали «Жарко». Самые откровенные карточки и игры — в отдельном приложении, '
                'только для вас двоих и только 18+.',
                style: TextStyle(color: VColors.soft, fontSize: 14, height: 1.4),
              ),
              const SizedBox(height: 12),
              const GoldButton(height: 46, onPressed: null, child: Text('Скоро')),
            ]),
          ),
        ],
      ]),
    );
  }
}
