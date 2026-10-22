import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/network_client.dart';
import 'package:vecher_app/core/network_endpoints.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter(this.reply);
  final Future<ResponseBody> Function(RequestOptions) reply;
  final seen = <RequestOptions>[];
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? body,
    Future<void>? cancelFuture,
  ) {
    seen.add(
      options.copyWith(
        headers: Map.of(options.headers),
        extra: Map.of(options.extra),
      ),
    );
    return reply(options);
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _response([int status = 200]) => ResponseBody.fromString(
  jsonEncode({'ok': true}),
  status,
  headers: {
    Headers.contentTypeHeader: ['application/json'],
  },
);

void main() {
  ApiEndpoints endpoints() => ApiEndpoints(
    primary: 'https://api.example.test:8443',
    alternativesJson: '["https://mirror.example.test"]',
  );

  test('only explicit HTTPS origins and debug loopback are trusted', () {
    final policy = endpoints();
    expect(policy.origins.length, 2);
    expect(
      policy.trusts(Uri.parse('https://api.example.test:8443/chat')),
      isTrue,
    );
    expect(policy.trusts(Uri.parse('https://api.example.test/chat')), isFalse);
    for (final invalid in [
      'http://api.example.test',
      'https://user:pass@api.example.test',
      'https://api.example.test/path',
      'https://api.example.test?host=evil',
      'https://api.example.test#fragment',
      'https://api.example.test:0',
      'https://api.example.test\\evil',
      ' https://api.example.test',
    ]) {
      expect(() => ApiEndpoints(primary: invalid), throwsFormatException);
    }
    expect(
      () => ApiEndpoints(primary: 'https://ok.test', alternativesJson: '{}'),
      throwsFormatException,
    );
    expect(
      () => ApiEndpoints(primary: 'https://ok.test', alternativesJson: '[7]'),
      throwsFormatException,
    );
    expect(
      () => ApiEndpoints(primary: 'http://127.0.0.1:8090'),
      throwsFormatException,
    );
    expect(
      ApiEndpoints(
        primary: 'http://127.0.0.1:8090',
        allowLocalHttp: true,
      ).primary.port,
      8090,
    );
    expect(
      () => ApiEndpoints(primary: 'http://192.168.1.5', allowLocalHttp: true),
      throwsFormatException,
    );
  });

  test(
    'GET retries a transport failure, keeps query/auth, and sticks to success',
    () async {
      final policy = endpoints();
      int tokenReads = 0;
      final adapter = _Adapter((options) async {
        if (options.uri.host == 'api.example.test') {
          throw DioException.connectionError(
            requestOptions: options,
            reason: 'offline',
          );
        }
        return _response();
      });
      final dio = createApiDio(
        endpoints: policy,
        adapter: adapter,
        readToken: () async {
          tokenReads++;
          return 'private-test-token';
        },
      );
      addTearDown(() => dio.close(force: true));
      final result = await dio.get(
        '/chat/messages',
        queryParameters: {'since': 'a b'},
      );
      expect(result.requestOptions.uri.host, 'mirror.example.test');
      expect(result.requestOptions.uri.port, 443);
      expect(result.requestOptions.uri.queryParameters, {'since': 'a b'});
      expect(adapter.seen.length, 2);
      expect(tokenReads, 1);
      expect(
        adapter.seen.every(
          (request) =>
              request.headers['Authorization'] == 'Bearer private-test-token' &&
              !request.followRedirects &&
              request.maxRedirects == 0,
        ),
        isTrue,
      );
      await dio.get('/today');
      expect(adapter.seen.last.uri.host, 'mirror.example.test');
      expect(adapter.seen.length, 3);
      expect(policy.connection.value, ApiConnectionState.reachable);
    },
  );

  for (final method in ['POST', 'PATCH', 'PUT', 'DELETE']) {
    test('$method is never replayed after a transport failure', () async {
      final adapter = _Adapter(
        (options) async => throw DioException.connectionError(
          requestOptions: options,
          reason: 'offline',
        ),
      );
      final policy = endpoints();
      policy.succeeded(policy.primary, startingRevision: policy.revision);
      final dio = createApiDio(endpoints: policy, adapter: adapter);
      addTearDown(() => dio.close(force: true));
      await expectLater(
        dio.request(
          '/action',
          data: {'id': 'once'},
          options: Options(method: method),
        ),
        throwsA(isA<DioException>()),
      );
      expect(adapter.seen.length, 1);
    });
  }

  test(
    'cold login probes trusted routes anonymously before one POST',
    () async {
      final policy = endpoints();
      final adapter = _Adapter((options) async {
        if (options.uri.host == 'api.example.test') {
          throw DioException.connectionError(
            requestOptions: options,
            reason: 'offline',
          );
        }
        return _response();
      });
      final dio = createApiDio(
        endpoints: policy,
        adapter: adapter,
        readToken: () async => 'account',
      );
      addTearDown(() => dio.close(force: true));
      await dio.post('/auth/request-code', data: {'phone': '+79990000000'});
      expect(adapter.seen.map((request) => request.method), [
        'GET',
        'GET',
        'POST',
      ]);
      expect(
        adapter.seen
            .take(2)
            .every(
              (request) =>
                  request.uri.path == '/health' &&
                  !request.headers.containsKey('Authorization'),
            ),
        isTrue,
      );
      expect(adapter.seen.last.uri.host, 'mirror.example.test');
      expect(adapter.seen.last.headers['Authorization'], 'Bearer account');
    },
  );

  test('HEAD retries at most once per compiled origin', () async {
    final policy = ApiEndpoints(
      primary: 'https://first.test',
      alternativesJson: '["https://second.test","https://third.test"]',
    );
    final adapter = _Adapter(
      (options) async => throw DioException.connectionError(
        requestOptions: options,
        reason: 'offline',
      ),
    );
    final dio = createApiDio(endpoints: policy, adapter: adapter);
    addTearDown(() => dio.close(force: true));
    await expectLater(dio.head('/health'), throwsA(isA<DioException>()));
    expect(adapter.seen.map((request) => request.uri.host), [
      'first.test',
      'second.test',
      'third.test',
    ]);
    expect(policy.connection.value, ApiConnectionState.unavailable);
  });

  for (final status in [401, 403, 409, 429, 500, 503, 302]) {
    test('HTTP $status is final and redirects are not followed', () async {
      final adapter = _Adapter((_) async => _response(status));
      final dio = createApiDio(endpoints: endpoints(), adapter: adapter);
      addTearDown(() => dio.close(force: true));
      await expectLater(dio.get('/data'), throwsA(isA<DioException>()));
      expect(adapter.seen.length, 1);
      expect(adapter.seen.single.followRedirects, isFalse);
    });
  }

  test('certificate failures are final', () async {
    final adapter = _Adapter(
      (options) async =>
          throw DioException.badCertificate(requestOptions: options),
    );
    final dio = createApiDio(endpoints: endpoints(), adapter: adapter);
    addTearDown(() => dio.close(force: true));
    await expectLater(dio.get('/data'), throwsA(isA<DioException>()));
    expect(adapter.seen.length, 1);
  });

  test(
    'absolute foreign origins and authority tricks never read or send a token',
    () async {
      int reads = 0;
      final adapter = _Adapter((_) async => _response());
      final dio = createApiDio(
        endpoints: endpoints(),
        adapter: adapter,
        readToken: () async {
          reads++;
          return 'secret';
        },
      );
      addTearDown(() => dio.close(force: true));
      for (final url in [
        'https://foreign.test/chat',
        '//foreign.test/chat',
        'https://api.example.test:8443@foreign.test/chat',
        'https://api.example.test:8443/chat#hidden',
        'https://api.example.test:8443\\@foreign.test/chat',
      ]) {
        await expectLater(dio.get(url), throwsA(isA<DioException>()));
      }
      expect(reads, 0);
      expect(adapter.seen, isEmpty);
    },
  );

  test('public API requests strip any caller authorization', () async {
    final adapter = _Adapter((_) async => _response());
    final dio = createApiDio(endpoints: endpoints(), adapter: adapter);
    addTearDown(() => dio.close(force: true));
    await dio.get(
      '/app/version',
      options: Options(headers: {'authorization': 'Bearer stale'}),
    );
    expect(
      adapter.seen.single.headers.keys.any(
        (key) => key.toLowerCase() == 'authorization',
      ),
      isFalse,
    );
  });

  test('cancellation while waiting does not start another origin', () async {
    final started = Completer<void>();
    final adapter = _Adapter((_) {
      started.complete();
      return Completer<ResponseBody>().future;
    });
    final dio = createApiDio(endpoints: endpoints(), adapter: adapter);
    addTearDown(() => dio.close(force: true));
    final cancel = CancelToken();
    final result = dio.get('/data', cancelToken: cancel);
    await started.future;
    cancel.cancel();
    await expectLater(
      result,
      throwsA(
        isA<DioException>().having(
          (error) => error.type,
          'type',
          DioExceptionType.cancel,
        ),
      ),
    );
    expect(adapter.seen.length, 1);
  });

  test('a single deadline bounds a stalled logical request', () async {
    final policy = endpoints();
    final adapter = _Adapter((_) => Completer<ResponseBody>().future);
    final dio = createApiDio(
      endpoints: policy,
      adapter: adapter,
      readBudget: const Duration(milliseconds: 40),
    );
    addTearDown(() => dio.close(force: true));
    await expectLater(dio.get('/data'), throwsA(isA<DioException>()));
    expect(adapter.seen.length, 1);
    expect(policy.connection.value, ApiConnectionState.unavailable);
  });

  test('changing accounts prevents retrying old credentials', () async {
    int revision = 0;
    final adapter = _Adapter((options) async {
      revision++;
      throw DioException.connectionError(
        requestOptions: options,
        reason: 'offline',
      );
    });
    final dio = createApiDio(
      endpoints: endpoints(),
      adapter: adapter,
      sessionRevision: () => revision,
      readToken: () async => 'first-account',
    );
    addTearDown(() => dio.close(force: true));
    await expectLater(
      dio.get('/private'),
      throwsA(
        isA<DioException>().having(
          (error) => error.type,
          'type',
          DioExceptionType.cancel,
        ),
      ),
    );
    expect(adapter.seen.length, 1);
  });

  test('a timed-out old session cannot mark the new session offline', () async {
    int revision = 0;
    final policy = endpoints();
    policy.succeeded(policy.primary, startingRevision: policy.revision);
    final started = Completer<void>();
    final adapter = _Adapter((_) {
      started.complete();
      return Completer<ResponseBody>().future;
    });
    final dio = createApiDio(
      endpoints: policy,
      adapter: adapter,
      sessionRevision: () => revision,
      readToken: () async => 'old-account',
      readBudget: const Duration(milliseconds: 40),
    );
    addTearDown(() => dio.close(force: true));
    final pending = dio.get('/private');
    await started.future;
    revision++;
    await expectLater(pending, throwsA(isA<DioException>()));
    expect(policy.connection.value, ApiConnectionState.reachable);
    expect(adapter.seen.length, 1);
  });

  test(
    'late primary success cannot undo another request choosing a mirror',
    () async {
      final policy = endpoints();
      final firstStarted = Completer<void>();
      final late = Completer<ResponseBody>();
      final adapter = _Adapter((options) async {
        if (options.uri.path == '/late') {
          firstStarted.complete();
          return late.future;
        }
        if (options.uri.host == 'api.example.test') {
          throw DioException.connectionError(
            requestOptions: options,
            reason: 'offline',
          );
        }
        return _response();
      });
      final dio = createApiDio(endpoints: policy, adapter: adapter);
      addTearDown(() => dio.close(force: true));
      final first = dio.get('/late');
      await firstStarted.future;
      await dio.get('/other');
      late.complete(_response());
      await first;
      expect(policy.selected.host, 'mirror.example.test');
    },
  );

  test('WSS mapping follows only the primary API origin and exact port', () {
    final policy = endpoints();
    final responding = Uri.parse(
      'https://mirror.example.test/realtime/session',
    );
    expect(
      policy.realtimeUrl(
        'wss://api.example.test:8443/connection/websocket',
        responding,
      ),
      'wss://mirror.example.test/connection/websocket',
    );
    expect(
      policy.realtimeUrl(
        'wss://realtime.example.test/connection/websocket',
        responding,
      ),
      'wss://realtime.example.test/connection/websocket',
    );
    expect(
      policy.realtimeUrl(
        'wss://api.example.test/connection/websocket',
        responding,
      ),
      'wss://api.example.test/connection/websocket',
    );
    expect(
      policy.realtimeUrl(
        'wss://api.example.test:8443/connection/websocket',
        Uri.parse('https://foreign.test'),
      ),
      'wss://api.example.test:8443/connection/websocket',
    );
  });
}
