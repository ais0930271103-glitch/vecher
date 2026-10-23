import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/network_client.dart';
import 'package:vecher_app/core/network_diagnostics_screen.dart';
import 'package:vecher_app/core/network_endpoints.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/session_changes.dart';

class _Adapter implements HttpClientAdapter {
  _Adapter({this.hang = false});
  final bool hang;
  final pending = <Completer<ResponseBody>>[];
  int requests = 0;
  int cancellations = 0;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) {
    requests++;
    expect(options.uri.path, '/health');
    expect(options.headers.containsKey('Authorization'), isFalse);
    cancelFuture?.then((_) => cancellations++);
    if (!hang) return Future.value(_ok());
    final completion = Completer<ResponseBody>();
    pending.add(completion);
    return completion.future;
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _ok() => ResponseBody.fromString(
  '{"ok":true}',
  200,
  headers: {
    Headers.contentTypeHeader: ['application/json'],
  },
);

void main() {
  String? clipboard;
  setUp(() {
    clipboard = null;
    FlutterSecureStorage.setMockInitialValues({});
    PinGate.unlock();
    PackageInfo.setMockInitialValues(
      appName: 'Вечер',
      packageName: 'online.vecher',
      version: '0.6.1',
      buildNumber: '12',
      buildSignature: '',
    );
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, (call) async {
          if (call.method == 'Clipboard.setData') {
            clipboard = (call.arguments as Map)['text'] as String;
          }
          return null;
        });
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(SystemChannels.platform, null);
  });

  testWidgets(
    'opening the page is offline; checking and copying are explicit',
    (tester) async {
      final adapter = _Adapter();
      await _mount(tester, adapter);
      await tester.pumpAndSettle();
      expect(adapter.requests, 0);
      expect(clipboard, isNull);
      await tester.tap(find.text('Проверить соединение'));
      await tester.pumpAndSettle();
      expect(adapter.requests, 1);
      expect(find.text('Сервер Вечера доступен'), findsOneWidget);
      expect(clipboard, isNull);
      await tester.ensureVisible(find.text('Скопировать отчёт'));
      await tester.tap(find.text('Скопировать отчёт'));
      await tester.pumpAndSettle();
      expect(clipboard, contains('APP_VERSION=0.6.1 (12)'));
      expect(clipboard, contains('VPN_SELECTED_BY_USER=UNSPECIFIED'));
      expect(clipboard, contains('HEALTH_URL=https://api.example.test/health'));
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'background cancels; resume does not restart or accept late data',
    (tester) async {
      final adapter = _Adapter(hang: true);
      await _mount(tester, adapter);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Проверить соединение'));
      await _pumpUntil(tester, () => adapter.requests == 1);
      expect(adapter.requests, 1);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      await _pumpUntil(tester, () => adapter.cancellations == 1);
      expect(adapter.cancellations, 1);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      adapter.pending.single.complete(_ok());
      await tester.pump();
      expect(adapter.requests, 1);
      expect(find.text('Сервер Вечера доступен'), findsNothing);
      expect(find.text('Скопировать отчёт'), findsNothing);
      expect(clipboard, isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pump();
    },
  );

  testWidgets(
    'session change cancels and a second probe is cancelled on dispose',
    (tester) async {
      final adapter = _Adapter(hang: true);
      await _mount(tester, adapter);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Проверить соединение'));
      await _pumpUntil(tester, () => adapter.requests == 1);
      final revision = sessionChanges.begin();
      await _pumpUntil(tester, () => adapter.cancellations == 1);
      expect(adapter.cancellations, 1);
      sessionChanges.finish(revision);
      await tester.pump();
      expect(adapter.requests, 1);
      await tester.tap(find.text('Проверить соединение'));
      await _pumpUntil(tester, () => adapter.requests == 2);
      expect(adapter.requests, 2);
      await tester.pumpWidget(const SizedBox.shrink());
      await _pumpUntil(tester, () => adapter.cancellations == 2);
      expect(adapter.cancellations, 2);
      expect(clipboard, isNull);
      expect(tester.takeException(), isNull);
    },
  );
}

Future<void> _pumpUntil(WidgetTester tester, bool Function() ready) async {
  for (var attempt = 0; attempt < 20 && !ready(); attempt++) {
    await tester.pump(const Duration(milliseconds: 5));
  }
  expect(ready(), isTrue);
}

Future<void> _mount(WidgetTester tester, _Adapter adapter) async {
  tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  final endpoints = ApiEndpoints(primary: 'https://api.example.test');
  final dio = createApiDio(
    endpoints: endpoints,
    adapter: adapter,
    readToken: () async => 'never-read-this-token',
    sessionRevision: () => sessionChanges.revision,
    sessionChanging: () => sessionChanges.isChanging,
  );
  addTearDown(() {
    dio.close(force: true);
    endpoints.dispose();
  });
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        apiEndpointsProvider.overrideWithValue(endpoints),
        dioProvider.overrideWithValue(dio),
      ],
      child: const MaterialApp(
        home: PinGate(child: NetworkDiagnosticsScreen()),
      ),
    ),
  );
}
