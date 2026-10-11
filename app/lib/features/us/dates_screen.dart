import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
import 'us_screen.dart';

const _kinds = <(String, String, IconData)>[
  ('met', 'Знакомство', Icons.auto_awesome),
  ('birthday', 'День рождения', Icons.cake_outlined),
  ('anniversary', 'Годовщина', Icons.favorite_outline),
  ('other', 'Другое', Icons.event_outlined),
];

IconData _icon(String kind) => _kinds.firstWhere((k) => k.$1 == kind, orElse: () => _kinds.last).$3;

class DatesScreen extends ConsumerWidget {
  const DatesScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final dates = ref.watch(datesProvider);
    return Scaffold(
      appBar: AppBar(title: Text('Важные даты', style: display(22))),
      floatingActionButton: FloatingActionButton.extended(
        backgroundColor: VColors.gold,
        foregroundColor: VColors.ink,
        onPressed: () => _add(context, ref, hasMet: _hasMet(dates.valueOrNull)),
        icon: const Icon(Icons.add),
        label: const Text('Добавить'),
      ),
      body: SafeArea(
        child: dates.when(
          skipLoadingOnRefresh: true,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(child: Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894)))),
          data: (d) {
            final items = (d['items'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
            if (items.isEmpty) {
              return const Padding(
                padding: EdgeInsets.all(32),
                child: Center(
                  child: Text(
                    'Добавь день знакомства, дни рождения и годовщины — «Вечер» напомнит заранее и подскажет идеи.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: VColors.soft, height: 1.4),
                  ),
                ),
              );
            }
            return ListView(padding: const EdgeInsets.fromLTRB(20, 4, 20, 96), children: [
              for (final i in items) _DateTile(i: i),
            ]);
          },
        ),
      ),
    );
  }

  bool _hasMet(Map<String, dynamic>? d) =>
      ((d?['items'] as List?) ?? const []).any((i) => (i as Map)['kind'] == 'met');

  Future<void> _add(BuildContext context, WidgetRef ref, {required bool hasMet}) async {
    final result = await showModalBottomSheet<Map<String, dynamic>>(
      context: context,
      isScrollControlled: true,
      backgroundColor: VColors.card,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
      builder: (_) => _AddDateSheet(hasMet: hasMet),
    );
    if (result == null) return;
    try {
      await ref.read(dioProvider).post('/dates', data: result);
      ref.invalidate(datesProvider);
    } catch (e) {
      if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    }
  }
}

class _DateTile extends ConsumerWidget {
  const _DateTile({required this.i});
  final Map<String, dynamic> i;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final days = i['days_left'] as int;
    final years = i['years'] as int?;
    final next = DateTime.parse(i['next'] as String);
    final soon = days >= 0 && days <= 14;
    final kind = i['kind'] as String;
    final yearsText = years == null
        ? ''
        : kind == 'birthday'
            ? ' · исполнится $years'
            : ' · $years ${plural(years, 'год', 'года', 'лет')}';

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: VColors.card,
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: soon ? const Color(0x80F3C98B) : const Color(0x14FFFFFF)),
      ),
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
        leading: Icon(_icon(kind), color: soon ? VColors.gold : VColors.lavender),
        title: Text(i['title'] as String, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
        subtitle: Text('${formatDay(next)}$yearsText',
            style: const TextStyle(color: VColors.muted, fontSize: 13)),
        trailing: Row(mainAxisSize: MainAxisSize.min, children: [
          Text(daysLeftText(days), style: TextStyle(color: soon ? VColors.gold : VColors.soft, fontSize: 13)),
          IconButton(
            tooltip: 'Удалить',
            icon: const Icon(Icons.delete_outline, color: VColors.muted, size: 20),
            onPressed: () async {
              await ref.read(dioProvider).delete('/dates/${i['id']}');
              ref.invalidate(datesProvider);
            },
          ),
        ]),
      ),
    );
  }
}

class _AddDateSheet extends StatefulWidget {
  const _AddDateSheet({required this.hasMet});
  final bool hasMet;

  @override
  State<_AddDateSheet> createState() => _AddDateSheetState();
}

class _AddDateSheetState extends State<_AddDateSheet> {
  late String _kind = widget.hasMet ? 'birthday' : 'met';
  final _title = TextEditingController();
  DateTime? _day;
  bool _yearly = true;

  @override
  void dispose() {
    _title.dispose();
    super.dispose();
  }

  String _defaultTitle() => _kinds.firstWhere((k) => k.$1 == _kind).$2;

  @override
  Widget build(BuildContext context) {
    final ready = _day != null;
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + MediaQuery.of(context).viewInsets.bottom),
      child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text('Новая дата', style: display(22)),
        const SizedBox(height: 14),
        Wrap(spacing: 8, runSpacing: 8, children: [
          for (final (id, label, _) in _kinds)
            if (id != 'met' || !widget.hasMet)
              ChoiceChip(
                label: Text(label),
                selected: _kind == id,
                onSelected: (_) => setState(() {
                  _kind = id;
                  if (id == 'met') _yearly = true;
                }),
              ),
        ]),
        const SizedBox(height: 14),
        TextField(
          controller: _title,
          maxLength: 80,
          textCapitalization: TextCapitalization.sentences,
          decoration: InputDecoration(
            labelText: 'Название',
            hintText: _kind == 'birthday' ? 'Например: День рождения Ани' : _defaultTitle(),
            counterText: '',
          ),
        ),
        const SizedBox(height: 12),
        OutlinedButton.icon(
          icon: const Icon(Icons.calendar_month),
          label: Text(_day == null ? 'Выбрать дату' : formatDay(_day!)),
          onPressed: () async {
            final now = DateTime.now();
            final picked = await showDatePicker(
              context: context,
              initialDate: _day ?? now,
              firstDate: DateTime(1940),
              lastDate: DateTime(now.year + 5),
            );
            if (picked != null) setState(() => _day = picked);
          },
        ),
        if (_kind != 'met')
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: _yearly,
            onChanged: (v) => setState(() => _yearly = v),
            title: const Text('Каждый год'),
          ),
        const SizedBox(height: 12),
        GoldButton(
          onPressed: ready
              ? () => Navigator.of(context).pop({
                    'title': _title.text.trim().isEmpty ? _defaultTitle() : _title.text.trim(),
                    'day': isoDate(_day!),
                    'kind': _kind,
                    'yearly': _yearly,
                  })
              : null,
          child: const Text('Сохранить'),
        ),
      ]),
    );
  }
}
