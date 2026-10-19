import 'call_media_exception.dart';

/// Keep the API/native boundary explicit: Android's IceServer builder rejects
/// a present-but-null username/password, even for a STUN server without auth.
/// Browser WebRTC accepting that JSON does not make it a valid native payload.
List<Map<String, dynamic>> normalizeIceServers(Object? value) {
  if (value is! List) _invalidConfiguration();

  final result = <Map<String, dynamic>>[];
  for (final item in value) {
    if (item is! Map) _invalidConfiguration();
    final rawUrls = item['urls'];
    final values = rawUrls is String ? <Object?>[rawUrls] : rawUrls;
    if (values is! List || values.isEmpty) _invalidConfiguration();

    final urls = <String>[];
    var needsCredentials = false;
    for (final value in values) {
      if (value is! String) _invalidConfiguration();
      final url = value.trim();
      if (!_iceUrl.hasMatch(url)) _invalidConfiguration();
      final scheme = url.substring(0, url.indexOf(':')).toLowerCase();
      needsCredentials =
          needsCredentials || scheme == 'turn' || scheme == 'turns';
      urls.add(url);
    }

    final username = _optionalCredential(item['username']);
    final credential = _optionalCredential(item['credential']);
    if (needsCredentials && (username == null || credential == null)) {
      _invalidConfiguration();
    }
    result.add(
      Map<String, dynamic>.unmodifiable({
        'urls': List<String>.unmodifiable(urls),
        if (username != null) 'username': username,
        if (credential != null) 'credential': credential,
      }),
    );
  }
  return List<Map<String, dynamic>>.unmodifiable(result);
}

final _iceUrl = RegExp(
  r'^(stun|stuns|turn|turns):[^\s]+$',
  caseSensitive: false,
);

String? _optionalCredential(Object? value) {
  if (value == null) return null;
  if (value is! String) _invalidConfiguration();
  // Credentials are opaque, case-sensitive values. Check for missing data, but
  // preserve every byte of a valid username/password instead of trimming it.
  return value.trim().isEmpty ? null : value;
}

Never _invalidConfiguration() => throw const CallMediaException(
  'Не удалось получить настройки соединения. Попробуй позвонить ещё раз.',
  stage: 'ice_configuration',
);
