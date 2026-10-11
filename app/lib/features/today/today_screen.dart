import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/privacy.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
import '../us/us_screen.dart';
import 'paired_today.dart';

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
    final paired = pair.valueOrNull?['status'] == 'active';
    return Scaffold(
      // В паре — без шапки, как в макете (приветствие и дата сверху; «Выйти» — в разделе «Пара»).
      appBar: paired ? null : AppBar(
        title: Text('Вечер', style: display(24)),
        actions: [
          IconButton(
            tooltip: 'Выйти',
            icon: const Icon(Icons.logout),
            onPressed: () async {
              await signOut(ref.read(dioProvider));
              if (context.mounted) context.go('/welcome');
            },
          ),
        ],
      ),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () async {
            ref.invalidate(todayProvider);
            ref.invalidate(upcomingProvider);
            ref.invalidate(remindersProvider);
            ref.invalidate(datesProvider);
            await ref.refresh(pairProvider.future);
          },
          child: ListView(padding: EdgeInsets.fromLTRB(20, paired ? 24 : 20, 20, 20), children: [
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
                final today = PairedToday(partnerName: p['partner_name'] as String?);
                if (p['paused'] != true) return today;
                return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  _PauseBanner(byMe: p['paused_by_me'] == true, partnerName: p['partner_name'] as String?),
                  const SizedBox(height: 16),
                  today,
                ]);
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
                colors: [Color(0xFF2A2438), Color(0xFF1B1A2A)],
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
      const Text('«Вечер» раскрывается вдвоём. Пригласи партнёра или введи его код.',
          style: TextStyle(color: VColors.soft, fontSize: 16)),
      const SizedBox(height: 20),
      _Card(
        highlight: true,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          const Text('Пригласить партнёра',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600)),
          const SizedBox(height: 6),
          const Text('Получи код и отправь его любым мессенджером.',
              style: TextStyle(color: VColors.soft)),
          const SizedBox(height: 14),
          GoldButton(
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
      const Text('Отправь код. Как только партнёр его введёт, «Вечер» откроется для вас двоих.',
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
                  .showSnackBar(const SnackBar(content: Text('Скопировано — отправь партнёру')));
            },
            icon: const Icon(Icons.copy),
            label: const Text('Скопировать приглашение'),
          ),
        ]),
      ),
      const SizedBox(height: 12),
      const Text('Потяни вниз, чтобы обновить.',
          textAlign: TextAlign.center, style: TextStyle(color: VColors.muted, fontSize: 13)),
    ]);
  }
}

/// «Вечер» на паузе: уведомления друг другу не приходят, всё остальное работает.
class _PauseBanner extends ConsumerWidget {
  const _PauseBanner({required this.byMe, this.partnerName});
  final bool byMe;
  final String? partnerName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return Container(
      padding: const EdgeInsets.fromLTRB(18, 14, 10, 14),
      decoration: cardBox(gold: true),
      child: Row(children: [
        const Icon(Icons.pause_circle_outline, color: VColors.gold),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            byMe
                ? 'Ты поставил(а) паузу. Уведомления друг другу не приходят.'
                : '${partnerName ?? 'Партнёр'} взял(а) паузу. Всё сохранится — дайте друг другу время.',
            style: const TextStyle(color: VColors.soft, fontSize: 14),
          ),
        ),
        if (byMe)
          TextButton(
            onPressed: () async {
              try {
                await ref.read(dioProvider).post('/pairs/pause', data: {'on': false});
                ref.invalidate(pairProvider);
              } catch (e) {
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
                }
              }
            },
            child: const Text('Снять', style: TextStyle(color: VColors.gold)),
          ),
      ]),
    );
  }
}
