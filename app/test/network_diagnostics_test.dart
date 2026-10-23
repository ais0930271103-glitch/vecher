import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/network_client.dart';
import 'package:vecher_app/core/network_diagnostics.dart';
import 'package:vecher_app/core/network_endpoints.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter(this.reply);
  final Future<ResponseBody> Function(RequestOptions) reply;
  final requests = <RequestOptions>[];
  int cancelled = 0;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) {
    requests.add(options);
    cancelFuture?.then((_) => cancelled++);
    return reply(options);
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _reply({int status = 200, String body = '{"ok":true}'}) =>
    ResponseBody.fromString(
      body,
      status,
      headers: {
        Headers.contentTypeHeader: ['application/json'],
      },
    );

void main() {
  Dio client(_Adapter adapter, {Future<String?> Function()? readToken}) {
    final endpoints = ApiEndpoints(primary: 'https://api.example.test');
    final dio = createApiDio(
      endpoints: endpoints,
      adapter: adapter,
      readToken: readToken,
    );
    addTearDown(() {
      dio.close(force: true);
      endpoints.dispose();
    });
    return dio;
  }

  test(
    'manual health uses the API transport without reading or sending auth',
    () async {
      int tokenReads = 0;
      final adapter = _Adapter(
        (_) async =>
            _reply(body: '{"ok":true,"detail":"private-server-value"}'),
      );
      final dio = client(
        adapter,
        readToken: () async {
          tokenReads++;
          return 'private-account-token';
        },
      );
      dio.options.headers['aUtHoRiZaTiOn'] = 'stale-private-token';
      final result = await diagnoseApiConnection(dio);
      expect(tokenReads, 0);
      expect(adapter.requests, hasLength(1));
      final request = adapter.requests.single;
      expect(request.uri.toString(), 'https://api.example.test/health');
      expect(request.method, 'GET');
      expect(request.data, isNull);
      expect(
        request.headers.keys.any(
          (name) => name.toLowerCase() == 'authorization',
        ),
        isFalse,
      );
      expect(request.followRedirects, isFalse);
      expect(result.status, ConnectionDiagnosticStatus.healthy);
      expect(result.httpStatus, 200);
      final report = result.report(
        appVersion: '0.6.1 (12)',
        vpn: DiagnosticVpnState.on,
      );
      expect(report, contains('VPN_SELECTED_BY_USER=ON'));
      expect(report, contains('HTTP_STATUS=200'));
      expect(report, isNot(contains('private')));
      expect(report, isNot(contains('Authorization')));
    },
  );

  test('the public helper refuses an unconfigured client', () async {
    final dio = Dio();
    addTearDown(() => dio.close(force: true));
    expect(() => probeApiHealth(dio), throwsArgumentError);
    await expectLater(diagnoseApiConnection(dio), throwsArgumentError);
  });

  test('an anonymous grant cannot carry inherited query parameters', () async {
    final adapter = _Adapter((_) async => _reply());
    final dio = client(adapter);
    dio.options.queryParameters['private'] = 'do-not-send';
    await expectLater(probeApiHealth(dio), throwsA(isA<DioException>()));
    expect(adapter.requests, isEmpty);
  });

  test(
    'safe fallback stays anonymous and reports the actual trusted origin',
    () async {
      final endpoints = ApiEndpoints(
        primary: 'https://api.example.test',
        alternativesJson: '["https://mirror.example.test"]',
      );
      int tokenReads = 0;
      final adapter = _Adapter((request) async {
        if (request.uri.host == 'api.example.test') {
          throw DioException.connectionError(
            requestOptions: request,
            reason: 'private-routing-details',
          );
        }
        return _reply();
      });
      final dio = createApiDio(
        endpoints: endpoints,
        adapter: adapter,
        readToken: () async {
          tokenReads++;
          return 'secret';
        },
      );
      addTearDown(() {
        dio.close(force: true);
        endpoints.dispose();
      });
      final result = await diagnoseApiConnection(dio);
      expect(adapter.requests, hasLength(2));
      expect(tokenReads, 0);
      expect(result.initialOrigin.host, 'api.example.test');
      expect(result.attemptedOrigin?.host, 'mirror.example.test');
      expect(result.healthUrl.toString(), 'https://mirror.example.test/health');
      expect(
        adapter.requests.every(
          (request) =>
              request.method == 'GET' &&
              request.uri.path == '/health' &&
              !request.headers.containsKey('Authorization'),
        ),
        isTrue,
      );
    },
  );

  for (final status in [302, 401, 403, 503]) {
    test(
      'HTTP $status is reported without body or redirect follow-up',
      () async {
        final adapter = _Adapter(
          (_) async =>
              _reply(status: status, body: '{"detail":"private-body"}'),
        );
        final result = await diagnoseApiConnection(client(adapter));
        expect(result.status, ConnectionDiagnosticStatus.httpError);
        expect(result.httpStatus, status);
        expect(adapter.requests, hasLength(1));
        expect(
          result.report(appVersion: '11', vpn: DiagnosticVpnState.off),
          isNot(contains('private-body')),
        );
      },
    );
  }

  test('a 200 without ok:true is not a healthy API', () async {
    final result = await diagnoseApiConnection(
      client(_Adapter((_) async => _reply(body: '{"detail":"other-service"}'))),
    );
    expect(result.status, ConnectionDiagnosticStatus.invalidReply);
    expect(result.httpStatus, 200);
  });

  test(
    'TLS and socket classes are classified without exception text',
    () async {
      for (final entry in [
        (
          const HandshakeException('private-tls-detail'),
          ConnectionDiagnosticStatus.tlsFailure,
        ),
        (
          const SocketException('private-dns-detail'),
          ConnectionDiagnosticStatus.networkFailure,
        ),
      ]) {
        final result = await diagnoseApiConnection(
          client(
            _Adapter((request) async {
              throw DioException(requestOptions: request, error: entry.$1);
            }),
          ),
        );
        expect(result.status, entry.$2);
        final report = result.report(
          appVersion: '0.6.1 (12)',
          vpn: DiagnosticVpnState.unspecified,
        );
        expect(report, isNot(contains('private')));
      }
    },
  );

  test(
    'outer deadline cancels a hung transport within the total budget',
    () async {
      final adapter = _Adapter((_) => Completer<ResponseBody>().future);
      final result = await diagnoseApiConnection(
        client(adapter),
        budget: const Duration(milliseconds: 40),
      );
      await Future<void>.delayed(Duration.zero);
      expect(result.status, ConnectionDiagnosticStatus.timeout);
      expect(result.elapsed, lessThan(const Duration(seconds: 2)));
      expect(adapter.cancelled, 1);
    },
  );

  test(
    'caller cancellation stops the probe and never becomes a timeout',
    () async {
      final started = Completer<void>();
      final adapter = _Adapter((_) {
        started.complete();
        return Completer<ResponseBody>().future;
      });
      final cancellation = CancelToken();
      final pending = diagnoseApiConnection(
        client(adapter),
        cancelToken: cancellation,
      );
      await started.future;
      cancellation.cancel('private-cancellation-reason');
      final result = await pending;
      expect(result.status, ConnectionDiagnosticStatus.cancelled);
      expect(adapter.cancelled, 1);
      expect(
        result.report(appVersion: '11', vpn: DiagnosticVpnState.on),
        isNot(contains('private')),
      );
    },
  );
}
