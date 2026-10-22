import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:crypto/crypto.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/network_client.dart';
import 'package:vecher_app/core/network_endpoints.dart';
import 'package:vecher_app/core/network_update.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter(this.body);
  final ResponseBody Function() body;
  final seen = <RequestOptions>[];
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? request,
    Future<void>? cancelFuture,
  ) async {
    seen.add(options);
    return body();
  }

  @override
  void close({bool force = false}) {}
}

Map<String, Object> _metadata(List<int> bytes) {
  final hash = sha256.convert(bytes).toString();
  return {
    'version_code': 11,
    'version_name': '0.6.0',
    'notes': 'Новое',
    'required': false,
    'sha256': hash, 'bytes': bytes.length,
    'download_path': '/download/vecher.apk?sha256=$hash',
    // This legacy field is deliberately ignored by the verified downloader.
    'url': 'https://foreign.test/unsafe.apk',
  };
}

void main() {
  final bytes = <int>[
    80,
    75,
    3,
    4,
    ...List.generate(1024, (index) => index % 256),
  ];
  late Directory folder;
  late Dio dio;
  late _Adapter adapter;

  setUp(() async {
    folder = await Directory.systemTemp.createTemp('vecher-update-test-');
    adapter = _Adapter(() => ResponseBody.fromBytes(bytes, 200));
    dio = createApiDio(
      endpoints: ApiEndpoints(primary: 'https://api.example.test'),
      adapter: adapter,
    );
  });
  tearDown(() async {
    dio.close(force: true);
    if (await folder.exists()) await folder.delete(recursive: true);
  });

  test(
    'manifest requires a bounded exact hash and a pinned relative route',
    () {
      final valid = _metadata(bytes);
      expect(ApkUpdateManifest.parse(valid).versionCode, 11);
      final variants = <Map<String, Object?>>[
        {...valid, 'bytes': maxUpdateBytes + 1},
        {...valid, 'bytes': -1},
        {...valid, 'sha256': 'bad'},
        {...valid, 'sha256': null},
        {...valid, 'required': 'false'},
        {...valid, 'version_code': 11.0},
        {...valid, 'download_path': 'https://foreign.test/download/vecher.apk'},
        {...valid, 'download_path': '//foreign.test/download/vecher.apk'},
        {...valid, 'download_path': '/chat/file?sha256=${valid['sha256']}'},
        {...valid, 'download_path': '/download/vecher.apk?sha256=${'0' * 64}'},
        {
          ...valid,
          'download_path':
              '${valid['download_path']}&sha256=${valid['sha256']}',
        },
        {...valid, 'download_path': '${valid['download_path']}#fragment'},
      ];
      for (final variant in variants) {
        expect(() => ApkUpdateManifest.parse(variant), throwsFormatException);
      }
    },
  );

  test('complete bytes are verified and renamed before returning an installable file', () async {
    final progress = <double>[];
    final file = await downloadVerifiedApk(
      manifest: ApkUpdateManifest.parse(_metadata(bytes)),
      dio: dio,
      directory: folder,
      onProgress: progress.add,
    );
    expect(await file.readAsBytes(), bytes);
    expect(file.path.endsWith('.apk'), isTrue);
    expect(progress.last, 1);
    expect(adapter.seen.single.uri.host, 'api.example.test');
    expect(
      adapter.seen.single.headers.keys.any(
        (key) => key.toLowerCase() == 'authorization',
      ),
      isFalse,
    );
    expect(
      (await folder.list(recursive: true).toList()).whereType<File>().length,
      1,
    );
  });

  for (final scenario in ['hash', 'truncated', 'oversized', 'length-header']) {
    test('$scenario failure removes all partial files', () async {
      final changed = switch (scenario) {
        'hash' => <int>[0, ...bytes.skip(1)],
        'truncated' => bytes.sublist(0, bytes.length - 1),
        'oversized' => [...bytes, 0],
        _ => bytes,
      };
      dio.httpClientAdapter = _Adapter(
        () => ResponseBody.fromBytes(
          changed,
          200,
          headers: scenario == 'length-header'
              ? {
                  Headers.contentLengthHeader: ['99'],
                }
              : null,
        ),
      );
      await expectLater(
        downloadVerifiedApk(
          manifest: ApkUpdateManifest.parse(_metadata(bytes)),
          dio: dio,
          directory: folder,
        ),
        throwsA(isA<UpdateIntegrityException>()),
      );
      expect(await folder.list().toList(), isEmpty);
    });
  }

  test(
    'cancel after headers aborts a streaming body and cleans the partial file',
    () async {
      final stream = StreamController<Uint8List>();
      dio.httpClientAdapter = _Adapter(() {
        scheduleMicrotask(
          () => stream.add(Uint8List.fromList(bytes.take(8).toList())),
        );
        return ResponseBody(stream.stream, 200);
      });
      final cancel = CancelToken();
      await expectLater(
        downloadVerifiedApk(
          manifest: ApkUpdateManifest.parse(_metadata(bytes)),
          dio: dio,
          directory: folder,
          cancelToken: cancel,
          onProgress: (_) => cancel.cancel('user cancelled'),
        ),
        throwsA(isA<DioException>()),
      );
      expect(await folder.list().toList(), isEmpty);
      await stream.close();
    },
  );

  test(
    'a total deadline ends a stalled body after successful headers',
    () async {
      final stream = StreamController<Uint8List>();
      dio.httpClientAdapter = _Adapter(() => ResponseBody(stream.stream, 200));
      await expectLater(
        downloadVerifiedApk(
          manifest: ApkUpdateManifest.parse(_metadata(bytes)),
          dio: dio,
          directory: folder,
          budget: const Duration(milliseconds: 50),
        ),
        throwsA(isA<DioException>()),
      );
      expect(await folder.list().toList(), isEmpty);
      await stream.close();
    },
  );

  test('redirected APKs are not downloaded or opened', () async {
    final redirect = _Adapter(
      () => ResponseBody.fromString(
        '',
        302,
        headers: {
          'location': ['https://foreign.test/file.apk'],
        },
      ),
    );
    dio.httpClientAdapter = redirect;
    await expectLater(
      downloadVerifiedApk(
        manifest: ApkUpdateManifest.parse(_metadata(bytes)),
        dio: dio,
        directory: folder,
      ),
      throwsA(isA<DioException>()),
    );
    expect(redirect.seen.length, 1);
    expect(redirect.seen.single.followRedirects, isFalse);
    expect(await folder.list().toList(), isEmpty);
  });
}
