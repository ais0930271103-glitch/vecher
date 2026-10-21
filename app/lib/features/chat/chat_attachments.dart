import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../../core/api.dart';
import '../../core/theme.dart';
import 'attachment_models.dart';

class SelectedChatFile {
  SelectedChatFile({
    required this.file,
    required this.name,
    required this.size,
    this.browserBytes,
  });
  final PlatformFile file;
  final String name;
  final int size;
  final Uint8List? browserBytes;
  ChatAttachment? uploaded;

  Stream<List<int>> openRead() {
    final bytes = browserBytes ?? file.bytes;
    if (bytes != null) return Stream.value(bytes);
    // Native picker paths refer to cached files and can be reopened after a
    // cancelled upload. A single-use picker stream cannot provide safe retries.
    if (!kIsWeb && file.path != null) return file.xFile.openRead();
    throw const ChatFileException(
      'Не получилось прочитать файл. Выбери его заново',
    );
  }
}

Future<SelectedChatFile?> pickChatFile() async {
  final result = await FilePicker.pickFiles(
    dialogTitle: 'Выбрать файл',
    allowMultiple: false,
    withData: false,
    withReadStream: true,
  );
  if (result == null || result.files.isEmpty) return null;
  final file = result.files.single;
  final size = file.size;
  if (size < 0) {
    throw const ChatFileException(
      'Не получилось прочитать файл. Попробуй выбрать его ещё раз',
    );
  }
  if (size > maxChatAttachmentBytes) {
    throw const ChatFileException('Можно отправить файл до 25 МБ');
  }
  Uint8List? browserBytes;
  if (kIsWeb) {
    final stream = file.readStream;
    if (stream == null) {
      throw const ChatFileException('Не получилось прочитать файл в браузере');
    }
    // Browser FilePicker 11 supplies a single-use stream. Buffer only AFTER the
    // size check, up to 25 MiB, so cancellation/retry can reopen identical bytes.
    final bytes = BytesBuilder(copy: false);
    await for (final chunk in checkedChatFileStream(stream, size)) {
      bytes.add(chunk);
    }
    browserBytes = bytes.takeBytes();
  }
  return SelectedChatFile(
    file: file,
    name: safeChatFilename(file.name),
    size: size,
    browserBytes: browserBytes,
  );
}

Future<ChatAttachment> uploadChatFile(
  Dio dio,
  SelectedChatFile selected, {
  required CancelToken cancelToken,
  required ProgressCallback onProgress,
}) async {
  final uploaded = selected.uploaded;
  if (uploaded != null) return uploaded;
  final part = MultipartFile.fromStream(
    () => checkedChatFileStream(selected.openRead(), selected.size),
    selected.size,
    filename: selected.name,
    contentType: MultipartFile.lookupMediaType(selected.name),
  );
  final response = await dio.post<Map<String, dynamic>>(
    '/chat/attachments',
    data: FormData.fromMap({'file': part}),
    cancelToken: cancelToken,
    options: Options(
      sendTimeout: const Duration(minutes: 3),
      receiveTimeout: const Duration(seconds: 45),
    ),
    onSendProgress: onProgress,
  );
  final metadata = response.data;
  if (metadata == null) {
    throw const ChatFileException('Сервер не подтвердил загрузку файла');
  }
  final result = ChatAttachment.fromJson(metadata);
  selected.uploaded = result;
  return result;
}

String chatFileErrorText(Object error) {
  if (error is ChatFileException) return error.message;
  if (error is DioException && error.error is ChatFileException) {
    return (error.error as ChatFileException).message;
  }
  return errorText(error);
}

List<ChatAttachment> messageAttachments(Map message) {
  final data = message['attachments'];
  if (message['deleted'] == true || data is! List) return const [];
  return data.whereType<Map>().map(ChatAttachment.fromJson).toList();
}

IconData chatFileIcon(String name, [String? mime]) {
  if (mime?.startsWith('image/') == true) return Icons.image_outlined;
  if (mime?.startsWith('video/') == true) return Icons.movie_outlined;
  if (mime?.startsWith('audio/') == true) return Icons.audio_file_outlined;
  final ext = name.split('.').last.toLowerCase();
  if (const ['zip', 'rar', '7z', 'tar', 'gz'].contains(ext)) {
    return Icons.folder_zip_outlined;
  }
  if (ext == 'pdf') return Icons.picture_as_pdf_outlined;
  return Icons.insert_drive_file_outlined;
}

class ChatAttachmentTile extends StatelessWidget {
  const ChatAttachmentTile({
    super.key,
    required this.attachment,
    required this.onOpen,
    required this.downloading,
    this.progress,
    this.onCancel,
  });
  final ChatAttachment attachment;
  final VoidCallback onOpen;
  final bool downloading;
  final double? progress;
  final VoidCallback? onCancel;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: 'Файл ${attachment.name}, ${chatFileSizeLabel(attachment.size)}',
      button: true,
      child: Material(
        color: const Color(0x10FFFFFF),
        borderRadius: BorderRadius.circular(12),
        child: InkWell(
          onTap: downloading ? null : onOpen,
          borderRadius: BorderRadius.circular(12),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(10, 10, 2, 10),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  chatFileIcon(attachment.name, attachment.mime),
                  color: VColors.gold,
                  size: 28,
                ),
                const SizedBox(width: 10),
                Flexible(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(
                        attachment.name,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          fontSize: 14,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        downloading
                            ? 'Скачиваем${progress == null ? '…' : ' ${(progress! * 100).round()}%'}'
                            : '${chatFileSizeLabel(attachment.size)} · открыть',
                        style: const TextStyle(
                          fontSize: 12,
                          color: VColors.muted,
                        ),
                      ),
                      if (downloading) ...[
                        const SizedBox(height: 6),
                        LinearProgressIndicator(
                          value: progress,
                          color: VColors.gold,
                          backgroundColor: VColors.card2,
                        ),
                      ],
                    ],
                  ),
                ),
                IconButton(
                  tooltip: downloading
                      ? 'Отменить скачивание'
                      : 'Скачать и открыть',
                  icon: Icon(
                    downloading ? Icons.close : Icons.download_outlined,
                    color: VColors.gold,
                    size: 22,
                  ),
                  onPressed: downloading ? onCancel : onOpen,
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class PendingChatFileTile extends StatelessWidget {
  const PendingChatFileTile({
    super.key,
    required this.file,
    required this.sending,
    required this.uploading,
    required this.onRemove,
    required this.onCancel,
    this.progress,
  });
  final SelectedChatFile file;
  final bool sending;
  final bool uploading;
  final double? progress;
  final VoidCallback onRemove;
  final VoidCallback onCancel;

  @override
  Widget build(BuildContext context) {
    final label = uploading
        ? 'Загружаем${progress == null ? '…' : ' ${(progress! * 100).round()}%'}'
        : sending
        ? 'Отправляем…'
        : '${chatFileSizeLabel(file.size)} · до 25 МБ';
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 8, 12, 0),
      padding: const EdgeInsets.fromLTRB(12, 8, 2, 8),
      decoration: BoxDecoration(
        color: VColors.card,
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(
        children: [
          Icon(chatFileIcon(file.name), size: 24, color: VColors.gold),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  file.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 14),
                ),
                Text(
                  label,
                  style: const TextStyle(color: VColors.muted, fontSize: 12),
                ),
                if (uploading) ...[
                  const SizedBox(height: 4),
                  LinearProgressIndicator(
                    value: progress,
                    color: VColors.gold,
                    backgroundColor: VColors.card2,
                  ),
                ],
              ],
            ),
          ),
          IconButton(
            tooltip: uploading ? 'Остановить загрузку' : 'Убрать файл',
            onPressed: uploading
                ? onCancel
                : sending
                ? null
                : onRemove,
            icon: const Icon(Icons.close, color: VColors.muted, size: 20),
          ),
        ],
      ),
    );
  }
}
