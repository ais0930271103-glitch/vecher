import 'package:dio/dio.dart';

import 'attachment_models.dart';

/// Browser downloads also fetch with the current Bearer token; only the bytes
/// are handed to the browser download, never a private URL containing a token.
Future<String?> downloadChatAttachment(
  Dio dio,
  ChatAttachment attachment, {
  required CancelToken cancelToken,
  required ProgressCallback onProgress,
}) async {
  await dio.download(
    chatAttachmentPath(attachment.id),
    safeChatFilename(attachment.name),
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
  return 'Файл передан в загрузки браузера';
}
