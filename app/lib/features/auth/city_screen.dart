import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

/// Шаг регистрации «Где ты живёшь?» — для подборок ресторанов, концертов и событий рядом.
class CityScreen extends ConsumerWidget {
  const CityScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Scaffold(
      body: SafeArea(
        child: CityPicker(
          title: 'Где ты живёшь?',
          subtitle: 'Подберём рестораны, концерты и события рядом. Поменять можно в любой момент в настройках.',
          onPicked: (name) async {
            try {
              await ref.read(dioProvider).put('/me/city', data: {'home_city': name});
              if (context.mounted) context.go('/today');
            } catch (e) {
              if (context.mounted) {
                ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
              }
            }
          },
        ),
      ),
    );
  }
}

/// Выбор города отдельной страницей (из настроек). Возвращает название или null.
Future<String?> pickCity(BuildContext context, {required String title, String? subtitle}) {
  return Navigator.of(context).push<String>(MaterialPageRoute(
    builder: (ctx) => Scaffold(
      appBar: AppBar(),
      body: SafeArea(
        child: CityPicker(title: title, subtitle: subtitle, onPicked: (name) => Navigator.of(ctx).pop(name)),
      ),
    ),
  ));
}

/// Поиск по списку городов; своего нет — можно вписать.
class CityPicker extends ConsumerStatefulWidget {
  const CityPicker({super.key, required this.title, this.subtitle, required this.onPicked});
  final String title;
  final String? subtitle;
  final FutureOr<void> Function(String name) onPicked;

  @override
  ConsumerState<CityPicker> createState() => _CityPickerState();
}

class _CityPickerState extends ConsumerState<CityPicker> {
  final _q = TextEditingController();
  List<Map<String, dynamic>> _items = const [];
  Timer? _debounce;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _load('');
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _q.dispose();
    super.dispose();
  }

  Future<void> _load(String q) async {
    try {
      final r = await ref.read(dioProvider).get('/cities', queryParameters: {'q': q});
      if (mounted) {
        setState(() => _items = (r.data as List).map((e) => Map<String, dynamic>.from(e as Map)).toList());
      }
    } catch (_) {}
  }

  void _onChanged(String v) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 250), () => _load(v));
    setState(() {});
  }

  Future<void> _pick(String name) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await widget.onPicked(name);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final typed = _q.text.trim();
    final exact = _items.any((c) => (c['name'] as String).toLowerCase() == typed.toLowerCase());
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 24, 24, 0),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text(widget.title, style: display(30)),
        if (widget.subtitle != null) ...[
          const SizedBox(height: 8),
          Text(widget.subtitle!, style: const TextStyle(color: VColors.soft, fontSize: 15, height: 1.4)),
        ],
        const SizedBox(height: 18),
        TextField(
          controller: _q,
          onChanged: _onChanged,
          textCapitalization: TextCapitalization.words,
          decoration: const InputDecoration(prefixIcon: Icon(Icons.search), hintText: 'Начни вводить название'),
        ),
        const SizedBox(height: 8),
        Expanded(
          child: ListView(children: [
            for (final c in _items)
              ListTile(
                contentPadding: const EdgeInsets.symmetric(horizontal: 4),
                leading: const Icon(Icons.location_on_outlined, color: VColors.lavender),
                title: Text(c['name'] as String),
                subtitle: c['region'] != null && c['region'] != c['name']
                    ? Text(c['region'] as String, style: const TextStyle(color: VColors.muted, fontSize: 13))
                    : null,
                onTap: _busy ? null : () => _pick(c['name'] as String),
              ),
            if (typed.length >= 2 && !exact)
              ListTile(
                contentPadding: const EdgeInsets.symmetric(horizontal: 4),
                leading: const Icon(Icons.add_location_alt_outlined, color: VColors.gold),
                title: Text('«$typed» — мой город', style: const TextStyle(color: VColors.goldText)),
                subtitle: const Text('Если его нет в списке', style: TextStyle(color: VColors.muted, fontSize: 13)),
                onTap: _busy ? null : () => _pick(typed),
              ),
            const SizedBox(height: 24),
          ]),
        ),
      ]),
    );
  }
}
