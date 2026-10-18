import 'dart:convert';
import 'dart:math';

const maxChatAttachmentBytes = 25 * 1024 * 1024;

class ChatFileException implements Exception {
  const ChatFileException(this.message);
  final String message;

  @override
  String toString() => message;
}

class ChatAttachment {
  const ChatAttachment({
    required this.id,
    required this.name,
    required this.mime,
    required this.size,
  });

  factory ChatAttachment.fromJson(Map data) {
    final id = data['id'];
    final name = data['name'];
    final size = data['size'];
    if (id is! String ||
        name is! String ||
        size is! int ||
        size < 0 ||
        size > maxChatAttachmentBytes) {
      throw const ChatFileException(
        'Сервер вернул некорректные сведения о файле',
      );
    }
    chatAttachmentPath(id);
    return ChatAttachment(
      id: id,
      name: safeChatFilename(name),
      mime: data['mime'] is String
          ? data['mime'] as String
          : 'application/octet-stream',
      size: size,
    );
  }

  final String id;
  final String name;
  final String mime;
  final int size;
}

/// A server identifier may never become an arbitrary URL or a local path.
String chatAttachmentPath(String id) {
  if (!RegExp(r'^[A-Za-z0-9_-]{1,128}$').hasMatch(id)) {
    throw const ChatFileException('Не получилось определить файл. Обнови чат');
  }
  return '/chat/attachments/$id';
}

String chatFileSizeLabel(int bytes) {
  if (bytes < 1024) return '$bytes Б';
  final unit = bytes < 1024 * 1024 ? 1024 : 1024 * 1024;
  final value = bytes / unit;
  final number = value
      .toStringAsFixed(value == value.truncateToDouble() ? 0 : 1)
      .replaceAll('.', ',');
  return '$number ${unit == 1024 ? 'КБ' : 'МБ'}';
}

/// Keep Unicode names readable while excluding traversal/control characters.
/// Limit UTF-8 bytes (not just Dart code units) to fit mobile filesystem names.
String safeChatFilename(String input) {
  var name = input
      .replaceAll('\\', '/')
      .split('/')
      .last
      .replaceAll(RegExp(r'[<>:"/\\|?*\x00-\x1f\x7f]'), '_')
      .replaceAll(RegExp('[\u202a-\u202e\u2066-\u2069]'), '')
      .trim()
      .replaceAll(RegExp(r'[. ]+$'), '');
  if (name.isEmpty || name == '.' || name == '..') return 'Файл';
  final stem = name.split('.').first.toUpperCase();
  if (RegExp(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$').hasMatch(stem)) {
    name = '_$name';
  }
  if (utf8.encode(name).length <= 180) return name;
  final dot = name.lastIndexOf('.');
  final suffix = dot > 0 && utf8.encode(name.substring(dot)).length <= 20
      ? name.substring(dot)
      : '';
  final budget = 180 - utf8.encode(suffix).length;
  final out = StringBuffer();
  var used = 0;
  final base = suffix.isEmpty ? name : name.substring(0, dot);
  for (final rune in base.runes) {
    final char = String.fromCharCode(rune);
    final length = utf8.encode(char).length;
    if (used + length > budget) break;
    out.write(char);
    used += length;
  }
  return '$out$suffix';
}

/// A retry keeps this identifier until the server acknowledges the message.
String newChatMessageId() {
  final random = Random.secure();
  return List.generate(
    16,
    (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
  ).join();
}

/// A picker may report a stale length. Enforce the limit while reading too.
Stream<List<int>> checkedChatFileStream(
  Stream<List<int>> source,
  int expectedBytes,
) async* {
  if (expectedBytes < 0 || expectedBytes > maxChatAttachmentBytes) {
    throw const ChatFileException('Можно отправить файл до 25 МБ');
  }
  var read = 0;
  await for (final chunk in source) {
    read += chunk.length;
    if (read > maxChatAttachmentBytes || read > expectedBytes) {
      throw const ChatFileException(
        'Файл изменился или больше 25 МБ. Выбери его заново',
      );
    }
    yield chunk;
  }
  if (read != expectedBytes) {
    throw const ChatFileException('Файл изменился. Выбери его заново');
  }
}
