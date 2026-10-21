import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/features/chat/attachment_models.dart';
import 'package:vecher_app/features/chat/chat_attachments.dart';

void main() {
  group('Attachment boundaries', () {
    test('private file ids cannot turn into URLs or traversal paths', () {
      for (final id in [
        '../token',
        'https://example.invalid/secret',
        'a/b',
        'a?token=x',
        '',
      ]) {
        expect(() => chatAttachmentPath(id), throwsA(isA<ChatFileException>()));
      }
      expect(
        chatAttachmentPath('44b57455-0aa2-4e56-bbbe-6ac718be1412'),
        '/chat/attachments/44b57455-0aa2-4e56-bbbe-6ac718be1412',
      );
    });

    test(
      'download names remain readable without escaping the cache directory',
      () {
        expect(
          safeChatFilename('../../Планы на вечер.pdf'),
          'Планы на вечер.pdf',
        );
        expect(safeChatFilename(r'C:\Users\someone\CON.txt'), '_CON.txt');
        expect(safeChatFilename('..'), 'Файл');
        expect(safeChatFilename('file\u202epdf.exe'), 'filepdf.exe');
        final long = safeChatFilename('${List.filled(150, '🌙').join()}.pdf');
        expect(utf8.encode(long).length, lessThanOrEqualTo(180));
        expect(long.endsWith('.pdf'), isTrue);
      },
    );

    test(
      'empty and exact-size uploads are valid; changing files are rejected',
      () async {
        expect(
          await checkedChatFileStream(const Stream.empty(), 0).toList(),
          isEmpty,
        );
        final exact = await checkedChatFileStream(
          Stream.fromIterable([
            [1, 2],
            [3],
          ]),
          3,
        ).toList();
        expect(exact.expand((c) => c), [1, 2, 3]);
        await expectLater(
          checkedChatFileStream(Stream.value([1, 2]), 1).drain<void>(),
          throwsA(isA<ChatFileException>()),
        );
        await expectLater(
          checkedChatFileStream(Stream.value([1]), 2).drain<void>(),
          throwsA(isA<ChatFileException>()),
        );
        await expectLater(
          checkedChatFileStream(
            const Stream.empty(),
            maxChatAttachmentBytes + 1,
          ).drain<void>(),
          throwsA(isA<ChatFileException>()),
        );
      },
    );

    test('oversized server metadata is rejected before downloading', () {
      expect(
        () => ChatAttachment.fromJson({
          'id': 'attachment-1',
          'name': 'large.zip',
          'mime': 'application/zip',
          'size': maxChatAttachmentBytes + 1,
        }),
        throwsA(isA<ChatFileException>()),
      );
      expect(chatFileSizeLabel(maxChatAttachmentBytes), '25 МБ');
      expect(chatFileSizeLabel(1536), '1,5 КБ');
    });
  });

  test(
    'an upload retry can reread bytes; completed uploads are reused',
    () async {
      final adapter = _UploadAdapter();
      final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
        ..httpClientAdapter = adapter;
      final selected = SelectedChatFile(
        file: PlatformFile(
          name: 'план.txt',
          size: 4,
          bytes: Uint8List.fromList([11, 22, 33, 44]),
        ),
        name: 'план.txt',
        size: 4,
      );
      await expectLater(
        uploadChatFile(
          dio,
          selected,
          cancelToken: CancelToken(),
          onProgress: (_, __) {},
        ),
        throwsA(isA<DioException>()),
      );
      expect(selected.uploaded, isNull);
      final result = await uploadChatFile(
        dio,
        selected,
        cancelToken: CancelToken(),
        onProgress: (_, __) {},
      );
      expect(result.id, 'upload-1');
      expect(adapter.payloads.length, 2);
      for (final bytes in adapter.payloads) {
        expect(_contains(bytes, [11, 22, 33, 44]), isTrue);
      }
      final reused = await uploadChatFile(
        dio,
        selected,
        cancelToken: CancelToken(),
        onProgress: (_, __) {},
      );
      expect(reused.id, 'upload-1');
      expect(adapter.payloads.length, 2);
      dio.close();
    },
  );
}

bool _contains(List<int> haystack, List<int> needle) {
  for (var i = 0; i <= haystack.length - needle.length; i++) {
    var match = true;
    for (var j = 0; j < needle.length; j++) {
      if (haystack[i + j] != needle[j]) match = false;
    }
    if (match) return true;
  }
  return false;
}

class _UploadAdapter implements HttpClientAdapter {
  final List<List<int>> payloads = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    expect(options.path, '/chat/attachments');
    final bytes = <int>[];
    if (requestStream != null) {
      await for (final chunk in requestStream) {
        bytes.addAll(chunk);
      }
    }
    payloads.add(bytes);
    if (payloads.length == 1) {
      throw DioException(
        requestOptions: options,
        type: DioExceptionType.connectionError,
      );
    }
    return ResponseBody.fromString(
      jsonEncode({
        'id': 'upload-1',
        'name': 'план.txt',
        'mime': 'text/plain',
        'size': 4,
      }),
      200,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}
