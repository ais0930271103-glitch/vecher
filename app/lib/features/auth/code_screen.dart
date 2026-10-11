import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/onboarding.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

class CodeScreen extends ConsumerStatefulWidget {
  const CodeScreen({super.key, required this.email});
  final String email;

  @override
  ConsumerState<CodeScreen> createState() => _CodeScreenState();
}

class _CodeScreenState extends ConsumerState<CodeScreen> {
  final _code = TextEditingController();
  bool _busy = false;
  String? _error;

  Future<void> _verify() async {
    if (_code.text.length != 6) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final r = await ref.read(dioProvider).post('/auth/verify', data: {
        'email': widget.email,
        'code': _code.text,
      });
      final data = r.data as Map;
      await saveToken(data['access_token'] as String);
      if (!mounted) return;
      // Новый человек: согласие на обработку данных → имя → город → «Сегодня».
      context.go(nextOnboardingStep(data));
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
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          tooltip: 'Назад',
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.go('/login'),
        ),
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 8, 24, 24),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Text('Код из письма', style: display(32)),
            const SizedBox(height: 8),
            Text('Отправили на ${widget.email}',
                style: const TextStyle(color: VColors.soft, fontSize: 16)),
            const SizedBox(height: 24),
            TextField(
              controller: _code,
              autofocus: true,
              keyboardType: TextInputType.number,
              autofillHints: const [AutofillHints.oneTimeCode],
              inputFormatters: [
                FilteringTextInputFormatter.digitsOnly,
                LengthLimitingTextInputFormatter(6),
              ],
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 28, letterSpacing: 10),
              onChanged: (v) {
                if (v.length == 6) _verify();
              },
              decoration: const InputDecoration(hintText: '••••••'),
            ),
            if (_error != null) ...[
              const SizedBox(height: 10),
              Text(_error!, style: const TextStyle(color: Color(0xFFF5A894))),
            ],
            const Spacer(),
            GoldButton(
              onPressed: _busy ? null : _verify,
              child: Text(_busy ? 'Проверяем…' : 'Войти'),
            ),
          ]),
        ),
      ),
    );
  }
}
