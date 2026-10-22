import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart' show kIsWeb, kReleaseMode;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import 'session_changes.dart';
import 'network_client.dart';
import 'network_endpoints.dart';

/// Адрес API. По умолчанию — локальный сервер:
/// в браузере это localhost, в эмуляторе Android — 10.0.2.2 («localhost» вашего ПК изнутри эмулятора).
/// Свой адрес: flutter run --dart-define=API_URL=https://api-test.вечер.online
const _apiUrlOverride = String.fromEnvironment('API_URL');
const _apiAlternatives = String.fromEnvironment('API_URLS', defaultValue: '[]');
String get apiUrl => _apiUrlOverride.isNotEmpty
    ? _apiUrlOverride
    : (kIsWeb ? 'http://localhost:8090' : 'http://10.0.2.2:8090');

/// No fallback host is assumed. A release supplies only deployed mirrors.
final apiEndpoints = ApiEndpoints(
  primary: apiUrl,
  alternativesJson: _apiAlternatives,
  allowLocalHttp: !kReleaseMode,
);

final apiEndpointsProvider = Provider<ApiEndpoints>((ref) => apiEndpoints);

/// Public endpoints use the same routes and trust policy, without credentials.
Dio createPublicApiDio() => createApiDio(endpoints: apiEndpoints);

const _tokenKey = 'vecher_token';
const secureStorage = FlutterSecureStorage();

Future<String?> readToken() => secureStorage.read(key: _tokenKey);
Future<void> saveToken(String token) async {
  final revision = sessionChanges.begin();
  try {
    await secureStorage.write(key: _tokenKey, value: token);
  } finally {
    sessionChanges.finish(revision);
  }
}

Future<void> clearToken() async {
  final revision = sessionChanges.begin();
  try {
    await secureStorage.delete(key: _tokenKey);
  } finally {
    sessionChanges.finish(revision);
  }
}

// Код из ссылки-приглашения (https://вечер.online/i/КОД): помним, пока человек входит и регистрируется.
const _pendingInviteKey = 'vecher_pending_invite';
Future<String?> readPendingInvite() =>
    secureStorage.read(key: _pendingInviteKey);
Future<void> savePendingInvite(String code) =>
    secureStorage.write(key: _pendingInviteKey, value: code);
Future<void> clearPendingInvite() =>
    secureStorage.delete(key: _pendingInviteKey);

final dioProvider = Provider<Dio>((ref) {
  final dio = createApiDio(
    endpoints: ref.read(apiEndpointsProvider),
    readToken: readToken,
    sessionRevision: () => sessionChanges.revision,
    sessionChanging: () => sessionChanges.isChanging,
  );
  ref.onDispose(() => dio.close(force: true));
  return dio;
});

/// Понятный текст ошибки для пользователя (сервер отдаёт его в поле detail).
String errorText(Object e) {
  if (e is DioException) {
    final data = e.response?.data;
    if (data is Map && data['detail'] is String) {
      return data['detail'] as String;
    }
    if (e.type == DioExceptionType.connectionError ||
        e.type == DioExceptionType.connectionTimeout ||
        e.type == DioExceptionType.receiveTimeout ||
        e.type == DioExceptionType.sendTimeout ||
        e.error is ApiRequestTimeout) {
      return 'Нет связи с сервером. Проверь подключение и попробуй ещё раз';
    }
  }
  return 'Что-то пошло не так, попробуй ещё раз';
}
