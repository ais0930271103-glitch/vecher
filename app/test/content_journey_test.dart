import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/chat_presence.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/realtime_provider.dart';
import 'package:vecher_app/core/session_changes.dart';
import 'package:vecher_app/features/chat/chat_screen.dart';
import 'package:vecher_app/features/content/content.dart';
import 'package:vecher_app/features/content/content_author_screen.dart';
import 'package:vecher_app/features/content/content_preferences_screen.dart';
import 'package:vecher_app/features/content/content_screen.dart';
import 'package:vecher_app/features/content/publication_screen.dart';
import 'package:vecher_app/features/today/today_screen.dart';

import 'support/fake_realtime.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({
      'vecher_token': 'account-a',
      'vecher_chat_hint_seen': '1',
    });
    PinGate.unlock();
  });
  tearDown(() {
    if (sessionChanges.isChanging) {
      sessionChanges.finish(sessionChanges.revision);
    }
  });

  test(
    'sources permit HTTPS without credentials and sharing is ordinary text',
    () {
      for (final value in [
        'http://example.org/source',
        'javascript:alert(1)',
        'file:///private',
        'https://user:secret@example.org/',
        'https://example.org:8000/',
      ]) {
        expect(contentSourceUri(value), isNull, reason: value);
      }
      expect(contentSourceUri('https://example.org/source'), isNotNull);
      final item = Publication.fromJson(
        _publication('one', 'Практический план'),
      );
      expect(
        item.shareDraft,
        'Практический план\n\nКороткое объяснение, которое можно применить сегодня.',
      );
      expect(
        contentLocation(seriesId: 'home-start', title: 'Дом и порядок'),
        '/content?series_id=home-start&title=%D0%94%D0%BE%D0%BC+%D0%B8+%D0%BF%D0%BE%D1%80%D1%8F%D0%B4%D0%BE%D0%BA',
      );
    },
  );

  testWidgets(
    'personal themes and language save explicitly without touching shared choices',
    (tester) async {
      final api = _ContentApi()..personalTopics = [];
      await _mount(tester, api, path: '/content/preferences');
      expect(
        find.textContaining('Личные интересы сюда не копируются'),
        findsNothing,
      );
      await tester.tap(find.byKey(const ValueKey('content-topic-digital')));
      await tester.tap(find.byKey(const ValueKey('content-language-all')));
      await _tap(tester, 'Сохранить мои интересы');
      expect(api.settingsChanges, [
        {
          'scope': 'personal',
          'topics': ['digital'],
          'language': 'all',
          'revision': 0,
        },
      ]);
      expect(api.sharedTopics, ['home']);
      expect(api.messages, isEmpty);
      await _unmount(tester);
    },
  );

  testWidgets(
    'shared settings conflict reloads current revision before a second deliberate save',
    (tester) async {
      final api = _ContentApi()..conflictShared = true;
      await _mount(tester, api, path: '/content/preferences?scope=shared');
      await tester.tap(find.byKey(const ValueKey('content-topic-digital')));
      await _tap(tester, 'Сохранить общие темы');
      expect(find.textContaining('Загружен актуальный выбор'), findsOneWidget);
      final home = tester.widget<FilterChip>(
        find.byKey(const ValueKey('content-topic-home')),
      );
      final digital = tester.widget<FilterChip>(
        find.byKey(const ValueKey('content-topic-digital')),
      );
      expect(home.selected, isFalse);
      expect(digital.selected, isTrue);
      expect(api.settingsChanges, hasLength(1));
      await _tap(tester, 'Сохранить общие темы');
      expect(api.settingsChanges.last['revision'], 1);
      expect(api.settingsChanges.last['topics'], ['digital']);
      expect(api.personalTopics, ['home']);
      await _unmount(tester);
    },
  );

  testWidgets(
    'reader renders native blocks and private useful/save/hide controls never send',
    (tester) async {
      final api = _ContentApi();
      final harness = await _mount(tester, api, path: '/content/one');
      expect(find.text('Практический план'), findsOneWidget);
      expect(find.text('Один маленький шаг'), findsOneWidget);
      await _tap(tester, 'Полезно');
      await _tap(tester, 'Сохранить');
      await _tap(tester, 'Не показывать в подборках');
      expect(api.stateChanges, [
        {'useful': true},
        {'saved': true},
        {'hidden': true},
      ]);
      expect(api.messages, isEmpty);
      expect(
        api.requests.every((r) => r.uri.host == 'api.example.invalid'),
        isTrue,
      );
      harness.router.go('/content?mode=saved');
      await tester.pumpAndSettle();
      expect(find.text('Практический план'), findsOneWidget);
      await _tap(tester, 'Практический план');
      await _tap(tester, 'Вернуть в подборки');
      expect(api.items.first['hidden'], isFalse);
      expect(api.items.first['saved'], isTrue);
      await _unmount(tester);
    },
  );

  testWidgets(
    'author browse is explicit and follow/hide can be reversed without leaking to partner',
    (tester) async {
      final api = _ContentApi()..personalTopics = ['digital'];
      final harness = await _mount(
        tester,
        api,
        path: '/content/authors/vecher-practicum',
      );
      expect(find.text('Практический план'), findsOneWidget);
      expect(api.feedQueries.last['author_id'], 'vecher-practicum');
      expect(api.feedQueries.last['mode'], 'personal');
      await _tap(tester, 'Подписаться');
      expect(api.followed, isTrue);
      await _tap(tester, 'Скрыть автора');
      expect(api.hiddenAuthor, isTrue);
      await _tap(tester, 'Вернуть в подборки');
      expect(api.hiddenAuthor, isFalse);
      harness.router.go('/content?mode=following');
      await tester.pumpAndSettle();
      expect(find.text('Практический план'), findsOneWidget);
      expect(api.messages, isEmpty);
      expect(api.sharedTopics, ['home']);
      await _unmount(tester);
    },
  );

  testWidgets(
    'empty interest match is honest and search remains bound to selected feed',
    (tester) async {
      final api = _ContentApi()..personalTopics = ['digital'];
      await _mount(tester, api);
      expect(find.text('Пока нет подходящих публикаций'), findsOneWidget);
      expect(find.text('Практический план'), findsNothing);
      expect(api.feedQueries.every((q) => q['mode'] == 'personal'), isTrue);
      await tester.enterText(
        find.byKey(const ValueKey('content-search')),
        'Невозможное',
      );
      await tester.testTextInput.receiveAction(TextInputAction.search);
      await tester.pumpAndSettle();
      expect(api.feedQueries.last['q'], 'Невозможное');
      expect(find.text('По запросу ничего не найдено'), findsOneWidget);
      expect(api.requests.where((r) => r.method != 'GET'), isEmpty);
      await _unmount(tester);
    },
  );

  testWidgets(
    'expired load-more cursor refreshes from start rather than merging two snapshots',
    (tester) async {
      final api = _ContentApi()
        ..paged = true
        ..conflictCursor = true;
      await _mount(tester, api);
      expect(find.text('Практический план'), findsOneWidget);
      await _tap(tester, 'Ещё публикации');
      expect(find.textContaining('Подборка изменилась'), findsOneWidget);
      expect(find.text('Обновлённый практикум'), findsOneWidget);
      expect(find.text('Практический план'), findsNothing);
      expect(
        api.feedQueries.where((q) => q.containsKey('cursor')),
        hasLength(1),
      );
      expect(api.feedQueries.last.containsKey('cursor'), isFalse);
      await _unmount(tester);
    },
  );

  testWidgets('pagination appends once and searching resets cursor', (
    tester,
  ) async {
    final api = _ContentApi()..paged = true;
    await _mount(tester, api);
    await _tap(tester, 'Ещё публикации');
    expect(find.text('Практический план'), findsOneWidget);
    expect(find.text('Вторая полезная публикация'), findsOneWidget);
    await tester.ensureVisible(find.byKey(const ValueKey('content-search')));
    await tester.enterText(
      find.byKey(const ValueKey('content-search')),
      'Вторая',
    );
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await tester.pumpAndSettle();
    expect(api.feedQueries.last['q'], 'Вторая');
    expect(api.feedQueries.last.containsKey('cursor'), isFalse);
    expect(find.text('Практический план'), findsNothing);
    await _unmount(tester);
  });

  testWidgets(
    'late feed from previous account and background content are discarded',
    (tester) async {
      final api = _ContentApi();
      final held = Completer<ResponseBody>();
      api.heldFeed = held;
      await _mount(tester, api, settle: false);
      await tester.pump(const Duration(milliseconds: 50));
      final old = List<Map<String, dynamic>>.from(api.items);
      final revision = sessionChanges.begin();
      await tester.pump();
      api.heldFeed = null;
      api.items = [_publication('new', 'Подборка другого аккаунта')];
      sessionChanges.finish(revision);
      held.complete(
        _json({'items': old, 'next_cursor': null, 'total': old.length}),
      );
      await tester.pumpAndSettle();
      expect(find.text('Практический план'), findsNothing);
      expect(find.text('Подборка другого аккаунта'), findsOneWidget);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      await tester.pump();
      expect(find.text('Подборка другого аккаунта'), findsNothing);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pumpAndSettle();
      expect(find.text('Подборка другого аккаунта'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets('share opens editable ordinary text and sends only after Send', (
    tester,
  ) async {
    final api = _ContentApi();
    await _mount(tester, api, path: '/content/one');
    await _tap(tester, 'Обсудить с партнёром');
    expect(api.messages, isEmpty);
    final composer = tester.widget<TextField>(find.byType(TextField).last);
    expect(composer.controller!.text, startsWith('Практический план\n\n'));
    await tester.tap(find.byTooltip('Отправить'));
    await tester.pumpAndSettle();
    expect(api.messages, hasLength(1));
    expect(api.messages.single['text'], startsWith('Практический план\n\n'));
    expect(api.messages.single['ref_kind'], isNull);
    expect(api.messages.single['client_message_id'], isNotEmpty);
    await _unmount(tester);
  });

  testWidgets('publication chat draft is cleared on account switch', (
    tester,
  ) async {
    final api = _ContentApi();
    await _mount(tester, api, path: '/content/one');
    await _tap(tester, 'Обсудить с партнёром');
    final revision = sessionChanges.begin();
    await tester.pump();
    sessionChanges.finish(revision);
    await tester.pumpAndSettle();
    final composer = tester.widget<TextField>(find.byType(TextField).last);
    expect(composer.controller!.text, isEmpty);
    expect(api.messages, isEmpty);
    await _unmount(tester);
  });

  testWidgets(
    'PIN blocks content requests and removes an already visible private feed',
    (tester) async {
      FlutterSecureStorage.setMockInitialValues({
        'vecher_token': 'account-a',
        'vecher_pin': '1234',
      });
      final api = _ContentApi();
      await _mount(tester, api);
      expect(api.requests, isEmpty);
      expect(find.text('Практический план'), findsNothing);
      PinGate.unlock();
      await tester.pumpAndSettle();
      expect(find.text('Практический план'), findsOneWidget);
      (PinGate.lockedListenable as ValueNotifier<bool>).value = true;
      await tester.pumpAndSettle();
      expect(find.text('Практический план'), findsNothing);
      final requestCount = api.requests.length;
      await tester.pump(const Duration(seconds: 2));
      expect(api.requests.length, requestCount);
      PinGate.unlock();
      await tester.pumpAndSettle();
      expect(find.text('Практический план'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets(
    'pair invalidation tears down accumulated shared publications before reload',
    (tester) async {
      final api = _ContentApi();
      final harness = await _mount(tester, api, path: '/content?mode=shared');
      harness.realtime.controller.activate('account-a');
      await tester.pumpAndSettle();
      harness.realtime.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      final held = Completer<ResponseBody>();
      api.heldFeed = held;
      harness.realtime.transport.publish(1, ['pair']);
      await tester.pump(const Duration(milliseconds: 200));
      expect(find.text('Практический план'), findsNothing);
      api.heldFeed = null;
      api.items = [_publication('pair-new', 'Общие темы новой пары')];
      held.complete(
        _json({'items': api.items, 'next_cursor': null, 'total': 1}),
      );
      await tester.pumpAndSettle();
      expect(find.text('Общие темы новой пары'), findsOneWidget);
      expect(find.text('Практический план'), findsNothing);
      await _unmount(tester);
    },
  );

  testWidgets(
    'reader/feed/preferences remain usable on a narrow screen with large text',
    (tester) async {
      final api = _ContentApi();
      final h = await _mount(
        tester,
        api,
        size: const Size(320, 800),
        textScale: 1.5,
      );
      expect(tester.takeException(), isNull);
      h.router.go('/content/one');
      await tester.pumpAndSettle();
      await _tap(tester, 'Сохранить');
      expect(tester.takeException(), isNull);
      h.router.go('/content/preferences');
      await tester.pumpAndSettle();
      await _tap(tester, 'Сохранить мои интересы');
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    },
  );

  testWidgets(
    'failed feed has a working retry without starting a different selection',
    (tester) async {
      final api = _ContentApi()..failFeed = true;
      await _mount(tester, api);
      expect(find.text('Практический план'), findsNothing);
      api.failFeed = false;
      await _tap(tester, 'Повторить загрузку');
      expect(find.text('Практический план'), findsOneWidget);
      expect(api.feedQueries.every((q) => q['mode'] == 'personal'), isTrue);
      await _unmount(tester);
    },
  );
}

Future<void> _tap(WidgetTester tester, String text) async {
  final target = find.text(text);
  if (target.evaluate().isEmpty) {
    final scrollable = find
        .descendant(
          of: find.byType(ListView).last,
          matching: find.byType(Scrollable),
        )
        .first;
    tester.state<ScrollableState>(scrollable).position.jumpTo(0);
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(target, 300, scrollable: scrollable);
  }
  await tester.ensureVisible(target.last);
  await tester.pumpAndSettle();
  await tester.tap(target.last);
  await tester.pumpAndSettle();
}

Future<void> _unmount(WidgetTester tester) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pumpAndSettle();
}

class _Harness {
  _Harness(this.router, this.realtime);
  final GoRouter router;
  final FakeRealtime realtime;
}

Future<_Harness> _mount(
  WidgetTester tester,
  _ContentApi api, {
  String path = '/content',
  Size size = const Size(420, 920),
  double textScale = 1,
  bool settle = true,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
    ..httpClientAdapter = api;
  final realtime = FakeRealtime();
  final router = GoRouter(
    initialLocation: path,
    observers: [chatRouteObserver],
    routes: [
      GoRoute(
        path: '/content',
        builder: (_, state) => ContentScreen(
          mode: ContentMode.parse(state.uri.queryParameters['mode']),
          authorId: state.uri.queryParameters['author_id'],
          seriesId: state.uri.queryParameters['series_id'],
          title: state.uri.queryParameters['title'],
        ),
      ),
      GoRoute(
        path: '/content/preferences',
        builder: (_, state) => ContentPreferencesScreen(
          shared: state.uri.queryParameters['scope'] == 'shared',
        ),
      ),
      GoRoute(
        path: '/content/authors',
        builder: (_, __) => const ContentAuthorsScreen(),
      ),
      GoRoute(
        path: '/content/authors/:id',
        builder: (_, state) =>
            ContentScreen(authorId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/content/:id',
        builder: (_, state) =>
            PublicationScreen(id: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/chat',
        builder: (_, state) => ChatScreen(attach: state.extra as Map?),
      ),
      GoRoute(
        path: '/videos',
        builder: (_, __) =>
            const Scaffold(body: Text('Legacy videos remain available')),
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
          (_) async => {
            'pair_id': 'pair-one',
            'status': 'active',
            'partner_name': 'Саша',
          },
        ),
      ],
      child: MaterialApp.router(
        theme: ThemeData(brightness: Brightness.dark, useMaterial3: true),
        routerConfig: router,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: TextScaler.linear(textScale)),
          child: PinGate(child: child!),
        ),
      ),
    ),
  );
  if (settle) await tester.pumpAndSettle();
  return _Harness(router, realtime);
}

Map<String, dynamic> _publication(String id, String title) => {
  'id': id,
  'kind': 'guide',
  'title': title,
  'summary': 'Короткое объяснение, которое можно применить сегодня.',
  'author': {
    'id': 'vecher-practicum',
    'name': 'Вечер · практикум',
    'bio': 'Редакция проекта с открытыми источниками.',
    'url': null,
  },
  'topics': ['home'],
  'language': 'ru',
  'blocks': [
    {'type': 'heading', 'text': 'Один маленький шаг'},
    {
      'type': 'paragraph',
      'text': 'Прочитай объяснение и выбери одно действие на сегодня.',
    },
    {
      'type': 'list',
      'items': ['Обсудить идею.', 'Попробовать её вместе.'],
    },
  ],
  'sources': [
    {'title': 'Первоисточник', 'url': 'https://example.org/source'},
  ],
  'published_at': '2026-10-22T17:00:00Z',
  'updated_at': '2026-10-22T17:00:00Z',
  'series_id': 'home-start',
  'series_title': 'Маленькие перемены',
  'series_order': id == 'one' ? 1 : 2,
  'reading_minutes': 3,
  'video_id': null,
  'useful': false,
  'saved': false,
  'hidden': false,
  'author_followed': false,
  'author_hidden': false,
};

ResponseBody _json(dynamic value, {int status = 200}) =>
    ResponseBody.fromString(
      jsonEncode(value),
      status,
      headers: {
        Headers.contentTypeHeader: ['application/json'],
      },
    );

class _ContentApi implements HttpClientAdapter {
  List<Map<String, dynamic>> items = [_publication('one', 'Практический план')];
  List<String> personalTopics = ['home'], sharedTopics = ['home'];
  String personalLanguage = 'ru', sharedLanguage = 'ru';
  int personalRevision = 0, sharedRevision = 0;
  bool conflictShared = false, followed = false, hiddenAuthor = false;
  bool paged = false, conflictCursor = false, failFeed = false;
  Completer<ResponseBody>? heldFeed;
  final requests = <RequestOptions>[];
  final feedQueries = <Map<String, dynamic>>[];
  final settingsChanges = <Map<String, dynamic>>[];
  final stateChanges = <Map<String, dynamic>>[];
  final messages = <Map<String, dynamic>>[];

  Map<String, dynamic> get settings => {
    'topics': [
      {'id': 'home', 'title': 'Дом'},
      {'id': 'digital', 'title': 'Цифровая жизнь'},
    ],
    'languages': [
      {'id': 'ru', 'title': 'Русский'},
      {'id': 'all', 'title': 'Все языки'},
    ],
    'personal_topics': personalTopics,
    'shared_topics': sharedTopics,
    'personal_language': personalLanguage,
    'shared_language': sharedLanguage,
    'personal_revision': personalRevision,
    'shared_revision': sharedRevision,
    'followed_author_ids': followed ? ['vecher-practicum'] : [],
    'hidden_author_ids': hiddenAuthor ? ['vecher-practicum'] : [],
    'has_pair': true,
    'catalogue_revision': 'catalogue-1',
  };
  Map<String, dynamic> get author => {
    'id': 'vecher-practicum',
    'name': 'Вечер · практикум',
    'bio': 'Редакция проекта с открытыми источниками.',
    'url': null,
    'published_count': items.length,
    'followed': followed,
    'hidden': hiddenAuthor,
  };

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? stream,
    Future<void>? cancelFuture,
  ) async {
    if (stream != null) await stream.drain<void>();
    requests.add(options);
    final path = options.path;
    final body = options.data as Map?;
    if (options.method == 'GET') {
      if (path == '/content/settings') return _json(settings);
      if (path == '/content/authors') {
        return _json({
          'items': [author],
        });
      }
      if (path == '/content/authors/vecher-practicum') return _json(author);
      if (path == '/content/feed') {
        final query = Map<String, dynamic>.from(options.queryParameters);
        feedQueries.add(query);
        if (heldFeed != null) return heldFeed!.future;
        if (failFeed) {
          return _json({'detail': 'Сервер пока недоступен'}, status: 503);
        }
        if (query['cursor'] != null && conflictCursor) {
          conflictCursor = false;
          paged = false;
          items = [_publication('new', 'Обновлённый практикум')];
          return _json({'detail': 'Подборка изменилась'}, status: 409);
        }
        if (paged && items.length == 1) {
          items.add(_publication('two', 'Вторая полезная публикация'));
        }
        final mode = query['mode'];
        var list = items
            .where((item) {
              if (mode == 'saved') return item['saved'] == true;
              if (item['hidden'] == true) return false;
              if (query['author_id'] != null || query['series_id'] != null) {
                return true;
              }
              if (hiddenAuthor) return false;
              if (mode == 'following') return followed;
              final topics = mode == 'shared' ? sharedTopics : personalTopics;
              return (item['topics'] as List).any(topics.contains);
            })
            .where(
              (item) =>
                  query['q'] == null ||
                  (item['title'] as String).toLowerCase().contains(
                    (query['q'] as String).toLowerCase(),
                  ),
            )
            .toList();
        String? cursor;
        if (paged && query['q'] == null) {
          if (query['cursor'] == null) {
            list = list.take(1).toList();
            cursor = 'page-2';
          } else {
            list = list.skip(1).toList();
          }
        }
        return _json({
          'items': list,
          'next_cursor': cursor,
          'total': list.length,
          'mode': mode,
          'catalogue_revision': 'catalogue-1',
        });
      }
      if (path.startsWith('/content/')) {
        final item = items
            .where((i) => i['id'] == path.split('/').last)
            .firstOrNull;
        return item == null
            ? _json({'detail': 'Публикация недоступна'}, status: 404)
            : _json(item);
      }
      if (path == '/chat') {
        return _json({
          'messages': [for (var i = 0; i < messages.length; i++) _message(i)],
          'has_more': false,
          'partner_read_at': null,
        });
      }
      if (path == '/chat/unread') return _json({'count': 0});
      if (path == '/calls/active') return _json(null);
    }
    if (options.method == 'PUT' && path.startsWith('/content/settings/')) {
      final shared = path.endsWith('/shared');
      settingsChanges.add({
        'scope': shared ? 'shared' : 'personal',
        ...Map<String, dynamic>.from(body!),
      });
      if (shared && conflictShared) {
        conflictShared = false;
        sharedTopics = ['digital'];
        sharedRevision++;
        return _json({'detail': 'Изменилось'}, status: 409);
      }
      if (shared) {
        sharedTopics = (body['topics'] as List).cast<String>();
        sharedLanguage = body['language'] as String;
        sharedRevision++;
      } else {
        personalTopics = (body['topics'] as List).cast<String>();
        personalLanguage = body['language'] as String;
        personalRevision++;
      }
      return _json(settings);
    }
    if (options.method == 'PATCH' && path.startsWith('/content/authors/')) {
      if (body!.containsKey('followed')) followed = body['followed'] == true;
      if (body.containsKey('hidden')) hiddenAuthor = body['hidden'] == true;
      return _json(author);
    }
    if (options.method == 'PATCH' && path.endsWith('/state')) {
      final item = items.singleWhere((i) => i['id'] == path.split('/')[2]);
      stateChanges.add(Map<String, dynamic>.from(body!));
      item.addAll(Map<String, dynamic>.from(body));
      return _json(item);
    }
    if (options.method == 'POST' && path == '/chat/read') {
      return _json({'ok': true});
    }
    if (options.method == 'POST' && path == '/chat') {
      messages.add(Map<String, dynamic>.from(body!));
      return _json(_message(messages.length - 1), status: 201);
    }
    return _json({'detail': 'Not Found'}, status: 404);
  }

  Map<String, dynamic> _message(int index) => {
    'id': 'message-$index',
    'mine': true,
    ...messages[index],
    'reply_to': null,
    'reply_text': null,
    'reaction': null,
    'deleted': false,
    'created_at': '2026-10-22T17:00:00Z',
    'attachments': [],
  };
  @override
  void close({bool force = false}) {}
}
