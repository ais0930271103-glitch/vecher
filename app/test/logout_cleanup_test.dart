import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:firebase_core_platform_interface/test.dart';
import 'package:flutter/foundation.dart'
    show debugDefaultTargetPlatformOverride, TargetPlatform;
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/network_client.dart';
import 'package:vecher_app/core/network_endpoints.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/push.dart';
import 'package:vecher_app/core/session_changes.dart';

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

ResponseBody _ok() => ResponseBody.fromString(
  jsonEncode({'ok': true}),
  200,
  headers: {
    Headers.contentTypeHeader: ['application/json'],
  },
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const messaging = MethodChannel('plugins.flutter.io/firebase_messaging');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  final clients = <Dio>[];
  late Future<Object?> Function(MethodCall) native;

  Dio client(
    _Adapter adapter, {
    Duration writeBudget = const Duration(seconds: 2),
    Future<String?> Function()? tokenReader,
  }) {
    final dio = createApiDio(
      endpoints: ApiEndpoints(primary: 'https://api.example.test'),
      readToken: tokenReader ?? readToken,
      sessionRevision: () => sessionChanges.revision,
      sessionChanging: () => sessionChanges.isChanging,
      adapter: adapter,
      writeBudget: writeBudget,
    );
    clients.add(dio);
    return dio;
  }

  setUp(() {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    setupFirebaseCoreMocks();
    FlutterSecureStorage.setMockInitialValues({
      'vecher_token': 'account-A',
      'vecher_pin': '1357',
    });
    native = (call) async => switch (call.method) {
      'Messaging#requestPermission' => {'authorizationStatus': 1},
      'Messaging#getToken' => {'token': 'device-token'},
      _ => null,
    };
    messenger.setMockMethodCallHandler(messaging, (call) => native(call));
  });

  tearDown(() async {
    if (clients.isNotEmpty) {
      await stopPush(clients.last, unregister: (_, __) async {});
    }
    for (final dio in clients) {
      dio.close(force: true);
    }
    clients.clear();
    sessionChanges.finish(sessionChanges.revision);
    messenger.setMockMethodCallHandler(messaging, null);
    debugDefaultTargetPlatformOverride = null;
  });

  final cancelled = throwsA(
    isA<DioException>().having(
      (error) => error.type,
      'type',
      DioExceptionType.cancel,
    ),
  );

  test(
    'real signOut unregisters old push while normal account traffic is closed',
    () async {
      final unregisterStarted = Completer<void>();
      final unregisterReply = Completer<ResponseBody>();
      final adapter = _Adapter((options) async {
        if (options.uri.path == '/push/unregister') {
          unregisterStarted.complete();
          return unregisterReply.future;
        }
        return _ok();
      });
      final dio = client(adapter);
      await initPush(dio);
      expect(adapter.seen.single.uri.path, '/push/register');

      final logout = signOut(dio);
      await unregisterStarted.future;
      expect(sessionChanges.isChanging, isTrue);
      await expectLater(dio.get('/me'), cancelled);
      await expectLater(
        dio.post('/chat/messages', data: {'text': 'late'}),
        cancelled,
      );
      expect(adapter.seen.length, 2);
      final request = adapter.seen.last;
      expect(
        request.uri.toString(),
        'https://api.example.test/push/unregister',
      );
      expect(request.method, 'POST');
      expect(request.data, {'token': 'device-token', 'platform': 'android'});
      expect(request.headers['Authorization'], 'Bearer account-A');
      expect(request.followRedirects, isFalse);
      unregisterReply.complete(_ok());
      await logout;

      expect(await readToken(), isNull);
      expect(await hasPin(), isFalse);
      expect(sessionChanges.isChanging, isFalse);
      await dio.get('/health');
      expect(adapter.seen.last.headers.containsKey('Authorization'), isFalse);
    },
  );

  test(
    'a newer login cancels old cleanup and preserves its token and PIN',
    () async {
      final unregisterStarted = Completer<void>();
      final oldReply = Completer<ResponseBody>();
      final adapter = _Adapter((options) async {
        if (options.uri.path == '/push/unregister') {
          unregisterStarted.complete();
          return oldReply.future;
        }
        return _ok();
      });
      final dio = client(adapter);
      await initPush(dio);
      final logout = signOut(dio);
      await unregisterStarted.future;
      await saveToken('account-B');
      await savePin('2468');
      await initPush(dio);
      await logout;
      expect(await readToken(), 'account-B');
      expect(await checkPin('2468'), isTrue);
      expect(sessionChanges.isChanging, isFalse);
      expect(adapter.seen.map((request) => request.uri.path), [
        '/push/register',
        '/push/unregister',
        '/push/register',
      ]);
      expect(adapter.seen[1].headers['Authorization'], 'Bearer account-A');
      expect(adapter.seen[2].headers['Authorization'], 'Bearer account-B');
      oldReply.complete(_ok());
      await Future<void>.delayed(Duration.zero);
      expect(await readToken(), 'account-B');
    },
  );

  test(
    'cleanup uses captured credentials once without reading a new token',
    () async {
      var reads = 0;
      final adapter = _Adapter((_) async => _ok());
      final dio = client(
        adapter,
        tokenReader: () async {
          reads++;
          return 'newer-token-must-not-be-read';
        },
      );
      final revision = sessionChanges.begin();
      await unregisterPushForLogout(
        dio,
        accessToken: 'captured-old-token',
        revision: revision,
        pushToken: 'device-token',
        platform: 'android',
      );
      expect(reads, 0);
      expect(
        adapter.seen.single.headers['Authorization'],
        'Bearer captured-old-token',
      );
      // Even retained internal request options cannot reuse the grant, nor move
      // it to an unrelated route during the same logout revision.
      final retained = adapter.seen.single;
      await expectLater(dio.fetch(retained), cancelled);
      await expectLater(
        dio.post('/account/erase', options: Options(extra: retained.extra)),
        cancelled,
      );
      expect(adapter.seen.length, 1);
    },
  );

  test(
    'a login during the initial token read starts its own push listeners',
    () async {
      final adapter = _Adapter((_) async => _ok());
      final dio = client(adapter);
      await initPush(dio);
      final logout = signOut(dio);
      // Deliberately replace the session before signOut's first await resumes.
      await saveToken('account-B');
      await initPush(dio);
      await logout;
      expect(await readToken(), 'account-B');
      expect(adapter.seen.map((request) => request.uri.path), [
        '/push/register',
        '/push/register',
      ]);
      expect(adapter.seen.last.headers['Authorization'], 'Bearer account-B');
    },
  );

  test(
    'cleanup authority rejects inactive and different session revisions',
    () async {
      final adapter = _Adapter((_) async => _ok());
      final dio = client(adapter);
      Future<void> cleanup(int revision) => unregisterPushForLogout(
        dio,
        accessToken: 'old-token',
        revision: revision,
        pushToken: 'device-token',
        platform: 'android',
      );
      await expectLater(cleanup(sessionChanges.revision), cancelled);
      final old = sessionChanges.begin();
      sessionChanges.begin();
      await expectLater(cleanup(old), cancelled);
      expect(adapter.seen, isEmpty);
    },
  );

  test(
    'a public client cannot acquire authenticated logout authority',
    () async {
      final adapter = _Adapter((_) async => _ok());
      final dio = createApiDio(
        endpoints: ApiEndpoints(primary: 'https://api.example.test'),
        adapter: adapter,
        sessionRevision: () => sessionChanges.revision,
        sessionChanging: () => sessionChanges.isChanging,
      );
      clients.add(dio);
      final revision = sessionChanges.begin();
      await expectLater(
        unregisterPushForLogout(
          dio,
          accessToken: 'old-token',
          revision: revision,
          pushToken: 'device-token',
          platform: 'android',
        ),
        cancelled,
      );
      expect(adapter.seen, isEmpty);
    },
  );

  test(
    'offline unregister is attempted once and does not prevent local logout',
    () async {
      final adapter = _Adapter((options) async {
        if (options.uri.path == '/push/unregister') {
          throw DioException.connectionError(
            requestOptions: options,
            reason: 'offline',
          );
        }
        return _ok();
      });
      final dio = client(adapter);
      await initPush(dio);
      await signOut(dio);
      expect(adapter.seen.map((request) => request.uri.path), [
        '/push/register',
        '/push/unregister',
      ]);
      expect(await readToken(), isNull);
      expect(await hasPin(), isFalse);
    },
  );

  test('a stalled cleanup request has a finite budget', () async {
    final adapter = _Adapter((options) async {
      if (options.uri.path == '/push/unregister') {
        return Completer<ResponseBody>().future;
      }
      return _ok();
    });
    final dio = client(adapter, writeBudget: const Duration(milliseconds: 40));
    await initPush(dio);
    await signOut(dio).timeout(const Duration(seconds: 2));
    expect(await readToken(), isNull);
    expect(sessionChanges.isChanging, isFalse);
    expect(adapter.seen.length, 2);
  });

  test(
    'native token initialization cannot keep logout waiting indefinitely',
    () async {
      final nativeStarted = Completer<void>();
      final nativeReply = Completer<Object?>();
      final fallback = native;
      native = (call) async {
        if (call.method == 'Messaging#getToken') {
          nativeStarted.complete();
          return nativeReply.future;
        }
        return fallback(call);
      };
      final adapter = _Adapter((_) async => _ok());
      final dio = client(adapter);
      final initialization = initPush(dio);
      await nativeStarted.future;
      await signOut(dio).timeout(const Duration(seconds: 10));
      expect(await readToken(), isNull);
      expect(sessionChanges.isChanging, isFalse);
      expect(adapter.seen, isEmpty);
      nativeReply.complete({'token': 'late-device-token'});
      await initialization;
      expect(adapter.seen, isEmpty);
    },
  );
}
