import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/privacy.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

/// Версия текста ниже. Должна совпадать с backend/app/consent.py.
const consentVersion = '2026-10-11';

/// Согласие на обработку персональных данных (152-ФЗ): общее и отдельное — на сведения
/// об интимной жизни и здоровье (ст. 10). Без обоих приложение не работает.
class ConsentScreen extends ConsumerStatefulWidget {
  const ConsentScreen({super.key});

  @override
  ConsumerState<ConsentScreen> createState() => _ConsentScreenState();
}

class _ConsentScreenState extends ConsumerState<ConsentScreen> {
  bool _personal = false;
  bool _special = false;
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
        'personal': _personal,
        'special': _special,
      });
      if (!mounted) return;
      context.go((r.data as Map)['display_name'] == null ? '/name' : '/today');
    } catch (e) {
      setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _decline() async {
    await signOut(ref.read(dioProvider));
    if (mounted) context.go('/welcome');
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Column(children: [
          Expanded(
            child: ListView(padding: const EdgeInsets.fromLTRB(24, 32, 24, 12), children: [
              Text('Твои данные', style: display(32)),
              const SizedBox(height: 8),
              const Text(
                '«Вечер» — про близость, поэтому здесь будет личное. Расскажем честно, что храним и зачем.',
                style: TextStyle(color: VColors.soft, fontSize: 16),
              ),
              const SizedBox(height: 20),
              Container(
                padding: const EdgeInsets.all(18),
                decoration: cardBox(),
                child: const _ConsentText(),
              ),
              const SizedBox(height: 16),
              _Check(
                value: _personal,
                onChanged: (v) => setState(() => _personal = v),
                text: 'Соглашаюсь на обработку моих персональных данных для работы «Вечера»',
              ),
              _Check(
                value: _special,
                onChanged: (v) => setState(() => _special = v),
                text: 'Отдельно соглашаюсь на обработку сведений об интимной жизни и здоровье '
                    '(ответы, выборы в карточках желаний, аллергии и ограничения в анкете)',
              ),
              if (_error != null) ...[
                const SizedBox(height: 8),
                Text(_error!, style: const TextStyle(color: VColors.coral)),
              ],
            ]),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(24, 4, 24, 16),
            child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              GoldButton(
                onPressed: _personal && _special && !_busy ? _accept : null,
                child: Text(_busy ? 'Сохраняем…' : 'Продолжить'),
              ),
              TextButton(
                onPressed: _busy ? null : _decline,
                child: const Text('Не соглашаюсь — выйти', style: TextStyle(color: VColors.muted)),
              ),
            ]),
          ),
        ]),
      ),
    );
  }
}

class _Check extends StatelessWidget {
  const _Check({required this.value, required this.onChanged, required this.text});
  final bool value;
  final ValueChanged<bool> onChanged;
  final String text;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      borderRadius: BorderRadius.circular(12),
      onTap: () => onChanged(!value),
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 8),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Checkbox(
            value: value,
            onChanged: (v) => onChanged(v ?? false),
            activeColor: VColors.gold,
            checkColor: VColors.ink,
          ),
          const SizedBox(width: 4),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Text(text, style: const TextStyle(fontSize: 15, height: 1.35)),
            ),
          ),
        ]),
      ),
    );
  }
}

/// Текст согласия. Черновик — перед публикацией показать юристу.
class _ConsentText extends StatelessWidget {
  const _ConsentText();

  static const _h = TextStyle(fontSize: 15, fontWeight: FontWeight.w600, color: VColors.goldText);
  static const _p = TextStyle(fontSize: 14, height: 1.45, color: VColors.text2);

  @override
  Widget build(BuildContext context) {
    Widget h(String t) => Padding(padding: const EdgeInsets.only(top: 12, bottom: 4), child: Text(t, style: _h));
    Widget p(String t) => Text(t, style: _p);
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      h('Кто обрабатывает'),
      p('ИП Дордуков Артем Юрьевич, ИНН 290128994870. Вопросы и отзыв согласия — privacy@вечер.online.'),
      h('Что храним'),
      p('Почту и имя; ответы на вопросы дня и сигналы партнёру; выборы в карточках желаний; '
          'анкету «о себе» (размеры, что любишь, аллергии и ограничения); заметки, даты, напоминания, '
          'письма и сюрпризы; технический идентификатор телефона для уведомлений.'),
      h('Особая категория'),
      p('Выборы в карточках желаний и часть ответов — это сведения об интимной жизни, аллергии и '
          'ограничения — сведения о здоровье. На них нужно отдельное согласие.'),
      h('Зачем'),
      p('Только чтобы «Вечер» работал для вашей пары: показывать совпадения, вопросы, планы, '
          'памятку о партнёре и присылать уведомления. Не продаём, не показываем рекламодателям.'),
      h('Кто видит'),
      p('Твой выбор в карточке партнёр увидит, только если он совпал с его выбором. Тихие желания и '
          'личные заметки видишь только ты. Анкету «о себе» видит партнёр.'),
      h('Где хранится'),
      p('На серверах в России. Уведомления доставляет сервис Google Firebase: туда уходит только '
          'идентификатор телефона и короткий текст без подробностей. В настройках можно включить '
          'уведомления без текста или выключить их совсем.'),
      h('Сколько и как отозвать'),
      p('Пока у тебя есть аккаунт. Отозвать согласие — значит удалить аккаунт: «Пара» → «Настройки» → '
          '«Удалить аккаунт» или письмом на privacy@вечер.online. Данные удаляются сразу и полностью.'),
    ]);
  }
}
