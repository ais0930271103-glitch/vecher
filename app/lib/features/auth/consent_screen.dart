import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/onboarding.dart';
import '../../core/privacy.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

/// Версия текста согласия. Должна совпадать с backend/app/consent.py.
const consentVersion = '2026-10-11';

/// Кто я и какие согласия уже дал: GET /auth/me.
final meProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/auth/me');
  return Map<String, dynamic>.from(r.data as Map);
});

void _openDetails(BuildContext context) => Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => const ConsentDetailsScreen()),
    );

/// Первый экран после входа: коротко и по-человечески, полный текст — по ссылке.
/// Согласие на интимное и здоровье спрашиваем позже, когда оно понадобится (SpecialConsentCard).
class ConsentScreen extends ConsumerStatefulWidget {
  const ConsentScreen({super.key});

  @override
  ConsumerState<ConsentScreen> createState() => _ConsentScreenState();
}

class _ConsentScreenState extends ConsumerState<ConsentScreen> {
  bool _agree = false;
  bool _busy = false;
  String? _error;

  Future<void> _accept() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final r = await ref.read(dioProvider).post('/auth/consent', data: {
        'version': consentVersion,
        'personal': true,
      });
      if (!mounted) return;
      context.go(nextOnboardingStep(r.data as Map));
    } catch (e) {
      setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _leave() async {
    await signOut(ref.read(dioProvider));
    if (mounted) context.go('/welcome');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: NightBackground(
        strong: false,
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(24, 24, 24, 12),
            child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              const Spacer(),
              const Center(child: Emblem(size: 130)),
              const SizedBox(height: 20),
              Text('Здесь будет личное —\nи оно останется вашим',
                  textAlign: TextAlign.center, style: display(26)),
              const SizedBox(height: 28),
              const _Promise(
                icon: Icons.nightlight_round,
                text: 'Партнёр видит твой выбор, только если он совпал с его выбором',
              ),
              const _Promise(
                icon: Icons.lock_outline,
                text: 'Храним в России. Не продаём и не показываем рекламу',
              ),
              const _Promise(
                icon: Icons.delete_outline,
                text: 'Удалить всё можно одной кнопкой в настройках',
              ),
              const Spacer(),
              InkWell(
                borderRadius: BorderRadius.circular(12),
                onTap: () => setState(() => _agree = !_agree),
                child: Row(children: [
                  Checkbox(
                    value: _agree,
                    onChanged: (v) => setState(() => _agree = v ?? false),
                    activeColor: VColors.gold,
                    checkColor: VColors.ink,
                  ),
                  const Expanded(
                    child: Text('Соглашаюсь на обработку данных', style: TextStyle(fontSize: 15)),
                  ),
                  TextButton(
                    onPressed: () => _openDetails(context),
                    child: const Text('подробнее', style: TextStyle(color: VColors.gold)),
                  ),
                ]),
              ),
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: Text(_error!, style: const TextStyle(color: VColors.coral)),
                ),
              const SizedBox(height: 8),
              GoldButton(
                onPressed: _agree && !_busy ? _accept : null,
                child: Text(_busy ? 'Минутку…' : 'Продолжить'),
              ),
              TextButton(
                onPressed: _busy ? null : _leave,
                child: const Text('Выйти', style: TextStyle(color: VColors.muted)),
              ),
            ]),
          ),
        ),
      ),
    );
  }
}

class _Promise extends StatelessWidget {
  const _Promise({required this.icon, required this.text});
  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Row(children: [
        Container(
          width: 40,
          height: 40,
          decoration: BoxDecoration(color: VColors.card2, borderRadius: BorderRadius.circular(12)),
          child: Icon(icon, color: VColors.gold, size: 20),
        ),
        const SizedBox(width: 14),
        Expanded(child: Text(text, style: const TextStyle(color: VColors.soft, fontSize: 15, height: 1.35))),
      ]),
    );
  }
}

/// Согласие на сведения об интимной жизни и здоровье — в момент, когда оно нужно:
/// перед карточками желаний и перед анкетой «о себе».
class SpecialConsentCard extends ConsumerStatefulWidget {
  const SpecialConsentCard({super.key, required this.title, required this.text});
  final String title;
  final String text;

  @override
  ConsumerState<SpecialConsentCard> createState() => _SpecialConsentCardState();
}

class _SpecialConsentCardState extends ConsumerState<SpecialConsentCard> {
  bool _agree = false;
  bool _busy = false;
  String? _error;

  Future<void> _accept() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(dioProvider).post('/auth/consent', data: {'version': consentVersion, 'special': true});
      ref.invalidate(meProvider);
    } catch (e) {
      if (mounted) setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(20, 20, 20, 16),
      decoration: glowBox(),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const Icon(Icons.favorite_border, color: VColors.gold, size: 28),
        const SizedBox(height: 10),
        Text(widget.title, style: display(22)),
        const SizedBox(height: 8),
        Text(widget.text, style: const TextStyle(color: VColors.soft, fontSize: 15, height: 1.4)),
        const SizedBox(height: 12),
        InkWell(
          borderRadius: BorderRadius.circular(12),
          onTap: () => setState(() => _agree = !_agree),
          child: Row(children: [
            Checkbox(
              value: _agree,
              onChanged: (v) => setState(() => _agree = v ?? false),
              activeColor: VColors.gold,
              checkColor: VColors.ink,
            ),
            const Expanded(child: Text('Согласен(на)', style: TextStyle(fontSize: 15))),
            TextButton(
              onPressed: () => _openDetails(context),
              child: const Text('подробнее', style: TextStyle(color: VColors.gold)),
            ),
          ]),
        ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(_error!, style: const TextStyle(color: VColors.coral)),
          ),
        GoldButton(
          height: 50,
          onPressed: _agree && !_busy ? _accept : null,
          child: const Text('Продолжить'),
        ),
      ]),
    );
  }
}

/// Полный текст согласия. Черновик — перед бетой показать юристу.
class ConsentDetailsScreen extends StatelessWidget {
  const ConsentDetailsScreen({super.key});

  static const _h = TextStyle(fontSize: 16, fontWeight: FontWeight.w600, color: VColors.goldText);
  static const _p = TextStyle(fontSize: 15, height: 1.5, color: VColors.text2);

  @override
  Widget build(BuildContext context) {
    Widget h(String t) => Padding(padding: const EdgeInsets.only(top: 18, bottom: 6), child: Text(t, style: _h));
    Widget p(String t) => Text(t, style: _p);
    return Scaffold(
      appBar: AppBar(title: Text('Согласие на обработку данных', style: display(18))),
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.fromLTRB(22, 0, 22, 32), children: [
          p('Версия от 11.10.2026.'),
          h('Кто обрабатывает'),
          p('ИП Дордуков Артем Юрьевич, ИНН 290128994870. Вопросы и отзыв согласия — privacy@вечер.online.'),
          h('Какие данные'),
          p('Почта и имя; ответы на вопросы дня и сигналы партнёру; заметки, важные даты, напоминания, '
              'письма и сюрпризы; технический идентификатор телефона для уведомлений.'),
          h('Сведения об интимной жизни и здоровье'),
          p('Выборы в карточках желаний, планы к совпадениям и анкета «о себе» (в том числе аллергии и '
              'ограничения) относятся к особой категории данных. На них мы отдельно спрашиваем согласие — '
              'когда ты впервые открываешь карточки или анкету. Без него остальной «Вечер» работает.'),
          h('Зачем'),
          p('Только чтобы «Вечер» работал для вашей пары: показывать вопросы, совпадения, планы, '
              'памятку о партнёре и присылать уведомления. Данные не продаются и не передаются рекламодателям.'),
          h('Кто видит'),
          p('Твой выбор в карточке партнёр видит, только если он совпал с его выбором. Тихие желания и личные '
              'заметки видишь только ты. Анкету «о себе» видит твой партнёр.'),
          h('Где хранится'),
          p('На серверах в России. Уведомления доставляет сервис Google Firebase Cloud Messaging: туда '
              'передаются идентификатор телефона и короткий текст уведомления без подробностей. В настройках '
              'можно включить уведомления без текста или выключить их.'),
          h('Что мы делаем с данными'),
          p('Сбор, запись, хранение, уточнение, использование, передача партнёру по паре в описанных выше '
              'пределах, удаление.'),
          h('Срок и отзыв'),
          p('Пока у тебя есть аккаунт. Отозвать согласие — значит удалить аккаунт: «Пара» → «Настройки» → '
              '«Удалить аккаунт» или письмом на privacy@вечер.online. Данные удаляются сразу и полностью.'),
        ]),
      ),
    );
  }
}
