import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/realtime_bridge.dart';
import 'package:vecher_app/core/realtime_provider.dart';
import 'package:vecher_app/core/session_changes.dart';
import 'package:vecher_app/features/today/today_screen.dart';
import 'package:vecher_app/features/wishes/evening_screen.dart';
import 'package:vecher_app/features/wishes/plan_screen.dart';

import 'support/fake_realtime.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late ByteData testFont;

  setUpAll(() async {
    final config = File('.dart_tool/package_config.json').absolute;
    final packages = jsonDecode(await config.readAsString()) as Map;
    final flutterTest = (packages['packages'] as List).cast<Map>().singleWhere(
      (p) => p['name'] == 'flutter_test',
    );
    final root = flutterTest['rootUri'] as String;
    testFont = ByteData.sublistView(
      await File.fromUri(
        config.uri
            .resolve(root.endsWith('/') ? root : '$root/')
            .resolve('../flutter_tools/static/Ahem.ttf'),
      ).readAsBytes(),
    );
  });

  setUp(() {
    FlutterSecureStorage.setMockInitialValues({'vecher_token': 'test-account'});
    PinGate.unlock();
    GoogleFonts.config.allowRuntimeFetching = false;
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMessageHandler('flutter/assets', (message) async {
          final key = const StringCodec().decodeMessage(message);
          if (key == 'AssetManifest.bin') {
            return const StandardMessageCodec().encodeMessage({
              'test/Comfortaa-Regular.ttf': [
                {'asset': 'test/Comfortaa-Regular.ttf'},
              ],
            });
          }
          if (key == 'test/Comfortaa-Regular.ttf') return testFont;
          return null;
        });
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMessageHandler('flutter/assets', null);
    GoogleFonts.config.allowRuntimeFetching = true;
    if (sessionChanges.isChanging) {
      sessionChanges.finish(sessionChanges.revision);
    }
  });

  testWidgets(
    'chosen plan becomes a saved apart evening, survives reopening and records a private result',
    (tester) async {
      final api = _EveningAdapter()..scheduled = '2026-10-24T17:00:00Z';
      final harness = await _mount(tester, api, start: '/wishes/plan/42');
      await _tap(tester, 'Начать вечер');
      expect(find.textContaining('уберём прежнюю дату'), findsOneWidget);
      await _tap(tester, 'Отдельно — начать');
      expect(api.scheduled, isNull);
      expect(api.planLoads, greaterThan(1));
      expect(api.posts.single.path, '/plans/42/start');
      expect((api.posts.single.data as Map)['mode'], 'apart');
      expect((api.posts.single.data as Map)['option'], 0);
      expect(find.text('Налейте любимый чай.'), findsOneWidget);
      await _tap(tester, 'Сделали');
      expect(find.text('Выберите песню друг для друга.'), findsOneWidget);

      // Reopen from a different route: progress comes from REST, not widget memory.
      harness.router.go('/today');
      await tester.pumpAndSettle();
      expect(find.text('ВАШ ВЕЧЕР ПРОДОЛЖАЕТСЯ'), findsOneWidget);
      await _tap(tester, 'Продолжить вечер');
      expect(find.text('Выберите песню друг для друга.'), findsOneWidget);
      await _tap(tester, 'Поставить на паузу');
      expect(find.text('Можно не торопиться'), findsOneWidget);
      expect(find.text('Сделали'), findsNothing);
      await _tap(tester, 'Возобновить вечер');
      await _tap(tester, 'Пропустить этот шаг');
      expect(find.text('Все шаги пройдены'), findsOneWidget);
      await _tap(tester, 'Завершить вечер');
      await _tap(tester, 'Завершить');
      expect(api.evening!['status'], 'completed');
      expect(find.text('Ваш вечер сохранён'), findsOneWidget);
      await _tap(tester, 'Понравилось');
      await _tap(tester, 'Мне хочется повторить');
      await _tap(tester, 'Сохранить только для себя');
      expect(api.posts.last.path, '/evenings/evening-1/feedback');
      expect(api.posts.last.data, {'feeling': 'liked', 'want_repeat': true});
      expect(find.text('Твоя оценка сохранена'), findsOneWidget);
      expect(find.textContaining('партнёр её не увидит'), findsOneWidget);

      harness.router.go('/us/evenings');
      await tester.pumpAndSettle();
      expect(find.text('Чай и музыка'), findsOneWidget);
      await _tap(tester, 'Открыть этот вечер');
      final beforeRepeat = api.posts.length;
      await _tap(tester, 'Выбрать этот план ещё раз');
      expect(find.text('Начать вечер'), findsOneWidget);
      expect(api.posts, hasLength(beforeRepeat));
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    },
  );

  testWidgets(
    'stale revision refreshes steps without silently repeating an action',
    (tester) async {
      final api = _EveningAdapter()
        ..evening = _fixture()
        ..conflictNextStep = true;
      await _mount(tester, api);
      await _tap(tester, 'Сделали');
      expect(find.textContaining('на другом телефоне'), findsOneWidget);
      expect(find.text('Выберите песню друг для друга.'), findsOneWidget);
      expect(api.posts, hasLength(1));
      expect(api.posts.first.data, {
        'revision': 1,
        'index': 0,
        'status': 'done',
      });
      await _tap(tester, 'Сделали');
      expect(api.posts.last.data, {
        'revision': 2,
        'index': 1,
        'status': 'done',
      });
      expect(find.text('Все шаги пройдены'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets(
    'offline step leaves the current step pending until a successful retry',
    (tester) async {
      final api = _EveningAdapter()
        ..evening = _fixture()
        ..offlineNextStep = true;
      await _mount(tester, api);
      await _tap(tester, 'Сделали');
      expect(
        find.text(
          'Нет связи с сервером. Проверь подключение и попробуй ещё раз',
        ),
        findsOneWidget,
      );
      expect(find.text('Налейте любимый чай.'), findsOneWidget);
      expect((api.evening!['steps'] as List).first['status'], 'pending');
      await _tap(tester, 'Сделали');
      expect(find.text('Выберите песню друг для друга.'), findsOneWidget);
      expect(api.posts.map((p) => (p.data as Map)['revision']), [1, 1]);
      await _unmount(tester);
    },
  );

  testWidgets('a lost start response retries with the same idempotency key', (
    tester,
  ) async {
    final api = _EveningAdapter()..loseNextStartResponse = true;
    await _mount(tester, api, start: '/wishes/plan/42');
    await _tap(tester, 'Начать вечер');
    await _tap(tester, 'Рядом — начать');
    expect(
      find.text('Нет связи с сервером. Проверь подключение и попробуй ещё раз'),
      findsOneWidget,
    );
    expect(api.posts, hasLength(1));
    await _tap(tester, 'Начать вечер');
    await _tap(tester, 'Рядом — начать');
    expect(api.posts, hasLength(2));
    expect(
      (api.posts.first.data as Map)['client_id'],
      (api.posts.last.data as Map)['client_id'],
    );
    expect(find.text('Налейте любимый чай.'), findsOneWidget);
    await _unmount(tester);
  });

  testWidgets(
    'a realtime acknowledgement before the start response does not lose navigation',
    (tester) async {
      final api = _EveningAdapter();
      final hold = api.holdStartResponse = Completer<void>();
      final harness = await _mount(
        tester,
        api,
        start: '/wishes/plan/42',
        bridge: true,
      );
      harness.realtime.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      api.onStartCommitted = () => harness.realtime.transport.publish(1, [
        'plans',
        'today',
        'us',
        'wishes',
      ]);
      await _tap(tester, 'Начать вечер');
      await tester.tap(find.text('Рядом — начать'));
      await tester.pump(const Duration(milliseconds: 300));
      await tester.pump(const Duration(milliseconds: 300));
      hold.complete();
      await tester.pumpAndSettle();
      expect(find.text('Наш вечер'), findsOneWidget);
      expect(find.text('Налейте любимый чай.'), findsOneWidget);
      expect(api.posts, hasLength(1));
      await _unmount(tester);
    },
  );

  testWidgets(
    'changed votes refresh the source plan and remove stale start consent',
    (tester) async {
      final api = _EveningAdapter()..conflictNextStart = true;
      await _mount(tester, api, start: '/wishes/plan/42');
      await _tap(tester, 'Начать вечер');
      await _tap(tester, 'Рядом — начать');
      expect(api.posts, hasLength(1));
      expect(api.evening, isNull);
      expect(api.planLoads, greaterThan(1));
      expect(find.text('Начать вечер'), findsNothing);
      expect(
        find.text('Вы выбрали разное — договоритесь и выберите один.'),
        findsOneWidget,
      );
      await _unmount(tester);
    },
  );

  testWidgets(
    'early completion explains skipped steps and needs the final tap',
    (tester) async {
      final api = _EveningAdapter()..evening = _fixture();
      await _mount(tester, api);
      await _tap(tester, 'Завершить вечер');
      expect(
        find.textContaining('Оставшиеся шаги будут пропущены'),
        findsOneWidget,
      );
      expect(api.posts, isEmpty);
      await _tap(tester, 'Вернуться');
      expect(api.posts, isEmpty);
      await _tap(tester, 'Завершить вечер');
      await _tap(tester, 'Завершить');
      expect(api.posts.single.path, '/evenings/evening-1/complete');
      expect((api.evening!['steps'] as List).map((s) => s['status']), [
        'skipped',
        'skipped',
      ]);
      await _unmount(tester);
    },
  );

  testWidgets(
    'stopping saves an unfinished evening without asking for a rating',
    (tester) async {
      final api = _EveningAdapter()..evening = _fixture();
      await _mount(tester, api);
      await _tap(tester, 'Остановить без завершения');
      await _tap(tester, 'Остановить');
      expect(api.posts.single.path, '/evenings/evening-1/cancel');
      expect(find.text('Вечер остановлен'), findsOneWidget);
      expect(find.text('Как тебе этот вечер?'), findsNothing);
      await _unmount(tester);
    },
  );

  testWidgets(
    'old API keeps the old plan functional and explains unavailable history',
    (tester) async {
      final api = _EveningAdapter()..supported = false;
      final harness = await _mount(tester, api, start: '/wishes/plan/42');
      expect(find.text('Начать вечер'), findsNothing);
      expect(find.text('Чай и музыка'), findsOneWidget);
      harness.router.go('/us/evenings');
      await tester.pumpAndSettle();
      expect(find.textContaining('после обновления сервиса'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsNothing);
      harness.router.go('/today');
      await tester.pumpAndSettle();
      expect(find.text('Продолжить вечер'), findsNothing);
      expect(api.posts, isEmpty);
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    },
  );

  testWidgets(
    'history pagination requests the server cursor and can return to newer evenings',
    (tester) async {
      final api = _EveningAdapter()
        ..evening = _fixture(status: 'completed')
        ..twoHistoryPages = true;
      await _mount(tester, api, start: '/us/evenings');
      await _tap(tester, 'Более ранние вечера');
      expect(api.historyCursors.last, 'older-page');
      expect(find.text('Тихая прогулка'), findsOneWidget);
      await _tap(tester, 'Более новые вечера');
      expect(find.text('Чай и музыка'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets('late private response cannot reappear after changing accounts', (
    tester,
  ) async {
    final api = _EveningAdapter()..evening = _fixture();
    final delayed = api.heldDetail = Completer<ResponseBody>();
    await _mount(tester, api, settle: false);
    for (var attempt = 0; attempt < 10 && api.detailLoads == 0; attempt++) {
      await tester.pump(const Duration(milliseconds: 20));
    }
    expect(api.detailLoads, greaterThan(0));
    final revision = sessionChanges.begin();
    await tester.pump();
    api.detailStatus = 404;
    api.heldDetail = null;
    sessionChanges.finish(revision);
    delayed.complete(_json(_fixture()));
    await tester.pumpAndSettle();
    expect(find.text('Чай и музыка'), findsNothing);
    expect(find.text('Налейте любимый чай.'), findsNothing);
    expect(find.text('Этот вечер недоступен'), findsOneWidget);
    await _unmount(tester);
  });

  testWidgets(
    'pair invalidation discards a delayed response from the previous pair',
    (tester) async {
      final api = _EveningAdapter()..evening = _fixture();
      final delayed = api.heldDetail = Completer<ResponseBody>();
      final harness = await _mount(tester, api, settle: false);
      harness.realtime.controller.activate('test-account');
      await tester.pump();
      harness.realtime.transport.subscribed();
      for (var attempt = 0; attempt < 10 && api.detailLoads == 0; attempt++) {
        await tester.pump(const Duration(milliseconds: 20));
      }
      expect(api.detailLoads, greaterThan(0));
      api.detailStatus = 404;
      api.heldDetail = null;
      harness.realtime.transport.publish(1, ['pair']);
      await tester.pump(const Duration(milliseconds: 200));
      delayed.complete(_json(_fixture()));
      await tester.pumpAndSettle();
      expect(find.text('Чай и музыка'), findsNothing);
      expect(find.text('Налейте любимый чай.'), findsNothing);
      expect(find.text('Этот вечер недоступен'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets(
    'PIN gate makes no private request before unlock and then checks permission',
    (tester) async {
      final api = _EveningAdapter()
        ..evening = _fixture()
        ..detailStatus = 403;
      FlutterSecureStorage.setMockInitialValues({
        'vecher_token': 'test-account',
        'vecher_pin': '1234',
      });
      await _mount(tester, api);
      expect(PinGate.isLocked, isTrue);
      expect(api.detailLoads, 0);
      expect(find.text('Налейте любимый чай.'), findsNothing);
      PinGate.unlock();
      await tester.pumpAndSettle();
      expect(api.detailLoads, greaterThan(0));
      expect(find.text('Налейте любимый чай.'), findsNothing);
      expect(find.text('Этот вечер недоступен'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets('plans realtime event refreshes the open evening from REST', (
    tester,
  ) async {
    final api = _EveningAdapter()..evening = _fixture();
    final harness = await _mount(tester, api, bridge: true);
    harness.realtime.transport.subscribed();
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pumpAndSettle();
    (api.evening!['steps'] as List).first['status'] = 'done';
    api.evening!['revision'] = 2;
    harness.realtime.transport.publish(1, ['plans']);
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pumpAndSettle();
    expect(find.text('Выберите песню друг для друга.'), findsOneWidget);
    expect(api.posts, isEmpty);
    await _unmount(tester);
  });
}

Future<void> _tap(WidgetTester tester, String text) async {
  final target = find.text(text);
  await tester.ensureVisible(target);
  await tester.tap(target);
  await tester.pumpAndSettle();
}

class _Harness {
  const _Harness(this.router, this.realtime);
  final GoRouter router;
  final FakeRealtime realtime;
}

Future<_Harness> _mount(
  WidgetTester tester,
  _EveningAdapter api, {
  String start = '/evenings/evening-1',
  bool settle = true,
  bool bridge = false,
}) async {
  tester.view.physicalSize = const Size(600, 1100);
  tester.view.devicePixelRatio = 1;
  tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
    ..httpClientAdapter = api;
  final realtime = FakeRealtime();
  final router = GoRouter(
    initialLocation: start,
    routes: [
      GoRoute(
        path: '/evenings/:id',
        builder: (_, state) =>
            EveningScreen(eveningId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/wishes/plan/:id',
        builder: (_, state) =>
            PlanScreen(cardId: int.parse(state.pathParameters['id']!)),
      ),
      GoRoute(
        path: '/us/evenings',
        builder: (_, __) => const EveningHistoryScreen(),
      ),
      GoRoute(
        path: '/today',
        builder: (_, __) => const Scaffold(body: CurrentEveningCard()),
      ),
    ],
  );
  addTearDown(dio.close);
  addTearDown(realtime.controller.dispose);
  addTearDown(router.dispose);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        dioProvider.overrideWithValue(dio),
        realtimeControllerProvider.overrideWithValue(realtime.controller),
        pairProvider.overrideWith(
          (_) async => {'id': 'pair-1', 'status': 'active'},
        ),
      ],
      child: MaterialApp.router(
        theme: ThemeData.dark(),
        routerConfig: router,
        builder: (context, child) =>
            PinGate(child: bridge ? RealtimeBridge(child: child!) : child!),
      ),
    ),
  );
  if (settle) await tester.pumpAndSettle();
  return _Harness(router, realtime);
}

Future<void> _unmount(WidgetTester tester) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
}

Map<String, dynamic> _fixture({String status = 'active'}) => {
  'id': 'evening-1',
  'card_id': 42,
  'title': 'Вечер дома',
  'option_index': 0,
  'option_title': 'Чай и музыка',
  'mode': 'apart',
  'status': status,
  'revision': 1,
  'steps': [
    {'index': 0, 'text': 'Налейте любимый чай.', 'status': 'pending'},
    {'index': 1, 'text': 'Выберите песню друг для друга.', 'status': 'pending'},
  ],
  'items': <String>[],
  'items_title': null,
  'note': '',
  'started_at': '2026-10-22T17:00:00Z',
  'updated_at': '2026-10-22T17:00:00Z',
  'completed_at': status == 'completed' ? '2026-10-22T18:00:00Z' : null,
  'my_feedback': null,
};

class _EveningAdapter implements HttpClientAdapter {
  Map<String, dynamic>? evening;
  bool supported = true,
      conflictNextStep = false,
      offlineNextStep = false,
      loseNextStartResponse = false,
      twoHistoryPages = false;
  int detailStatus = 200, detailLoads = 0, planLoads = 0;
  bool conflictNextStart = false;
  int? chosen = 0;
  String? scheduled;
  VoidCallback? onStartCommitted;
  Completer<void>? holdStartResponse;
  Completer<ResponseBody>? heldDetail;
  final posts = <RequestOptions>[];
  final historyCursors = <String?>[];

  Map<String, dynamic> get plan => {
    'card_id': 42,
    'text': 'Вечер дома',
    'deck_title': 'Дома',
    'kind': 'yes',
    'intro': 'Выберите подходящий вариант.',
    'locked': false,
    'premium': false,
    'core_plans_unlimited': true,
    if (supported) 'evening_tracking_available': true,
    'my_vote': 0,
    'partner_vote': chosen == null ? 1 : 0,
    'chosen': chosen,
    'places_available': false,
    'scheduled_at': scheduled,
    'options': [
      {
        'index': 0,
        'title': 'Чай и музыка',
        'note': '',
        'items': <String>[],
        'steps': ['Налейте любимый чай.', 'Выберите песню друг для друга.'],
      },
    ],
  };

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    if (requestStream != null) await requestStream.drain<void>();
    final path = options.path;
    if (options.method == 'GET') {
      if (path == '/pairs/me') {
        return _json({'my_name': 'Аня', 'partner_name': 'Саша'});
      }
      if (path == '/plans/42') {
        planLoads++;
        return _json(plan);
      }
      if (path == '/evenings/current') {
        if (!supported) return _json({'detail': 'Not Found'}, status: 404);
        return _json(
          evening != null && {'active', 'paused'}.contains(evening!['status'])
              ? evening
              : null,
        );
      }
      if (path == '/evenings/history') {
        if (!supported) return _json({'detail': 'Not Found'}, status: 404);
        final before = options.queryParameters['before'] as String?;
        historyCursors.add(before);
        return _json({
          'items': [
            if (evening != null)
              {
                ...evening!,
                if (before != null) 'option_title': 'Тихая прогулка',
              },
          ],
          'next_before': twoHistoryPages && before == null
              ? 'older-page'
              : null,
        });
      }
      if (path == '/evenings/evening-1') {
        detailLoads++;
        if (heldDetail != null) return heldDetail!.future;
        if (detailStatus != 200) {
          return _json({
            'detail': 'Этот вечер недоступен',
          }, status: detailStatus);
        }
        return _json(evening);
      }
    }
    if (options.method == 'POST') {
      posts.add(options);
      final body = options.data as Map;
      if (path == '/plans/42/start') {
        if (conflictNextStart) {
          conflictNextStart = false;
          chosen = null;
          return _json({
            'detail': 'Выбранный вариант уже изменился',
          }, status: 409);
        }
        if (evening == null) {
          evening = _fixture()..['mode'] = body['mode'];
          scheduled = null;
          onStartCommitted?.call();
          await holdStartResponse?.future;
        }
        if (loseNextStartResponse) {
          loseNextStartResponse = false;
          throw DioException(
            requestOptions: options,
            type: DioExceptionType.connectionError,
          );
        }
        return _json(evening);
      }
      final action = path.split('/').last;
      if (action == 'feedback') {
        evening!['my_feedback'] = Map<String, dynamic>.from(body);
        return _json(evening);
      }
      if (action == 'step' && offlineNextStep) {
        offlineNextStep = false;
        throw DioException(
          requestOptions: options,
          type: DioExceptionType.connectionError,
        );
      }
      if (action == 'step' && conflictNextStep) {
        conflictNextStep = false;
        (evening!['steps'] as List).first['status'] = 'done';
        evening!['revision'] = 2;
        return _json({'detail': 'Вечер уже изменился'}, status: 409);
      }
      if (body['revision'] != evening!['revision']) {
        throw StateError('Unexpected client revision');
      }
      if (action == 'step') {
        (evening!['steps'] as List)[body['index'] as int]['status'] =
            body['status'];
      }
      if (action == 'pause') evening!['status'] = 'paused';
      if (action == 'resume') evening!['status'] = 'active';
      if (action == 'cancel') evening!['status'] = 'cancelled';
      if (action == 'complete') {
        evening!['status'] = 'completed';
        for (final step in evening!['steps'] as List) {
          if (step['status'] == 'pending') step['status'] = 'skipped';
        }
        evening!['completed_at'] = '2026-10-22T18:00:00Z';
      }
      evening!['revision'] = (evening!['revision'] as int) + 1;
      return _json(evening);
    }
    throw StateError('Unexpected ${options.method} $path');
  }

  @override
  void close({bool force = false}) {}
}

ResponseBody _json(Object? data, {int status = 200}) => ResponseBody.fromString(
  jsonEncode(data),
  status,
  headers: {
    Headers.contentTypeHeader: [Headers.jsonContentType],
  },
);
