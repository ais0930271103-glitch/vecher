import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/chat_presence.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/realtime_provider.dart';
import 'package:vecher_app/core/session_changes.dart';
import 'package:vecher_app/features/chat/chat_screen.dart';
import 'package:vecher_app/features/today/today_screen.dart';
import 'package:vecher_app/features/videos/video_player.dart';
import 'package:vecher_app/features/videos/video_screen.dart';
import 'package:vecher_app/features/videos/videos.dart';

import 'support/fake_realtime.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late ByteData testFont;
  setUpAll(() async {
    final reviewFont = Platform.environment['VECHER_VIDEO_REVIEW_FONT'];
    if (reviewFont != null && reviewFont.isNotEmpty) {
      testFont = ByteData.sublistView(await File(reviewFont).readAsBytes());
      await (FontLoader(
        'VideoReviewSans',
      )..addFont(Future.value(testFont))).load();
      final icons = Platform.environment['VECHER_VIDEO_REVIEW_ICON_FONT'];
      if (icons != null && icons.isNotEmpty) {
        await (FontLoader('MaterialIcons')
              ..addFont(File(icons).readAsBytes().then(ByteData.sublistView)))
            .load();
      }
      return;
    }
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
    FlutterSecureStorage.setMockInitialValues({
      'vecher_token': 'test-account',
      'vecher_chat_hint_seen': '1',
    });
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

  test('media and source URI gates reject foreign, cleartext, credentialed and nonstandard-port URLs', () {
    expect(
      safeVideoUri(
        'https://upload.wikimedia.org/wikipedia/commons/a/a1/Example.webm',
      ),
      isNotNull,
    );
    expect(
      safeVideoUri('https://thumb.wikimedia.org/a.webp', thumbnail: true),
      isNotNull,
    );
    for (final url in [
      'http://upload.wikimedia.org/a.webm',
      'https://upload.wikimedia.org.evil.test/a',
      'file:///data/user/0/secret',
      'https://user@upload.wikimedia.org/a',
      'https://upload.wikimedia.org:8080/a',
      'https://thumb.wikimedia.org/a.webm',
    ]) {
      expect(safeVideoUri(url), isNull, reason: url);
    }
    expect(
      safeVideoSourceUri('https://commons.wikimedia.org/wiki/File:Cat.webm'),
      isNotNull,
    );
    expect(
      safeVideoSourceUri(
        'https://creativecommons.org/licenses/by-sa/4.0/',
        license: true,
      ),
      isNotNull,
    );
    expect(safeVideoSourceUri('javascript:alert(1)'), isNull);
  });

  testWidgets(
    'only visible clip plays; route cover, background and session suspension release it',
    (tester) async {
      final api = _VideoApi();
      final h = await _mount(tester, api);
      expect(h.players.where((p) => !p.closed), hasLength(1));
      expect(h.players.last.uri.path, contains('cat'));
      expect(h.players.last.playing, isTrue);
      expect(h.players.last.isMuted, isTrue);
      expect(api.viewed, isEmpty);
      await tester.drag(find.byType(PageView), const Offset(0, -600));
      await tester.pumpAndSettle();
      expect(h.players.first.closed, isTrue);
      expect(h.players.where((p) => !p.closed), hasLength(1));
      expect(h.players.last.uri.path, contains('aurora'));
      h.router.push('/cover');
      await tester.pumpAndSettle();
      expect(h.players.every((p) => p.closed), isTrue);
      h.router.pop();
      await tester.pumpAndSettle();
      expect(h.players.last.playing, isTrue);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await tester.pumpAndSettle();
      expect(h.players.every((p) => p.closed), isTrue);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pumpAndSettle();
      expect(h.players.last.playing, isTrue);
      final revision = sessionChanges.begin();
      await tester.pumpAndSettle();
      expect(h.players.every((p) => p.closed), isTrue);
      expect(find.text('Кот открывает дверь'), findsNothing);
      sessionChanges.finish(revision);
      await _unmount(tester);
    },
  );

  testWidgets('late initialization never plays after the page is covered', (
    tester,
  ) async {
    final gate = Completer<void>();
    final h = await _mount(
      tester,
      _VideoApi(),
      initializeGate: gate,
      settle: false,
    );
    for (var i = 0; i < 5 && h.players.isEmpty; i++) {
      await tester.pump(const Duration(milliseconds: 100));
    }
    expect(h.players, isNotEmpty);
    h.router.push('/cover');
    await tester.pump(const Duration(milliseconds: 400));
    gate.complete();
    await tester.pumpAndSettle();
    expect(h.players.every((p) => p.playCalls == 0 && p.closed), isTrue);
    await _unmount(tester);
  });

  testWidgets(
    'view marks require actual progress and like/save never send a message',
    (tester) async {
      final api = _VideoApi();
      final h = await _mount(tester, api);
      expect(api.viewed, isEmpty);
      h.players.last.advance(const Duration(seconds: 1));
      await tester.pumpAndSettle();
      expect(api.viewed, isEmpty);
      h.players.last.advance(const Duration(seconds: 3));
      await tester.pumpAndSettle();
      h.players.last.advance(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      expect(api.viewed, ['cat']);
      await _tap(tester, 'Нравится');
      await _tap(tester, 'Сохранить');
      expect(api.changes, [
        {'liked': true},
        {'saved': true},
      ]);
      expect(api.videos.first['liked'], isTrue);
      expect(api.videos.first['saved'], isTrue);
      expect(api.messages, isEmpty);
      await _tap(tester, 'Не интересно');
      expect(api.videos.first['hidden'], isTrue);
      await _tap(tester, 'Вернуть');
      expect(api.videos.first['hidden'], isFalse);
      expect(api.videos.first['saved'], isTrue);
      expect(api.messages, isEmpty);
      await _unmount(tester);
    },
  );

  testWidgets(
    'share opens an editable chat draft and sends only on explicit Send',
    (tester) async {
      final api = _VideoApi();
      final h = await _mount(tester, api);
      await _tap(tester, 'Отправить партнёру');
      expect(h.players.every((p) => p.closed), isTrue);
      expect(api.messages, isEmpty);
      expect(find.text('Кот открывает дверь'), findsOneWidget);
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pumpAndSettle();
      expect(api.messages, hasLength(1));
      expect(api.messages.single['ref_kind'], 'video');
      expect(api.messages.single['ref_id'], 'cat');
      expect(api.messages.single['client_message_id'], isNotEmpty);
      expect(find.text('Смотреть'), findsOneWidget);
      await _tap(tester, 'Смотреть');
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();
      expect(find.text('Короткое видео'), findsOneWidget);
      expect(h.players.last.playing, isTrue);
      await _unmount(tester);
    },
  );

  testWidgets(
    'personal interests are independent; shared revision conflict reloads before save',
    (tester) async {
      final api = _VideoApi();
      await _mount(tester, api);
      await tester.tap(find.byTooltip('Мои интересы'));
      await tester.pumpAndSettle();
      await _tap(tester, 'Животные');
      await _tap(tester, 'Сохранить мои интересы');
      expect(api.personalTopics, ['animals']);
      expect(api.sharedTopics, isEmpty);
      await _tap(tester, 'Для нас');
      await tester.tap(find.byTooltip('Общие темы'));
      await tester.pumpAndSettle();
      await _tap(tester, 'Природа');
      api.conflictShared = true;
      await _tap(tester, 'Сохранить общие темы');
      expect(find.textContaining('на другом телефоне'), findsOneWidget);
      expect(api.personalTopics, ['animals']);
      expect(api.sharedTopics, ['animals']);
      await _tap(tester, 'Природа');
      await _tap(tester, 'Сохранить общие темы');
      expect(api.sharedTopics, ['animals', 'nature']);
      expect(api.personalTopics, ['animals']);
      await _unmount(tester);
    },
  );

  testWidgets('old server and missing clip show recovery text', (tester) async {
    final api = _VideoApi()..supported = false;
    final h = await _mount(tester, api);
    expect(find.textContaining('после обновления сервера'), findsOneWidget);
    expect(find.text('Повторить'), findsOneWidget);
    api.supported = true;
    h.router.go('/videos/missing');
    await tester.pumpAndSettle();
    expect(find.textContaining('Этот ролик уже недоступен'), findsOneWidget);
    expect(h.players, isEmpty);
    await _unmount(tester);
  });

  testWidgets(
    'scrolling clip details preserves the active player and progress',
    (tester) async {
      final h = await _mount(tester, _VideoApi(), size: const Size(320, 640));
      final player = h.players.last;
      player.advance(const Duration(seconds: 4));
      await tester.pumpAndSettle();
      await tester.drag(
        find.byType(SingleChildScrollView).first,
        const Offset(0, -150),
      );
      await tester.pumpAndSettle();
      expect(h.players.last, same(player));
      expect(player.closed, isFalse);
      expect(player.playing, isTrue);
      expect(player.position, const Duration(seconds: 4));
      await _unmount(tester);
    },
  );

  testWidgets('open interests sheet discards the previous account draft', (
    tester,
  ) async {
    final api = _VideoApi();
    await _mount(tester, api);
    await tester.tap(find.byTooltip('Мои интересы'));
    await tester.pumpAndSettle();
    await _tap(tester, 'Животные');
    final revision = sessionChanges.begin();
    await tester.pumpAndSettle();
    api.personalTopics = ['nature'];
    api.personalRevision = 0;
    sessionChanges.finish(revision);
    await tester.pumpAndSettle();
    final animals = tester.widget<FilterChip>(
      find.widgetWithText(FilterChip, 'Животные'),
    );
    final nature = tester.widget<FilterChip>(
      find.widgetWithText(FilterChip, 'Природа'),
    );
    expect(animals.selected, isFalse);
    expect(nature.selected, isTrue);
    await _tap(tester, 'Сохранить мои интересы');
    expect(api.personalTopics, ['nature']);
    await _unmount(tester);
  });

  testWidgets(
    'saved collection can restore hidden clips and remove an unsaved item',
    (tester) async {
      final api = _VideoApi();
      api.videos.first['saved'] = true;
      api.videos.first['hidden'] = true;
      final h = await _mount(tester, api);
      h.router.go('/videos?mode=saved');
      await tester.pumpAndSettle();
      expect(find.text('Кот открывает дверь'), findsOneWidget);
      await _tap(tester, 'Вернуть в подборку');
      expect(api.videos.first['hidden'], isFalse);
      expect(api.videos.first['saved'], isTrue);
      await _tap(tester, 'Сохранено');
      expect(api.videos.first['saved'], isFalse);
      expect(find.textContaining('Здесь появятся ролики'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets('late private feed cannot render after account changes', (
    tester,
  ) async {
    final api = _VideoApi()..heldFeed = Completer<ResponseBody>();
    final h = await _mount(tester, api, settle: false);
    await tester.pump(const Duration(milliseconds: 100));
    final revision = sessionChanges.begin();
    await tester.pump();
    api.heldFeed!.complete(
      _json({
        'items': [_fixture('cat', 'PRIVATE OLD ACCOUNT')],
        'total': 1,
      }),
    );
    await tester.pumpAndSettle();
    expect(find.text('PRIVATE OLD ACCOUNT'), findsNothing);
    expect(h.players, isEmpty);
    api.heldFeed = null;
    api.videos = [_fixture('aurora', 'Новая подборка')];
    sessionChanges.finish(revision);
    await tester.pumpAndSettle();
    expect(find.text('Новая подборка'), findsOneWidget);
    expect(find.text('PRIVATE OLD ACCOUNT'), findsNothing);
    await _unmount(tester);
  });

  testWidgets(
    'narrow screen at larger text scale keeps controls reachable without overflow',
    (tester) async {
      final api = _VideoApi();
      await _mount(tester, api, size: const Size(320, 640), textScale: 1.3);
      expect(tester.takeException(), isNull);
      await _tap(tester, 'Сохранить');
      expect(api.videos.first['saved'], isTrue);
      await _tap(tester, 'Отправить партнёру');
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    },
  );

  testWidgets('390 by 844 visual review frame', (tester) async {
    final h = await _mount(tester, _VideoApi(), size: const Size(390, 844));
    expect(tester.takeException(), isNull);
    final output = Platform.environment['VECHER_VIDEO_REVIEW_IMAGE'];
    if (output != null && output.isNotEmpty) {
      await tester.runAsync(() => GoogleFonts.pendingFonts());
      await tester.pumpAndSettle();
      final boundary =
          h.capture.currentContext!.findRenderObject()!
              as RenderRepaintBoundary;
      await tester.runAsync(() async {
        final image = await boundary.toImage(pixelRatio: 1);
        final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
        await File(output).writeAsBytes(bytes!.buffer.asUint8List());
        image.dispose();
      });
    }
    await _unmount(tester);
  });
}

Future<void> _tap(WidgetTester tester, String text) async {
  final target = find.text(text).first;
  if (target.hitTestable().evaluate().isEmpty) {
    await tester.ensureVisible(target);
  }
  await tester.tap(target);
  await tester.pumpAndSettle();
}

Future<void> _unmount(WidgetTester tester) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pumpAndSettle();
}

class _Harness {
  _Harness(this.router, this.players, this.capture);
  final GoRouter router;
  final List<_FakePlayer> players;
  final GlobalKey capture;
}

Future<_Harness> _mount(
  WidgetTester tester,
  _VideoApi api, {
  Size size = const Size(420, 920),
  double textScale = 1,
  bool settle = true,
  Completer<void>? initializeGate,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
    ..httpClientAdapter = api;
  final realtime = FakeRealtime();
  final players = <_FakePlayer>[];
  final capture = GlobalKey();
  final router = GoRouter(
    initialLocation: '/videos',
    observers: [chatRouteObserver],
    routes: [
      GoRoute(
        path: '/videos',
        builder: (_, state) => VideoScreen(
          mode: VideoMode.parse(state.uri.queryParameters['mode']),
        ),
      ),
      GoRoute(
        path: '/videos/:id',
        builder: (_, state) =>
            VideoScreen(videoId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/cover',
        builder: (_, __) => const Scaffold(body: Text('Covered route')),
      ),
      GoRoute(
        path: '/chat',
        builder: (_, state) => ChatScreen(attach: state.extra as Map?),
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
            'id': 'pair-1',
            'status': 'active',
            'partner_name': 'Саша',
          },
        ),
        shortVideoPlayerFactoryProvider.overrideWithValue((uri) {
          final p = _FakePlayer(uri, initializeGate);
          players.add(p);
          return p;
        }),
      ],
      child: MaterialApp.router(
        theme: ThemeData(
          brightness: Brightness.dark,
          useMaterial3: true,
          fontFamily: Platform.environment['VECHER_VIDEO_REVIEW_FONT'] == null
              ? null
              : 'VideoReviewSans',
        ),
        routerConfig: router,
        builder: (context, child) => RepaintBoundary(
          key: capture,
          child: MediaQuery(
            data: MediaQuery.of(context)
                .copyWith(textScaler: TextScaler.linear(textScale)),
            child: PinGate(child: child!),
          ),
        ),
      ),
    ),
  );
  if (settle) await tester.pumpAndSettle();
  return _Harness(router, players, capture);
}

class _FakePlayer extends ShortVideoPlayer {
  _FakePlayer(this.uri, this.gate);
  final Uri uri;
  final Completer<void>? gate;
  bool closed = false, isMuted = false;
  int playCalls = 0;
  @override
  bool ready = false;
  @override
  bool playing = false;
  @override
  bool buffering = false;
  @override
  bool failed = false;
  @override
  Duration position = Duration.zero;
  @override
  Duration get duration => const Duration(seconds: 22);
  @override
  double get aspectRatio => 9 / 16;
  @override
  Future<void> initialize() async {
    await gate?.future;
    if (closed) return;
    ready = true;
    notifyListeners();
  }

  @override
  Future<void> play() async {
    if (!closed) {
      playCalls++;
      playing = true;
      notifyListeners();
    }
  }

  @override
  Future<void> pause() async {
    if (!closed) {
      playing = false;
      notifyListeners();
    }
  }

  @override
  Future<void> mute(bool muted) async {
    isMuted = muted;
  }

  @override
  Future<void> seekToStart() async {
    position = Duration.zero;
  }

  void advance(Duration value) {
    position = value;
    notifyListeners();
  }

  @override
  Future<void> close() async {
    if (closed) return;
    closed = true;
    playing = false;
    super.dispose();
  }

  @override
  Widget buildView() => const DecoratedBox(
    decoration: BoxDecoration(
      gradient: LinearGradient(
        colors: [Color(0xFF103D52), Color(0xFF6E8C75)],
        begin: Alignment.topLeft,
        end: Alignment.bottomRight,
      ),
    ),
    child: Center(child: Icon(Icons.pets, size: 90, color: Colors.white70)),
  );
}

Map<String, dynamic> _fixture(String id, String title) => {
  'id': id,
  'title': title,
  'description': 'У маленького исследователя свой план на вечер.',
  'author': 'Example Author',
  'source_url': 'https://commons.wikimedia.org/wiki/File:$id.webm',
  'media_url': 'https://upload.wikimedia.org/wikipedia/commons/a/a1/$id.webm',
  'thumbnail_url': null,
  'license_name': 'CC BY-SA 4.0',
  'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/',
  'topics': [id == 'cat' ? 'animals' : 'nature'],
  'language': 'none',
  'duration_seconds': 22,
  'aspect_ratio': 9 / 16,
  'liked': false,
  'saved': false,
  'hidden': false,
  'plan_card_id': null,
};

ResponseBody _json(dynamic value, {int status = 200}) =>
    ResponseBody.fromString(
      jsonEncode(value),
      status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );

class _VideoApi implements HttpClientAdapter {
  List<Map<String, dynamic>> videos = [
    _fixture('cat', 'Кот открывает дверь'),
    _fixture('aurora', 'Северное сияние'),
  ];
  List<String> personalTopics = [], sharedTopics = [];
  int personalRevision = 0, sharedRevision = 0;
  bool supported = true, conflictShared = false;
  Completer<ResponseBody>? heldFeed;
  final viewed = <String>[];
  final changes = <Map>[];
  final messages = <Map>[];
  Map<String, dynamic> get settings => {
    'topics': [
      {'id': 'animals', 'title': 'Животные'},
      {'id': 'nature', 'title': 'Природа'},
    ],
    'personal_topics': personalTopics,
    'shared_topics': sharedTopics,
    'personal_revision': personalRevision,
    'shared_revision': sharedRevision,
    'has_pair': true,
  };
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? stream,
    Future<void>? cancelFuture,
  ) async {
    if (stream != null) await stream.drain<void>();
    final path = options.path;
    if (path.startsWith('/videos') && !supported) {
      return _json({'detail': 'Not Found'}, status: 404);
    }
    if (options.method == 'GET') {
      if (path == '/videos/settings') return _json(settings);
      if (path == '/videos/feed' || path == '/videos/shared') {
        if (heldFeed != null) return heldFeed!.future;
        final list = videos
            .where(
              (v) => (options.queryParameters['mode'] == 'saved'
                  ? v['saved'] == true
                  : v['hidden'] != true),
            )
            .toList();
        return _json({
          'items': list,
          'total': list.length,
          'mode': options.queryParameters['mode'] ?? 'shared_collection',
        });
      }
      if (path.startsWith('/videos/')) {
        final video = videos
            .where((v) => v['id'] == path.split('/').last)
            .firstOrNull;
        return video == null
            ? _json({'detail': 'No video'}, status: 404)
            : _json(video);
      }
      if (path == '/chat') {
        return _json({
          'messages': [for (var i = 0; i < messages.length; i++) _message(i)],
          'partner_read_at': null,
          'has_more': false,
        });
      }
      if (path == '/chat/unread') return _json({'count': 0});
      if (path == '/pairs/me') {
        return _json({
          'id': 'pair-1',
          'status': 'active',
          'partner_name': 'Саша',
        });
      }
      if (path == '/calls/active') return _json(null);
    }
    if (options.method == 'PUT' && path.startsWith('/videos/settings/')) {
      final body = options.data as Map;
      if (path.endsWith('/personal')) {
        personalTopics = (body['topics'] as List).cast<String>();
        personalRevision++;
      } else {
        if (conflictShared) {
          conflictShared = false;
          sharedTopics = ['animals'];
          sharedRevision++;
          return _json({'detail': 'conflict'}, status: 409);
        }
        sharedTopics = (body['topics'] as List).cast<String>();
        sharedRevision++;
      }
      return _json(settings);
    }
    if (options.method == 'PATCH' && path.endsWith('/state')) {
      final video = videos.singleWhere((v) => v['id'] == path.split('/')[2]);
      final body = options.data as Map;
      changes.add(Map.of(body));
      body.forEach((key, value) => video[key as String] = value);
      return _json(video);
    }
    if (options.method == 'POST') {
      if (path.endsWith('/viewed')) {
        viewed.add(path.split('/')[2]);
        return _json({'ok': true});
      }
      if (path == '/chat') {
        messages.add(Map.of(options.data as Map));
        return _json(_message(messages.length - 1), status: 201);
      }
      if (path == '/chat/read') return _json({'ok': true});
    }
    return _json({'detail': 'Not Found'}, status: 404);
  }

  Map<String, dynamic> _message(int index) => {
    'id': 'message-$index',
    'mine': true,
    'text': null,
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
