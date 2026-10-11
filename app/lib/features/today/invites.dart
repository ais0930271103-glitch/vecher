import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

/// Мягкие приглашения пары: GET /invites.
final invitesProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final r = await ref.read(dioProvider).get('/invites');
  return (r.data as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
});

const _presets = ['Ужин вдвоём', 'Прогулка', 'Кино дома', 'Свидание', 'Побыть вдвоём'];

String _when(Map<String, dynamic> i, [String key = 'at']) {
  final v = i[key] as String?;
  return v == null ? '' : formatWhen(DateTime.parse(v).toLocal());
}

void _toast(BuildContext context, String text) =>
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

/// Приглашения на «Сегодня»: входящие (с мягкими ответами) и мои (со статусом).
class InvitesSection extends ConsumerWidget {
  const InvitesSection({super.key, this.partnerName});
  final String? partnerName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final list = ref.watch(invitesProvider).valueOrNull ?? const [];
    if (list.isEmpty) return const SizedBox.shrink();
    return Column(children: [
      for (final i in list)
        Padding(
          padding: const EdgeInsets.only(bottom: 14),
          child: i['mine'] == true
              ? _Outgoing(invite: i, partnerName: partnerName)
              : _Incoming(invite: i, partnerName: partnerName),
        ),
    ]);
  }
}

class _Incoming extends ConsumerStatefulWidget {
  const _Incoming({required this.invite, this.partnerName});
  final Map<String, dynamic> invite;
  final String? partnerName;

  @override
  ConsumerState<_Incoming> createState() => _IncomingState();
}

class _IncomingState extends ConsumerState<_Incoming> {
  bool _busy = false;

  Future<void> _answer(String answer, {DateTime? proposed}) async {
    setState(() => _busy = true);
    try {
      await ref.read(dioProvider).post('/invites/${widget.invite['id']}/answer', data: {
        'answer': answer,
        if (proposed != null) 'proposed_at': isoWithOffset(proposed),
      });
      ref.invalidate(invitesProvider);
    } catch (e) {
      if (mounted) _toast(context, errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _otherDay() async {
    final now = DateTime.now();
    final day = await showDatePicker(
      context: context,
      initialDate: now.add(const Duration(days: 1)),
      firstDate: now,
      lastDate: now.add(const Duration(days: 60)),
      helpText: 'Какой день тебе удобен?',
    );
    if (day == null || !mounted) return;
    final time = await showTimePicker(
      context: context,
      initialTime: const TimeOfDay(hour: 19, minute: 0),
      helpText: 'Во сколько?',
    );
    if (time == null) return;
    await _answer('later', proposed: DateTime(day.year, day.month, day.day, time.hour, time.minute));
  }

  @override
  Widget build(BuildContext context) {
    final i = widget.invite;
    final name = widget.partnerName ?? 'Партнёр';
    final status = i['status'] as String;
    if (status != 'pending') {
      final text = switch (status) {
        'yes' => 'Договорились: ${i['title']}${i['at'] != null ? ' · ${_when(i)}' : ''}',
        'later' => 'Ты предложил(а) другой день${i['proposed_at'] != null ? ': ${_when(i, 'proposed_at')}' : ''}',
        _ => 'Ты ответил(а): «Не сегодня, но мне приятно»',
      };
      return _Note(icon: status == 'yes' ? Icons.favorite : Icons.schedule, text: text);
    }
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: glowBox(radius: 20),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Row(children: [
          Initial(name: widget.partnerName),
          const SizedBox(width: 12),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('$name приглашает', style: const TextStyle(color: VColors.muted, fontSize: 13)),
              const SizedBox(height: 2),
              Text(i['title'] as String, style: display(22)),
            ]),
          ),
        ]),
        if (i['at'] != null) ...[
          const SizedBox(height: 10),
          Row(children: [
            const Icon(Icons.schedule, size: 16, color: VColors.gold),
            const SizedBox(width: 6),
            Text(_when(i), style: const TextStyle(color: VColors.soft)),
          ]),
        ],
        if (i['note'] != null) ...[
          const SizedBox(height: 8),
          Text('«${i['note']}»', style: const TextStyle(color: VColors.soft, fontStyle: FontStyle.italic)),
        ],
        const SizedBox(height: 16),
        GoldButton(height: 48, onPressed: _busy ? null : () => _answer('yes'), child: const Text('Да!')),
        const SizedBox(height: 8),
        OutlinedButton(
          style: OutlinedButton.styleFrom(
            minimumSize: const Size.fromHeight(46),
            shape: const StadiumBorder(),
            side: const BorderSide(color: Color(0x73F3C98B)),
            foregroundColor: VColors.goldText,
          ),
          onPressed: _busy ? null : _otherDay,
          child: const Text('Давай в другой день'),
        ),
        TextButton(
          onPressed: _busy ? null : () => _answer('not_today'),
          child: const Text('Не сегодня, но мне приятно', style: TextStyle(color: VColors.link)),
        ),
      ]),
    );
  }
}

class _Outgoing extends ConsumerWidget {
  const _Outgoing({required this.invite, this.partnerName});
  final Map<String, dynamic> invite;
  final String? partnerName;

  Future<void> _post(BuildContext context, WidgetRef ref, String action) async {
    try {
      await ref.read(dioProvider).post('/invites/${invite['id']}/$action');
      ref.invalidate(invitesProvider);
    } catch (e) {
      if (context.mounted) _toast(context, errorText(e));
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = invite;
    final name = partnerName ?? 'Партнёр';
    final title = i['title'] as String;
    switch (i['status']) {
      case 'pending':
        return _Note(
          icon: Icons.mail_outline,
          text: 'Ты пригласил(а): $title${i['at'] != null ? ' · ${_when(i)}' : ''}. $name ещё не ответил(а).',
          action: TextButton(
            onPressed: () => _post(context, ref, 'cancel'),
            child: const Text('Отменить', style: TextStyle(color: VColors.muted)),
          ),
        );
      case 'yes':
        return _Note(
          icon: Icons.favorite,
          gold: true,
          text: '$name: «Да!» — $title${i['at'] != null ? ' · ${_when(i)}' : ''}',
        );
      case 'later':
        final proposed = i['proposed_at'] != null ? _when(i, 'proposed_at') : null;
        return _Note(
          icon: Icons.schedule,
          text: proposed == null
              ? '$name: «Давай в другой день» — $title'
              : '$name предлагает другой день: $proposed — $title',
          action: proposed == null
              ? null
              : TextButton(
                  onPressed: () => _post(context, ref, 'agree'),
                  child: const Text('Договорились', style: TextStyle(color: VColors.gold)),
                ),
        );
      default:
        return _Note(icon: Icons.favorite_border, text: '$name: «Не сегодня, но мне приятно» — $title');
    }
  }
}

class _Note extends StatelessWidget {
  const _Note({required this.icon, required this.text, this.action, this.gold = false});
  final IconData icon;
  final String text;
  final Widget? action;
  final bool gold;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 12, 8, 12),
      decoration: cardBox(gold: gold),
      child: Row(children: [
        Icon(icon, size: 20, color: VColors.gold),
        const SizedBox(width: 12),
        Expanded(child: Text(text, style: const TextStyle(fontSize: 14, height: 1.35, color: VColors.soft))),
        if (action != null) action!,
      ]),
    );
  }
}

/// Кнопка «Пригласить» на «Сегодня».
class InviteButton extends ConsumerWidget {
  const InviteButton({super.key, this.partnerName});
  final String? partnerName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return OutlinedButton.icon(
      style: OutlinedButton.styleFrom(
        minimumSize: const Size.fromHeight(50),
        shape: const StadiumBorder(),
        side: const BorderSide(color: Color(0x73F3C98B)),
        foregroundColor: VColors.goldText,
      ),
      onPressed: () async {
        final sent = await showModalBottomSheet<bool>(
          context: context,
          isScrollControlled: true,
          backgroundColor: VColors.card,
          shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
          builder: (_) => _InviteSheet(partnerName: partnerName),
        );
        if (sent == true) ref.invalidate(invitesProvider);
      },
      icon: const Icon(Icons.nightlight_outlined, size: 18),
      label: const Text('Пригласить провести время вместе'),
    );
  }
}

class _InviteSheet extends ConsumerStatefulWidget {
  const _InviteSheet({this.partnerName});
  final String? partnerName;

  @override
  ConsumerState<_InviteSheet> createState() => _InviteSheetState();
}

class _InviteSheetState extends ConsumerState<_InviteSheet> {
  final _title = TextEditingController(text: _presets.first);
  final _note = TextEditingController();
  String _whenKey = 'tonight';
  DateTime? _custom;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _title.dispose();
    _note.dispose();
    super.dispose();
  }

  DateTime? get _at {
    final now = DateTime.now();
    return switch (_whenKey) {
      'tonight' => now.hour < 19 ? DateTime(now.year, now.month, now.day, 19) : now.add(const Duration(hours: 1)),
      'tomorrow' => DateTime(now.year, now.month, now.day + 1, 19),
      'custom' => _custom,
      _ => null,
    };
  }

  Future<void> _pickCustom() async {
    final now = DateTime.now();
    final day = await showDatePicker(
      context: context,
      initialDate: now,
      firstDate: now,
      lastDate: now.add(const Duration(days: 60)),
    );
    if (day == null || !mounted) return;
    final time = await showTimePicker(context: context, initialTime: const TimeOfDay(hour: 19, minute: 0));
    if (time == null) return;
    setState(() {
      _custom = DateTime(day.year, day.month, day.day, time.hour, time.minute);
      _whenKey = 'custom';
    });
  }

  Future<void> _send() async {
    final title = _title.text.trim();
    if (title.isEmpty) {
      setState(() => _error = 'Напиши, куда или на что приглашаешь');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final at = _at;
      await ref.read(dioProvider).post('/invites', data: {
        'title': title,
        if (_note.text.trim().isNotEmpty) 'note': _note.text.trim(),
        if (at != null) 'at': isoWithOffset(at),
      });
      if (mounted) Navigator.of(context).pop(true);
    } catch (e) {
      setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Widget _chip(String label, bool selected, VoidCallback onTap) => ChoiceChip(
        label: Text(label),
        selected: selected,
        onSelected: (_) => onTap(),
        selectedColor: VColors.segment,
        backgroundColor: VColors.chip,
        labelStyle: TextStyle(color: selected ? VColors.goldText : VColors.text),
        side: BorderSide(color: selected ? VColors.gold : const Color(0x14FFFFFF)),
        showCheckmark: false,
        shape: const StadiumBorder(),
      );

  @override
  Widget build(BuildContext context) {
    final name = widget.partnerName ?? 'Партнёр';
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + MediaQuery.of(context).viewInsets.bottom),
      child: SingleChildScrollView(
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, mainAxisSize: MainAxisSize.min, children: [
          Text('Пригласить', style: display(26)),
          const SizedBox(height: 6),
          Text('$name сможет ответить «Да!», «Давай в другой день» или «Не сегодня, но мне приятно» — без «нет».',
              style: const TextStyle(color: VColors.muted, fontSize: 13, height: 1.4)),
          const SizedBox(height: 16),
          Wrap(spacing: 8, runSpacing: 8, children: [
            for (final p in _presets)
              _chip(p, _title.text == p, () => setState(() => _title.text = p)),
          ]),
          const SizedBox(height: 10),
          TextField(
            controller: _title,
            maxLength: 80,
            textCapitalization: TextCapitalization.sentences,
            onChanged: (_) => setState(() {}),
            decoration: const InputDecoration(labelText: 'Куда или на что'),
          ),
          const SectionLabel('Когда', color: VColors.label),
          const SizedBox(height: 8),
          Wrap(spacing: 8, runSpacing: 8, children: [
            _chip('Сегодня вечером', _whenKey == 'tonight', () => setState(() => _whenKey = 'tonight')),
            _chip('Завтра вечером', _whenKey == 'tomorrow', () => setState(() => _whenKey = 'tomorrow')),
            _chip(_custom == null ? 'Выбрать…' : formatWhen(_custom!), _whenKey == 'custom', _pickCustom),
            _chip('Без времени', _whenKey == 'none', () => setState(() => _whenKey = 'none')),
          ]),
          const SizedBox(height: 14),
          TextField(
            controller: _note,
            maxLength: 300,
            maxLines: 2,
            minLines: 1,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(labelText: 'Пара слов (необязательно)'),
          ),
          if (_error != null) ...[
            Text(_error!, style: const TextStyle(color: VColors.coral)),
            const SizedBox(height: 8),
          ],
          const SizedBox(height: 4),
          GoldButton(onPressed: _busy ? null : _send, child: Text(_busy ? 'Отправляем…' : 'Пригласить')),
        ]),
      ),
    );
  }
}
