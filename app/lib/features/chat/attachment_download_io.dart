import 'dart:io';

import 'package:dio/dio.dart';
import 'package:open_filex/open_filex.dart';
import 'package:path_provider/path_provider.dart';

import 'attachment_models.dart';

/// Download through the authenticated API, then hand a local file to the OS.
/// The access token is never placed in a URL or passed to another application.
Future<String?> downloadChatAttachment(
  Dio dio,
  ChatAttachment attachment, {
  required CancelToken cancelToken,
  required ProgressCallback onProgress,
}) async {
  final endpoint = chatAttachmentPath(attachment.id);
  final root = Directory(
    '${(await getTemporaryDirectory()).path}/vecher-chat-files',
  );
  await root.create(recursive: true);
  await _removeOldDownloads(root);
  final folder = Directory('${root.path}/${attachment.id}');
  await folder.create(recursive: true);
  final file = File('${folder.path}/${safeChatFilename(attachment.name)}');
  try {
    await dio.download(
      endpoint,
      file.path,
      cancelToken: cancelToken,
      options: Options(
        followRedirects: false,
        receiveTimeout: const Duration(minutes: 2),
      ),
      onReceiveProgress: (received, total) {
        if (received > maxChatAttachmentBytes || received > attachment.size) {
          cancelToken.cancel(
            const ChatFileException('Файл больше ожидаемого размера'),
          );
          return;
        }
        onProgress(received, attachment.size);
      },
    );
    if (cancelToken.isCancelled) throw cancelToken.cancelError!;
    if (await file.length() != attachment.size) {
      throw const ChatFileException(
        'Файл загрузился не полностью. Попробуй ещё раз',
      );
    }
    final result = await OpenFilex.open(file.path, type: attachment.mime);
    if (result.type == ResultType.noAppToOpen) {
      return 'Файл скачан, но на устройстве нет приложения для этого формата';
    }
    if (result.type != ResultType.done) {
      throw const ChatFileException(
        'Не получилось открыть файл на этом устройстве',
      );
    }
    return null;
  } catch (_) {
    if (await file.exists()) await file.delete();
    rethrow;
  }
}

Future<void> _removeOldDownloads(Directory root) async {
  final cutoff = DateTime.now().subtract(const Duration(hours: 24));
  try {
    await for (final item in root.list(followLinks: false)) {
      if (item is Directory && (await item.stat()).modified.isBefore(cutoff)) {
        await item.delete(recursive: true);
      }
    }
  } catch (_) {
    // An OS cache cleanup must not prevent a fresh authorized download.
  }
}
