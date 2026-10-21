import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_secure_storage/test/test_flutter_secure_storage_platform.dart';
import 'package:flutter_secure_storage_platform_interface/flutter_secure_storage_platform_interface.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/chat_presence.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/realtime_provider.dart';
import 'package:vecher_app/core/top_banner.dart';
import 'package:vecher_app/features/chat/chat_screen.dart';
import 'package:vecher_app/features/today/today_screen.dart' show pairProvider;

import 'support/fake_realtime.dart';

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({'vecher_chat_hint_seen': '1'});
    PinGate.unlock();
  });

  testWidgets(
    'covered or background chat does not suppress push or mark read',
    (tester) async {
      final adapter = _ChatAdapter();
      final navigator = GlobalKey<NavigatorState>();
      await _mountChat(tester, adapter, navigator: navigator);
      expect(chatOnScreen, isTrue);
      expect(adapter.reads, greaterThan(0));

      navigator.currentState!.push(
        MaterialPageRoute<void>(
          builder: (_) => const Scaffold(body: Text('Другой экран')),
        ),
      );
      await tester.pumpAndSettle();
      expect(chatOnScreen, isFalse);
      final coveredReads = adapter.reads;
      final coveredLoads = adapter.loads;
      await tester.pump(const Duration(seconds: 5));
      expect(adapter.reads, coveredReads);
      expect(adapter.loads, coveredLoads);

      navigator.currentState!.pop();
      await tester.pumpAndSettle();
      expect(chatOnScreen, isTrue);
      expect(adapter.reads, greaterThan(coveredReads));

      for (final state in [
        AppLifecycleState.inactive,
        AppLifecycleState.hidden,
        AppLifecycleState.paused,
      ]) {
        tester.binding.handleAppLifecycleStateChanged(state);
      }
      await tester.pump();
      expect(chatOnScreen, isFalse);
      final backgroundReads = adapter.reads;
      final backgroundLoads = adapter.loads;
      await tester.pump(const Duration(seconds: 5));
      expect(adapter.reads, backgroundReads);
      expect(adapter.loads, backgroundLoads);

      for (final state in [
        AppLifecycleState.hidden,
        AppLifecycleState.inactive,
        AppLifecycleState.resumed,
      ]) {
        tester.binding.handleAppLifecycleStateChanged(state);
      }
      await tester.pumpAndSettle();
      expect(chatOnScreen, isTrue);
      expect(adapter.reads, greaterThan(backgroundReads));
      await _unmountChat(tester);
      expect(chatOnScreen, isFalse);
    },
  );

  testWidgets('a timed-out message retries the same id and immutable payload', (
    tester,
  ) async {
    final adapter = _ChatAdapter(failFirstPost: true);
    await _mountChat(tester, adapter);
    await tester.enterText(find.byType(TextField), 'Привет из чата');
    await tester.tap(find.byTooltip('Отправить'));
    await tester.pumpAndSettle();

    expect(adapter.posts, hasLength(1));
    final original = adapter.posts.single;
    expect(original['client_message_id'], matches(RegExp(r'^[a-f0-9]{32}$')));
    expect(tester.widget<TextField>(find.byType(TextField)).readOnly, isTrue);
    expect(find.byTooltip('Повторить отправку'), findsOneWidget);
    expect(find.byType(SnackBar), findsNothing);

    await tester.tap(find.byTooltip('Повторить отправку'));
    await tester.pumpAndSettle();
    expect(adapter.posts, hasLength(2));
    expect(adapter.posts.last, original);
    expect(adapter.messages, hasLength(1));
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      '',
    );
    expect(find.byTooltip('Повторить отправку'), findsNothing);
    expect(find.text('Привет из чата'), findsOneWidget);
    await _unmountChat(tester);
  });

  testWidgets(
    'history acknowledges a committed message after lost POST reply',
    (tester) async {
      final adapter = _ChatAdapter(
        failFirstPost: true,
        commitBeforeFailure: true,
      );
      await _mountChat(tester, adapter);
      await tester.enterText(find.byType(TextField), 'Сообщение доставлено');
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pumpAndSettle();
      expect(find.byTooltip('Повторить отправку'), findsOneWidget);

      await tester.pump(const Duration(seconds: 4));
      await tester.pumpAndSettle();
      expect(adapter.posts, hasLength(1));
      expect(find.text('Сообщение доставлено'), findsOneWidget);
      expect(find.byTooltip('Повторить отправку'), findsNothing);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        '',
      );
      await _unmountChat(tester);
    },
  );

  testWidgets('a narrow chat shows an attachment with a long Unicode name', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 740);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final adapter = _ChatAdapter();
    adapter.messages.add({
      'id': 'message-file',
      'mine': false,
      'created_at': '2026-10-18T19:00:00Z',
      'attachments': [
        {
          'id': 'file-1',
          'name': 'Очень длинное имя нашего общего плана на вечер 🌙.pdf',
          'mime': 'application/pdf',
          'size': 1536,
        },
      ],
    });
    await _mountChat(tester, adapter);
    expect(find.textContaining('Очень длинное имя'), findsOneWidget);
    expect(find.text('1,5 КБ · открыть'), findsOneWidget);
    expect(find.byTooltip('Скачать и открыть'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await _unmountChat(tester);
  });

  testWidgets(
    'delayed startup PIN lookup never loads chat or marks messages read',
    (tester) async {
      final storage = _DelayedPinStorage();
      FlutterSecureStoragePlatform.instance = storage;
      final adapter = _ChatAdapter();
      await _mountChat(tester, adapter, settle: false);
      expect(PinGate.isReady, isFalse);
      expect(chatOnScreen, isFalse);
      await tester.pump(const Duration(seconds: 5));
      expect(adapter.loads, 0);
      expect(adapter.reads, 0);
      storage.pin.complete('1234');
      await tester.idle();
      expect(PinGate.isReady, isTrue);
      expect(PinGate.isLocked, isTrue);
      expect(adapter.loads, 0);
      expect(adapter.reads, 0);
      PinGate.unlock();
      await tester.pumpAndSettle();
      expect(adapter.loads, greaterThan(0));
      expect(adapter.reads, greaterThan(0));
      await _unmountChat(tester);
    },
  );

  for (final older in [false, true]) {
    testWidgets(
      'full resync discards delayed ${older ? 'older history' : 'chat refresh'} from prior pair',
      (tester) async {
        final fake = FakeRealtime();
        final adapter = _ChatAdapter()..hasMore = true;
        final oldMessage = {
          'id': 'old-pair-message',
          'mine': false,
          'text': 'Сообщение прежней пары',
          'created_at': '2026-10-21T18:00:00Z',
          'attachments': <Object>[],
        };
        adapter.messages.add(oldMessage);
        addTearDown(fake.controller.dispose);
        await _mountChat(tester, adapter, realtime: fake);
        fake.controller.activate('account');
        await tester.pump();
        fake.transport.subscribed();
        await tester.pump(const Duration(milliseconds: 200));
        await tester.pumpAndSettle();
        expect(find.text('Сообщение прежней пары'), findsOneWidget);

        final delayed = adapter.delayedLoad = Completer<ResponseBody>();
        if (older) {
          await tester.tap(find.text('Показать ранние'));
          await tester.pump(const Duration(milliseconds: 20));
        } else {
          fake.transport.publish(10, ['chat']);
          await tester.pump(const Duration(milliseconds: 200));
        }
        expect(adapter.delayedLoad, isNull);
        adapter.messages
          ..clear()
          ..add({
            ...oldMessage,
            'id': 'new-pair-message',
            'text': 'Сообщение новой пары',
          });
        adapter.hasMore = false;
        fake.transport.publish(11, ['all']);
        await tester.pump(const Duration(milliseconds: 200));
        await tester.pumpAndSettle();
        expect(find.text('Сообщение прежней пары'), findsNothing);
        expect(find.text('Сообщение новой пары'), findsOneWidget);
        delayed.complete(
          adapter._json({
            'messages': [oldMessage],
            'has_more': true,
            'partner_read_at': null,
          }),
        );
        await tester.pumpAndSettle();
        expect(find.text('Сообщение прежней пары'), findsNothing);
        expect(find.text('Сообщение новой пары'), findsOneWidget);
        await _unmountChat(tester);
      },
    );
  }

  testWidgets(
    'late send response cannot restore messages cleared by full resync',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      addTearDown(fake.controller.dispose);
      await _mountChat(tester, adapter, realtime: fake);
      fake.controller.activate('account');
      await tester.pump();
      fake.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      final delayed = adapter.delayedPost = Completer<ResponseBody>();
      await tester.enterText(find.byType(TextField), 'Отправлено прежней паре');
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pump(const Duration(milliseconds: 20));
      expect(adapter.delayedPost, isNull);
      final response = adapter._json(adapter.messages.single);
      adapter.messages.clear();
      fake.transport.publish(21, ['all']);
      await tester.pump(const Duration(milliseconds: 200));
      delayed.complete(response);
      await tester.pumpAndSettle();
      expect(find.text('Отправлено прежней паре'), findsNothing);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        '',
      );
      await _unmountChat(tester);
    },
  );

  testWidgets(
    'realtime refreshes visible chat immediately but cannot mark covered chat read',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      final navigator = GlobalKey<NavigatorState>();
      addTearDown(fake.controller.dispose);
      await _mountChat(tester, adapter, navigator: navigator, realtime: fake);
      fake.controller.activate('account');
      await tester.pump();
      fake.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      final loads = adapter.loads;
      adapter.messages.add({
        'id': 'partner-realtime',
        'mine': false,
        'text': 'Новое сообщение',
        'created_at': '2026-10-21T19:00:00Z',
        'attachments': <Object>[],
      });
      fake.transport.publish(1, ['chat']);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      expect(adapter.loads, greaterThan(loads));
      expect(find.text('Новое сообщение'), findsOneWidget);
      navigator.currentState!.push(
        MaterialPageRoute<void>(
          builder: (_) => const Scaffold(body: Text('План')),
        ),
      );
      await tester.pumpAndSettle();
      final coveredLoads = adapter.loads;
      final coveredReads = adapter.reads;
      fake.transport.publish(2, ['chat']);
      await tester.pump(const Duration(milliseconds: 200));
      expect(adapter.loads, coveredLoads);
      expect(adapter.reads, coveredReads);
      navigator.currentState!.pop();
      await tester.pumpAndSettle();
      expect(adapter.loads, greaterThan(coveredLoads));
      await _unmountChat(tester);
    },
  );
}

Future<void> _mountChat(
  WidgetTester tester,
  _ChatAdapter adapter, {
  GlobalKey<NavigatorState>? navigator,
  FakeRealtime? realtime,
  bool settle = true,
}) async {
  final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
    ..httpClientAdapter = adapter;
  addTearDown(dio.close);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        dioProvider.overrideWithValue(dio),
        pairProvider.overrideWith((_) async => null),
        if (realtime != null)
          realtimeControllerProvider.overrideWithValue(realtime.controller),
      ],
      child: MaterialApp(
        theme: ThemeData.dark(),
        navigatorKey: navigator,
        navigatorObservers: [chatRouteObserver],
        home: const PinGate(child: ChatScreen()),
      ),
    ),
  );
  if (settle) await tester.pumpAndSettle();
}

Future<void> _unmountChat(WidgetTester tester) async {
  hideTopBanner();
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
}

class _DelayedPinStorage extends TestFlutterSecureStoragePlatform {
  _DelayedPinStorage() : super({'vecher_chat_hint_seen': '1'});
  final pin = Completer<String?>();

  @override
  Future<String?> read({
    required String key,
    required Map<String, String> options,
  }) {
    if (key == 'vecher_pin') return pin.future;
    return super.read(key: key, options: options);
  }
}

class _ChatAdapter implements HttpClientAdapter {
  _ChatAdapter({this.failFirstPost = false, this.commitBeforeFailure = false});
  final bool failFirstPost;
  final bool commitBeforeFailure;
  final List<Map<String, dynamic>> posts = [];
  final List<Map<String, dynamic>> messages = [];
  int reads = 0;
  int loads = 0;
  bool hasMore = false;
  Completer<ResponseBody>? delayedLoad;
  Completer<ResponseBody>? delayedPost;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    if (requestStream != null) await requestStream.drain<void>();
    if (options.path == '/chat' && options.method == 'GET') {
      loads++;
      final delayed = delayedLoad;
      delayedLoad = null;
      if (delayed != null) return delayed.future;
      return _json({
        'messages': messages,
        'has_more': hasMore,
        'partner_read_at': null,
      });
    }
    if (options.path == '/chat/read') {
      reads++;
      return _json({'ok': true});
    }
    if (options.path == '/chat' && options.method == 'POST') {
      final body = Map<String, dynamic>.from(options.data as Map);
      posts.add(body);
      final message = {
        'id': 'message-${body['client_message_id']}',
        'mine': true,
        'created_at': '2026-10-18T19:30:00Z',
        'attachments': <Object>[],
        ...body,
      };
      if (posts.length == 1 && failFirstPost) {
        if (commitBeforeFailure) messages.add(message);
        throw DioException(
          requestOptions: options,
          type: DioExceptionType.receiveTimeout,
        );
      }
      if (!messages.any((m) => m['id'] == message['id'])) messages.add(message);
      final delayed = delayedPost;
      delayedPost = null;
      if (delayed != null) return delayed.future;
      return _json(message);
    }
    if (options.path == '/chat/unread') return _json({'count': 0});
    throw StateError('Unexpected request ${options.method} ${options.path}');
  }

  ResponseBody _json(Object data) => ResponseBody.fromString(
    jsonEncode(data),
    200,
    headers: {
      Headers.contentTypeHeader: [Headers.jsonContentType],
    },
  );

  @override
  void close({bool force = false}) {}
}
