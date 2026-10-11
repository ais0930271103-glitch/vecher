import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:share_plus/share_plus.dart';

import '../../core/api.dart';
import '../../core/privacy.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
import '../us/us_screen.dart';
import 'invites.dart';
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
            ref.invalidate(invitesProvider);
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
                if (p['status'] == 'pending') {
                  return _Waiting(code: p['invite_code'] as String?, url: p['invite_url'] as String?);
                }
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
  String? _linkCode; // пришли по ссылке-приглашению
  String? _inviter;

  @override
  void initState() {
    super.initState();
    _checkLink();
  }

  Future<void> _checkLink() async {
    final code = await readPendingInvite();
    if (code == null) return;
    try {
      final r = await ref.read(dioProvider).get('/pairs/invite/$code');
      if (!mounted) return;
      setState(() {
        _linkCode = code;
        _inviter = (r.data as Map)['inviter_name'] as String?;
      });
    } catch (e) {
      await clearPendingInvite();
      if (mounted) setState(() => _error = errorText(e));
    }
  }

  Future<void> _acceptLink() async {
    final code = _linkCode!;
    await _run(() async {
      await ref.read(dioProvider).post('/pairs/join', data: {'code': code});
      await clearPendingInvite();
    });
  }

  Future<void> _declineLink() async {
    await clearPendingInvite();
    setState(() {
      _linkCode = null;
      _inviter = null;
    });
  }

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
    if (_linkCode != null) {
      final name = _inviter ?? 'Партнёр';
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const SizedBox(height: 24),
        Center(child: Initial(name: _inviter, size: 88)),
        const SizedBox(height: 18),
        Text('$name приглашает тебя', textAlign: TextAlign.center, style: display(28)),
        const SizedBox(height: 10),
        const Text(
          '«Вечер» — пространство только для вас двоих: вопросы, совпадения желаний, планы и ваши даты.',
          textAlign: TextAlign.center,
          style: TextStyle(color: VColors.soft, fontSize: 16, height: 1.4),
        ),
        const SizedBox(height: 28),
        GoldButton(onPressed: _busy ? null : _acceptLink, child: Text(_busy ? 'Соединяем…' : 'Принять приглашение')),
        TextButton(
          onPressed: _busy ? null : _declineLink,
          child: const Text('Не сейчас', style: TextStyle(color: VColors.muted)),
        ),
        if (_error != null) Text(_error!, style: const TextStyle(color: Color(0xFFF5A894))),
      ]);
    }
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
          const Text('Получи ссылку и отправь её любым мессенджером.',
              style: TextStyle(color: VColors.soft)),
          const SizedBox(height: 14),
          GoldButton(
            onPressed: _busy ? null : () => _run(() => dio.post('/pairs/invite')),
            child: const Text('Получить приглашение'),
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

final _prepProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/pairs/prep');
  return Map<String, dynamic>.from(r.data as Map);
});

final _prepTodayProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/today');
  return Map<String, dynamic>.from(r.data as Map);
});

/// Ждём партнёра — и готовим «Вечер»: карточки, «о себе», вопрос дня, письмо в первое утро.
class _Waiting extends ConsumerStatefulWidget {
  const _Waiting({required this.code, this.url});
  final String? code;
  final String? url;

  @override
  ConsumerState<_Waiting> createState() => _WaitingState();
}

class _WaitingState extends ConsumerState<_Waiting> {
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    // Подготовка обновляется, а как только партнёр примет приглашение — откроется «Сегодня».
    _poll = Timer.periodic(const Duration(seconds: 10), (_) {
      ref.invalidate(_prepProvider);
      ref.invalidate(pairProvider);
    });
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  String get _pretty {
    final code = widget.code;
    return code == null ? '' : '${code.substring(0, 4)}-${code.substring(4)}';
  }

  String get _message => [
        'Присоединяйся ко мне в «Вечере» — это пространство только для нас двоих.',
        if (widget.url != null) widget.url!,
        'Если ссылка не откроется, введи в приложении код $_pretty',
      ].join('\n');

  void _toast(String t) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));

  Future<void> _answer(String? question) async {
    final text = await askText(context,
        title: question ?? 'Вопрос дня', hint: 'Твой ответ', maxLength: 500, maxLines: 4);
    if (text == null || text.trim().isEmpty) return;
    try {
      await ref.read(dioProvider).post('/today/answer', data: {'text': text.trim()});
      ref.invalidate(_prepProvider);
      ref.invalidate(_prepTodayProvider);
      _toast('Готово — партнёр увидит ответ после своего');
    } catch (e) {
      _toast(errorText(e));
    }
  }

  Future<void> _letter() async {
    final text = await askText(context,
        title: 'Письмо в первое утро', hint: 'Что ты хочешь сказать?', maxLength: 2000, maxLines: 6);
    if (text == null || text.trim().isEmpty) return;
    try {
      await ref.read(dioProvider).post('/letters', data: {'text': text.trim()});
      ref.invalidate(_prepProvider);
      _toast('Письмо ждёт — партнёр получит его утром в «Вечере»');
    } catch (e) {
      _toast(errorText(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final prep = ref.watch(_prepProvider).valueOrNull;
    final today = ref.watch(_prepTodayProvider).valueOrNull;
    final question = (today?['question'] as Map?)?['text'] as String?;
    final done = prep?['done'] as int? ?? 0;
    final swiped = prep?['swiped'] as int? ?? 0;
    final about = prep?['about_me'] as int? ?? 0;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Text('Подготовь «Вечер» для вас двоих', style: display(26)),
      const SizedBox(height: 8),
      const Text('Пока партнёр не присоединился, всё, что ты сделаешь, будет ждать его здесь.',
          style: TextStyle(color: VColors.soft, fontSize: 15, height: 1.4)),
      const SizedBox(height: 18),
      _Card(
        highlight: true,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Row(children: [
            const Expanded(
              child: Text('Пригласи партнёра', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w600)),
            ),
            Text(_pretty, style: const TextStyle(letterSpacing: 2, fontWeight: FontWeight.w700, color: VColors.goldText)),
          ]),
          const SizedBox(height: 12),
          GoldButton(
            onPressed: () => Share.share(_message, subject: 'Приглашение в «Вечер»'),
            child: const Row(mainAxisSize: MainAxisSize.min, children: [
              Icon(Icons.ios_share, size: 20),
              SizedBox(width: 8),
              Text('Отправить приглашение'),
            ]),
          ),
          TextButton.icon(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: _message));
              _toast('Скопировано — отправь партнёру');
            },
            icon: const Icon(Icons.copy, size: 18, color: VColors.muted),
            label: const Text('Скопировать', style: TextStyle(color: VColors.muted)),
          ),
        ]),
      ),
      const SizedBox(height: 20),
      Row(children: [
        const Expanded(child: SectionLabel('Пока ждём', color: VColors.label)),
        Text('$done из 4', style: const TextStyle(color: VColors.goldText, fontSize: 13)),
      ]),
      const SizedBox(height: 8),
      ClipRRect(
        borderRadius: BorderRadius.circular(4),
        child: LinearProgressIndicator(
          value: done / 4, minHeight: 6, backgroundColor: const Color(0x1FFFFFFF), color: VColors.gold),
      ),
      const SizedBox(height: 14),
      _PrepStep(
        icon: Icons.style_outlined,
        title: 'Пролистай первые карточки',
        subtitle: 'Пролистано: $swiped из 5 · совпадения откроются, как только партнёр тоже полистает',
        done: swiped >= 5,
        onTap: () => context.go('/wishes'),
      ),
      _PrepStep(
        icon: Icons.person_outline,
        title: 'Расскажи о себе',
        subtitle: 'Заполнено: $about из 3 · памятка для партнёра будет готова с первого дня',
        done: about >= 3,
        onTap: () async {
          await context.push('/us/me');
          ref.invalidate(_prepProvider);
        },
      ),
      _PrepStep(
        icon: Icons.chat_bubble_outline,
        title: 'Ответь на вопрос дня',
        subtitle: prep?['answered'] == true ? 'Ответ ждёт партнёра' : (question ?? 'Партнёр увидит ответ после своего'),
        done: prep?['answered'] == true,
        onTap: prep?['answered'] == true ? null : () => _answer(question),
      ),
      _PrepStep(
        icon: Icons.mail_outline,
        title: 'Письмо в первое утро',
        subtitle: prep?['letter'] == true
            ? 'Письмо ждёт партнёра'
            : 'Партнёр получит его утром, когда будет в «Вечере»',
        done: prep?['letter'] == true,
        onTap: prep?['letter'] == true ? null : _letter,
      ),
    ]);
  }
}

class _PrepStep extends StatelessWidget {
  const _PrepStep({required this.icon, required this.title, required this.subtitle, required this.done, this.onTap});
  final IconData icon;
  final String title, subtitle;
  final bool done;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Material(
        color: VColors.card,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(18),
          side: BorderSide(color: done ? const Color(0x4DF3C98B) : const Color(0x14FFFFFF)),
        ),
        child: InkWell(
          borderRadius: BorderRadius.circular(18),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Row(children: [
              Container(
                width: 42,
                height: 42,
                decoration: BoxDecoration(
                  color: done ? const Color(0xFF3A3020) : VColors.card2,
                  borderRadius: BorderRadius.circular(13),
                ),
                child: Icon(done ? Icons.check : icon, color: done ? VColors.gold : VColors.lavender),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(title, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
                  const SizedBox(height: 3),
                  Text(subtitle, style: const TextStyle(color: VColors.muted, fontSize: 13, height: 1.3)),
                ]),
              ),
              if (onTap != null) const Icon(Icons.chevron_right, color: VColors.muted),
            ]),
          ),
        ),
      ),
    );
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
