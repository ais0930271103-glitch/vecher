import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
import '../content/content_entries.dart';

/// Данные «Моего пространства»: GET /solo.
final soloProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/solo');
  return Map<String, dynamic>.from(r.data as Map);
});

/// Вкладка «Я» по макету «Моё пространство».
class SoloScreen extends ConsumerWidget {
  const SoloScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final solo = ref.watch(soloProvider);
    return Scaffold(
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () => ref.refresh(soloProvider.future),
          child: solo.when(
            skipLoadingOnRefresh: true,
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => ListView(padding: const EdgeInsets.all(20), children: [
              Text(errorText(e), style: const TextStyle(color: VColors.coral)),
            ]),
            data: (s) => _Body(s: s),
          ),
        ),
      ),
    );
  }
}

class _Body extends ConsumerWidget {
  const _Body({required this.s});
  final Map<String, dynamic> s;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final name = s['partner_name'] as String?;
    final active = s['partner_active_today'] == true;
    final swiped = s['swiped'] as int;
    final remaining = s['remaining'] as int;
    final wishes = (s['wishes'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
    final letters = (s['letters'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
    final surprises = (s['surprises'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
    final pending = letters.where((l) => l['delivered'] != true).toList();

    return ListView(padding: const EdgeInsets.fromLTRB(20, 24, 20, 24), children: [
      Text('Моё пространство', style: display(26)),
      const SizedBox(height: 4),
      Row(children: [
        Container(
          width: 8,
          height: 8,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: active ? const Color(0xFF7FD6A4) : const Color(0xFF6F6C80),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            active
                ? '${name ?? 'Партнёр'} сегодня уже здесь — загляни в «Сегодня»'
                : '${name ?? 'Партнёр'} ответит позже — а пока это время для тебя',
            style: const TextStyle(fontSize: 13, color: VColors.muted),
          ),
        ),
      ]),
      const SizedBox(height: 16),

      const ContentPersonalCard(),
      const SizedBox(height: 16),
      // Прогресс по карточкам — кольцо как в макете.
      InkWell(
        borderRadius: BorderRadius.circular(24),
        onTap: () => StatefulNavigationShell.maybeOf(context)?.goBranch(1),
        child: Ink(
          padding: const EdgeInsets.fromLTRB(20, 18, 20, 18),
          decoration: glowBox(),
          child: Row(children: [
            _Ring(done: swiped, total: swiped + remaining),
            const SizedBox(width: 16),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text('Ты ответил(а) на $swiped ${plural(swiped, 'карточку', 'карточки', 'карточек')}',
                    style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600, height: 1.35)),
                const SizedBox(height: 4),
                Text(
                  remaining == 0
                      ? 'Все колоды пройдены. Совпадения появятся, когда ${name ?? 'партнёр'} тоже ответит'
                      : 'Совпадения откроются, когда ${name ?? 'партнёр'} тоже ответит. '
                          'Ещё $remaining ${plural(remaining, 'карточка', 'карточки', 'карточек')}',
                  style: const TextStyle(fontSize: 13, height: 1.4, color: VColors.link),
                ),
              ]),
            ),
          ]),
        ),
      ),
      const SizedBox(height: 16),
      const Text('Сделать для партнёра сейчас', style: TextStyle(fontSize: 13, color: VColors.muted)),
      const SizedBox(height: 10),
      GridView.count(
        crossAxisCount: 2,
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        mainAxisSpacing: 10,
        crossAxisSpacing: 10,
        childAspectRatio: 1.45,
        children: [
          _Tile(
            icon: Icons.mail_outline,
            title: 'Письмо на утро',
            subtitle: pending.isEmpty ? 'Придёт в 8:00' : 'Ждёт доставки: ${pending.length}',
            onTap: () => _letter(context, ref, name),
          ),
          const _Tile(
            icon: Icons.mic_none,
            title: 'Голосовое',
            subtitle: 'Скоро',
            disabled: true,
          ),
          _Tile(
            icon: Icons.nightlight_outlined,
            lavender: true,
            title: 'Тихое желание',
            subtitle: wishes.isEmpty ? 'Видно только тебе' : 'Записано: ${wishes.length}',
            onTap: () => _wish(context, ref),
          ),
          _Tile(
            icon: Icons.card_giftcard,
            lavender: true,
            title: 'Собрать сюрприз',
            subtitle: surprises.isEmpty ? 'Увидит только время' : 'Запланировано: ${surprises.length}',
            onTap: () => _surprise(context, ref, name),
          ),
        ],
      ),

      if (pending.isNotEmpty || surprises.isNotEmpty) ...[
        const SizedBox(height: 18),
        const SectionLabel('Уже готовишь'),
        const SizedBox(height: 10),
        for (final l in pending)
          _Row(
            icon: Icons.mail_outline,
            title: 'Письмо · придёт ${formatWhen(DateTime.parse(l['deliver_at'] as String))}',
            subtitle: l['text'] as String,
            onDelete: () => _del(context, ref, '/letters/${l['id']}'),
          ),
        for (final x in surprises)
          _Row(
            icon: Icons.card_giftcard,
            title: '${x['title']} · ${formatWhen(DateTime.parse(x['at'] as String))}',
            subtitle: (x['note'] as String?) ?? '${name ?? 'Партнёр'} видит только время',
            onDelete: () => _del(context, ref, '/surprises/${x['id']}'),
          ),
      ],

      if (wishes.isNotEmpty) ...[
        const SizedBox(height: 18),
        const SectionLabel('Тихие желания — только для тебя'),
        const SizedBox(height: 10),
        for (final w in wishes)
          _Row(
            icon: Icons.nightlight_outlined,
            title: w['text'] as String,
            onDelete: () => _del(context, ref, '/solo/wishes/${w['id']}'),
          ),
      ],

      const SizedBox(height: 18),
      Container(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        decoration: cardBox(),
        child: Row(children: [
          Container(
            width: 48,
            height: 48,
            decoration: BoxDecoration(color: const Color(0xFF2C2440), borderRadius: BorderRadius.circular(14)),
            child: const Icon(Icons.play_arrow_rounded, color: VColors.gold),
          ),
          const SizedBox(width: 14),
          const Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('Как говорить о желаниях', style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
              SizedBox(height: 3),
              Text('Аудиоуроки · скоро', style: TextStyle(fontSize: 13, color: VColors.text2)),
            ]),
          ),
        ]),
      ),
    ]);
  }

  // ---------- действия ----------

  Future<void> _del(BuildContext context, WidgetRef ref, String path) async {
    try {
      await ref.read(dioProvider).delete(path);
      ref.invalidate(soloProvider);
    } catch (e) {
      if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    }
  }

  Future<void> _post(BuildContext context, WidgetRef ref, String path, Map<String, dynamic> data, String ok) async {
    try {
      await ref.read(dioProvider).post(path, data: data);
      ref.invalidate(soloProvider);
      if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(ok)));
    } catch (e) {
      if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    }
  }

  Future<void> _letter(BuildContext context, WidgetRef ref, String? name) async {
    final text = await _sheetText(context,
        title: 'Письмо на утро',
        hint: 'Напиши то, что хочется сказать с утра…',
        note: '${name ?? 'Партнёр'} получит его в 8:00 на экране «Сегодня»',
        maxLines: 8,
        maxLength: 2000,
        action: 'Отправить к утру');
    if (text == null || text.trim().isEmpty || !context.mounted) return;
    await _post(context, ref, '/letters', {'text': text.trim()}, 'Письмо придёт утром');
  }

  Future<void> _wish(BuildContext context, WidgetRef ref) async {
    final text = await _sheetText(context,
        title: 'Тихое желание',
        hint: 'Чего хочется — даже если пока не готовы сказать вслух',
        note: 'Это видишь только ты',
        maxLines: 4,
        maxLength: 300,
        action: 'Сохранить');
    if (text == null || text.trim().isEmpty || !context.mounted) return;
    await _post(context, ref, '/solo/wishes', {'text': text.trim()}, 'Сохранено');
  }

  Future<void> _surprise(BuildContext context, WidgetRef ref, String? name) async {
    final result = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      backgroundColor: VColors.card,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
      builder: (_) => _SurpriseSheet(partnerName: name),
    );
    if (result == null || !context.mounted) return;
    await _post(context, ref, '/surprises', result, 'Сюрприз запланирован');
  }
}

/// Нижний лист с полем ввода.
Future<String?> _sheetText(BuildContext context,
    {required String title,
    required String hint,
    required String note,
    required int maxLines,
    required int maxLength,
    required String action}) {
  final c = TextEditingController();
  return showModalBottomSheet<String>(
    context: context,
    isScrollControlled: true,
    backgroundColor: VColors.card,
    shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
    builder: (ctx) => Padding(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + MediaQuery.of(ctx).viewInsets.bottom),
      child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text(title, style: display(22)),
        const SizedBox(height: 4),
        Text(note, style: const TextStyle(fontSize: 13, color: VColors.muted)),
        const SizedBox(height: 14),
        TextField(
          controller: c,
          autofocus: true,
          minLines: 3,
          maxLines: maxLines,
          maxLength: maxLength,
          textCapitalization: TextCapitalization.sentences,
          decoration: InputDecoration(hintText: hint),
        ),
        const SizedBox(height: 8),
        GoldButton(onPressed: () => Navigator.of(ctx).pop(c.text), child: Text(action)),
      ]),
    ),
  );
}

class _SurpriseSheet extends StatefulWidget {
  const _SurpriseSheet({this.partnerName});
  final String? partnerName;

  @override
  State<_SurpriseSheet> createState() => _SurpriseSheetState();
}

class _SurpriseSheetState extends State<_SurpriseSheet> {
  final _title = TextEditingController();
  final _note = TextEditingController();
  DateTime? _at;

  @override
  void dispose() {
    _title.dispose();
    _note.dispose();
    super.dispose();
  }

  Future<void> _pick() async {
    final now = DateTime.now();
    final d = await showDatePicker(
      context: context,
      initialDate: _at ?? now.add(const Duration(days: 1)),
      firstDate: DateTime(now.year, now.month, now.day),
      lastDate: now.add(const Duration(days: 365)),
      helpText: 'Когда сюрприз?',
    );
    if (d == null || !mounted) return;
    final t = await showTimePicker(
      context: context,
      initialTime: _at != null ? TimeOfDay.fromDateTime(_at!) : const TimeOfDay(hour: 19, minute: 0),
    );
    if (t == null) return;
    setState(() => _at = DateTime(d.year, d.month, d.day, t.hour, t.minute));
  }

  @override
  Widget build(BuildContext context) {
    final who = widget.partnerName ?? 'Партнёр';
    final ready = _title.text.trim().isNotEmpty && _at != null;
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + MediaQuery.of(context).viewInsets.bottom),
      child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text('Собрать сюрприз', style: display(22)),
        const SizedBox(height: 4),
        Text('$who увидит только «будет сюрприз» и время. Подробности откроются в назначенный момент.',
            style: const TextStyle(fontSize: 13, color: VColors.muted, height: 1.4)),
        const SizedBox(height: 14),
        TextField(
          controller: _title,
          maxLength: 120,
          textCapitalization: TextCapitalization.sentences,
          onChanged: (_) => setState(() {}),
          decoration: const InputDecoration(labelText: 'Что за сюрприз', hintText: 'Ужин на крыше', counterText: ''),
        ),
        const SizedBox(height: 10),
        TextField(
          controller: _note,
          maxLength: 500,
          minLines: 1,
          maxLines: 3,
          textCapitalization: TextCapitalization.sentences,
          decoration: const InputDecoration(labelText: 'Подсказка (по желанию)', hintText: 'Оденься тепло', counterText: ''),
        ),
        const SizedBox(height: 10),
        OutlinedButton.icon(
          icon: const Icon(Icons.event),
          label: Text(_at == null ? 'Выбрать день и время' : formatWhen(_at!)),
          onPressed: _pick,
        ),
        const SizedBox(height: 14),
        GoldButton(
          onPressed: ready
              ? () => Navigator.of(context).pop({
                    'title': _title.text.trim(),
                    'note': _note.text.trim().isEmpty ? null : _note.text.trim(),
                    'at': isoWithOffset(_at!),
                  })
              : null,
          child: const Text('Запланировать'),
        ),
      ]),
    );
  }
}

class _Ring extends StatelessWidget {
  const _Ring({required this.done, required this.total});
  final int done, total;

  @override
  Widget build(BuildContext context) {
    final v = total == 0 ? 0.0 : math.min(1.0, done / total);
    return SizedBox(
      width: 64,
      height: 64,
      child: Stack(alignment: Alignment.center, children: [
        SizedBox(
          width: 64,
          height: 64,
          child: CircularProgressIndicator(
            value: v,
            strokeWidth: 7,
            backgroundColor: const Color(0xFF2A2938),
            color: VColors.gold,
          ),
        ),
        Text('$done', style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700, color: VColors.goldText)),
      ]),
    );
  }
}

class _Tile extends StatelessWidget {
  const _Tile({
    required this.icon,
    required this.title,
    required this.subtitle,
    this.onTap,
    this.lavender = false,
    this.disabled = false,
  });
  final IconData icon;
  final String title, subtitle;
  final VoidCallback? onTap;
  final bool lavender, disabled;

  @override
  Widget build(BuildContext context) {
    return Opacity(
      opacity: disabled ? 0.55 : 1,
      child: Material(
        color: VColors.card,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(20),
          side: const BorderSide(color: Color(0x14FFFFFF)),
        ),
        child: InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: disabled ? null : onTap,
          child: Padding(
            padding: const EdgeInsets.all(14),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
              Icon(icon, size: 22, color: lavender ? const Color(0xFFCDBDF7) : VColors.gold),
              Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(title, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                const SizedBox(height: 2),
                Text(subtitle, style: const TextStyle(fontSize: 12, color: VColors.muted)),
              ]),
            ]),
          ),
        ),
      ),
    );
  }
}

class _Row extends StatelessWidget {
  const _Row({required this.icon, required this.title, this.subtitle, required this.onDelete});
  final IconData icon;
  final String title;
  final String? subtitle;
  final VoidCallback onDelete;

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      decoration: cardBox(radius: 16),
      child: ListTile(
        leading: Icon(icon, color: VColors.lavender, size: 20),
        title: Text(title, style: const TextStyle(fontSize: 15)),
        subtitle: subtitle == null
            ? null
            : Text(subtitle!, maxLines: 2, overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 13, color: VColors.text2)),
        trailing: IconButton(
          tooltip: 'Отменить',
          icon: const Icon(Icons.close, size: 18, color: VColors.muted),
          onPressed: onDelete,
        ),
      ),
    );
  }
}
