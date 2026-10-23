import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:url_launcher/url_launcher.dart';

import 'api.dart';
import 'network_diagnostics.dart';
import 'privacy.dart';
import 'session_changes.dart';

class NetworkDiagnosticsScreen extends ConsumerStatefulWidget {
  const NetworkDiagnosticsScreen({super.key});

  @override
  ConsumerState<NetworkDiagnosticsScreen> createState() =>
      _NetworkDiagnosticsScreenState();
}

class _NetworkDiagnosticsScreenState
    extends ConsumerState<NetworkDiagnosticsScreen>
    with WidgetsBindingObserver {
  AppLifecycleState _lifecycle = AppLifecycleState.resumed;
  DiagnosticVpnState _vpn = DiagnosticVpnState.unspecified;
  DiagnosticVpnState _resultVpn = DiagnosticVpnState.unspecified;
  ConnectionDiagnostic? _result;
  CancelToken? _request;
  String _version = 'UNKNOWN';
  String? _notice;

  bool get _allowed =>
      mounted &&
      _lifecycle == AppLifecycleState.resumed &&
      PinGate.isReady &&
      !PinGate.isLocked &&
      !sessionChanges.isChanging;

  @override
  void initState() {
    super.initState();
    _lifecycle =
        WidgetsBinding.instance.lifecycleState ?? AppLifecycleState.resumed;
    WidgetsBinding.instance.addObserver(this);
    PinGate.lockedListenable.addListener(_privacyChanged);
    PinGate.readyListenable.addListener(_privacyChanged);
    sessionChanges.addListener(_sessionChanged);
    _loadVersion();
  }

  Future<void> _loadVersion() async {
    try {
      final info = await PackageInfo.fromPlatform();
      if (mounted) {
        setState(() => _version = '${info.version} (${info.buildNumber})');
      }
    } catch (_) {
      // A local package-info error must not prevent a connection check.
    }
  }

  void _stop() {
    final request = _request;
    _request = null;
    if (request == null) return;
    request.cancel();
    if (mounted) {
      setState(() {
        _notice = 'Проверка остановлена. Вернись и запусти её ещё раз.';
      });
    }
  }

  void _privacyChanged() {
    if (!_allowed) _stop();
    if (mounted) setState(() {});
  }

  void _sessionChanged() {
    _stop();
    if (mounted) setState(() => _result = null);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _lifecycle = state;
    if (!_allowed) _stop();
    if (mounted) setState(() {});
  }

  Future<void> _check() async {
    if (!_allowed || _request != null) return;
    final request = CancelToken();
    final chosenVpn = _vpn;
    setState(() {
      _request = request;
      _result = null;
      _notice = null;
    });
    final result = await diagnoseApiConnection(
      ref.read(dioProvider),
      cancelToken: request,
    );
    if (!_allowed || !identical(_request, request)) return;
    setState(() {
      _request = null;
      _result = result;
      _resultVpn = chosenVpn;
    });
  }

  Future<void> _openBrowser() async {
    if (!_allowed || _request != null) return;
    final url =
        _result?.healthUrl ??
        ref.read(apiEndpointsProvider).selected.resolve('/health');
    try {
      if (await launchUrl(url, mode: LaunchMode.externalApplication)) return;
    } catch (_) {
      // Do not expose platform exception text or fall back to another URL.
    }
    if (mounted && _allowed) {
      setState(() => _notice = 'Не удалось открыть браузер.');
    }
  }

  Future<void> _copy() async {
    final result = _result;
    if (!_allowed || result == null) return;
    try {
      await Clipboard.setData(
        ClipboardData(
          text: result.report(appVersion: _version, vpn: _resultVpn),
        ),
      );
      if (_allowed) {
        setState(() => _notice = 'Отчёт скопирован. Можно вставить его в чат.');
      }
    } catch (_) {
      if (_allowed) setState(() => _notice = 'Не удалось скопировать отчёт.');
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    PinGate.lockedListenable.removeListener(_privacyChanged);
    PinGate.readyListenable.removeListener(_privacyChanged);
    sessionChanges.removeListener(_sessionChanged);
    _request?.cancel();
    _request = null;
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final busy = _request != null;
    final result = _result;
    final origin = ref.read(apiEndpointsProvider).selected;
    return Scaffold(
      appBar: AppBar(title: const Text('Проверка соединения')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(20),
          children: [
            const Text(
              'Укажи, включён ли VPN, и запусти проверку. Затем измени VPN '
              'и повтори на этом же телефоне.',
            ),
            const SizedBox(height: 16),
            DropdownButtonFormField<DiagnosticVpnState>(
              initialValue: _vpn,
              decoration: const InputDecoration(labelText: 'VPN сейчас'),
              items: [
                for (final value in DiagnosticVpnState.values)
                  DropdownMenuItem(value: value, child: Text(value.label)),
              ],
              onChanged: busy || !_allowed
                  ? null
                  : (value) {
                      if (value != null) setState(() => _vpn = value);
                    },
            ),
            const SizedBox(height: 12),
            const Text(
              'Состояние VPN выбирается вручную. Проверяем доступ к серверу '
              'Вечера; воспроизведение видео и звонки здесь не проверяются.',
            ),
            const SizedBox(height: 20),
            FilledButton.icon(
              onPressed: busy || !_allowed ? null : _check,
              icon: busy
                  ? const SizedBox.square(
                      dimension: 18,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.network_check),
              label: Text(
                busy ? 'Проверяем… до 20 секунд' : 'Проверить соединение',
              ),
            ),
            if (result != null) ...[
              const SizedBox(height: 20),
              Text(
                result.status.label,
                style: Theme.of(context).textTheme.titleMedium,
              ),
              const SizedBox(height: 8),
              Text('VPN: ${_resultVpn.label} (указано вручную)'),
              Text('Время: ${result.elapsed.inMilliseconds} мс'),
              if (result.httpStatus != null) Text('HTTP: ${result.httpStatus}'),
              Text('Адрес: ${result.healthUrl}'),
              const SizedBox(height: 12),
              OutlinedButton.icon(
                onPressed: _allowed ? _copy : null,
                icon: const Icon(Icons.copy),
                label: const Text('Скопировать отчёт'),
              ),
            ],
            if (_notice != null) ...[
              const SizedBox(height: 12),
              Text(_notice!),
            ],
            const SizedBox(height: 20),
            Text('Версия: $_version'),
            Text('Сервер: $origin'),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              onPressed: busy || !_allowed ? null : _openBrowser,
              icon: const Icon(Icons.open_in_browser),
              label: const Text('Открыть проверку в браузере'),
            ),
            const SizedBox(height: 8),
            const Text(
              'Откроется публичная страница /health этого сервера. '
              'Если в VPN есть исключения для приложений, проверь, что '
              'браузер и Вечер используют одинаковый режим.',
            ),
            const SizedBox(height: 16),
            const Text(
              'Отчёт содержит версию приложения, адрес сервера, результат '
              'и время ответа. Он копируется только по твоей команде; '
              'сообщения, токены, данные аккаунта и IP телефона в него не входят.',
            ),
          ],
        ),
      ),
    );
  }
}
