import 'dart:async';

import 'package:dio/dio.dart';

import 'network_endpoints.dart';

const _contextKey = 'vecher.network.request';
const _endpointsKey = 'vecher.network.endpoints';
const _anonymousProbeKey = 'vecher.network.anonymousProbe';
const _logoutGrantKey = 'vecher.network.logoutGrant';

class ApiRequestTimeout implements Exception {
  const ApiRequestTimeout();
}

ApiEndpoints? endpointsOf(Dio dio) =>
    dio.options.extra[_endpointsKey] as ApiEndpoints?;

/// One fixed cleanup operation while ordinary account traffic is suspended.
/// The private grant cannot be reused for another route or a later session.
Future<void> unregisterPushForLogout(
  Dio dio, {
  required String accessToken,
  required int revision,
  required String pushToken,
  required String platform,
  CancelToken? cancelToken,
}) async {
  final interceptor = dio.interceptors
      .whereType<_ApiInterceptor>()
      .singleOrNull;
  if (interceptor == null) {
    throw ArgumentError('Logout cleanup requires the trusted API client');
  }
  await interceptor.unregisterPush(
    accessToken: accessToken,
    revision: revision,
    pushToken: pushToken,
    platform: platform,
    cancelToken: cancelToken,
  );
}

class _LogoutGrant {
  _LogoutGrant(this.owner, this.token, this.revision);
  final _ApiInterceptor owner;
  final String token;
  final int revision;
  bool used = false;
  bool revoked = false;
}

/// Uses the OS network route, including its VPN. No proxy or TLS override.
/// Only GET/HEAD with no body may be replayed, on transport failures only.
Dio createApiDio({
  required ApiEndpoints endpoints,
  Future<String?> Function()? readToken,
  int Function()? sessionRevision,
  bool Function()? sessionChanging,
  HttpClientAdapter? adapter,
  Duration readBudget = const Duration(seconds: 25),
  Duration writeBudget = const Duration(minutes: 2),
}) {
  final dio = Dio(
    BaseOptions(
      baseUrl: endpoints.primary.toString(),
      connectTimeout: const Duration(seconds: 5),
      receiveTimeout: const Duration(seconds: 12),
      sendTimeout: const Duration(seconds: 45),
      followRedirects: false,
      extra: {_endpointsKey: endpoints},
    ),
  );
  if (adapter != null) dio.httpClientAdapter = adapter;
  final interceptor = _ApiInterceptor(
    dio: dio,
    endpoints: endpoints,
    readToken: readToken,
    sessionRevision: sessionRevision ?? () => 0,
    sessionChanging: sessionChanging ?? () => false,
    readBudget: readBudget,
    writeBudget: writeBudget,
  );
  dio.interceptors.add(interceptor);
  return dio;
}

class _Request {
  _Request({
    required this.uri,
    required this.origins,
    required this.endpointRevision,
    required this.sessionRevision,
    required this.retryable,
    required this.budget,
    required CancelToken? caller,
    required void Function() onExpired,
    this.logout,
  }) {
    timer = Timer(budget, () {
      expired = true;
      onExpired();
      if (!cancel.isCancelled) cancel.cancel(const ApiRequestTimeout());
    });
    caller?.whenCancel.then((_) {
      // Streaming downloads are still active after their headers were returned.
      if (!cancel.isCancelled) cancel.cancel('Request cancelled');
    });
    if (caller?.isCancelled == true) cancel.cancel('Request cancelled');
  }

  final Uri uri;
  final List<Uri> origins;
  final int endpointRevision, sessionRevision;
  final bool retryable;
  final Duration budget;
  final _LogoutGrant? logout;
  final CancelToken cancel = CancelToken();
  late final Timer timer;
  int attempt = 0;
  bool expired = false;
  bool finished = false;
  String? token;

  Uri get origin => origins[attempt];
  void finish() {
    finished = true;
    timer.cancel();
  }
}

class _ApiInterceptor extends Interceptor {
  _ApiInterceptor({
    required this.dio,
    required this.endpoints,
    required this.readToken,
    required this.sessionRevision,
    required this.sessionChanging,
    required this.readBudget,
    required this.writeBudget,
  });

  final Dio dio;
  final ApiEndpoints endpoints;
  final Future<String?> Function()? readToken;
  final int Function() sessionRevision;
  final bool Function() sessionChanging;
  final Duration readBudget, writeBudget;
  Future<void>? _healthProbe;

  Future<void> unregisterPush({
    required String accessToken,
    required int revision,
    required String pushToken,
    required String platform,
    CancelToken? cancelToken,
  }) async {
    final grant = _LogoutGrant(this, accessToken, revision);
    try {
      if (readToken == null ||
          accessToken.isEmpty ||
          !sessionChanging() ||
          sessionRevision() != revision) {
        throw DioException.requestCancelled(
          requestOptions: RequestOptions(path: '/push/unregister'),
          reason: 'Logout session ended',
        );
      }
      await dio.post(
        '/push/unregister',
        data: {'token': pushToken, 'platform': platform},
        options: Options(extra: {_logoutGrantKey: grant}),
        cancelToken: cancelToken,
      );
    } finally {
      grant.revoked = true;
    }
  }

  Future<void> _ensureWriteRoute() => _healthProbe ??= () async {
    try {
      final response = await dio.get(
        '/health',
        options: Options(extra: {_anonymousProbeKey: true}),
      );
      if (response.data is! Map || response.data['ok'] != true) {
        endpoints.unavailable(startingRevision: endpoints.revision);
        throw DioException(
          requestOptions: response.requestOptions,
          error: const FormatException('API health check failed'),
        );
      }
    } finally {
      _healthProbe = null;
    }
  }();

  bool _sessionValid(_Request state) {
    final logout = state.logout;
    if (logout != null) {
      return identical(logout.owner, this) &&
          !logout.revoked &&
          sessionChanging() &&
          sessionRevision() == logout.revision;
    }
    return readToken == null ||
        (!sessionChanging() && sessionRevision() == state.sessionRevision);
  }

  @override
  void onRequest(
    RequestOptions options,
    RequestInterceptorHandler handler,
  ) async {
    _Request? state = options.extra[_contextKey] as _Request?;
    if (state?.logout != null) {
      // Cleanup writes are single use; the request context must not become a
      // reusable authenticated capability if an observer retains its options.
      handler.reject(
        DioException.requestCancelled(
          requestOptions: options,
          reason: 'Logout cleanup cannot be replayed',
        ),
      );
      return;
    }
    try {
      if (state == null) {
        final uri = options.uri;
        if (options.path.startsWith('//') ||
            RegExp(r'[\\\x00-\x20\x7f]').hasMatch(options.path) ||
            !endpoints.trusts(uri)) {
          throw const FormatException('Untrusted API address');
        }
        final logout = options.extra[_logoutGrantKey] as _LogoutGrant?;
        if (logout != null) {
          if (!identical(logout.owner, this) ||
              logout.used ||
              logout.revoked ||
              options.method.toUpperCase() != 'POST' ||
              uri.path != '/push/unregister' ||
              uri.hasQuery ||
              !sessionChanging() ||
              sessionRevision() != logout.revision) {
            throw DioException.requestCancelled(
              requestOptions: options,
              reason: 'Invalid logout cleanup',
            );
          }
          logout.used = true;
        }
        final retryable =
            {'GET', 'HEAD'}.contains(options.method.toUpperCase()) &&
            options.data == null;
        if (logout == null &&
            !retryable &&
            endpoints.origins.length > 1 &&
            (_healthProbe != null ||
                endpoints.connection.value != ApiConnectionState.reachable)) {
          // Find a reachable route using a safe read before the first write.
          // This does not retry a write whose result could be ambiguous.
          final revision = sessionRevision();
          await _ensureWriteRoute();
          if (options.cancelToken?.isCancelled == true ||
              (readToken != null &&
                  (sessionChanging() || sessionRevision() != revision))) {
            throw DioException.requestCancelled(
              requestOptions: options,
              reason: 'Request no longer active',
            );
          }
        }
        state = _Request(
          uri: uri,
          origins: endpoints.candidates,
          endpointRevision: endpoints.revision,
          sessionRevision: sessionRevision(),
          retryable: retryable,
          budget: logout != null
              ? (writeBudget < const Duration(seconds: 8)
                    ? writeBudget
                    : const Duration(seconds: 8))
              : (retryable ? readBudget : writeBudget),
          logout: logout,
          caller: options.cancelToken,
          onExpired: () {
            if (_sessionValid(state!)) {
              endpoints.unavailable(startingRevision: state.endpointRevision);
            }
          },
        );
        options.extra[_contextKey] = state;
        if (!_sessionValid(state)) {
          throw DioException.requestCancelled(
            requestOptions: options,
            reason: 'Session changed',
          );
        }
        // Storage is bounded too. It must never leave an interceptor pending.
        state.token =
            await (logout != null
                    ? Future<String?>.value(logout.token)
                    : options.extra[_anonymousProbeKey] == true
                    ? Future<String?>.value(null)
                    : (readToken?.call() ?? Future.value(null)))
                .timeout(const Duration(seconds: 3));
      }
      if (state.cancel.isCancelled || !_sessionValid(state)) {
        throw DioException.requestCancelled(
          requestOptions: options,
          reason: 'Request no longer active',
        );
      }
      final target = state.uri.replace(
        scheme: state.origin.scheme,
        host: state.origin.host,
        port: state.origin.port,
      );
      options.path = target.toString();
      options.baseUrl = state.origin.toString();
      options.queryParameters = {};
      options.cancelToken = state.cancel;
      options.followRedirects = false;
      options.maxRedirects = 0;
      final headers = <String, dynamic>{
        for (final entry in options.headers.entries)
          if (entry.key.toLowerCase() != 'authorization')
            entry.key: entry.value,
      };
      if (state.token != null) {
        headers['Authorization'] = 'Bearer ${state.token}';
      }
      options.headers = headers;
      handler.next(options);
    } catch (error, stackTrace) {
      state?.finish();
      handler.reject(
        error is DioException
            ? error
            : DioException(
                requestOptions: options,
                error: error,
                stackTrace: stackTrace,
              ),
      );
    }
  }

  @override
  void onResponse(Response response, ResponseInterceptorHandler handler) {
    final state = response.requestOptions.extra[_contextKey] as _Request?;
    state?.finish();
    if (state != null && !_sessionValid(state)) {
      handler.reject(
        DioException.requestCancelled(
          requestOptions: response.requestOptions,
          reason: 'Session changed',
        ),
      );
      return;
    }
    if (state != null) {
      endpoints.succeeded(
        state.origin,
        startingRevision: state.endpointRevision,
      );
    }
    handler.next(response);
  }

  @override
  void onError(DioException error, ErrorInterceptorHandler handler) async {
    final state = error.requestOptions.extra[_contextKey] as _Request?;
    final transportFailure =
        {
          DioExceptionType.connectionError,
          DioExceptionType.connectionTimeout,
          DioExceptionType.sendTimeout,
          DioExceptionType.receiveTimeout,
        }.contains(error.type) &&
        error.response == null;
    if (state != null &&
        !state.finished &&
        state.retryable &&
        transportFailure &&
        !state.cancel.isCancelled &&
        _sessionValid(state) &&
        state.attempt + 1 < state.origins.length) {
      state.attempt++;
      try {
        handler.resolve(await dio.fetch(error.requestOptions));
      } on DioException catch (next) {
        handler.next(next);
      }
      return;
    }
    state?.finish();
    if (state != null && !_sessionValid(state)) {
      handler.next(
        DioException.requestCancelled(
          requestOptions: error.requestOptions,
          reason: 'Session changed',
        ),
      );
      return;
    }
    if (state != null && (transportFailure || state.expired)) {
      endpoints.unavailable(startingRevision: state.endpointRevision);
    }
    // A 401/403/409/429, a certificate failure, or any HTTP response is final.
    if (state?.expired == true) {
      handler.next(
        DioException.receiveTimeout(
          timeout: state!.budget,
          requestOptions: error.requestOptions,
        ),
      );
    } else {
      handler.next(error);
    }
  }
}
