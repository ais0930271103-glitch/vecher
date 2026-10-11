import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
import '../us/reminders_screen.dart';
import '../us/us_screen.dart';
import '../wishes/wishes_screen.dart' show decksProvider;

/// Данные экрана «Сегодня» для пары: GET /today.
final todayProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/today');
  return Map<String, dynamic>.from(r.data as Map);
});

/// Ближайшие запланированные вечера: GET /plans/upcoming.
final upcomingProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final r = await ref.read(dioProvider).get('/plans/upcoming');
  return (r.data as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
});

const _radarButtons = <(String, String)>[
  ('thinking', 'Думаю о тебе'),
  ('miss', 'Скучаю'),
  ('plans', 'Есть планы…'),
  ('hug', 'Обнимаю'),
];

const _categoryLabel = {
  'warm': 'ТЕПЛО',
  'fun': 'С УЛЫБКОЙ',
  'flirt': 'ФЛИРТ',
  'deep': 'ПО-НАСТОЯЩЕМУ',
};

class PairedToday extends ConsumerStatefulWidget {
  const PairedToday({super.key, this.partnerName});
  final String? partnerName;

  @override
  ConsumerState<PairedToday> createState() => _PairedTodayState();
}

class _PairedTodayState extends ConsumerState<PairedToday> {
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    // Пока нет мгновенных обновлений (Centrifugo — следующий шаг), подтягиваем раз в 15 секунд.
    _poll = Timer.periodic(const Duration(seconds: 15), (_) {
      ref.invalidate(todayProvider);
      ref.invalidate(upcomingProvider);
      ref.invalidate(remindersProvider);
    });
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final today = ref.watch(todayProvider);
    return today.when(
      skipLoadingOnRefresh: true,
      loading: () => const Padding(
        padding: EdgeInsets.only(top: 80),
        child: Center(child: CircularProgressIndicator()),
      ),
      error: (e, _) => Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894))),
      data: (t) => Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(formatLongDate(DateTime.now()), style: const TextStyle(fontSize: 13, color: VColors.muted)),
              const SizedBox(height: 2),
              Text(greeting(), style: display(26)),
            ]),
          ),
          _StreakChip(streak: t['streak'] as int, complete: t['today_complete'] as bool),
        ]),
        const SizedBox(height: 16),
        _IncomingRadar(signals: (t['radar_incoming'] as List).cast<Map>(), partnerName: widget.partnerName),
        const _Upcoming(),
        const _UsStrip(),
        _QuestionCard(data: t, partnerName: widget.partnerName),
        const SizedBox(height: 18),
        const _RadarRow(),
        const SizedBox(height: 16),
        const _DeckPromo(),
      ]),
    );
  }
}

class _StreakChip extends StatelessWidget {
  const _StreakChip({required this.streak, required this.complete});
  final int streak;
  final bool complete;

  @override
  Widget build(BuildContext context) {
    final days = switch (streak % 10) {
      1 when streak % 100 != 11 => 'день',
      2 || 3 || 4 when (streak % 100) < 12 || (streak % 100) > 14 => 'дня',
      _ => 'дней',
    };
    return Tooltip(
      message: complete
          ? 'Сегодня вы оба уже отметились'
          : 'Огонёк растёт, когда вы оба что-то делаете в течение дня',
      child: Container(
        height: 36,
        padding: const EdgeInsets.symmetric(horizontal: 14),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(18),
          color: const Color(0xFF241F3F),
          border: Border.all(color: const Color(0x4DF3C98B)),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(Icons.local_fire_department_outlined,
              size: 17, color: complete ? const Color(0xFFF3B26B) : const Color(0xFFB98B5C)),
          const SizedBox(width: 6),
          Text('$streak $days',
              style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600, color: VColors.goldText)),
        ]),
      ),
    );
  }
}

class _IncomingRadar extends ConsumerWidget {
  const _IncomingRadar({required this.signals, this.partnerName});
  final List<Map> signals;
  final String? partnerName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (signals.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(bottom: 14),
      child: Material(
        color: VColors.card,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
        child: InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: () async {
            await ref.read(dioProvider).post('/radar/seen');
            ref.invalidate(todayProvider);
          },
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Row(children: [
              Initial(name: partnerName),
              const SizedBox(width: 14),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text('${partnerName ?? 'Партнёр'}: «${signals.first['label']}»',
                      style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                  const SizedBox(height: 3),
                  Text(
                    signals.length > 1 ? 'И ещё ${signals.length - 1} · нажмите, чтобы отметить' : 'Нажмите, чтобы отметить',
                    style: const TextStyle(fontSize: 13, color: VColors.text2),
                  ),
                ]),
              ),
              const Icon(Icons.chevron_right, color: VColors.muted),
            ]),
          ),
        ),
      ),
    );
  }
}

class _QuestionCard extends ConsumerStatefulWidget {
  const _QuestionCard({required this.data, this.partnerName});
  final Map<String, dynamic> data;
  final String? partnerName;

  @override
  ConsumerState<_QuestionCard> createState() => _QuestionCardState();
}

class _QuestionCardState extends ConsumerState<_QuestionCard> {
  final _answer = TextEditingController();
  bool _busy = false;
  String? _error;

  Future<void> _send() async {
    final text = _answer.text.trim();
    if (text.isEmpty) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(dioProvider).post('/today/answer', data: {'text': text});
      _answer.clear();
      ref.invalidate(todayProvider);
    } catch (e) {
      setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _answer.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final d = widget.data;
    final q = Map<String, dynamic>.from(d['question'] as Map);
    final mine = d['my_answer'] as String?;
    final partnerAnswered = d['partner_answered'] as bool;
    final partner = d['partner_answer'] as String?;

    return Container(
      padding: const EdgeInsets.all(20),
      decoration: glowBox(),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          Expanded(
            child: Text('ВОПРОС ДНЯ · ${_categoryLabel[q['category']] ?? ''}',
                style: const TextStyle(letterSpacing: 1.4, fontSize: 12, color: Color(0xFFE2B57C))),
          ),
          if (partnerAnswered && mine == null)
            Row(mainAxisSize: MainAxisSize.min, children: [
              const Icon(Icons.check, size: 14, color: VColors.lavender),
              const SizedBox(width: 4),
              Text('${widget.partnerName ?? 'Партнёр'} — есть ответ',
                  style: const TextStyle(fontSize: 12, color: VColors.lavender)),
            ]),
        ]),
        const SizedBox(height: 10),
        Text(q['text'] as String,
            style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600, height: 1.35)),
        const SizedBox(height: 14),
        if (mine == null) ...[
          TextField(
            controller: _answer,
            minLines: 1,
            maxLines: 4,
            maxLength: 500,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(hintText: 'Ваш ответ', counterText: ''),
          ),
          const SizedBox(height: 8),
          _Lock(text: partnerAnswered
              ? 'Ответ откроется после вашего'
              : '${widget.partnerName ?? 'Партнёр'} увидит ваш ответ, когда ответит сам'),
          if (_error != null) ...[
            const SizedBox(height: 8),
            Text(_error!, style: const TextStyle(color: Color(0xFFF5A894))),
          ],
          const SizedBox(height: 12),
          GoldButton(
            height: 48,
            onPressed: _busy ? null : _send,
            child: Text(_busy ? 'Отправляем…' : 'Ответить'),
          ),
        ] else ...[
          _AnswerBubble(who: 'Вы', text: mine),
          const SizedBox(height: 10),
          if (partner != null)
            _AnswerBubble(who: widget.partnerName ?? 'Партнёр', text: partner, accent: true)
          else
            _Lock(text: 'Ждём ответ — ${widget.partnerName ?? 'партнёр'} увидит ваш, когда ответит сам'),
        ],
      ]),
    );
  }
}

class _Lock extends StatelessWidget {
  const _Lock({required this.text});
  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: const Color(0x8C0E1029),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(children: [
        const Icon(Icons.lock_outline, size: 18, color: VColors.lavender),
        const SizedBox(width: 10),
        Expanded(child: Text(text, style: const TextStyle(fontSize: 14, color: VColors.link))),
      ]),
    );
  }
}

class _AnswerBubble extends StatelessWidget {
  const _AnswerBubble({required this.who, required this.text, this.accent = false});
  final String who;
  final String text;
  final bool accent;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: accent ? const Color(0x249B7FE0) : const Color(0x8C0E1029),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text(who, style: TextStyle(fontSize: 12, color: accent ? VColors.lavender : VColors.muted)),
        const SizedBox(height: 4),
        Text(text, style: const TextStyle(fontSize: 16, height: 1.4)),
      ]),
    );
  }
}

class _RadarRow extends ConsumerStatefulWidget {
  const _RadarRow();

  @override
  ConsumerState<_RadarRow> createState() => _RadarRowState();
}

class _RadarRowState extends ConsumerState<_RadarRow> {
  String? _sending;

  Future<void> _send(String kind, String label) async {
    setState(() => _sending = kind);
    try {
      await ref.read(dioProvider).post('/radar', data: {'kind': kind});
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('Отправлено: «$label»')));
      ref.invalidate(todayProvider);
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    } finally {
      if (mounted) setState(() => _sending = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      const Text('Дневной радар — одним касанием',
          style: TextStyle(fontSize: 13, color: VColors.muted)),
      const SizedBox(height: 10),
      // Сетка 2×2 одинаковых кнопок, как в макете.
      for (var row = 0; row < _radarButtons.length; row += 2) ...[
        if (row > 0) const SizedBox(height: 8),
        Row(children: [
          for (var i = row; i < row + 2 && i < _radarButtons.length; i++) ...[
            if (i > row) const SizedBox(width: 8),
            Expanded(
              child: _RadarButton(
                label: _radarButtons[i].$2,
                busy: _sending == _radarButtons[i].$1,
                onTap: _sending == null ? () => _send(_radarButtons[i].$1, _radarButtons[i].$2) : null,
              ),
            ),
          ],
        ]),
      ],
    ]);
  }
}

class _Upcoming extends ConsumerWidget {
  const _Upcoming();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final list = ref.watch(upcomingProvider).valueOrNull ?? const [];
    if (list.isEmpty) return const SizedBox.shrink();
    return Column(children: [
      for (final u in list)
        Padding(
          padding: const EdgeInsets.only(bottom: 14),
          child: Material(
            color: VColors.card,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(18),
              side: const BorderSide(color: Color(0x4DF3C98B)),
            ),
            child: InkWell(
              borderRadius: BorderRadius.circular(18),
              onTap: () async {
                await context.push('/wishes/plan/${u['card_id']}');
                ref.invalidate(upcomingProvider);
              },
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Row(children: [
                  const Icon(Icons.event_available, color: VColors.gold),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(formatWhen(DateTime.parse(u['scheduled_at'] as String)),
                          style: const TextStyle(fontSize: 13, color: VColors.gold)),
                      const SizedBox(height: 2),
                      Text((u['chosen_title'] as String?) ?? (u['text'] as String),
                          style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                    ]),
                  ),
                  const Icon(Icons.chevron_right, color: VColors.muted),
                ]),
              ),
            ),
          ),
        ),
    ]);
  }
}

/// «Вместе N дней», ближайшая дата и мои напоминания на сегодня.
class _UsStrip extends ConsumerWidget {
  const _UsStrip();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final dates = ref.watch(datesProvider).valueOrNull;
    final reminders = ref.watch(remindersProvider).valueOrNull ?? const [];
    final together = dates?['together_days'] as int?;
    final next = ((dates?['items'] as List?) ?? const [])
        .cast<Map>()
        .where((i) => (i['days_left'] as int) >= 0 && (i['days_left'] as int) <= 30)
        .firstOrNull;

    final now = DateTime.now();
    final endOfDay = DateTime(now.year, now.month, now.day, 23, 59, 59);
    final todays = reminders.where((r) {
      if (r['done'] == true || r['for_me'] != true) return false;
      final due = r['due_at'] == null ? null : DateTime.parse(r['due_at'] as String).toLocal();
      return due == null || !due.isAfter(endOfDay);
    }).take(3).toList();

    final chips = <Widget>[
      if (together != null) _InfoChip(icon: Icons.favorite, text: '$together ${plural(together, 'день', 'дня', 'дней')} вместе'),
      if (next != null)
        _InfoChip(
          icon: Icons.event,
          text: '${next['title']} — ${daysLeftText(next['days_left'] as int)}',
          gold: (next['days_left'] as int) <= 7,
          onTap: () => context.push('/us/dates'),
        ),
    ];

    return Padding(
      padding: const EdgeInsets.only(bottom: 14),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        if (chips.isNotEmpty) ...[
          Wrap(spacing: 8, runSpacing: 8, children: chips),
          const SizedBox(height: 12),
        ],
        for (final r in todays) ReminderTile(r: r, compact: true),
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            onPressed: () => addReminder(context, ref),
            icon: const Icon(Icons.add, size: 18, color: VColors.gold),
            label: const Text('Напоминание', style: TextStyle(color: VColors.gold)),
          ),
        ),
      ]),
    );
  }
}

class _InfoChip extends StatelessWidget {
  const _InfoChip({required this.icon, required this.text, this.gold = false, this.onTap});
  final IconData icon;
  final String text;
  final bool gold;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      borderRadius: BorderRadius.circular(16),
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(16),
          color: const Color(0xFF1B1E44),
          border: Border.all(color: gold ? const Color(0x80F3C98B) : const Color(0x1FFFFFFF)),
        ),
        child: Row(mainAxisSize: MainAxisSize.min, children: [
          Icon(icon, size: 15, color: gold ? VColors.gold : VColors.lavender),
          const SizedBox(width: 6),
          Text(text, style: const TextStyle(fontSize: 13, color: VColors.soft)),
        ]),
      ),
    );
  }
}

class _RadarButton extends StatelessWidget {
  const _RadarButton({required this.label, required this.onTap, this.busy = false});
  final String label;
  final VoidCallback? onTap;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: busy ? const Color(0xFF2A2350) : VColors.chip,
      shape: StadiumBorder(
        side: BorderSide(color: busy ? const Color(0x809B7FE0) : const Color(0x1FFFFFFF)),
      ),
      child: InkWell(
        customBorder: const StadiumBorder(),
        onTap: onTap,
        child: SizedBox(
          height: 44,
          child: Center(
            child: Text(label,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: 13, color: busy ? VColors.lavenderText : VColors.text)),
          ),
        ),
      ),
    );
  }
}

/// Карточка колоды, как в макете: «Колода … · ответили на N из M».
class _DeckPromo extends ConsumerWidget {
  const _DeckPromo();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final data = ref.watch(decksProvider).valueOrNull;
    if (data == null) return const SizedBox.shrink();
    final decks = (data['decks'] as List).cast<Map>();
    final deck = decks.where((d) => (d['done'] as int) < (d['total'] as int)).firstOrNull;
    final fresh = data['new_matches'] as int;
    final title = deck == null ? 'Совпадения' : 'Колода «${deck['title']}»';
    final subtitle = fresh > 0
        ? 'Новых совпадений: $fresh — откройте план вечера'
        : deck == null
            ? 'Все колоды пройдены — смотрите, что совпало'
            : '${deck['total']} карточек · вы ответили на ${deck['done']}';
    return Material(
      color: VColors.card,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
      child: InkWell(
        borderRadius: BorderRadius.circular(20),
        onTap: () async {
          if (deck == null || fresh > 0) {
            await context.push('/wishes/matches');
          } else {
            await context.push('/wishes/deck/${deck['id']}', extra: deck['title'] as String);
          }
          ref.invalidate(decksProvider);
        },
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
          child: Row(children: [
            Transform.rotate(
              angle: -0.1,
              child: Container(
                width: 44,
                height: 54,
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(10),
                  gradient: const LinearGradient(
                    begin: Alignment.topLeft,
                    end: Alignment.bottomRight,
                    colors: [Color(0xFFF6D29A), Color(0xFF9B7FE0)],
                  ),
                ),
              ),
            ),
            const SizedBox(width: 14),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(title, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                const SizedBox(height: 3),
                Text(subtitle, style: const TextStyle(fontSize: 13, color: VColors.text2)),
              ]),
            ),
            const Icon(Icons.chevron_right, color: VColors.muted),
          ]),
        ),
      ),
    );
  }
}
