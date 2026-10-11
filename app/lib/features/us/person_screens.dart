import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'package:go_router/go_router.dart';

import '../auth/consent_screen.dart';
import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
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

/// «Мой человек» — памятка о партнёре по макету: размеры, любит, нельзя, мечты; отдельно — мои заметки.
class PartnerMemoScreen extends ConsumerStatefulWidget {
  const PartnerMemoScreen({super.key});

  @override
  ConsumerState<PartnerMemoScreen> createState() => _PartnerMemoScreenState();
}

class _PartnerMemoScreenState extends ConsumerState<PartnerMemoScreen> {
  int _tab = 0;

  @override
  Widget build(BuildContext context) {
    final person = ref.watch(personProvider);
    final dates = ref.watch(datesProvider).valueOrNull;
    return Scaffold(
      body: SafeArea(
        child: person.when(
          skipLoadingOnRefresh: true,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(child: Text(errorText(e), style: const TextStyle(color: VColors.coral))),
          data: (p) {
            final name = p['partner_name'] as String?;
            final facts = <String, String>{
              for (final s in _sections(p, 'partner'))
                for (final f in _fields(s))
                  if (f['value'] != null) f['key'] as String: f['value'] as String,
            };
            final together = dates?['together_days'] as int?;
            final next = ((dates?['items'] as List?) ?? const [])
                .cast<Map>()
                .where((i) => (i['days_left'] as int) >= 0)
                .firstOrNull;
            final notes = (p['notes'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();

            return ListView(padding: const EdgeInsets.fromLTRB(20, 8, 20, 32), children: [
              Row(children: [
                const RoundBack(),
                const SizedBox(width: 12),
                Initial(name: name, size: 56),
                const SizedBox(width: 14),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('Мой человек', style: display(24)),
                    const SizedBox(height: 2),
                    Text(
                      [name ?? 'Партнёр', if (together != null) 'вместе $together ${plural(together, 'день', 'дня', 'дней')}']
                          .join(' · '),
                      style: const TextStyle(fontSize: 13, color: VColors.muted),
                    ),
                  ]),
                ),
              ]),
              const SizedBox(height: 16),
              PillTabs(
                labels: ['${name ?? 'Партнёр'} о себе', 'Мои заметки'],
                index: _tab,
                onChanged: (i) => setState(() => _tab = i),
              ),
              const SizedBox(height: 14),
              if (_tab == 0) ..._about(context, facts, next) else ..._notes(context, notes),
            ]);
          },
        ),
      ),
    );
  }

  List<Widget> _about(BuildContext context, Map<String, String> f, Map? next) {
    final likes = [for (final k in const ['flowers', 'food', 'sweets', 'drinks', 'color', 'music', 'scent', 'place']) f[k]]
        .whereType<String>()
        .toList();
    final mood = f['mood'];
    final never = [f['allergies'], f['no_gifts']].whereType<String>().toList();
    final dreams = <(String, String)>[
      if (f['dream_gift'] != null) ('Хочет в подарок', f['dream_gift']!),
      if (f['dream_trip'] != null) ('Мечтает съездить', f['dream_trip']!),
      if (f['dream_try'] != null) ('Хочет попробовать', f['dream_try']!),
    ];
    final hasSizes = ['clothes_top', 'clothes_bottom', 'shoes', 'ring'].any(f.containsKey);

    return [
      if (next != null) ...[
        Material(
          color: Colors.transparent,
          child: InkWell(
            borderRadius: BorderRadius.circular(18),
            onTap: () => context.push('/us/dates'),
            child: Ink(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
              decoration: glowBox(radius: 18),
              child: Row(children: [
                const Icon(Icons.calendar_month_outlined, color: VColors.gold),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('${next['title']} · ${formatDay(DateTime.parse(next['next'] as String))}',
                        style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
                    const SizedBox(height: 2),
                    Text(
                      [
                        _cap(daysLeftText(next['days_left'] as int)),
                        if (next['years'] != null)
                          '${next['years']} ${plural(next['years'] as int, 'год', 'года', 'лет')}',
                      ].join(' · '),
                      style: const TextStyle(fontSize: 13, color: VColors.label),
                    ),
                  ]),
                ),
                const Icon(Icons.chevron_right, color: VColors.muted),
              ]),
            ),
          ),
        ),
        const SizedBox(height: 14),
      ],
      if (f.isEmpty)
        Container(
          padding: const EdgeInsets.all(18),
          decoration: cardBox(radius: 18),
          child: const Text(
            'Пока пусто. Попроси партнёра заполнить «О себе» в разделе «Пара» — '
            'размеры, любимое, мечты. Это займёт пару минут.',
            style: TextStyle(color: VColors.soft, height: 1.4),
          ),
        ),
      if (hasSizes) ...[
        _Box(
          label: 'Размеры',
          child: Row(children: [
            for (final (k, l) in const [('clothes_top', 'Одежда'), ('clothes_bottom', 'Низ'), ('shoes', 'Обувь'), ('ring', 'Кольцо')])
              Expanded(
                child: GestureDetector(
                  onLongPress: f[k] == null ? null : () => _copy(context, f[k]!),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(f[k] ?? '—',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: 18,
                          fontWeight: FontWeight.w700,
                          color: f[k] == null ? VColors.navInactive : VColors.text,
                        )),
                    const SizedBox(height: 2),
                    Text(l, style: const TextStyle(fontSize: 12, color: VColors.muted)),
                  ]),
                ),
              ),
          ]),
        ),
        const SizedBox(height: 12),
      ],
      if (likes.isNotEmpty || mood != null) ...[
        _Box(
          label: 'Любит',
          child: Wrap(spacing: 8, runSpacing: 8, children: [
            for (final l in likes) _Chip(l, onLongPress: () => _copy(context, l)),
            if (mood != null) _Chip('Поднимает настроение: $mood', lavender: true),
          ]),
        ),
        const SizedBox(height: 12),
      ],
      if (never.isNotEmpty) ...[
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
          decoration: BoxDecoration(
            color: const Color(0xFF1E1630),
            borderRadius: BorderRadius.circular(18),
            border: Border.all(color: const Color(0x66F08C78)),
          ),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Row(children: [
              Icon(Icons.warning_amber_rounded, size: 16, color: VColors.coral),
              SizedBox(width: 8),
              Text('НЕЛЬЗЯ', style: TextStyle(fontSize: 12, letterSpacing: 1.5, color: VColors.coral)),
            ]),
            const SizedBox(height: 8),
            Text(never.join(' · '), style: const TextStyle(fontSize: 14, height: 1.5, color: Color(0xFFF1E3E0))),
          ]),
        ),
        const SizedBox(height: 12),
      ],
      if (dreams.isNotEmpty)
        _Box(
          label: 'Мечты',
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (final (l, v) in dreams)
              Padding(
                padding: const EdgeInsets.only(bottom: 6),
                child: GestureDetector(
                  onLongPress: () => _copy(context, v),
                  child: Text.rich(TextSpan(children: [
                    TextSpan(text: '$l: ', style: const TextStyle(color: VColors.muted)),
                    TextSpan(text: v),
                  ]), style: const TextStyle(fontSize: 14, height: 1.5, color: VColors.soft)),
                ),
              ),
          ]),
        ),
    ];
  }

  List<Widget> _notes(BuildContext context, List<Map<String, dynamic>> notes) {
    return [
      _Box(
        label: 'Заметил(а) и запомнил(а) — видишь только ты',
        trailing: Material(
          color: const Color(0xFF2A2938),
          borderRadius: BorderRadius.circular(16),
          child: InkWell(
            borderRadius: BorderRadius.circular(16),
            onTap: () => _addNote(context),
            child: const SizedBox(
              width: 44,
              height: 32,
              child: Icon(Icons.add, size: 18, color: VColors.gold, semanticLabel: 'Добавить заметку'),
            ),
          ),
        ),
        child: notes.isEmpty
            ? const Text('Запиши, что партнёр упомянул вскользь: «хочу в Казань», серьги в витрине…',
                style: TextStyle(fontSize: 14, height: 1.5, color: VColors.text2))
            : Column(children: [
                for (final n in notes)
                  Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Expanded(
                      child: Padding(
                        padding: const EdgeInsets.symmetric(vertical: 6),
                        child: Text('«${n['text']}» · ${formatDay(DateTime.parse(n['created_at'] as String).toLocal())}',
                            style: const TextStyle(fontSize: 14, height: 1.5, color: VColors.soft)),
                      ),
                    ),
                    IconButton(
                      tooltip: 'Удалить',
                      icon: const Icon(Icons.close, size: 18, color: VColors.muted),
                      onPressed: () async {
                        await ref.read(dioProvider).delete('/person/notes/${n['id']}');
                        ref.invalidate(personProvider);
                      },
                    ),
                  ]),
              ]),
      ),
    ];
  }

  Future<void> _addNote(BuildContext context) async {
    final text = await askText(context,
        title: 'Новая заметка', hint: 'Например: заглядывалась на серёжки в «…»', maxLength: 500, maxLines: 4);
    if (text == null || text.trim().isEmpty) return;
    try {
      await ref.read(dioProvider).post('/person/notes', data: {'text': text.trim()});
      ref.invalidate(personProvider);
    } catch (e) {
      if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    }
  }

  void _copy(BuildContext context, String v) {
    Clipboard.setData(ClipboardData(text: v));
    ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Скопировано')));
  }

  static String _cap(String s) => s.isEmpty ? s : s[0].toUpperCase() + s.substring(1);
}

class _Box extends StatelessWidget {
  const _Box({required this.label, required this.child, this.trailing});
  final String label;
  final Widget child;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
      decoration: cardBox(radius: 18),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(child: SectionLabel(label)),
          if (trailing != null) trailing!,
        ]),
        const SizedBox(height: 10),
        child,
      ]),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip(this.text, {this.lavender = false, this.onLongPress});
  final String text;
  final bool lavender;
  final VoidCallback? onLongPress;

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onLongPress: onLongPress,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(15),
          color: lavender ? const Color(0x24B9A3F0) : const Color(0x1FF3C98B),
        ),
        child: Text(text, style: TextStyle(fontSize: 13, color: lavender ? VColors.lavenderText : VColors.goldText)),
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
    final needConsent = ref.watch(meProvider).valueOrNull?['special_ok'] == false;
    return Scaffold(
      appBar: AppBar(title: Text('О себе', style: display(22))),
      body: SafeArea(
        child: needConsent
            ? ListView(padding: const EdgeInsets.all(20), children: const [
                SpecialConsentCard(
                  title: 'Памятка для партнёра',
                  text: 'Размеры, любимое, аллергии и ограничения — это личное, часть сведений о здоровье. '
                      'Увидит только твой партнёр. Чтобы сохранять их, нужно твоё согласие.',
                ),
              ])
            : person.when(
          skipLoadingOnRefresh: true,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (e, _) => Center(child: Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894)))),
          data: (p) {
            final myName = p['my_name'] as String?;
            return ListView(padding: const EdgeInsets.fromLTRB(20, 4, 20, 32), children: [
              const Text('Партнёр увидит это в своей памятке. Заполняй что хочешь — любое поле можно оставить пустым.',
                  style: TextStyle(color: VColors.soft, height: 1.4)),
              _sectionTitle('Как тебя называть'),
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
