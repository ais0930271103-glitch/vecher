import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:open_filex/open_filex.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:path_provider/path_provider.dart';

import 'api.dart';
import 'network_update.dart';
import 'theme.dart';
import 'ui.dart';

/// Обновление без магазина: сервер говорит, какая сборка последняя (/app/version),
/// приложение скачивает APK и открывает установщик Android (одно нажатие «Установить»).
/// Тихо обновиться Android не позволяет — подтверждение всегда за человеком.

bool _checkedThisLaunch = false;

/// Текущая версия: «0.1.2 (3)».
Future<String> currentVersionLabel() async {
  final info = await PackageInfo.fromPlatform();
  return '${info.version} (${info.buildNumber})';
}

/// Проверить обновление. [manual] — из настроек: сообщить, даже если обновлений нет.
Future<void> checkForUpdate(BuildContext context, {bool manual = false}) async {
  if (kIsWeb || !Platform.isAndroid) return;
  if (!manual && _checkedThisLaunch) return;
  _checkedThisLaunch = true;
  final dio = createPublicApiDio();
  try {
    final response = await dio.get('/app/version');
    final manifest = ApkUpdateManifest.parse(response.data);
    final current =
        int.tryParse((await PackageInfo.fromPlatform()).buildNumber) ?? 0;
    if (manifest.versionCode <= current) {
      if (manual && context.mounted) _toast(context, 'У тебя последняя версия');
      return;
    }
    if (!context.mounted) return;
    await showDialog<void>(
      context: context,
      barrierDismissible: !manifest.required,
      builder: (_) => _UpdateDialog(info: manifest, dio: dio),
    );
  } catch (_) {
    if (manual && context.mounted) {
      _toast(context, 'Не получилось проверить обновления');
    }
  } finally {
    dio.close(force: true);
  }
}

void _toast(BuildContext context, String text) =>
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

class _UpdateDialog extends StatefulWidget {
  const _UpdateDialog({required this.info, required this.dio});
  final ApkUpdateManifest info;
  final Dio dio;

  @override
  State<_UpdateDialog> createState() => _UpdateDialogState();
}

class _UpdateDialogState extends State<_UpdateDialog> {
  double? _progress;
  String? _error;
  CancelToken? _cancel;

  @override
  void dispose() {
    _cancel?.cancel('Update dialog closed');
    super.dispose();
  }

  Future<void> _update() async {
    final cancel = CancelToken();
    _cancel = cancel;
    setState(() {
      _progress = 0;
      _error = null;
    });
    try {
      final dir = await getTemporaryDirectory();
      final file = await downloadVerifiedApk(
        manifest: widget.info,
        dio: widget.dio,
        directory: dir,
        cancelToken: cancel,
        onProgress: (progress) {
          if (mounted) setState(() => _progress = progress);
        },
      );
      if (!mounted || cancel.isCancelled) return;
      final res = await OpenFilex.open(
        file.path,
        type: 'application/vnd.android.package-archive',
      );
      if (res.type != ResultType.done) {
        throw StateError('Installer unavailable');
      }
      if (mounted) setState(() => _progress = null);
    } catch (error) {
      if (mounted) {
        setState(() {
          _progress = null;
          _error = error is UpdateIntegrityException
              ? 'Файл обновления не прошёл проверку. Закрой окно и проверь обновления ещё раз.'
              : error is DioException
              ? 'Загрузка прервалась. Проверь подключение и попробуй ещё раз.'
              : 'Не получилось открыть установку. Проверь разрешение на установку из «Вечера» и попробуй снова.';
        });
      }
    } finally {
      if (identical(_cancel, cancel)) _cancel = null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final required = widget.info.required;
    final notes = widget.info.notes;
    final p = _progress;
    return PopScope(
      canPop: !required,
      child: AlertDialog(
        backgroundColor: VColors.card,
        title: Text(
          'Новая версия ${widget.info.versionName}',
          style: display(20),
        ),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (notes.isNotEmpty)
              Text(
                notes,
                style: const TextStyle(color: VColors.soft, height: 1.4),
              ),
            if (notes.isNotEmpty) const SizedBox(height: 12),
            if (p != null) ...[
              LinearProgressIndicator(
                value: p == 0 ? null : p,
                color: VColors.gold,
                backgroundColor: const Color(0x1FFFFFFF),
              ),
              const SizedBox(height: 8),
              Text(
                p == 0 ? 'Скачиваем…' : 'Скачано ${(p * 100).round()}%',
                style: const TextStyle(color: VColors.muted, fontSize: 13),
              ),
            ] else
              const Text(
                'Скачаем и откроем установку — нажми «Установить». Всё твоё сохранится.',
                style: TextStyle(
                  color: VColors.muted,
                  fontSize: 13,
                  height: 1.4,
                ),
              ),
            if (_error != null) ...[
              const SizedBox(height: 8),
              Text(
                _error!,
                style: const TextStyle(color: VColors.coral, fontSize: 13),
              ),
            ],
          ],
        ),
        actions: [
          if (!required)
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('Позже'),
            ),
          SizedBox(
            width: 150,
            child: GoldButton(
              height: 44,
              onPressed: p != null ? null : _update,
              child: const Text('Обновить'),
            ),
          ),
        ],
      ),
    );
  }
}
