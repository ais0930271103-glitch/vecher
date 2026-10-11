import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

/// Свайпер колоды: вправо — «да», влево — «нет», вверх — «может быть».
class SwipeScreen extends ConsumerStatefulWidget {
  const SwipeScreen({super.key, required this.deckId, required this.title});
  final String deckId;
  final String title;

  @override
  ConsumerState<SwipeScreen> createState() => _SwipeScreenState();
}

class _SwipeScreenState extends ConsumerState<SwipeScreen> with SingleTickerProviderStateMixin {
  static const _threshold = 110.0;

  final List<Map<String, dynamic>> _cards = [];
  bool _loading = true;
  String? _error;
  int _total = 0; // карточек в колоде
  int _done = 0; // из них пролистано
  String? _partner; // имя партнёра для подсказки

  Offset _drag = Offset.zero;
  late final AnimationController _anim;
  Animation<Offset>? _flight;
  String? _pendingChoice; // выбор, который отправим, когда карточка улетит

  @override
  void initState() {
    super.initState();
    _anim = AnimationController(vsync: this, duration: const Duration(milliseconds: 220))
      ..addListener(() {
        if (_flight != null) setState(() => _drag = _flight!.value);
      })
      ..addStatusListener((s) {
        if (s == AnimationStatus.completed) _onFlightDone();
      });
    _load();
  }

  @override
  void dispose() {
    _anim.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final dio = ref.read(dioProvider);
      final r = await dio.get('/wishes/decks/${widget.deckId}/next');
      final list = (r.data as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
      // Прогресс колоды и имя партнёра — для шапки и подсказки, как в макете.
      final decks = await dio.get('/wishes/decks');
      final deck = ((decks.data as Map)['decks'] as List)
          .cast<Map>()
          .where((d) => d['id'] == widget.deckId)
          .firstOrNull;
      if (_partner == null) {
        try {
          final pair = await dio.get('/pairs/me');
          _partner = (pair.data as Map?)?['partner_name'] as String?;
        } catch (_) {}
      }
      if (!mounted) return;
      setState(() {
        _cards
          ..clear()
          ..addAll(list);
        if (deck != null) {
          _total = deck['total'] as int;
          _done = deck['done'] as int;
        }
      });
    } catch (e) {
      if (mounted) setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  // ---------- жесты и анимация ----------

  void _animateTo(Offset target, {String? choice}) {
    _pendingChoice = choice;
    _flight = Tween<Offset>(begin: _drag, end: target)
        .animate(CurvedAnimation(parent: _anim, curve: Curves.easeOut));
    _anim.forward(from: 0);
  }

  void _onPanEnd(DragEndDetails _) {
    final w = MediaQuery.of(context).size.width;
    if (_drag.dx > _threshold) {
      _animateTo(Offset(w * 1.5, _drag.dy), choice: 'yes');
    } else if (_drag.dx < -_threshold) {
      _animateTo(Offset(-w * 1.5, _drag.dy), choice: 'no');
    } else if (_drag.dy < -_threshold) {
      _animateTo(Offset(_drag.dx, -900), choice: 'maybe');
    } else {
      _animateTo(Offset.zero); // вернуть на место
    }
  }

  void _press(String choice) {
    if (_cards.isEmpty || _anim.isAnimating) return;
    final w = MediaQuery.of(context).size.width;
    final target = switch (choice) {
      'yes' => Offset(w * 1.5, 0),
      'no' => Offset(-w * 1.5, 0),
      _ => const Offset(0, -900),
    };
    _animateTo(target, choice: choice);
  }

  void _onFlightDone() {
    final choice = _pendingChoice;
    _pendingChoice = null;
    _flight = null;
    if (choice == null || _cards.isEmpty) {
      setState(() => _drag = Offset.zero);
      return;
    }
    final card = _cards.removeAt(0);
    setState(() {
      _drag = Offset.zero;
      _done = math.min(_done + 1, _total);
      if (_cards.isEmpty) _loading = true; // пока сохраняем и грузим следующую порцию
    });
    _send(card, choice);
  }

  Future<void> _send(Map<String, dynamic> card, String choice) async {
    try {
      final r = await ref.read(dioProvider).post('/wishes/swipe', data: {
        'card_id': card['id'],
        'choice': choice,
      });
      final match = (r.data as Map)['match'];
      // Карточки кончились — подтягиваем следующую порцию (после сохранения выбора, чтобы эта не вернулась).
      if (mounted && _cards.isEmpty) _load();
      if (match != null && mounted) {
        await _showMatch(Map<String, dynamic>.from(match as Map));
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _cards.insert(0, card); // вернуть карточку, чтобы не потерять выбор
        _loading = false;
        _done = math.max(_done - 1, 0);
      });
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    }
  }

  Future<void> _showMatch(Map<String, dynamic> m) async {
    final openPlan = await showGeneralDialog<bool>(
      context: context,
      barrierDismissible: false,
      barrierColor: Colors.black54,
      transitionDuration: const Duration(milliseconds: 260),
      transitionBuilder: (_, a, __, child) => FadeTransition(opacity: a, child: child),
      pageBuilder: (ctx, _, __) => _MatchScreen(match: m),
    );
    if (openPlan == true && mounted) {
      await context.push('/wishes/plan/${m['card_id']}');
    }
  }

  // ---------- интерфейс ----------

  @override
  Widget build(BuildContext context) {
    final progress = _total == 0 ? 0.0 : _done / _total;
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
          child: Column(children: [
            TopBar(
              title: 'Колода «${widget.title}»',
              below: SizedBox(
                width: 140,
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(2),
                  child: LinearProgressIndicator(
                    value: progress,
                    minHeight: 4,
                    backgroundColor: const Color(0xFF2A2938),
                    color: VColors.gold,
                  ),
                ),
              ),
              trailing: _total == 0
                  ? null
                  : Text('$_done/$_total', style: const TextStyle(fontSize: 13, color: VColors.muted)),
            ),
            const SizedBox(height: 18),
            Expanded(child: _body()),
            const SizedBox(height: 18),
            if (_cards.isNotEmpty) _Buttons(onPress: _press),
          ]),
        ),
      ),
    );
  }

  Widget _body() {
    if (_loading && _cards.isEmpty) return const Center(child: CircularProgressIndicator());
    if (_error != null && _cards.isEmpty) {
      return Center(
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          Text(_error!, style: const TextStyle(color: VColors.coral)),
          const SizedBox(height: 12),
          TextButton(onPressed: _load, child: const Text('Повторить')),
        ]),
      );
    }
    if (_cards.isEmpty) return const _DeckDone();

    final w = MediaQuery.of(context).size.width;
    final angle = (_drag.dx / w) * 0.35;
    final hint = _partner == null
        ? 'Партнёр увидит это, только если тоже скажет «да»'
        : '$_partner увидит это, только если тоже скажет «да»';
    return Stack(children: [
      // Колода «стопкой» позади, как в макете.
      Positioned(
        left: 22, right: 22, top: 0, bottom: 30,
        child: Transform.rotate(angle: 0.07, child: _backCard(const Color(0xFF1F1E2E))),
      ),
      Positioned(
        left: 12, right: 12, top: 6, bottom: 22,
        child: Transform.rotate(angle: -0.05, child: _backCard(const Color(0xFF26243A))),
      ),
      Positioned.fill(
        top: 12,
        bottom: 12,
        child: GestureDetector(
          onPanUpdate: _anim.isAnimating ? null : (d) => setState(() => _drag += d.delta),
          onPanEnd: _anim.isAnimating ? null : _onPanEnd,
          child: Transform.translate(
            offset: _drag,
            child: Transform.rotate(
              angle: angle,
              child: _CardFace(
                text: _cards[0]['text'] as String,
                deckTitle: widget.title,
                hint: hint,
                drag: _hint(),
              ),
            ),
          ),
        ),
      ),
    ]);
  }

  Widget _backCard(Color c) =>
      DecoratedBox(decoration: BoxDecoration(color: c, borderRadius: BorderRadius.circular(28)));

  /// Подсказка поверх карточки, пока её тянут.
  (String, Color, double)? _hint() {
    final dx = _drag.dx, dy = _drag.dy;
    if (dx.abs() < 20 && dy > -20) return null;
    if (dy < -20 && dy.abs() > dx.abs()) {
      return ('МОЖЕТ БЫТЬ', VColors.lavender, math.min(1.0, -dy / _threshold));
    }
    return dx > 0
        ? ('ДА', VColors.gold, math.min(1.0, dx / _threshold))
        : ('НЕТ', VColors.coral, math.min(1.0, -dx / _threshold));
  }
}

class _CardFace extends StatelessWidget {
  const _CardFace({required this.text, required this.deckTitle, required this.hint, this.drag});
  final String text;
  final String deckTitle;
  final String hint;
  final (String, Color, double)? drag;

  @override
  Widget build(BuildContext context) {
    final d = drag;
    return Container(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(28),
        gradient: const RadialGradient(
          center: Alignment(-0.4, -1),
          radius: 1.3,
          colors: [Color(0xFF33293F), Color(0xFF1D1C2C)],
          stops: [0, 0.65],
        ),
        border: Border.all(color: d == null ? const Color(0x47F3C98B) : d.$2.withOpacity(0.4 + 0.6 * d.$3)),
        boxShadow: const [BoxShadow(color: Color(0x66000000), blurRadius: 24, offset: Offset(0, 12))],
      ),
      padding: const EdgeInsets.fromLTRB(24, 26, 24, 24),
      child: Stack(children: [
        Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            _Tag(deckTitle, gold: true),
          ]),
          Expanded(
            child: Align(
              alignment: Alignment.bottomLeft,
              child: Text(text, maxLines: 7, overflow: TextOverflow.fade, style: display(28).copyWith(height: 1.3)),
            ),
          ),
          const SizedBox(height: 18),
          Row(children: [
            const Icon(Icons.visibility_outlined, size: 15, color: VColors.lavender),
            const SizedBox(width: 8),
            Expanded(child: Text(hint, style: const TextStyle(fontSize: 12, color: VColors.lavender))),
          ]),
        ]),
        if (d != null)
          Align(
            alignment: Alignment.topRight,
            child: Opacity(
              opacity: d.$3,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
                decoration: BoxDecoration(
                  borderRadius: BorderRadius.circular(12),
                  border: Border.all(color: d.$2, width: 2),
                ),
                child: Text(d.$1,
                    style: TextStyle(color: d.$2, fontSize: 18, fontWeight: FontWeight.w800, letterSpacing: 2)),
              ),
            ),
          ),
      ]),
    );
  }
}

class _Tag extends StatelessWidget {
  const _Tag(this.text, {this.gold = false});
  final String text;
  final bool gold;

  @override
  Widget build(BuildContext context) {
    return Container(
      height: 28,
      padding: const EdgeInsets.symmetric(horizontal: 12),
      alignment: Alignment.center,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(14),
        color: gold ? const Color(0x24F3C98B) : const Color(0x24B9A3F0),
      ),
      child: Text(text, style: TextStyle(fontSize: 12, color: gold ? VColors.gold : const Color(0xFFCDBDF7))),
    );
  }
}

class _Buttons extends StatelessWidget {
  const _Buttons({required this.onPress});
  final void Function(String) onPress;

  @override
  Widget build(BuildContext context) {
    return Row(mainAxisAlignment: MainAxisAlignment.center, crossAxisAlignment: CrossAxisAlignment.end, children: [
      _RoundButton(
        label: 'Нет',
        onTap: () => onPress('no'),
        child: const Icon(Icons.close, color: VColors.link, size: 26),
      ),
      const SizedBox(width: 22),
      _RoundButton(
        label: 'Может быть',
        fill: const Color(0xFF262236),
        border: const Color(0x80B9A3F0),
        onTap: () => onPress('maybe'),
        child: const Icon(Icons.waves, color: Color(0xFFCDBDF7), size: 26),
      ),
      const SizedBox(width: 22),
      _RoundButton(
        label: 'Да',
        big: true,
        onTap: () => onPress('yes'),
        child: const Icon(Icons.favorite_border, color: VColors.ink, size: 30),
      ),
    ]);
  }
}

class _RoundButton extends StatelessWidget {
  const _RoundButton({
    required this.label,
    required this.onTap,
    required this.child,
    this.big = false,
    this.fill = VColors.card,
    this.border = const Color(0x24FFFFFF),
  });
  final String label;
  final VoidCallback onTap;
  final Widget child;
  final bool big;
  final Color fill;
  final Color border;

  @override
  Widget build(BuildContext context) {
    final size = big ? 76.0 : 64.0;
    return Column(mainAxisSize: MainAxisSize.min, children: [
      Semantics(
        button: true,
        label: label,
        child: Container(
          width: size,
          height: size,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: big ? null : fill,
            gradient: big ? VColors.goldGradient : null,
            border: big ? null : Border.all(color: border),
            boxShadow: big ? const [BoxShadow(color: Color(0x59F3B46E), blurRadius: 30)] : null,
          ),
          child: Material(
            type: MaterialType.transparency,
            child: InkWell(customBorder: const CircleBorder(), onTap: onTap, child: Center(child: child)),
          ),
        ),
      ),
      const SizedBox(height: 6),
      Text(label,
          style: TextStyle(
            fontSize: 12,
            color: big ? VColors.gold : VColors.muted,
            fontWeight: big ? FontWeight.w600 : FontWeight.w400,
          )),
    ]);
  }
}

/// Полноэкранное «Совпадение» по макету: эмблема, свечение, карточка и шаги.
class _MatchScreen extends StatelessWidget {
  const _MatchScreen({required this.match});
  final Map<String, dynamic> match;

  @override
  Widget build(BuildContext context) {
    final both = match['kind'] == 'yes';
    const steps = [
      'Откройте план вечера — там варианты, продукты и шаги',
      'Выберите вариант вдвоём: как только совпадёт — план готов',
      'Назначьте день — план появится на «Сегодня» у обоих',
    ];
    return Material(
      color: Colors.transparent,
      child: Container(
        decoration: const BoxDecoration(
          gradient: RadialGradient(
            center: Alignment(0, -0.76),
            radius: 1.0,
            colors: [Color(0xFF35294A), Color(0xFF17161F), VColors.bgDeep],
            stops: [0, 0.55, 1],
          ),
        ),
        child: SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 24, 20, 20),
            child: Column(children: [
              const Emblem(size: 130),
              const SizedBox(height: 16),
              Text(both ? 'СОВПАДЕНИЕ' : 'МОЖНО ОБСУДИТЬ',
                  style: const TextStyle(fontSize: 12, letterSpacing: 3, color: VColors.label)),
              const SizedBox(height: 10),
              Text(both ? 'Вы оба этого хотите' : 'Один хочет, другой — может быть',
                  textAlign: TextAlign.center, style: display(30).copyWith(height: 1.25)),
              const SizedBox(height: 18),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.fromLTRB(20, 18, 20, 18),
                decoration: BoxDecoration(
                  color: const Color(0xD91C1B2B),
                  borderRadius: BorderRadius.circular(22),
                  border: Border.all(color: const Color(0x4DF3C98B)),
                ),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text((match['deck_title'] as String?) ?? '',
                      style: const TextStyle(fontSize: 12, color: Color(0xFFCDBDF7))),
                  const SizedBox(height: 6),
                  Text(match['text'] as String,
                      style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, height: 1.35)),
                ]),
              ),
              const SizedBox(height: 18),
              const Align(
                alignment: Alignment.centerLeft,
                child: Text('Как начать', style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600)),
              ),
              const SizedBox(height: 10),
              for (var i = 0; i < steps.length; i++)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Container(
                      width: 24,
                      height: 24,
                      alignment: Alignment.center,
                      decoration: const BoxDecoration(color: Color(0xFF2C2440), shape: BoxShape.circle),
                      child: Text('${i + 1}',
                          style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: VColors.gold)),
                    ),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Text(steps[i], style: const TextStyle(fontSize: 14, height: 1.45, color: VColors.soft)),
                    ),
                  ]),
                ),
              const Spacer(),
              GoldButton(
                onPressed: () => Navigator.of(context).pop(true),
                child: const Text('Открыть план вечера'),
              ),
              const SizedBox(height: 6),
              TextButton(
                style: TextButton.styleFrom(foregroundColor: VColors.link),
                onPressed: () => Navigator.of(context).pop(false),
                child: const Text('Листать дальше', style: TextStyle(fontSize: 15)),
              ),
            ]),
          ),
        ),
      ),
    );
  }
}

class _DeckDone extends StatelessWidget {
  const _DeckDone();

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        const Icon(Icons.done_all, color: VColors.gold, size: 48),
        const SizedBox(height: 14),
        Text('Колода пройдена', style: display(24)),
        const SizedBox(height: 8),
        const Text('Когда партнёр тоже её пролистает, совпадения появятся сами.',
            textAlign: TextAlign.center, style: TextStyle(color: VColors.soft)),
        const SizedBox(height: 20),
        GoldButton(
          onPressed: () => context.pushReplacement('/wishes/matches'),
          child: const Text('Смотреть совпадения'),
        ),
      ]),
    );
  }
}
