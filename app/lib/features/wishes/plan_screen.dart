import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart' show kDebugMode;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../chat/chat_screen.dart';
import 'places_screen.dart';
import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/realtime_polling.dart';
import '../../core/realtime_provider.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

/// План к совпадению: GET /plans/{id}.
final planProvider = FutureProvider.autoDispose.family<Map<String, dynamic>, int>((ref, id) async {
  final r = await ref.read(dioProvider).get('/plans/$id');
  return Map<String, dynamic>.from(r.data as Map);
});

class PlanScreen extends ConsumerStatefulWidget {
  const PlanScreen({super.key, required this.cardId});
  final int cardId;

  @override
  ConsumerState<PlanScreen> createState() => _PlanScreenState();
}

class _PlanScreenState extends ConsumerState<PlanScreen> {
  RealtimePoll? _poll;
  bool _busy = false;
  int? _expanded;
  String? _myName, _partnerName;

  @override
  void initState() {
    super.initState();
    // Имена — для кружков с буквами у голосов, как в макете.
    ref.read(dioProvider).get('/pairs/me').then((r) {
      final d = r.data as Map?;
      if (mounted && d != null) {
        setState(() {
          _myName = d['my_name'] as String?;
          _partnerName = d['partner_name'] as String?;
        });
      }
    }).catchError((_) {});
    _poll = RealtimePoll(
      controller: ref.read(realtimeControllerProvider),
      interval: const Duration(seconds: 15),
      onPoll: () => ref.invalidate(planProvider(widget.cardId)),
    );
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _call(Future<void> Function(Dio dio) action) async {
    setState(() => _busy = true);
    try {
      await action(ref.read(dioProvider));
      ref.invalidate(planProvider(widget.cardId));
    } catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _pickDate(DateTime? current) async {
    final now = DateTime.now();
    final start = current?.toLocal() ?? DateTime(now.year, now.month, now.day, 19);
    final date = await showDatePicker(
      context: context,
      initialDate: start.isBefore(now) ? now : start,
      firstDate: DateTime(now.year, now.month, now.day),
      lastDate: now.add(const Duration(days: 365)),
      helpText: 'Когда?',
    );
    if (date == null || !mounted) return;
    final time = await showTimePicker(
      context: context,
      initialTime: TimeOfDay(hour: start.hour, minute: start.minute),
      helpText: 'Во сколько?',
    );
    if (time == null) return;
    final at = DateTime(date.year, date.month, date.day, time.hour, time.minute);
    await _call((dio) => dio.post('/plans/${widget.cardId}/schedule', data: {'at': isoWithOffset(at)}));
  }

  @override
  Widget build(BuildContext context) {
    final plan = ref.watch(planProvider(widget.cardId));
    return Scaffold(
      body: SafeArea(
        child: plan.when(
          skipLoadingOnRefresh: true,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894))),
            ),
          ),
          data: (p) => ListView(padding: const EdgeInsets.fromLTRB(20, 8, 20, 24), children: [
            TopBar(
              title: 'План вечера',
              trailing: IconButton(
                tooltip: 'Обсудить в чате',
                icon: const Icon(Icons.chat_bubble_outline, color: VColors.goldText),
                onPressed: () => openChat(context, kind: 'plan', id: '${widget.cardId}', title: p['text'] as String),
              ),
            ),
            const SizedBox(height: 16),
            _Header(p: p),
            const SizedBox(height: 18),
            if (p['locked'] == true)
              ..._locked(p)
            else
              ..._unlocked(p),
          ]),
        ),
      ),
    );
  }

  // ---------- закрытый план: превью и открытие ----------

  List<Widget> _locked(Map<String, dynamic> p) {
    final options = (p['options'] as List).cast<Map>();
    final freeLeft = p['free_plans_left'] as int;
    return [
      for (final o in options)
        Container(
          margin: const EdgeInsets.only(bottom: 10),
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(
            color: VColors.card,
            borderRadius: BorderRadius.circular(18),
            border: Border.all(color: const Color(0x14FFFFFF)),
          ),
          child: Row(children: [
            const Icon(Icons.lock_outline, color: VColors.lavender, size: 20),
            const SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(o['title'] as String, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
                if ((o['note'] as String? ?? '').isNotEmpty)
                  Text(o['note'] as String, style: const TextStyle(color: VColors.muted, fontSize: 13)),
              ]),
            ),
          ]),
        ),
      const SizedBox(height: 10),
      if (freeLeft > 0) ...[
        GoldButton(
          onPressed: _busy ? null : () => _call((dio) => dio.post('/plans/${widget.cardId}/open')),
          child: const Text('Открыть план — бесплатно'),
        ),
        const SizedBox(height: 8),
        const Text('Первый план — подарок. Остальные открываются с «Вечер+».',
            textAlign: TextAlign.center, style: TextStyle(color: VColors.muted, fontSize: 13)),
      ] else
        _Paywall(
          busy: _busy,
          onDevActivate: () => _call((dio) async {
            await dio.post('/subscription/dev-activate');
            await dio.post('/plans/${widget.cardId}/open');
          }),
        ),
    ];
  }

  // ---------- открытый план: выбор вдвоём и расписание ----------

  List<Widget> _unlocked(Map<String, dynamic> p) {
    final options = (p['options'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
    final mine = p['my_vote'] as int?;
    final theirs = p['partner_vote'] as int?;
    final chosen = p['chosen'] as int?;
    final open = _expanded ?? chosen ?? mine ?? 0;
    final scheduled = p['scheduled_at'] == null ? null : DateTime.parse(p['scheduled_at'] as String);

    return [
      Text(
        chosen != null
            ? 'Вы выбрали вместе — осталось назначить время.'
            : theirs != null && mine == null
                ? 'Партнёр уже выбрал вариант. Твой ход!'
                : mine != null && theirs != null
                    ? 'Вы выбрали разное — договоритесь и выберите один.'
                    : 'Выбери вариант. Как только вы выберете одинаково — план готов.',
        style: const TextStyle(color: VColors.soft, fontSize: 15),
      ),
      const SizedBox(height: 14),
      for (final o in options)
        _OptionCard(
          o: o,
          expanded: open == o['index'],
          mine: mine == o['index'],
          theirs: theirs == o['index'],
          chosen: chosen == o['index'],
          myName: _myName,
          partnerName: _partnerName,
          busy: _busy,
          onToggle: () => setState(() => _expanded = o['index'] as int),
          onVote: () => _call((dio) => dio.post('/plans/${widget.cardId}/vote', data: {'option': o['index']})),
        ),
      if (p['places_available'] == true) ...[
        const SizedBox(height: 8),
        PlacesTile(cardId: widget.cardId, planTitle: p['text'] as String),
        const SizedBox(height: 14),
      ],
      const SizedBox(height: 8),
      _ScheduleCard(
        scheduled: scheduled,
        busy: _busy,
        onPick: () => _pickDate(scheduled),
        onClear: () => _call((dio) => dio.post('/plans/${widget.cardId}/schedule', data: {'at': null})),
      ),
    ];
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.p});
  final Map<String, dynamic> p;

  @override
  Widget build(BuildContext context) {
    final both = p['kind'] == 'yes';
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(24),
        gradient: VColors.cardGradient,
        border: Border.all(color: const Color(0x4DF3C98B)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('${both ? 'ВЫ ОБА ХОТИТЕ' : 'МОЖНО ОБСУДИТЬ'} · ${(p['deck_title'] as String).toUpperCase()}',
            style: const TextStyle(letterSpacing: 1.4, fontSize: 12, color: Color(0xFFE2B57C))),
        const SizedBox(height: 10),
        Text(p['text'] as String, style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600, height: 1.3)),
        const SizedBox(height: 10),
        Text(p['intro'] as String, style: const TextStyle(color: VColors.soft, height: 1.4)),
      ]),
    );
  }
}

class _OptionCard extends StatelessWidget {
  const _OptionCard({
    required this.o,
    required this.expanded,
    required this.mine,
    required this.theirs,
    required this.chosen,
    required this.busy,
    required this.onToggle,
    required this.onVote,
    this.myName,
    this.partnerName,
  });
  final Map<String, dynamic> o;
  final bool expanded, mine, theirs, chosen, busy;
  final String? myName, partnerName;
  final VoidCallback onToggle, onVote;

  @override
  Widget build(BuildContext context) {
    final items = (o['items'] as List? ?? const []).cast<String>();
    final steps = (o['steps'] as List? ?? const []).cast<String>();
    final itemsTitle = o['items_title'] as String? ?? 'Понадобится';
    final note = o['note'] as String? ?? '';

    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      decoration: BoxDecoration(
        color: chosen ? null : VColors.card,
        gradient: chosen
            ? const LinearGradient(
                begin: Alignment(-0.6, -1),
                end: Alignment(0.6, 1),
                colors: [Color(0xFF2D2639), Color(0xFF1B1A2A)],
                stops: [0, 0.8],
              )
            : null,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(
          color: chosen ? VColors.gold : (mine ? const Color(0x80F3C98B) : const Color(0x14FFFFFF)),
          width: chosen ? 1.5 : 1,
        ),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: onToggle,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Row(children: [
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(o['title'] as String, style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600)),
                  if (note.isNotEmpty) ...[
                    const SizedBox(height: 2),
                    Text(note, style: const TextStyle(color: VColors.muted, fontSize: 13)),
                  ],
                  if (mine || theirs) ...[
                    const SizedBox(height: 10),
                    Row(children: [
                      if (mine) Initial(name: myName, size: 28, me: true),
                      if (mine && theirs) const SizedBox(width: 6),
                      if (theirs) Initial(name: partnerName, size: 28),
                    ]),
                  ],
                ]),
              ),
              if (chosen)
                Container(
                  height: 26,
                  padding: const EdgeInsets.symmetric(horizontal: 10),
                  alignment: Alignment.center,
                  decoration: BoxDecoration(color: VColors.gold, borderRadius: BorderRadius.circular(13)),
                  child: const Text('Совпало',
                      style: TextStyle(color: VColors.ink, fontSize: 12, fontWeight: FontWeight.w700)),
                )
              else
                Icon(expanded ? Icons.expand_less : Icons.expand_more, color: VColors.muted),
            ]),
          ),
        ),
        if (expanded) ...[
          if (items.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  Expanded(
                    child: Text(itemsTitle.toUpperCase(),
                        style: const TextStyle(letterSpacing: 1.2, fontSize: 12, color: Color(0xFFE2B57C))),
                  ),
                  TextButton.icon(
                    onPressed: () {
                      Clipboard.setData(ClipboardData(text: '${o['title']}\n${items.map((i) => '— $i').join('\n')}'));
                      ScaffoldMessenger.of(context)
                          .showSnackBar(const SnackBar(content: Text('Список скопирован')));
                    },
                    icon: const Icon(Icons.copy, size: 16),
                    label: const Text('Скопировать'),
                  ),
                ]),
                for (final i in items)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 3),
                    child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      const Padding(
                        padding: EdgeInsets.only(top: 6, right: 10),
                        child: Icon(Icons.circle, size: 6, color: VColors.lavender),
                      ),
                      Expanded(child: Text(i, style: const TextStyle(fontSize: 15, height: 1.35))),
                    ]),
                  ),
              ]),
            ),
          if (steps.isNotEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                const Text('КАК',
                    style: TextStyle(letterSpacing: 1.2, fontSize: 12, color: Color(0xFFE2B57C))),
                const SizedBox(height: 6),
                for (var n = 0; n < steps.length; n++)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      SizedBox(
                        width: 24,
                        child: Text('${n + 1}.', style: const TextStyle(color: VColors.gold, fontWeight: FontWeight.w700)),
                      ),
                      Expanded(child: Text(steps[n], style: const TextStyle(fontSize: 15, height: 1.35))),
                    ]),
                  ),
              ]),
            ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 16),
            child: chosen
                ? const Center(
                    child: Text('Этот вариант выбрали вы оба', style: TextStyle(color: VColors.gold)),
                  )
                : GoldButton(
                    onPressed: busy || mine ? null : onVote,
                    child: Text(mine ? 'Ты выбрал(а) этот вариант' : 'Выбрать этот вариант'),
                  ),
          ),
        ],
      ]),
    );
  }
}

class _ScheduleCard extends StatelessWidget {
  const _ScheduleCard({required this.scheduled, required this.busy, required this.onPick, required this.onClear});
  final DateTime? scheduled;
  final bool busy;
  final VoidCallback onPick, onClear;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: VColors.card,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: const Color(0x14FFFFFF)),
      ),
      child: scheduled == null
          ? Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              const Text('Когда устроим?', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w600)),
              const SizedBox(height: 4),
              const Text('Назначь день — план появится на экране «Сегодня» у вас обоих.',
                  style: TextStyle(color: VColors.muted, fontSize: 13)),
              const SizedBox(height: 12),
              FilledButton.icon(
                onPressed: busy ? null : onPick,
                icon: const Icon(Icons.event),
                label: const Text('Запланировать'),
              ),
            ])
          : Row(children: [
              const Icon(Icons.event_available, color: VColors.gold),
              const SizedBox(width: 12),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  const Text('Запланировано', style: TextStyle(color: VColors.muted, fontSize: 13)),
                  Text(formatWhen(scheduled!), style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600)),
                ]),
              ),
              TextButton(onPressed: busy ? null : onPick, child: const Text('Изменить')),
              IconButton(
                tooltip: 'Отменить',
                onPressed: busy ? null : onClear,
                icon: const Icon(Icons.close, color: VColors.muted),
              ),
            ]),
    );
  }
}

class _Paywall extends StatelessWidget {
  const _Paywall({required this.busy, required this.onDevActivate});
  final bool busy;
  final VoidCallback onDevActivate;

  @override
  Widget build(BuildContext context) {
    const perks = [
      'Планы ко всем совпадениям',
      'Новые колоды каждый месяц',
      'Колоды 18+ (с подтверждением возраста)',
      'Одна подписка на двоих',
    ];
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(24),
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFF2A2438), Color(0xFF1B1A2A)],
        ),
        border: Border.all(color: const Color(0x66F3C98B)),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          const Icon(Icons.auto_awesome, color: VColors.gold),
          const SizedBox(width: 10),
          Text('Вечер+', style: display(24)),
        ]),
        const SizedBox(height: 6),
        const Text('Бесплатный план уже открыт. Остальные — с подпиской.',
            style: TextStyle(color: VColors.soft)),
        const SizedBox(height: 14),
        for (final perk in perks)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 4),
            child: Row(children: [
              const Icon(Icons.check, size: 18, color: VColors.gold),
              const SizedBox(width: 10),
              Expanded(child: Text(perk)),
            ]),
          ),
        const SizedBox(height: 16),
        const GoldButton(onPressed: null, child: Text('Оформить — скоро')),
        if (kDebugMode) ...[
          const SizedBox(height: 8),
          TextButton(
            onPressed: busy ? null : onDevActivate,
            child: const Text('Включить тестовую подписку (только для разработки)'),
          ),
        ],
      ]),
    );
  }
}
