import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/theme.dart';
import 'us_screen.dart';

class RemindersScreen extends ConsumerWidget {
  const RemindersScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final list = ref.watch(remindersProvider);
    return Scaffold(
      appBar: AppBar(title: Text('Напоминания', style: display(22))),
      floatingActionButton: FloatingActionButton.extended(
        backgroundColor: VColors.gold,
        foregroundColor: VColors.ink,
        onPressed: () => addReminder(context, ref),
        icon: const Icon(Icons.add),
        label: const Text('Напомнить'),
      ),
      body: SafeArea(
        child: list.when(
          skipLoadingOnRefresh: true,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(child: Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894)))),
          data: (items) {
            if (items.isEmpty) {
              return const Padding(
                padding: EdgeInsets.all(32),
                child: Center(
                  child: Text(
                    '«Купить хлеб», «забрать сына», «позвонить маме» — себе, партнёру или обоим.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: VColors.soft, height: 1.4),
                  ),
                ),
              );
            }
            return ListView(padding: const EdgeInsets.fromLTRB(20, 4, 20, 96), children: [
              for (final r in items) ReminderTile(r: r),
            ]);
          },
        ),
      ),
    );
  }
}

/// Строка напоминания — используется и в разделе «Мы», и на экране «Сегодня».
class ReminderTile extends ConsumerWidget {
  const ReminderTile({super.key, required this.r, this.compact = false});
  final Map<String, dynamic> r;
  final bool compact;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final done = r['done'] == true;
    final due = r['due_at'] == null ? null : DateTime.parse(r['due_at'] as String);
    final overdue = !done && due != null && due.isBefore(DateTime.now());
    final meta = [r['who'] as String, if (due != null) formatWhen(due)].join(' · ');

    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      decoration: BoxDecoration(
        color: VColors.card,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: overdue ? const Color(0x80F5A894) : const Color(0x14FFFFFF)),
      ),
      child: ListTile(
        dense: compact,
        leading: Checkbox(
          value: done,
          activeColor: VColors.gold,
          checkColor: VColors.ink,
          shape: const CircleBorder(),
          onChanged: (_) async {
            await ref.read(dioProvider).post('/reminders/${r['id']}/done');
            ref.invalidate(remindersProvider);
          },
        ),
        title: Text(
          r['text'] as String,
          style: TextStyle(
            fontSize: 16,
            decoration: done ? TextDecoration.lineThrough : null,
            color: done ? VColors.muted : VColors.text,
          ),
        ),
        subtitle: Text(meta,
            style: TextStyle(fontSize: 12, color: overdue ? const Color(0xFFF5A894) : VColors.muted)),
        trailing: r['mine'] == true && !compact
            ? IconButton(
                tooltip: 'Удалить',
                icon: const Icon(Icons.delete_outline, color: VColors.muted, size: 20),
                onPressed: () async {
                  await ref.read(dioProvider).delete('/reminders/${r['id']}');
                  ref.invalidate(remindersProvider);
                },
              )
            : null,
      ),
    );
  }
}

Future<void> addReminder(BuildContext context, WidgetRef ref) async {
  final result = await showModalBottomSheet<Map<String, dynamic>>(
    context: context,
    isScrollControlled: true,
    backgroundColor: VColors.card,
    shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
    builder: (_) => const _AddReminderSheet(),
  );
  if (result == null) return;
  try {
    await ref.read(dioProvider).post('/reminders', data: result);
    ref.invalidate(remindersProvider);
  } catch (e) {
    if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
  }
}

class _AddReminderSheet extends StatefulWidget {
  const _AddReminderSheet();

  @override
  State<_AddReminderSheet> createState() => _AddReminderSheetState();
}

class _AddReminderSheetState extends State<_AddReminderSheet> {
  final _text = TextEditingController();
  String _target = 'me';
  DateTime? _due;

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  Future<void> _pickDue() async {
    final now = DateTime.now();
    final date = await showDatePicker(
      context: context,
      initialDate: _due ?? now,
      firstDate: DateTime(now.year, now.month, now.day),
      lastDate: now.add(const Duration(days: 365)),
    );
    if (date == null || !mounted) return;
    final time = await showTimePicker(
      context: context,
      initialTime: _due != null ? TimeOfDay.fromDateTime(_due!) : TimeOfDay(hour: (now.hour + 1) % 24, minute: 0),
    );
    if (time == null) return;
    setState(() => _due = DateTime(date.year, date.month, date.day, time.hour, time.minute));
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + MediaQuery.of(context).viewInsets.bottom),
      child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text('Напоминание', style: display(22)),
        const SizedBox(height: 14),
        TextField(
          controller: _text,
          autofocus: true,
          maxLength: 200,
          textCapitalization: TextCapitalization.sentences,
          decoration: const InputDecoration(hintText: 'Например: купить хлеб', counterText: ''),
          onChanged: (_) => setState(() {}),
        ),
        const SizedBox(height: 14),
        SegmentedButton<String>(
          segments: const [
            ButtonSegment(value: 'me', label: Text('Себе')),
            ButtonSegment(value: 'partner', label: Text('Партнёру')),
            ButtonSegment(value: 'both', label: Text('Обоим')),
          ],
          selected: {_target},
          onSelectionChanged: (s) => setState(() => _target = s.first),
        ),
        const SizedBox(height: 12),
        Row(children: [
          Expanded(
            child: OutlinedButton.icon(
              icon: const Icon(Icons.schedule),
              label: Text(_due == null ? 'Без времени' : formatWhen(_due!)),
              onPressed: _pickDue,
            ),
          ),
          if (_due != null)
            IconButton(
              tooltip: 'Без времени',
              icon: const Icon(Icons.close, color: VColors.muted),
              onPressed: () => setState(() => _due = null),
            ),
        ]),
        const SizedBox(height: 14),
        FilledButton(
          onPressed: _text.text.trim().isEmpty
              ? null
              : () => Navigator.of(context).pop({
                    'text': _text.text.trim(),
                    'target': _target,
                    'due_at': _due == null ? null : isoWithOffset(_due!),
                  }),
          child: const Text('Сохранить'),
        ),
      ]),
    );
  }
}
