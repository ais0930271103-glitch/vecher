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
import 'package:vecher_app/core/theme.dart';
import 'package:vecher_app/features/today/today_screen.dart';
import 'package:vecher_app/features/videos/rutube_player.dart';
import 'package:vecher_app/features/videos/video_screen.dart';
import 'package:vecher_app/features/videos/videos.dart';

import 'support/fake_realtime.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late ByteData testFont;
  setUpAll(() async {
    final reviewFont = Platform.environment['VECHER_RUTUBE_REVIEW_FONT'];
    if (reviewFont != null && reviewFont.isNotEmpty) {
      final bodyFont = ByteData.sublistView(
        await File(reviewFont).readAsBytes(),
      );
      final bodyLoader = FontLoader('Manrope')..addFont(Future.value(bodyFont));
      for (final weight in [
        'Medium',
        'SemiBold',
        'Bold',
        'ExtraBold',
        'Light',
        'ExtraLight',
      ]) {
        bodyLoader.addFont(
          File('assets/fonts/Manrope-$weight.ttf')
              .readAsBytes()
              .then(ByteData.sublistView),
        );
      }
      await bodyLoader.load();
      testFont = ByteData.sublistView(
        await File('assets/fonts/Comfortaa-Regular.ttf').readAsBytes(),
      );
      await (FontLoader('Comfortaa')..addFont(Future.value(testFont))).load();
      final iconFont = Platform.environment['VECHER_RUTUBE_REVIEW_ICON_FONT'];
      if (iconFont != null && iconFont.isNotEmpty) {
        await (FontLoader(
              'MaterialIcons',
            )..addFont(File(iconFont).readAsBytes().then(ByteData.sublistView)))
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
          return key == 'test/Comfortaa-Regular.ttf' ? testFont : null;
        });
  });
  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMessageHandler('flutter/assets', null);
    GoogleFonts.config.allowRuntimeFetching = true;
    if (sessionChanges.isChanging) {
      sessionChanges.finish(sessionChanges.revision);
    }
    PinGate.unlock();
  });

  testWidgets(
    'only the active platform card initializes; covered routes release it',
    (tester) async {
      final h = await _mount(tester);
      expect(h.players.where((p) => !p.closed), hasLength(1));
      expect(h.players.single.providerId, _firstId);
      expect(h.players.single.playing, isFalse);
      expect(
        h.api.requests.where((r) => r.path.startsWith('/videos')),
        everyElement(
          predicate<RequestOptions>(
            (r) => r.queryParameters['playback'] == 'platform-v1',
          ),
        ),
      );
      h.players.single.progress(const Duration(seconds: 1), playing: true);
      await tester.drag(find.byType(PageView), const Offset(0, -600));
      await tester.pumpAndSettle();
      expect(h.players.first.closed, isTrue);
      expect(h.players.where((p) => !p.closed), hasLength(1));
      expect(h.players.last.providerId, _secondId);
      h.router.push('/cover');
      await tester.pumpAndSettle();
      expect(h.players.every((p) => p.closed), isTrue);
      h.router.pop();
      await tester.pumpAndSettle();
      expect(h.players.where((p) => !p.closed), hasLength(1));
      expect(
        h.players.last.playing,
        isFalse,
        reason: 'Restore must keep the platform play gesture explicit.',
      );
      expect(h.api.viewed, isEmpty);
      await _unmount(tester);
    },
  );

  testWidgets(
    'rapid cover and uncover restores once without reviving a late initialization',
    (tester) async {
      final visibility = ValueNotifier(true);
      addTearDown(visibility.dispose);
      final gate = Completer<void>();
      final h = await _mount(
        tester,
        visibility: visibility,
        firstInitializeGate: gate,
        settle: false,
      );
      await _untilPlayer(tester, h);
      final old = h.players.single;
      visibility.value = false;
      visibility.value = true;
      await tester.pumpAndSettle();
      expect(old.closed, isTrue);
      expect(h.players, hasLength(2));
      expect(h.players.last.ready, isTrue);
      gate.complete();
      await tester.pumpAndSettle();
      expect(old.ready, isFalse);
      expect(old.playing, isFalse);
      expect(h.players, hasLength(2));
      expect(h.viewed, 0);
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    },
  );

  testWidgets(
    'views need real playing progress; suspension pauses without auto-resume',
    (tester) async {
      final suspended = ValueNotifier(false);
      final visibility = ValueNotifier(true);
      addTearDown(suspended.dispose);
      addTearDown(visibility.dispose);
      final h = await _mount(
        tester,
        visibility: visibility,
        suspended: suspended,
      );
      final player = h.players.single;
      player.progress(const Duration(seconds: 8), playing: false);
      await tester.pumpAndSettle();
      expect(h.viewed, 0);
      player.progress(const Duration(seconds: 1), playing: true);
      await tester.pumpAndSettle();
      expect(h.viewed, 0);
      player.progress(const Duration(seconds: 3), playing: true);
      player.progress(const Duration(seconds: 8), playing: true);
      await tester.pumpAndSettle();
      expect(h.viewed, 1);
      suspended.value = true;
      await tester.pumpAndSettle();
      expect(player.pauseCalls, 1);
      expect(player.playing, isFalse);
      suspended.value = false;
      await tester.pumpAndSettle();
      expect(h.players, hasLength(1));
      expect(player.playing, isFalse);
      visibility.value = false;
      visibility.value = true;
      await tester.pumpAndSettle();
      h.players.last.progress(const Duration(seconds: 5), playing: true);
      await tester.pumpAndSettle();
      expect(h.viewed, 1);
      await _unmount(tester);
    },
  );

  testWidgets(
    'provider failure closes the view and retries only when requested',
    (tester) async {
      final h = await _mount(tester);
      final old = h.players.single;
      old.failed = true;
      old.notifyListeners();
      await tester.pumpAndSettle();
      expect(old.closed, isTrue);
      expect(find.text('Плеер RUTUBE не загрузился'), findsOneWidget);
      await tester.pump(const Duration(seconds: 60));
      expect(h.players, hasLength(1));
      expect(h.api.viewed, isEmpty);
      await tester.tap(find.text('Повторить'));
      await tester.pumpAndSettle();
      expect(h.players, hasLength(2));
      expect(h.players.last.ready, isTrue);
      expect(h.players.last.playing, isFalse);
      expect(find.text('Плеер RUTUBE не загрузился'), findsNothing);
      await _unmount(tester);
    },
  );

  testWidgets(
    'initialization deadline closes the view and late success cannot revive it',
    (tester) async {
      final gate = Completer<void>();
      final h = await _mount(tester, firstInitializeGate: gate, settle: false);
      await _untilPlayer(tester, h);
      await tester.pump(const Duration(seconds: 21));
      await tester.pumpAndSettle();
      expect(h.players.single.closed, isTrue);
      expect(find.text('Плеер RUTUBE не загрузился'), findsOneWidget);
      gate.complete();
      await tester.pumpAndSettle();
      expect(h.players.single.ready, isFalse);
      expect(h.players.single.playing, isFalse);
      expect(h.api.viewed, isEmpty);
      await tester.tap(find.text('Повторить'));
      await tester.pumpAndSettle();
      expect(h.players, hasLength(2));
      expect(h.players.last.ready, isTrue);
      await _unmount(tester);
    },
  );

  testWidgets(
    'a loaded WebView without provider readiness has its own deadline',
    (tester) async {
      final h = await _mount(
        tester,
        firstReadyOnInitialize: false,
        settle: false,
      );
      await _untilPlayer(tester, h);
      await tester.pump(const Duration(seconds: 21));
      expect(h.players.single.closed, isFalse);
      expect(find.text('Плеер RUTUBE не загрузился'), findsNothing);
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      expect(h.players.single.closed, isTrue);
      expect(find.text('Плеер RUTUBE не загрузился'), findsOneWidget);
      expect(h.api.viewed, isEmpty);
      await _unmount(tester);
    },
  );

  testWidgets(
    'PIN, background and session changes remove fullscreen content and release playback',
    (tester) async {
      final h = await _mount(tester);
      Future<void> fullscreen() async {
        h.players.last.showFullscreen();
        await tester.pumpAndSettle();
        expect(find.text('Private fullscreen video'), findsOneWidget);
      }

      await fullscreen();
      (PinGate.lockedListenable as ValueNotifier<bool>).value = true;
      await tester.pumpAndSettle();
      expect(find.text('Private fullscreen video'), findsNothing);
      expect(h.players.every((p) => p.closed), isTrue);
      PinGate.unlock();
      await tester.pumpAndSettle();
      expect(h.players.last.closed, isFalse);
      await fullscreen();
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await tester.pumpAndSettle();
      expect(h.players.every((p) => p.closed), isTrue);
      // A paused app does not schedule frames; force one to inspect the
      // already-removed overlay rather than the previous rendered frame.
      tester.binding.scheduleForcedFrame();
      await tester.pump();
      expect(find.text('Private fullscreen video'), findsNothing);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pumpAndSettle();
      expect(h.players.last.closed, isFalse);
      await fullscreen();
      final revision = sessionChanges.begin();
      await tester.pumpAndSettle();
      expect(find.text('Private fullscreen video'), findsNothing);
      expect(find.text('Полезный урок'), findsNothing);
      expect(h.players.every((p) => p.closed), isTrue);
      sessionChanges.finish(revision);
      await _unmount(tester);
    },
  );

  testWidgets(
    'fullscreen closes on back and cannot remain above a covered route',
    (tester) async {
      final h = await _mount(tester);
      final player = h.players.single;
      player.showFullscreen();
      await tester.pumpAndSettle();
      await tester.binding.handlePopRoute();
      await tester.pumpAndSettle();
      expect(find.text('Private fullscreen video'), findsNothing);
      expect(player.fullscreen, isNull);
      expect(player.closed, isFalse);
      expect(find.text('Видео'), findsOneWidget);
      player.showFullscreen();
      await tester.pumpAndSettle();
      h.router.push('/cover');
      await tester.pumpAndSettle();
      expect(find.text('Private fullscreen video'), findsNothing);
      expect(find.text('Covered route'), findsOneWidget);
      expect(player.closed, isTrue);
      await _unmount(tester);
    },
  );

  testWidgets(
    'pending close must finish before a replacement native player initializes',
    (tester) async {
      final visibility = ValueNotifier(true);
      addTearDown(visibility.dispose);
      final h = await _mount(tester, visibility: visibility);
      final old = h.players.single;
      final gate = Completer<void>();
      old.closeGate = gate;
      old.progress(const Duration(seconds: 1), playing: true);
      visibility.value = false;
      visibility.value = true;
      await tester.pump(const Duration(milliseconds: 200));
      expect(old.closeRequested, isTrue);
      expect(
        h.players.where((p) => p.initializeCalls > 0 && !p.closed),
        hasLength(1),
        reason: 'A second native view must not overlap unconfirmed disposal of the first.',
      );
      gate.complete();
      await tester.pumpAndSettle();
      expect(old.closed, isTrue);
      expect(
        h.players.where((p) => p.initializeCalls > 0 && !p.closed),
        hasLength(1),
      );
      await _unmount(tester);
    },
  );

  testWidgets(
    'a swipe waits for the previous card cleanup across surface instances',
    (tester) async {
      final h = await _mount(tester);
      final old = h.players.single;
      final gate = Completer<void>();
      old.closeGate = gate;
      old.progress(const Duration(seconds: 1), playing: true);
      await tester.drag(find.byType(PageView), const Offset(0, -600));
      await tester.pumpAndSettle();
      final concurrentNativeViews = h.players
          .where((p) => p.initializeCalls > 0 && !p.closed)
          .length;
      final oldCloseRequested = old.closeRequested;
      gate.complete();
      await tester.pumpAndSettle();
      expect(h.players.last.providerId, _secondId);
      expect(old.closed, isTrue);
      await _unmount(tester);
      expect(oldCloseRequested, isTrue);
      expect(
        concurrentNativeViews,
        1,
        reason: 'Different page states share one cleanup barrier before creating the next WebView.',
      );
    },
  );

  testWidgets(
    'small viewports offer the source without creating an undersized native player',
    (tester) async {
      final h = await _mount(tester, size: const Size(300, 720));
      expect(h.players, isEmpty);
      expect(find.text('Открыть на RUTUBE').hitTestable(), findsWidgets);
      expect(tester.takeException(), isNull);
      tester.view.physicalSize = const Size(420, 240);
      await tester.pumpAndSettle();
      expect(tester.getSize(find.byType(PageView)).height, lessThan(180));
      expect(h.players, isEmpty);
      expect(find.text('Открыть на RUTUBE').hitTestable(), findsWidgets);
      expect(tester.takeException(), isNull);
      tester.view.physicalSize = const Size(420, 420);
      await tester.pumpAndSettle();
      expect(h.players, hasLength(1));
      expect(h.players.single.ready, isTrue);
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    },
  );

  testWidgets('390 by 844 platform card layout', (tester) async {
    final h = await _mount(tester, size: const Size(390, 844));
    expect(tester.takeException(), isNull);
    final output = Platform.environment['VECHER_RUTUBE_REVIEW_IMAGE'];
    if (output != null && output.isNotEmpty) {
      expect(
        Platform.environment['VECHER_RUTUBE_REVIEW_FONT'],
        isNotEmpty,
        reason: 'A visual artifact must use readable UI fonts, not Ahem.',
      );
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

const _firstId = '11111111111111111111111111111111';
const _secondId = '22222222222222222222222222222222';

Future<void> _untilPlayer(WidgetTester tester, _Harness harness) async {
  for (var i = 0; i < 8 && harness.players.isEmpty; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
  expect(harness.players, isNotEmpty);
}

Future<void> _unmount(WidgetTester tester) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pumpAndSettle();
}

class _Harness {
  _Harness(this.api, this.players, this.router, this.capture);
  final _VideoApi api;
  final List<_FakeRutubePlayer> players;
  final GoRouter router;
  final GlobalKey capture;
  int viewed = 0;
}

Future<_Harness> _mount(
  WidgetTester tester, {
  ValueNotifier<bool>? visibility,
  ValueNotifier<bool>? suspended,
  Completer<void>? firstInitializeGate,
  bool firstReadyOnInitialize = true,
  bool settle = true,
  Size size = const Size(420, 920),
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final api = _VideoApi();
  final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
    ..httpClientAdapter = api;
  final realtime = FakeRealtime();
  final players = <_FakeRutubePlayer>[];
  final capture = GlobalKey();
  late _Harness harness;
  final router = GoRouter(
    initialLocation: visibility == null ? '/videos' : '/surface',
    observers: [chatRouteObserver],
    routes: [
      GoRoute(path: '/videos', builder: (_, __) => const VideoScreen()),
      GoRoute(
        path: '/surface',
        builder: (_, __) => Consumer(
          builder: (context, ref, child) {
            final access = ref.watch(videoAccessProvider);
            Widget surface(bool paused) => RutubeVideoSurface(
              video: ShortVideo.fromJson(api.videos.first),
              active: access.allowed,
              suspended: paused,
              routeVisible: visibility,
              onViewed: () => harness.viewed++,
              onSource: () {},
            );
            return Scaffold(
              body: suspended == null
                  ? surface(false)
                  : ValueListenableBuilder<bool>(
                      valueListenable: suspended,
                      builder: (_, value, __) => surface(value),
                    ),
            );
          },
        ),
      ),
      GoRoute(
        path: '/cover',
        builder: (_, __) => const Scaffold(body: Text('Covered route')),
      ),
    ],
  );
  harness = _Harness(api, players, router, capture);
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
            'pair_id': 'pair-1',
            'status': 'active',
            'partner_name': 'Саша',
          },
        ),
        rutubePlayerFactoryProvider.overrideWithValue((id) {
          final first = players.isEmpty;
          final player = _FakeRutubePlayer(
            id,
            initializeGate: first ? firstInitializeGate : null,
            readyOnInitialize: !first || firstReadyOnInitialize,
          );
          players.add(player);
          return player;
        }),
      ],
      child: MaterialApp.router(
        theme: Platform.environment['VECHER_RUTUBE_REVIEW_FONT'] == null
            ? ThemeData(brightness: Brightness.dark, useMaterial3: true)
            : buildTheme(),
        routerConfig: router,
        builder: (_, child) => RepaintBoundary(
          key: capture,
          child: PinGate(child: child!),
        ),
      ),
    ),
  );
  if (settle) await tester.pumpAndSettle();
  return harness;
}

class _FakeRutubePlayer extends RutubePlayer {
  _FakeRutubePlayer(
    this.providerId, {
    this.initializeGate,
    this.readyOnInitialize = true,
  });
  final String providerId;
  final Completer<void>? initializeGate;
  final bool readyOnInitialize;
  Completer<void>? closeGate;
  bool closed = false, closeRequested = false;
  int initializeCalls = 0, pauseCalls = 0, exitFullscreenCalls = 0;
  @override
  bool ready = false;
  @override
  bool failed = false;
  @override
  bool playing = false;
  @override
  Duration position = Duration.zero;
  @override
  Widget? fullscreen;
  @override
  Future<void> initialize() async {
    initializeCalls++;
    await initializeGate?.future;
    if (closeRequested) return;
    ready = readyOnInitialize;
    notifyListeners();
  }

  @override
  Future<void> pause() async {
    pauseCalls++;
    playing = false;
    if (!closeRequested) notifyListeners();
  }

  void progress(Duration value, {required bool playing}) {
    position = value;
    this.playing = playing;
    if (!closeRequested) notifyListeners();
  }

  void showFullscreen() {
    fullscreen = const Center(child: Text('Private fullscreen video'));
    notifyListeners();
  }

  @override
  void exitFullscreen() {
    exitFullscreenCalls++;
    fullscreen = null;
  }

  @override
  Future<void> close() async {
    if (closeRequested) return;
    closeRequested = true;
    await closeGate?.future;
    closed = true;
    playing = false;
    fullscreen = null;
    super.dispose();
  }

  @override
  Widget buildView() => const ColoredBox(
    color: Colors.black,
    child: Center(child: Text('Official player surface')),
  );
}

Map<String, dynamic> _fixture(String id, String title) => {
  'id': 'rutube-$id',
  'provider_video_id': id,
  'playback_kind': 'rutube_embed',
  'title': title,
  'description': 'Проверенный полезный материал',
  'author': 'Автор урока',
  'source_url': 'https://rutube.ru/video/$id/',
  'embed_url': 'https://rutube.ru/play/embed/$id',
  'media_url': null,
  'license_name': null,
  'license_url': null,
  'thumbnail_url': null,
  'topics': ['hobbies'],
  'language': 'ru',
  'duration_seconds': 400,
  'aspect_ratio': 16 / 9,
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
  final videos = [
    _fixture(_firstId, 'Полезный урок'),
    _fixture(_secondId, 'Домашний проект'),
  ];
  final viewed = <String>[];
  final requests = <RequestOptions>[];
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? stream,
    Future<void>? cancelFuture,
  ) async {
    if (stream != null) await stream.drain<void>();
    requests.add(options);
    if (options.method == 'GET') {
      if (options.path == '/videos/settings') {
        return _json({
          'topics': [
            {'id': 'hobbies', 'title': 'Увлечения', 'available': true},
          ],
          'personal_topics': [],
          'shared_topics': [],
          'personal_revision': 0,
          'shared_revision': 0,
          'has_pair': true,
        });
      }
      if (options.path == '/videos/feed') {
        return _json({
          'items': videos,
          'total': videos.length,
          'mode': 'personal',
        });
      }
    }
    if (options.method == 'POST' && options.path.endsWith('/viewed')) {
      viewed.add(options.path.split('/')[2]);
      return _json({'ok': true});
    }
    return _json({'detail': 'Not found'}, status: 404);
  }

  @override
  void close({bool force = false}) {}
}
