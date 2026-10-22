import 'dart:async';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:dio/dio.dart';

import 'network_client.dart';

const maxUpdateBytes = 200 * 1024 * 1024;

class ApkUpdateManifest {
  const ApkUpdateManifest._({
    required this.versionCode,
    required this.versionName,
    required this.notes,
    required this.required,
    required this.sha256,
    required this.bytes,
    required this.downloadPath,
  });

  factory ApkUpdateManifest.parse(Object? value) {
    if (value is! Map) throw const FormatException('Invalid update manifest');
    final code = value['version_code'];
    final name = value['version_name'];
    final notes = value['notes'] ?? '';
    final required = value['required'] ?? false;
    final hash = value['sha256'];
    final bytes = value['bytes'];
    final download = value['download_path'];
    final path = download is String ? Uri.tryParse(download) : null;
    if (code is! int ||
        code < 1 ||
        code > 2147483647 ||
        name is! String ||
        name.isEmpty ||
        name.length > 32 ||
        notes is! String ||
        notes.length > 10000 ||
        required is! bool ||
        hash is! String ||
        !RegExp(r'^[0-9a-f]{64}$').hasMatch(hash) ||
        bytes is! int ||
        bytes < 2 ||
        bytes > maxUpdateBytes ||
        path == null ||
        path.hasScheme ||
        path.hasAuthority ||
        path.hasFragment ||
        path.path != '/download/vecher.apk' ||
        path.queryParametersAll.length != 1 ||
        path.queryParametersAll['sha256']?.length != 1 ||
        path.queryParameters['sha256'] != hash) {
      throw const FormatException('Update cannot be verified');
    }
    return ApkUpdateManifest._(
      versionCode: code,
      versionName: name,
      notes: notes,
      required: required,
      sha256: hash,
      bytes: bytes,
      // Construct the route, rather than trusting host/escaping from metadata.
      downloadPath: '/download/vecher.apk?sha256=$hash',
    );
  }

  final int versionCode, bytes;
  final String versionName, notes, sha256, downloadPath;
  final bool required;
}

class UpdateIntegrityException implements Exception {
  const UpdateIntegrityException();
}

class _DigestResult implements Sink<Digest> {
  Digest? value;
  @override
  void add(Digest data) => value = data;
  @override
  void close() {}
}

/// Returns a complete, size/hash-verified file. The installer is a separate step.
/// Each attempt owns its temporary directory; failed or cancelled files are removed.
Future<File> downloadVerifiedApk({
  required ApkUpdateManifest manifest,
  required Dio dio,
  required Directory directory,
  CancelToken? cancelToken,
  void Function(double progress)? onProgress,
  Duration budget = const Duration(minutes: 5),
}) async {
  if (endpointsOf(dio) == null) {
    throw ArgumentError('APK downloads require the trusted API client');
  }
  final cancel = CancelToken();
  bool finished = false;
  bool expired = false;
  final timer = Timer(budget, () {
    expired = true;
    if (!cancel.isCancelled) cancel.cancel('Update download deadline');
  });
  cancelToken?.whenCancel.then((_) {
    if (!finished && !cancel.isCancelled) cancel.cancel('Update cancelled');
  });
  if (cancelToken?.isCancelled == true) cancel.cancel('Update cancelled');
  Directory? attempt;
  RandomAccessFile? output;
  try {
    attempt = await directory.createTemp('vecher-update-');
    final partial = File(
      '${attempt.path}/vecher-${manifest.versionCode}.apk.part',
    );
    final response = await dio.get<ResponseBody>(
      manifest.downloadPath,
      options: Options(
        responseType: ResponseType.stream,
        receiveTimeout: const Duration(seconds: 30),
        followRedirects: false,
      ),
      cancelToken: cancel,
    );
    final body = response.data;
    if (response.statusCode != 200 || body == null) {
      throw const UpdateIntegrityException();
    }
    final contentLength = response.headers.value(Headers.contentLengthHeader);
    if (contentLength != null &&
        int.tryParse(contentLength) != manifest.bytes) {
      throw const UpdateIntegrityException();
    }
    output = await partial.open(mode: FileMode.write);
    final result = _DigestResult();
    final digest = sha256.startChunkedConversion(result);
    int received = 0;
    await for (final chunk in body.stream) {
      if (cancel.isCancelled) throw cancel.cancelError!;
      received += chunk.length;
      if (received > manifest.bytes) throw const UpdateIntegrityException();
      digest.add(chunk);
      await output.writeFrom(chunk);
      onProgress?.call(received / manifest.bytes);
    }
    digest.close();
    await output.close();
    output = null;
    if (cancel.isCancelled || expired) throw cancel.cancelError!;
    if (received != manifest.bytes ||
        result.value.toString() != manifest.sha256) {
      throw const UpdateIntegrityException();
    }
    final complete = await partial.rename(
      '${attempt.path}/vecher-${manifest.versionCode}.apk',
    );
    if (cancel.isCancelled || expired) throw cancel.cancelError!;
    return complete;
  } catch (_) {
    if (!cancel.isCancelled) cancel.cancel('Update download stopped');
    await output?.close();
    output = null;
    if (attempt != null && await attempt.exists()) {
      await attempt.delete(recursive: true);
    }
    rethrow;
  } finally {
    finished = true;
    timer.cancel();
  }
}
