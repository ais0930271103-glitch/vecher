import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/onboarding.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

/// Имя при регистрации — партнёр будет видеть его в приложении.
class NameScreen extends ConsumerStatefulWidget {
  const NameScreen({super.key});

  @override
  ConsumerState<NameScreen> createState() => _NameScreenState();
}

class _NameScreenState extends ConsumerState<NameScreen> {
  final _name = TextEditingController();
  bool _busy = false;
  String? _error;

  Future<void> _save() async {
    final name = _name.text.trim();
    if (name.isEmpty) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final r = await ref.read(dioProvider).put('/auth/me', data: {'display_name': name});
      if (mounted) context.go(nextOnboardingStep(r.data as Map));
    } catch (e) {
      setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 40, 24, 24),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Text('Как тебя зовут?', style: display(32)),
            const SizedBox(height: 8),
            const Text('Так тебя будет видеть партнёр. Изменить можно в разделе «Пара».',
                style: TextStyle(color: VColors.soft, fontSize: 16)),
            const SizedBox(height: 24),
            TextField(
              controller: _name,
              autofocus: true,
              maxLength: 60,
              textCapitalization: TextCapitalization.words,
              autofillHints: const [AutofillHints.givenName],
              decoration: const InputDecoration(hintText: 'Имя', counterText: ''),
              onChanged: (_) => setState(() {}),
              onSubmitted: (_) => _save(),
            ),
            if (_error != null) ...[
              const SizedBox(height: 10),
              Text(_error!, style: const TextStyle(color: Color(0xFFF5A894))),
            ],
            const Spacer(),
            GoldButton(
              onPressed: _busy || _name.text.trim().isEmpty ? null : _save,
              child: Text(_busy ? 'Сохраняем…' : 'Продолжить'),
            ),
          ]),
        ),
      ),
    );
  }
}
