import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/network_diagnostics_screen.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';

class EmailScreen extends ConsumerStatefulWidget {
  const EmailScreen({super.key});

  @override
  ConsumerState<EmailScreen> createState() => _EmailScreenState();
}

class _EmailScreenState extends ConsumerState<EmailScreen> {
  final _email = TextEditingController();
  bool _busy = false;
  String? _error;

  Future<void> _send() async {
    final email = _email.text.trim();
    if (!RegExp(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$').hasMatch(email)) {
      setState(() => _error = 'Проверь e-mail');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final r = await ref.read(dioProvider).post('/auth/request-code', data: {'email': email});
      // В режиме разработки сервер возвращает код прямо в ответе — удобно для тестов.
      final devCode = (r.data as Map)['dev_code'];
      if (kDebugMode && devCode != null) debugPrint('DEV-код входа: $devCode');
      if (!mounted) return;
      context.go('/code', extra: email);
    } catch (e) {
      setState(() => _error = errorText(e));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _email.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          tooltip: 'Назад',
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.go('/welcome'),
        ),
        actions: [
          IconButton(
            tooltip: 'Проверить соединение',
            icon: const Icon(Icons.network_check),
            onPressed: () => Navigator.of(context).push(MaterialPageRoute<void>(
              builder: (_) => const NetworkDiagnosticsScreen(),
            )),
          ),
        ],
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 8, 24, 24),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Text('Вход', style: display(32)),
            const SizedBox(height: 8),
            const Text(
              'Пришлём шестизначный код. Пароль не нужен.',
              style: TextStyle(color: VColors.soft, fontSize: 16),
            ),
            const SizedBox(height: 24),
            TextField(
              controller: _email,
              keyboardType: TextInputType.emailAddress,
              autofillHints: const [AutofillHints.email],
              textInputAction: TextInputAction.done,
              onSubmitted: (_) => _send(),
              decoration: const InputDecoration(labelText: 'E-mail'),
            ),
            if (_error != null) ...[
              const SizedBox(height: 10),
              Text(_error!, style: const TextStyle(color: Color(0xFFF5A894))),
            ],
            const Spacer(),
            GoldButton(
              onPressed: _busy ? null : _send,
              child: Text(_busy ? 'Отправляем…' : 'Получить код'),
            ),
          ]),
        ),
      ),
    );
  }
}
