import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/push.dart';
import '../../core/format.dart';
import '../../core/theme.dart';

// ---------- данные раздела «Мы» ----------

final personProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/person');
  return Map<String, dynamic>.from(r.data as Map);
});

final datesProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/dates');
  return Map<String, dynamic>.from(r.data as Map);
});

final remindersProvider = FutureProvider.autoDispose<List<Map<String, dynamic>>>((ref) async {
  final r = await ref.read(dioProvider).get('/reminders');
  return (r.data as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
});

void invalidateUs(WidgetRef ref) {
  ref.invalidate(personProvider);
  ref.invalidate(datesProvider);
  ref.invalidate(remindersProvider);
}

/// Вкладка «Мы».
class UsScreen extends ConsumerWidget {
  const UsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final person = ref.watch(personProvider);
    final dates = ref.watch(datesProvider).valueOrNull;
    final reminders = ref.watch(remindersProvider).valueOrNull ?? const [];

    return Scaffold(
      appBar: AppBar(
        title: Text('Пара', style: display(24)),
        actions: [
          IconButton(
            tooltip: 'Выйти',
            icon: const Icon(Icons.logout, color: VColors.muted),
            onPressed: () async {
              await stopPush(ref.read(dioProvider));
              await clearToken();
              if (context.mounted) context.go('/welcome');
            },
          ),
        ],
      ),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () async {
            invalidateUs(ref);
            await ref.read(personProvider.future);
          },
          child: person.when(
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => ListView(padding: const EdgeInsets.all(20), children: [
              Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894))),
            ]),
            data: (p) {
              final partnerName = (p['partner_name'] as String?) ?? 'партнёр';
              final together = dates?['together_days'] as int?;
              final items = ((dates?['items'] as List?) ?? const []).cast<Map>();
              final next = items.where((i) => (i['days_left'] as int) >= 0).firstOrNull;
              final open = reminders.where((r) => r['done'] != true && r['for_me'] == true).length;
              final filled = p['partner_filled'] as int;
              final myFilled = (p['me'] as List)
                  .expand((s) => (s as Map)['fields'] as List)
                  .where((f) => (f as Map)['value'] != null)
                  .length;

              return ListView(padding: const EdgeInsets.all(20), children: [
                _TogetherCard(days: together, onAdd: () => _go(context, ref, '/us/dates')),
                const SizedBox(height: 16),
                _Tile(
                  icon: Icons.favorite_outline,
                  title: 'Мой человек${p['partner_name'] == null ? '' : ': ${p['partner_name']}'}',
                  subtitle: filled == 0
                      ? '${_cap(partnerName)} пока ничего о себе не рассказал(а)'
                      : 'Размеры, любимое, мечты — $filled ${plural(filled, 'пункт', 'пункта', 'пунктов')}',
                  onTap: () => _go(context, ref, '/us/partner'),
                ),
                _Tile(
                  icon: Icons.person_outline,
                  title: 'О себе',
                  subtitle: myFilled == 0
                      ? 'Расскажи о себе — это станет памяткой для партнёра'
                      : 'Заполнено: $myFilled',
                  onTap: () => _go(context, ref, '/us/me'),
                ),
                _Tile(
                  icon: Icons.event_outlined,
                  title: 'Важные даты',
                  subtitle: next == null
                      ? 'Знакомство, дни рождения, годовщины'
                      : '${_cap(daysLeftText(next['days_left'] as int))}: ${next['title']}',
                  onTap: () => _go(context, ref, '/us/dates'),
                ),
                _Tile(
                  icon: Icons.checklist,
                  title: 'Напоминания',
                  subtitle: open == 0 ? 'Себе, партнёру или обоим' : 'Для тебя: $open',
                  onTap: () => _go(context, ref, '/us/reminders'),
                ),
              ]);
            },
          ),
        ),
      ),
    );
  }

  static String _cap(String s) => s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);

  static Future<void> _go(BuildContext context, WidgetRef ref, String path) async {
    await context.push(path);
    invalidateUs(ref);
  }
}

class _TogetherCard extends StatelessWidget {
  const _TogetherCard({required this.days, required this.onAdd});
  final int? days;
  final VoidCallback onAdd;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(22),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(24),
        gradient: VColors.cardGradient,
        border: Border.all(color: const Color(0x4DF3C98B)),
      ),
      child: days == null
          ? Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('Сколько вы вместе?', style: display(22)),
              const SizedBox(height: 6),
              const Text('Укажи день знакомства — будем считать ваши дни.',
                  style: TextStyle(color: VColors.soft)),
              const SizedBox(height: 12),
              OutlinedButton(onPressed: onAdd, child: const Text('Указать дату')),
            ])
          : Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              const Text('ВМЕСТЕ', style: TextStyle(letterSpacing: 1.4, fontSize: 12, color: Color(0xFFE2B57C))),
              const SizedBox(height: 6),
              Text('${days!} ${plural(days!, 'день', 'дня', 'дней')}', style: display(36)),
            ]),
    );
  }
}

class _Tile extends StatelessWidget {
  const _Tile({required this.icon, required this.title, required this.subtitle, required this.onTap});
  final IconData icon;
  final String title, subtitle;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Material(
        color: VColors.card,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(20),
          side: const BorderSide(color: Color(0x14FFFFFF)),
        ),
        child: InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(18),
            child: Row(children: [
              Container(
                width: 46,
                height: 46,
                decoration: BoxDecoration(color: VColors.card2, borderRadius: BorderRadius.circular(14)),
                child: Icon(icon, color: VColors.lavender),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(title, style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600)),
                  const SizedBox(height: 3),
                  Text(subtitle, style: const TextStyle(color: VColors.muted, fontSize: 13)),
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

/// Общий диалог ввода текста.
Future<String?> askText(BuildContext context,
    {required String title, String initial = '', String hint = '', int maxLength = 300, int maxLines = 1}) {
  final c = TextEditingController(text: initial);
  return showDialog<String>(
    context: context,
    builder: (ctx) => AlertDialog(
      backgroundColor: VColors.card,
      title: Text(title),
      content: TextField(
        controller: c,
        autofocus: true,
        maxLength: maxLength,
        minLines: 1,
        maxLines: maxLines,
        textCapitalization: TextCapitalization.sentences,
        decoration: InputDecoration(hintText: hint),
        onSubmitted: maxLines == 1 ? (v) => Navigator.of(ctx).pop(v) : null,
      ),
      actions: [
        TextButton(onPressed: () => Navigator.of(ctx).pop(), child: const Text('Отмена')),
        TextButton(
          onPressed: () => Navigator.of(ctx).pop(c.text),
          child: const Text('Сохранить', style: TextStyle(color: VColors.gold)),
        ),
      ],
    ),
  );
}
