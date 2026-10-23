import 'dart:async';

import 'package:dio/dio.dart';

import 'network_client.dart';
import 'network_diagnostics_platform_stub.dart'
    if (dart.library.io) 'network_diagnostics_platform_io.dart';
import 'network_endpoints.dart';

/// A person's explicit label, never an inferred VPN or network configuration.
enum DiagnosticVpnState {
  unspecified('Не указан', 'UNSPECIFIED'),
  on('Включён', 'ON'),
  off('Выключен', 'OFF');

  const DiagnosticVpnState(this.label, this.code);
  final String label, code;
}

enum ConnectionDiagnosticStatus {
  healthy('Сервер Вечера доступен', 'HEALTH_OK'),
  invalidReply('Сервер ответил, но проверка API не прошла', 'INVALID_REPLY'),
  httpError('Сервер ответил с ошибкой HTTP', 'HTTP_ERROR'),
  timeout('Сервер не ответил вовремя', 'TIMEOUT'),
  tlsFailure('Не удалось установить защищённое соединение', 'TLS_ERROR'),
  networkFailure('Не удалось соединиться: сеть или DNS', 'NETWORK_OR_DNS'),
  cancelled('Проверка остановлена', 'CANCELLED'),
  unknownFailure('Не удалось выполнить проверку', 'UNKNOWN_ERROR');

  const ConnectionDiagnosticStatus(this.label, this.code);
  final String label, code;
}

/// Contains only a compiled public origin, numeric timings/status and an enum.
/// Never retain a Response, RequestOptions, exception, header or server body.
class ConnectionDiagnostic {
  const ConnectionDiagnostic._({
    required this.initialOrigin,
    required this.status,
    required this.elapsed,
    this.attemptedOrigin,
    this.httpStatus,
  });

  final Uri initialOrigin;
  final Uri? attemptedOrigin;
  final ConnectionDiagnosticStatus status;
  final Duration elapsed;
  final int? httpStatus;

  Uri get healthUrl => (attemptedOrigin ?? initialOrigin).resolve('/health');

  String report({required String appVersion, required DiagnosticVpnState vpn}) {
    final safeVersion =
        RegExp(r'^[A-Za-z0-9.()+ _-]{1,64}$').hasMatch(appVersion)
        ? appVersion
        : 'UNKNOWN';
    return [
      'VECHER_CONNECTION_DIAGNOSTIC=1',
      'APP_VERSION=$safeVersion',
      'VPN_SELECTED_BY_USER=${vpn.code}',
      'START_API_ORIGIN=$initialOrigin',
      if (attemptedOrigin != null) 'LAST_API_ORIGIN=$attemptedOrigin',
      'HEALTH_URL=$healthUrl',
      'RESULT=${status.code}',
      'HTTP_STATUS=${httpStatus ?? 'NONE'}',
      'ELAPSED_MS=${elapsed.inMilliseconds}',
    ].join('\n');
  }
}

class _ProbeDeadline implements Exception {
  const _ProbeDeadline();
}

/// One manual, anonymous /health check through the same transport as the API.
/// The outer deadline also covers a stuck adapter/interceptor. No writes or
/// alternate network bindings are introduced; configured safe GET retries stay.
Future<ConnectionDiagnostic> diagnoseApiConnection(
  Dio dio, {
  CancelToken? cancelToken,
  Duration budget = const Duration(seconds: 20),
}) async {
  final endpoints = endpointsOf(dio);
  if (endpoints == null) {
    throw ArgumentError('Diagnostics require the trusted API client');
  }
  if (budget <= Duration.zero || budget > const Duration(seconds: 25)) {
    throw ArgumentError.value(budget, 'budget', 'Must be within 25 seconds');
  }
  final initialOrigin = endpoints.selected;
  final cancel = CancelToken();
  if (cancelToken?.isCancelled == true) cancel.cancel();
  cancelToken?.whenCancel.then((_) {
    if (!cancel.isCancelled) cancel.cancel();
  });
  final watch = Stopwatch()..start();
  var status = ConnectionDiagnosticStatus.unknownFailure;
  Uri? attemptedOrigin;
  int? httpStatus;
  try {
    final response = await probeApiHealth(dio, cancelToken: cancel).timeout(
      budget,
      onTimeout: () {
        if (!cancel.isCancelled) cancel.cancel();
        throw const _ProbeDeadline();
      },
    );
    attemptedOrigin = _trustedOrigin(endpoints, response.requestOptions.uri);
    httpStatus = _httpStatus(response.statusCode);
    status = response.data is Map && response.data['ok'] == true
        ? ConnectionDiagnosticStatus.healthy
        : ConnectionDiagnosticStatus.invalidReply;
  } on _ProbeDeadline {
    status = ConnectionDiagnosticStatus.timeout;
  } on DioException catch (error) {
    attemptedOrigin = _trustedOrigin(endpoints, error.requestOptions.uri);
    httpStatus = _httpStatus(error.response?.statusCode);
    if (error.response != null) {
      status = ConnectionDiagnosticStatus.httpError;
    } else if (error.type == DioExceptionType.badCertificate ||
        isTlsFailure(error.error)) {
      status = ConnectionDiagnosticStatus.tlsFailure;
    } else if ({
          DioExceptionType.connectionTimeout,
          DioExceptionType.sendTimeout,
          DioExceptionType.receiveTimeout,
        }.contains(error.type) ||
        error.error is ApiRequestTimeout) {
      status = ConnectionDiagnosticStatus.timeout;
    } else if (error.type == DioExceptionType.cancel) {
      status = ConnectionDiagnosticStatus.cancelled;
    } else if (error.type == DioExceptionType.connectionError ||
        isSocketFailure(error.error)) {
      // A generic socket failure cannot reliably distinguish DNS, routing,
      // firewall, a VPN rule, or an unavailable server on every OS.
      status = ConnectionDiagnosticStatus.networkFailure;
    }
  } catch (_) {
    // No exception message is copied into either the screen or the report.
    status = ConnectionDiagnosticStatus.unknownFailure;
  } finally {
    watch.stop();
  }
  return ConnectionDiagnostic._(
    initialOrigin: initialOrigin,
    attemptedOrigin: attemptedOrigin,
    status: status,
    elapsed: watch.elapsed,
    httpStatus: httpStatus,
  );
}

int? _httpStatus(int? value) =>
    value != null && value >= 100 && value <= 599 ? value : null;

Uri? _trustedOrigin(ApiEndpoints endpoints, Uri uri) {
  if (!endpoints.trusts(uri)) return null;
  return endpoints.origins.firstWhere(
    (origin) => ApiEndpoints.sameOrigin(origin, uri),
  );
}
