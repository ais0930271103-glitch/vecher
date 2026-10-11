import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/theme.dart';

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
      final r = await ref.read(dioProvider).get('/wishes/decks/${widget.deckId}/next');
      final list = (r.data as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
      if (!mounted) return;
      setState(() {
        _cards
          ..clear()
          ..addAll(list);
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
      });
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    }
  }

  Future<void> _showMatch(Map<String, dynamic> m) {
    final both = m['kind'] == 'yes';
    return showDialog<void>(
      context: context,
      builder: (ctx) => Dialog(
        backgroundColor: VColors.card,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(28)),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 28, 24, 20),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Icon(both ? Icons.favorite : Icons.chat_bubble_outline, color: VColors.gold, size: 44),
            const SizedBox(height: 14),
            Text(both ? 'Совпадение!' : 'Можно обсудить', style: display(26)),
            const SizedBox(height: 10),
            Text(m['text'] as String,
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 18, height: 1.35, fontWeight: FontWeight.w600)),
            const SizedBox(height: 8),
            Text(both ? 'Вы оба этого хотите' : 'Один хочет, другой — может быть',
                style: const TextStyle(color: VColors.soft)),
            const SizedBox(height: 20),
            FilledButton(onPressed: () => Navigator.of(ctx).pop(), child: const Text('Листать дальше')),
          ]),
        ),
      ),
    );
  }

  // ---------- интерфейс ----------

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(widget.title, style: display(22))),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 4, 20, 20),
          child: Column(children: [
            Expanded(child: _body()),
            const SizedBox(height: 16),
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
          Text(_error!, style: const TextStyle(color: Color(0xFFF5A894))),
          const SizedBox(height: 12),
          TextButton(onPressed: _load, child: const Text('Повторить')),
        ]),
      );
    }
    if (_cards.isEmpty) return const _DeckDone();

    final w = MediaQuery.of(context).size.width;
    final angle = (_drag.dx / w) * 0.35;
    return Stack(alignment: Alignment.center, children: [
      if (_cards.length > 1)
        Transform.scale(scale: 0.94, child: _CardFace(text: _cards[1]['text'] as String, dim: true)),
      GestureDetector(
        onPanUpdate: _anim.isAnimating ? null : (d) => setState(() => _drag += d.delta),
        onPanEnd: _anim.isAnimating ? null : _onPanEnd,
        child: Transform.translate(
          offset: _drag,
          child: Transform.rotate(
            angle: angle,
            child: _CardFace(text: _cards[0]['text'] as String, hint: _hint()),
          ),
        ),
      ),
    ]);
  }

  /// Подсказка поверх карточки, пока её тянут.
  (String, Color, double)? _hint() {
    final dx = _drag.dx, dy = _drag.dy;
    if (dx.abs() < 20 && dy > -20) return null;
    if (dy < -20 && dy.abs() > dx.abs()) {
      return ('МОЖЕТ БЫТЬ', VColors.lavender, math.min(1.0, -dy / _threshold));
    }
    return dx > 0
        ? ('ДА', VColors.gold, math.min(1.0, dx / _threshold))
        : ('НЕТ', const Color(0xFFF5A894), math.min(1.0, -dx / _threshold));
  }
}

class _CardFace extends StatelessWidget {
  const _CardFace({required this.text, this.dim = false, this.hint});
  final String text;
  final bool dim;
  final (String, Color, double)? hint;

  @override
  Widget build(BuildContext context) {
    final h = hint;
    return AspectRatio(
      aspectRatio: 3 / 4,
      child: Container(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(28),
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: dim
                ? const [Color(0xFF1C1E44), Color(0xFF15173A)]
                : const [Color(0xFF2E2760), Color(0xFF1A1C42)],
          ),
          border: Border.all(color: h == null ? const Color(0x38F3C98B) : h.$2.withOpacity(0.4 + 0.6 * h.$3)),
          boxShadow: dim ? null : const [BoxShadow(color: Color(0x66000000), blurRadius: 24, offset: Offset(0, 12))],
        ),
        padding: const EdgeInsets.all(28),
        child: Stack(children: [
          Center(
            child: Text(
              text,
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 26,
                height: 1.3,
                fontWeight: FontWeight.w600,
                color: dim ? VColors.muted : VColors.text,
              ),
            ),
          ),
          if (h != null)
            Align(
              alignment: Alignment.topCenter,
              child: Opacity(
                opacity: h.$3,
                child: Container(
                  padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
                  decoration: BoxDecoration(
                    borderRadius: BorderRadius.circular(12),
                    border: Border.all(color: h.$2, width: 2),
                  ),
                  child: Text(h.$1,
                      style: TextStyle(color: h.$2, fontSize: 20, fontWeight: FontWeight.w800, letterSpacing: 2)),
                ),
              ),
            ),
        ]),
      ),
    );
  }
}

class _Buttons extends StatelessWidget {
  const _Buttons({required this.onPress});
  final void Function(String) onPress;

  @override
  Widget build(BuildContext context) {
    return Row(mainAxisAlignment: MainAxisAlignment.spaceEvenly, children: [
      _RoundButton(icon: Icons.close, color: const Color(0xFFF5A894), label: 'Нет', onTap: () => onPress('no')),
      _RoundButton(
          icon: Icons.help_outline, color: VColors.lavender, label: 'Может быть', onTap: () => onPress('maybe')),
      _RoundButton(icon: Icons.favorite, color: VColors.gold, label: 'Да', big: true, onTap: () => onPress('yes')),
    ]);
  }
}

class _RoundButton extends StatelessWidget {
  const _RoundButton({
    required this.icon,
    required this.color,
    required this.label,
    required this.onTap,
    this.big = false,
  });
  final IconData icon;
  final Color color;
  final String label;
  final VoidCallback onTap;
  final bool big;

  @override
  Widget build(BuildContext context) {
    final size = big ? 72.0 : 60.0;
    return Column(mainAxisSize: MainAxisSize.min, children: [
      Material(
        color: VColors.card,
        shape: CircleBorder(side: BorderSide(color: color.withOpacity(0.6), width: 1.5)),
        child: InkWell(
          customBorder: const CircleBorder(),
          onTap: onTap,
          child: SizedBox(width: size, height: size, child: Icon(icon, color: color, size: big ? 34 : 28)),
        ),
      ),
      const SizedBox(height: 6),
      Text(label, style: const TextStyle(fontSize: 12, color: VColors.muted)),
    ]);
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
        FilledButton(
          onPressed: () => context.pushReplacement('/wishes/matches'),
          child: const Text('Смотреть совпадения'),
        ),
      ]),
    );
  }
}
