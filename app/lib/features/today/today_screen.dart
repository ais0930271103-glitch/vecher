import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/theme.dart';

/// Состояние пары с сервера: null — пары нет, иначе pending (ждём партнёра) или active.
final pairProvider = FutureProvider.autoDispose<Map<String, dynamic>?>((ref) async {
  final r = await ref.read(dioProvider).get('/pairs/me');
  return r.data == null ? null : Map<String, dynamic>.from(r.data as Map);
});

class TodayScreen extends ConsumerWidget {
  const TodayScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final pair = ref.watch(pairProvider);
    return Scaffold(
      appBar: AppBar(
        title: Text('Вечер', style: display(24)),
        actions: [
          IconButton(
            tooltip: 'Выйти',
            icon: const Icon(Icons.logout),
            onPressed: () async {
              await clearToken();
              if (context.mounted) context.go('/welcome');
            },
          ),
        ],
      ),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () => ref.refresh(pairProvider.future),
          child: ListView(padding: const EdgeInsets.all(20), children: [
            pair.when(
              loading: () => const Padding(
                padding: EdgeInsets.only(top: 80),
                child: Center(child: CircularProgressIndicator()),
              ),
              error: (e, _) => _Card(
                child: Text(errorText(e), style: const TextStyle(color: Color(0xFFF5A894))),
              ),
              data: (p) {
                if (p == null) return const _NoPair();
                if (p['status'] == 'pending') return _Waiting(code: p['invite_code'] as String?);
                return const _Paired();
              },
            ),
          ]),
        ),
      ),
    );
  }
}

class _Card extends StatelessWidget {
  const _Card({required this.child, this.highlight = false});
  final Widget child;
  final bool highlight;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(24),
        color: highlight ? null : VColors.card,
        gradient: highlight
            ? const LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [Color(0xFF2A2458), Color(0xFF1A1C42)],
              )
            : null,
        border: Border.all(color: highlight ? const Color(0x38F3C98B) : const Color(0x14FFFFFF)),
      ),
      child: child,
    );
  }
}

class _NoPair extends ConsumerStatefulWidget {
  const _NoPair();

  @override
  ConsumerState<_NoPair> createState() => _NoPairState();
}

class _NoPairState extends ConsumerState<_NoPair> {
  final _code = TextEditingController();
  bool _busy = false;
  String? _error;

  Future<void> _run(Future<void> Function() action) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await action();
      ref.invalidate(pairProvider);
    } catch (e) {
      setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _code.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final dio = ref.read(dioProvider);
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Text('Добро пожаловать', style: display(28)),
      const SizedBox(height: 8),
      const Text('«Вечер» раскрывается вдвоём. Пригласите партнёра или введите его код.',
          style: TextStyle(color: VColors.soft, fontSize: 16)),
      const SizedBox(height: 20),
      _Card(
        highlight: true,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          const Text('Пригласить партнёра',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
          const SizedBox(height: 6),
          const Text('Получите код и отправьте его любым мессенджером.',
              style: TextStyle(color: VColors.soft)),
          const SizedBox(height: 14),
          FilledButton(
            onPressed: _busy ? null : () => _run(() => dio.post('/pairs/invite')),
            child: const Text('Получить код'),
          ),
        ]),
      ),
      const SizedBox(height: 14),
      _Card(
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          const Text('У меня есть код', style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
          const SizedBox(height: 12),
          TextField(
            controller: _code,
            textCapitalization: TextCapitalization.characters,
            decoration: const InputDecoration(labelText: 'Код из 8 символов'),
          ),
          const SizedBox(height: 12),
          OutlinedButton(
            style: OutlinedButton.styleFrom(
              minimumSize: const Size.fromHeight(52),
              shape: const StadiumBorder(),
              side: const BorderSide(color: Color(0x73F3C98B)),
              foregroundColor: VColors.goldText,
            ),
            onPressed: _busy
                ? null
                : () => _run(() => dio.post('/pairs/join', data: {'code': _code.text})),
            child: const Text('Присоединиться'),
          ),
        ]),
      ),
      if (_error != null) ...[
        const SizedBox(height: 12),
        Text(_error!, style: const TextStyle(color: Color(0xFFF5A894))),
      ],
    ]);
  }
}

class _Waiting extends StatelessWidget {
  const _Waiting({required this.code});
  final String? code;

  @override
  Widget build(BuildContext context) {
    final pretty = code == null ? '' : '${code!.substring(0, 4)}-${code!.substring(4)}';
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Text('Ждём партнёра', style: display(28)),
      const SizedBox(height: 8),
      const Text('Отправьте код. Как только партнёр его введёт, «Вечер» откроется для вас двоих.',
          style: TextStyle(color: VColors.soft, fontSize: 16)),
      const SizedBox(height: 20),
      _Card(
        highlight: true,
        child: Column(children: [
          Text(pretty,
              style: const TextStyle(fontSize: 34, letterSpacing: 4, fontWeight: FontWeight.w700,
                  color: VColors.goldText)),
          const SizedBox(height: 14),
          FilledButton.icon(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: 'Присоединяйся ко мне в «Вечере»: код $pretty'));
              ScaffoldMessenger.of(context)
                  .showSnackBar(const SnackBar(content: Text('Скопировано — отправьте партнёру')));
            },
            icon: const Icon(Icons.copy),
            label: const Text('Скопировать приглашение'),
          ),
        ]),
      ),
      const SizedBox(height: 12),
      const Text('Потяните вниз, чтобы обновить.',
          textAlign: TextAlign.center, style: TextStyle(color: VColors.muted, fontSize: 13)),
    ]);
  }
}

class _Paired extends StatelessWidget {
  const _Paired();

  @override
  Widget build(BuildContext context) {
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Text('Добрый вечер', style: display(28)),
      const SizedBox(height: 16),
      const _Card(
        highlight: true,
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('ВЫ В ПАРЕ', style: TextStyle(letterSpacing: 1.5, fontSize: 12, color: Color(0xFFE2B57C))),
          SizedBox(height: 8),
          Text('Скоро здесь появятся вопрос дня, радар и огонёк.',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
        ]),
      ),
    ]);
  }
}
