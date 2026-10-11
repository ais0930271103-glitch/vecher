import 'package:flutter/material.dart';

import 'theme.dart';

/// Общие элементы оформления из утверждённых макетов.

/// Главная кнопка: золотой градиент, высота 56, скругление-пилюля.
class GoldButton extends StatelessWidget {
  const GoldButton({super.key, required this.onPressed, required this.child, this.height = 56, this.glow = false});
  final VoidCallback? onPressed;
  final Widget child;
  final double height;
  final bool glow;

  @override
  Widget build(BuildContext context) {
    final enabled = onPressed != null;
    return Opacity(
      opacity: enabled ? 1 : 0.45,
      child: Container(
        height: height,
        decoration: BoxDecoration(
          gradient: VColors.goldGradient,
          borderRadius: BorderRadius.circular(height / 2),
          boxShadow: glow ? const [BoxShadow(color: Color(0x59F3B46E), blurRadius: 30)] : null,
        ),
        child: Material(
          type: MaterialType.transparency,
          child: InkWell(
            borderRadius: BorderRadius.circular(height / 2),
            onTap: onPressed,
            child: Center(
              child: DefaultTextStyle.merge(
                style: TextStyle(
                  color: VColors.ink,
                  fontSize: height >= 52 ? 17 : 16,
                  fontWeight: FontWeight.w700,
                ),
                child: IconTheme.merge(data: const IconThemeData(color: VColors.ink), child: child),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// Круглая кнопка «назад» 44×44 на фоне карточки.
class RoundBack extends StatelessWidget {
  const RoundBack({super.key, this.onTap});
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: VColors.card,
      shape: const CircleBorder(),
      child: InkWell(
        customBorder: const CircleBorder(),
        onTap: onTap ?? () => Navigator.of(context).maybePop(),
        child: const SizedBox(
          width: 44,
          height: 44,
          child: Icon(Icons.chevron_left, color: VColors.text, semanticLabel: 'Назад'),
        ),
      ),
    );
  }
}

/// Верхняя строка экрана: круглая «назад», заголовок по центру, место справа.
class TopBar extends StatelessWidget {
  const TopBar({super.key, required this.title, this.trailing, this.below});
  final String title;
  final Widget? trailing;
  final Widget? below;

  @override
  Widget build(BuildContext context) {
    return Row(children: [
      const RoundBack(),
      Expanded(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Text(title,
              textAlign: TextAlign.center,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600)),
          if (below != null) ...[const SizedBox(height: 6), below!],
        ]),
      ),
      SizedBox(width: 44, child: trailing == null ? null : Align(alignment: Alignment.centerRight, child: trailing)),
    ]);
  }
}

/// Ночной фон с мягким свечением сверху (вход, совпадение, напоминание).
class NightBackground extends StatelessWidget {
  const NightBackground({super.key, required this.child, this.center = const Alignment(0, -0.36), this.strong = true});
  final Widget child;
  final Alignment center;
  final bool strong;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        gradient: RadialGradient(
          center: center,
          radius: 1.15,
          colors: strong
              ? const [Color(0xFF262A5C), Color(0xFF12143A), VColors.bgDeep]
              : const [Color(0xFF33295F), VColors.bg, VColors.bg],
          stops: const [0, 0.45, 1],
        ),
      ),
      child: child,
    );
  }
}

/// Эмблема «Вечер» (полумесяц и пара) с тёплым свечением.
class Emblem extends StatelessWidget {
  const Emblem({super.key, this.size = 250});
  final double size;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        boxShadow: [BoxShadow(color: const Color(0x40F3B46E), blurRadius: size * 0.3, spreadRadius: -size * 0.12)],
      ),
      child: Image.asset('assets/emblem.png', fit: BoxFit.contain, semanticLabel: 'Эмблема Вечер'),
    );
  }
}

/// Кружок с первой буквой имени: золотой — я, лавандовый — партнёр.
class Initial extends StatelessWidget {
  const Initial({super.key, required this.name, this.size = 44, this.me = false});
  final String? name;
  final double size;
  final bool me;

  @override
  Widget build(BuildContext context) {
    final empty = name == null || name!.trim().isEmpty;
    final letter = empty ? (me ? 'Я' : '') : name!.trim()[0].toUpperCase();
    return Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: me ? const Color(0xFF2E2A63) : VColors.avatar,
        border: Border.all(color: me ? VColors.gold : VColors.lavender, width: 1.5),
      ),
      child: letter.isEmpty
          // Имя партнёра ещё не указано — контур человека вместо буквы (без эмодзи).
          ? Icon(Icons.person_outline, size: size * 0.5, color: VColors.lavenderText)
          : Text(letter, style: display(size * 0.4, color: me ? VColors.goldText : VColors.lavenderText)),
    );
  }
}

/// Подпись раздела КАПСОМ.
class SectionLabel extends StatelessWidget {
  const SectionLabel(this.text, {super.key, this.color = VColors.muted});
  final String text;
  final Color color;

  @override
  Widget build(BuildContext context) => Text(text.toUpperCase(),
      style: TextStyle(fontSize: 12, letterSpacing: 1.5, color: color));
}

/// Переключатель-вкладки в капсуле (как «Аня рассказала / Мои заметки»).
class PillTabs extends StatelessWidget {
  const PillTabs({super.key, required this.labels, required this.index, required this.onChanged});
  final List<String> labels;
  final int index;
  final ValueChanged<int> onChanged;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(4),
      decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(22)),
      child: Row(children: [
        for (var i = 0; i < labels.length; i++)
          Expanded(
            child: Material(
              color: i == index ? VColors.segment : Colors.transparent,
              borderRadius: BorderRadius.circular(18),
              child: InkWell(
                borderRadius: BorderRadius.circular(18),
                onTap: () => onChanged(i),
                child: SizedBox(
                  height: 38,
                  child: Center(
                    child: Text(labels[i],
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: 14,
                          fontWeight: i == index ? FontWeight.w700 : FontWeight.w400,
                          color: i == index ? VColors.goldText : VColors.link,
                        )),
                  ),
                ),
              ),
            ),
          ),
      ]),
    );
  }
}

/// Карточка-плашка в цвет фона карточек.
BoxDecoration cardBox({double radius = 20, bool gold = false}) => BoxDecoration(
      color: VColors.card,
      borderRadius: BorderRadius.circular(radius),
      border: gold ? Border.all(color: const Color(0x4DF3C98B)) : null,
    );

/// Карточка с фирменным градиентом и золотой рамкой.
BoxDecoration glowBox({double radius = 24}) => BoxDecoration(
      gradient: VColors.cardGradient,
      borderRadius: BorderRadius.circular(radius),
      border: Border.all(color: const Color(0x38F3C98B)),
    );
