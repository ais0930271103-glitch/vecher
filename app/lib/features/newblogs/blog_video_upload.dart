import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/network_client.dart';
import '../../core/session_changes.dart';
import 'blogs.dart';

class BlogVideoException implements Exception {
  const BlogVideoException(this.message);
  final String message;
  @override
  String toString() => message;
}

class SelectedBlogVideo {
  SelectedBlogVideo({required this.file, this.browserBytes, String? requestId})
    : requestId = requestId ?? newBlogRequestId();
  final PlatformFile file;
  final Uint8List? browserBytes;
  final String requestId;
  BlogMedia? uploaded;
  int get size => file.size;
  String get name => file.name;
  String get mime =>
      name.toLowerCase().endsWith('.mov') ? 'video/quicktime' : 'video/mp4';
  Stream<List<int>> openRead() {
    final bytes = browserBytes ?? file.bytes;
    if (bytes != null) return Stream.value(bytes);
    if (!kIsWeb && file.path != null) return file.xFile.openRead();
    throw const BlogVideoException(
      'Не удалось прочитать видео. Выбери файл заново.',
    );
  }
}

Stream<List<int>> checkedBlogVideoStream(
  Stream<List<int>> source,
  int expected,
) async* {
  if (expected <= 0 || expected > maxBlogVideoBytes) {
    throw const BlogVideoException('Выбери непустое видео размером до 100 МБ.');
  }
  var read = 0;
  await for (final chunk in source) {
    read += chunk.length;
    if (read > expected || read > maxBlogVideoBytes) {
      throw const BlogVideoException(
        'Файл изменился или стал больше 100 МБ. Выбери видео заново.',
      );
    }
    yield chunk;
  }
  if (read != expected) {
    throw const BlogVideoException('Файл изменился. Выбери видео заново.');
  }
}

Future<SelectedBlogVideo?> pickBlogVideo() async {
  final result = await FilePicker.pickFiles(
    dialogTitle: 'Выбрать видео для блога',
    type: FileType.custom,
    allowedExtensions: const ['mp4', 'mov'],
    allowMultiple: false,
    withData: false,
    withReadStream: true,
  );
  if (result == null || result.files.isEmpty) return null;
  final file = result.files.single;
  if (file.size <= 0 || file.size > maxBlogVideoBytes) {
    throw const BlogVideoException('Можно загрузить непустое видео до 100 МБ.');
  }
  if (!RegExp(r'\.(mp4|mov)$', caseSensitive: false).hasMatch(file.name)) {
    throw const BlogVideoException('Выбери видео в формате MP4 или MOV.');
  }
  Uint8List? bytes;
  if (kIsWeb) {
    final stream = file.readStream;
    if (stream == null) {
      throw const BlogVideoException('Не удалось прочитать видео в браузере.');
    }
    final buffer = BytesBuilder(copy: false);
    await for (final chunk in checkedBlogVideoStream(stream, file.size)) {
      buffer.add(chunk);
    }
    bytes = buffer.takeBytes();
  }
  return SelectedBlogVideo(file: file, browserBytes: bytes);
}

final blogVideoPickerProvider = Provider<Future<SelectedBlogVideo?> Function()>(
  (_) => pickBlogVideo,
);

/// Uploads have a larger, still bounded request budget. They retain the same
/// trusted origins/session checks and are never automatically retried.
final blogUploadDioProvider = Provider.autoDispose<Dio>((ref) {
  final dio = createApiDio(
    endpoints: ref.read(apiEndpointsProvider),
    readToken: readToken,
    sessionRevision: () => sessionChanges.revision,
    sessionChanging: () => sessionChanges.isChanging,
    writeBudget: const Duration(minutes: 10),
  );
  ref.onDispose(() => dio.close(force: true));
  return dio;
});

Future<BlogMedia> uploadBlogVideo(
  Dio dio,
  String blogId,
  SelectedBlogVideo selected, {
  required CancelToken cancelToken,
  required ProgressCallback onProgress,
}) async {
  final existing = selected.uploaded;
  if (existing != null) return existing;
  final response = await dio.post<Map<String, dynamic>>(
    '/blogs/${Uri.encodeComponent(blogId)}/media',
    data: checkedBlogVideoStream(selected.openRead(), selected.size),
    options: Options(
      contentType: selected.mime,
      headers: {
        Headers.contentLengthHeader: selected.size,
        'Idempotency-Key': selected.requestId,
      },
      sendTimeout: const Duration(minutes: 10),
      receiveTimeout: const Duration(seconds: 45),
    ),
    cancelToken: cancelToken,
    onSendProgress: onProgress,
  );
  if (response.data == null) {
    throw const BlogVideoException(
      'Сервер не подтвердил загрузку. Повтори её.',
    );
  }
  final media = BlogMedia.fromJson(response.data!);
  selected.uploaded = media;
  return media;
}
