import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/theme.dart';
import 'us_screen.dart';

List<Map<String, dynamic>> _sections(Map<String, dynamic> p, String key) =>
    (p[key] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();

List<Map<String, dynamic>> _fields(Map<String, dynamic> section) =>
    (section['fields'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();

Widget _sectionTitle(String t) => Padding(
      padding: const EdgeInsets.only(top: 18, bottom: 8),
      child: Text(t.toUpperCase(),
          style: const TextStyle(letterSpacing: 1.4, fontSize: 12, color: Color(0xFFE2B57C))),
    );

/// Памятка о партнёре: что он рассказал о себе + мои личные заметки.
class PartnerMemoScreen extends ConsumerWidget {
  const PartnerMemoScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final person = ref.watch(personProvider);
    return Scaffold(
      appBar: AppBar(title: Text('Памятка', style: display(22))),
      body: SafeArea(
        child: person.when(
          skipLoadingOnRefresh: true,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(child: Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894)))),
          data: (p) {
            final name = (p['partner_name'] as String?) ?? 'Партнёр';
            final notes = (p['notes'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
            final filledSections = _sections(p, 'partner')
                .map((s) => (s, _fields(s).where((f) => f['value'] != null).toList()))
                .where((pair) => pair.$2.isNotEmpty)
                .toList();

            return ListView(padding: const EdgeInsets.fromLTRB(20, 4, 20, 32), children: [
              Text(name, style: display(28)),
              const SizedBox(height: 4),
              const Text('Что партнёр рассказал о себе. Долгое нажатие — скопировать.',
                  style: TextStyle(color: VColors.muted, fontSize: 13)),
              if (filledSections.isEmpty)
                Container(
                  margin: const EdgeInsets.only(top: 18),
                  padding: const EdgeInsets.all(18),
                  decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(18)),
                  child: const Text(
                    'Пока пусто. Попросите партнёра заполнить «О себе» в разделе «Мы» — '
                    'размеры, любимые цветы, мечты. Это займёт пару минут.',
                    style: TextStyle(color: VColors.soft, height: 1.4),
                  ),
                ),
              for (final (section, fields) in filledSections) ...[
                _sectionTitle(section['title'] as String),
                Container(
                  decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(18)),
                  child: Column(children: [
                    for (final f in fields)
                      ListTile(
                        title: Text(f['label'] as String, style: const TextStyle(color: VColors.muted, fontSize: 13)),
                        subtitle: Text(f['value'] as String,
                            style: const TextStyle(color: VColors.text, fontSize: 16, height: 1.35)),
                        onLongPress: () {
                          Clipboard.setData(ClipboardData(text: f['value'] as String));
                          ScaffoldMessenger.of(context)
                              .showSnackBar(const SnackBar(content: Text('Скопировано')));
                        },
                      ),
                  ]),
                ),
              ],
              _sectionTitle('Мои заметки — видите только вы'),
              for (final n in notes)
                Container(
                  margin: const EdgeInsets.only(bottom: 8),
                  decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(14)),
                  child: ListTile(
                    leading: const Icon(Icons.lock_outline, color: VColors.lavender, size: 20),
                    title: Text(n['text'] as String, style: const TextStyle(fontSize: 15, height: 1.35)),
                    trailing: IconButton(
                      tooltip: 'Удалить',
                      icon: const Icon(Icons.delete_outline, color: VColors.muted),
                      onPressed: () async {
                        await ref.read(dioProvider).delete('/person/notes/${n['id']}');
                        ref.invalidate(personProvider);
                      },
                    ),
                  ),
                ),
              OutlinedButton.icon(
                onPressed: () async {
                  final text = await askText(context,
                      title: 'Новая заметка',
                      hint: 'Например: заглядывалась на серёжки в «…»',
                      maxLength: 500,
                      maxLines: 4);
                  if (text == null || text.trim().isEmpty) return;
                  try {
                    await ref.read(dioProvider).post('/person/notes', data: {'text': text.trim()});
                    ref.invalidate(personProvider);
                  } catch (e) {
                    if (context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
                    }
                  }
                },
                icon: const Icon(Icons.add),
                label: const Text('Добавить заметку'),
              ),
            ]);
          },
        ),
      ),
    );
  }
}

/// «О себе»: имя и поля, которые партнёр увидит в своей памятке.
class AboutMeScreen extends ConsumerWidget {
  const AboutMeScreen({super.key});

  Future<void> _save(BuildContext context, WidgetRef ref, String path, Map<String, dynamic> data) async {
    try {
      await ref.read(dioProvider).put(path, data: data);
      ref.invalidate(personProvider);
    } catch (e) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
      }
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final person = ref.watch(personProvider);
    return Scaffold(
      appBar: AppBar(title: Text('О себе', style: display(22))),
      body: SafeArea(
        child: person.when(
          skipLoadingOnRefresh: true,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(child: Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894)))),
          data: (p) {
            final myName = p['my_name'] as String?;
            return ListView(padding: const EdgeInsets.fromLTRB(20, 4, 20, 32), children: [
              const Text('Партнёр увидит это в своей памятке. Заполняйте что хотите — любое поле можно оставить пустым.',
                  style: TextStyle(color: VColors.soft, height: 1.4)),
              _sectionTitle('Как вас называть'),
              _EditRow(
                label: 'Имя',
                value: myName,
                onTap: () async {
                  final v = await askText(context, title: 'Имя', initial: myName ?? '', maxLength: 60);
                  if (v != null && context.mounted) await _save(context, ref, '/person/name', {'name': v});
                },
              ),
              for (final s in _sections(p, 'me')) ...[
                _sectionTitle(s['title'] as String),
                Container(
                  decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(18)),
                  child: Column(children: [
                    for (final f in _fields(s))
                      _EditRow(
                        label: f['label'] as String,
                        value: f['value'] as String?,
                        flat: true,
                        onTap: () async {
                          final v = await askText(context,
                              title: f['label'] as String,
                              initial: (f['value'] as String?) ?? '',
                              hint: f['hint'] as String? ?? '',
                              maxLines: 3);
                          if (v != null && context.mounted) {
                            await _save(context, ref, '/person/me', {'key': f['key'], 'value': v});
                          }
                        },
                      ),
                  ]),
                ),
              ],
            ]);
          },
        ),
      ),
    );
  }
}

class _EditRow extends StatelessWidget {
  const _EditRow({required this.label, required this.value, required this.onTap, this.flat = false});
  final String label;
  final String? value;
  final VoidCallback onTap;
  final bool flat;

  @override
  Widget build(BuildContext context) {
    final tile = ListTile(
      onTap: onTap,
      title: Text(label, style: const TextStyle(color: VColors.muted, fontSize: 13)),
      subtitle: Text(
        value ?? 'Не заполнено',
        style: TextStyle(
          fontSize: 16,
          height: 1.35,
          color: value == null ? const Color(0x66F4EEE6) : VColors.text,
        ),
      ),
      trailing: const Icon(Icons.edit_outlined, size: 18, color: VColors.muted),
    );
    if (flat) return tile;
    return Container(
      decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(18)),
      child: tile,
    );
  }
}
