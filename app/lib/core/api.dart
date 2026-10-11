import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';

/// Адрес API. По умолчанию — локальный сервер для эмулятора Android
/// (10.0.2.2 — это «localhost» вашего компьютера изнутри эмулятора).
/// Свой адрес: flutter run --dart-define=API_URL=https://api-test.вечер.online
const apiUrl = String.fromEnvironment('API_URL', defaultValue: 'http://10.0.2.2:8090');

const _tokenKey = 'vecher_token';
final secureStorage = const FlutterSecureStorage();

Future<String?> readToken() => secureStorage.read(key: _tokenKey);
Future<void> saveToken(String token) => secureStorage.write(key: _tokenKey, value: token);
Future<void> clearToken() => secureStorage.delete(key: _tokenKey);

final dioProvider = Provider<Dio>((ref) {
  final dio = Dio(BaseOptions(
    baseUrl: apiUrl,
    connectTimeout: const Duration(seconds: 10),
    receiveTimeout: const Duration(seconds: 15),
  ));
  dio.interceptors.add(InterceptorsWrapper(onRequest: (options, handler) async {
    final token = await readToken();
    if (token != null) options.headers['Authorization'] = 'Bearer $token';
    handler.next(options);
  }));
  return dio;
});

/// Понятный текст ошибки для пользователя (сервер отдаёт его в поле detail).
String errorText(Object e) {
  if (e is DioException) {
    final data = e.response?.data;
    if (data is Map && data['detail'] is String) return data['detail'] as String;
    if (e.type == DioExceptionType.connectionError ||
        e.type == DioExceptionType.connectionTimeout) {
      return 'Нет связи с сервером';
    }
  }
  return 'Что-то пошло не так, попробуйте ещё раз';
}
