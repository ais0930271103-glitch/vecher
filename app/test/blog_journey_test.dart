import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:dio/dio.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/chat_presence.dart';
import 'package:vecher_app/core/network_endpoints.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/realtime_provider.dart';
import 'package:vecher_app/core/session_changes.dart';
import 'package:vecher_app/core/theme.dart';
import 'package:vecher_app/features/content/content.dart';
import 'package:vecher_app/features/content/content_screen.dart';
import 'package:vecher_app/features/content/publication_screen.dart';
import 'package:vecher_app/features/newblogs/blog_post_editor_screen.dart';
import 'package:vecher_app/features/newblogs/blog_profile_screen.dart';
import 'package:vecher_app/features/newblogs/blog_studio_screen.dart';
import 'package:vecher_app/features/newblogs/blog_video_upload.dart';
import 'package:vecher_app/features/newblogs/blogs.dart';
import 'package:vecher_app/features/newblogs/hosted_content_video.dart';
import 'package:vecher_app/features/today/today_screen.dart';
import 'package:vecher_app/features/videos/video_player.dart';

import 'support/fake_realtime.dart';

const _blogId = '11111111-1111-4111-8111-111111111111';
const _postId = '22222222-2222-4222-8222-222222222222';
const _mediaId = '33333333-3333-4333-8333-333333333333';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({'vecher_token': 'account-a'});
    PinGate.unlock();
  });
  tearDown(() {
    if (sessionChanges.isChanging) {
      sessionChanges.finish(sessionChanges.revision);
    }
  });

  test('old API defaults to no public blogs; hosted route rejects foreign URL and malformed media paths', () {
    final api = _BlogApi()..enabled = false;
    expect(ContentSettings.fromJson(api.settings).publicBlogsEnabled, isFalse);
    final endpoints = ApiEndpoints(primary: 'https://api.example.invalid');
    addTearDown(endpoints.dispose);
    expect(
      hostedBlogVideoUri(BlogMedia.fromJson(_media()), endpoints)?.toString(),
      'https://api.example.invalid/blogs/media/$_mediaId/video',
    );
    for (final url in [
      'https://evil.example/video',
      '//evil.example/video',
      '/blogs/media/$_mediaId/video?token=x',
      '/blogs/media/../private/video',
      '/chat/attachments/$_mediaId',
    ]) {
      expect(
        hostedBlogVideoUri(
          BlogMedia.fromJson({..._media(), 'url': url}),
          endpoints,
        ),
        isNull,
        reason: url,
      );
    }
    final hosted = Publication.fromJson(api.publication);
    expect(hosted.isVideo, isTrue);
    expect(hosted.openLocation, '/content/post-$_postId');
    expect(hosted.metadataLabel, 'Видео · 0:45');
  });

  test(
    'bounded streaming rejects stale, empty, oversized file lengths',
    () async {
      expect(
        await checkedBlogVideoStream(
          Stream.value([1, 2, 3]),
          3,
        ).expand((e) => e).toList(),
        [1, 2, 3],
      );
      for (final count in [0, 2, 4, maxBlogVideoBytes + 1]) {
        await expectLater(
          checkedBlogVideoStream(Stream.value([1, 2, 3]), count).drain<void>(),
          throwsA(isA<BlogVideoException>()),
        );
      }
    },
  );

  test('media quotas, pause conflicts and processing failures have distinct messages', () {
    DioException error(int status, String detail) => DioException(
      requestOptions: RequestOptions(),
      response: Response(
        requestOptions: RequestOptions(),
        statusCode: status,
        data: {'detail': detail},
      ),
    );
    expect(
      blogErrorText(error(413, 'media_quota_exceeded')),
      contains('лимит хранения'),
    );
    expect(blogErrorText(error(413, 'video_size_limit')), contains('100 МБ'));
    expect(blogErrorText(error(429, 'media_queue_full')), contains('Очередь'));
    expect(
      blogErrorText(error(409, 'Пара на паузе — общий блог пока недоступен')),
      startsWith('Пара на паузе'),
    );
    expect(
      BlogMedia.fromJson({
        'id': _mediaId,
        'status': 'failed',
        'error_code': 'video_too_long',
      }).statusLabel,
      contains('3 минут'),
    );
  });

  test('upload retries retain exact raw bytes and idempotency key after lost response', () async {
    final api = _BlogApi()..failFirstUpload = true;
    final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
      ..httpClientAdapter = api;
    addTearDown(dio.close);
    final selected = _selected();
    await expectLater(
      uploadBlogVideo(
        dio,
        _blogId,
        selected,
        cancelToken: CancelToken(),
        onProgress: (_, __) {},
      ),
      throwsA(isA<DioException>()),
    );
    final result = await uploadBlogVideo(
      dio,
      _blogId,
      selected,
      cancelToken: CancelToken(),
      onProgress: (_, __) {},
    );
    expect(result.status, 'queued');
    expect(api.uploadedBytes, [
      [1, 2, 3, 4],
      [1, 2, 3, 4],
    ]);
    final requests = api.requests
        .where((r) => r.path.endsWith('/media'))
        .toList();
    expect(requests.map((r) => r.headers['Idempotency-Key']).toSet(), {
      selected.requestId,
    });
    expect(
      requests.every((r) => r.headers[Headers.contentLengthHeader] == 4),
      isTrue,
    );
    expect(requests.every((r) => r.contentType == 'video/mp4'), isTrue);
    await uploadBlogVideo(
      dio,
      _blogId,
      selected,
      cancelToken: CancelToken(),
      onProgress: (_, __) {},
    );
    expect(api.uploadedBytes, hasLength(2));
  });

  testWidgets(
    'old server hides the creator entry and direct studio never requests unsupported blogs',
    (tester) async {
      final api = _BlogApi()..enabled = false;
      final harness = await _mount(tester, api, path: '/content');
      expect(find.text('Мои блоги'), findsNothing);
      expect(
        api.requests
            .where((r) => r.path == '/content/feed')
            .single
            .queryParameters['hosted_video'],
        isTrue,
      );
      harness.router.go('/blogs');
      await tester.pumpAndSettle();
      expect(
        find.textContaining('Публикация блогов пока недоступна'),
        findsOneWidget,
      );
      expect(api.requests.where((r) => r.path.startsWith('/blogs')), isEmpty);
      await _unmount(tester);
    },
  );

  testWidgets(
    'individual profile requires explicit consent and does not copy private identity',
    (tester) async {
      final api = _BlogApi()..hasBlog = false;
      await _mount(tester, api, path: '/blogs/new');
      expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('blog-public-name')))
            .controller!
            .text,
        isEmpty,
      );
      await tester.enterText(
        find.byKey(const ValueKey('blog-public-name')),
        'Маленькие открытия',
      );
      await tester.enterText(
        find.byKey(const ValueKey('blog-public-bio')),
        'Идеи для хорошего вечера',
      );
      await _visible(tester, find.text('Создать публичный профиль'));
      expect(
        tester
            .widget<FilledButton>(
              find.widgetWithText(FilledButton, 'Создать публичный профиль'),
            )
            .onPressed,
        isNull,
      );
      await _tapFinder(
        tester,
        find.byKey(const ValueKey('blog-profile-consent')),
      );
      await _tap(tester, 'Создать публичный профиль');
      expect(api.createdProfiles, hasLength(1));
      expect(api.createdProfiles.single['public_consent'], isTrue);
      expect(
        api.createdProfiles.single['consent_version'],
        publicBlogConsentVersion,
      );
      expect(api.createdProfiles.single.keys.toSet(), {
        'client_id',
        'kind',
        'name',
        'bio',
        'public_consent',
        'consent_version',
      });
      expect(jsonEncode(api.createdProfiles).contains('private-name'), isFalse);
      expect(find.text('Добавить видео'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets(
    'couple profile shows exact public text and waits for deliberate second consent',
    (tester) async {
      final api = _BlogApi()
        ..kind = 'couple'
        ..profileStatus = 'pending_consent'
        ..myConsent = false;
      await _mount(tester, api, path: '/blogs/$_blogId/profile');
      expect(find.text('Нужно твоё согласие на профиль'), findsOneWidget);
      expect(api.profileConsents, isEmpty);
      await _tapFinder(
        tester,
        find.byKey(const ValueKey('blog-profile-consent')),
      );
      await _tap(tester, 'Подтвердить публичный профиль');
      expect(api.profileConsents.single, {
        'revision': 1,
        'accepted': true,
        'consent_version': publicBlogConsentVersion,
      });
      expect(find.text('Публичный профиль подтверждён'), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets(
    'editor uploads, processes, saves draft then separately confirms review submission',
    (tester) async {
      final api = _BlogApi()..hasPost = false;
      await _mount(tester, api, path: '/blogs/$_blogId/posts/new');
      await tester.enterText(
        find.byKey(const ValueKey('blog-post-title')),
        'Охота за деталями',
      );
      await _visible(
        tester,
        find.byKey(const ValueKey('blog-post-description')),
      );
      await tester.enterText(
        find.byKey(const ValueKey('blog-post-description')),
        'Показываем маленькие фрагменты предметов и угадываем их.',
      );
      await _tapFinder(
        tester,
        find.byKey(const ValueKey('blog-post-topic-home')),
      );
      await _tap(tester, 'Выбрать видео с устройства', settle: false);
      await _pump(tester);
      expect(api.uploadedBytes.single, [1, 2, 3, 4]);
      expect(find.text('Видео в очереди на обработку'), findsOneWidget);
      expect(api.createdPosts, isEmpty);
      await tester.pump(const Duration(seconds: 5));
      await _pump(tester);
      await _tap(tester, 'Сохранить черновик');
      expect(api.createdPosts, hasLength(1));
      expect(api.createdPosts.single['topics'], ['home']);
      expect(api.createdPosts.single['media_id'], _mediaId);
      expect(api.submits, isEmpty);
      await _tap(tester, 'Отправить на проверку');
      expect(
        tester
            .widget<FilledButton>(
              find.widgetWithText(FilledButton, 'Подтвердить'),
            )
            .onPressed,
        isNull,
      );
      await _tapFinder(
        tester,
        find.byKey(const ValueKey('blog-post-public-consent')),
      );
      await _tap(tester, 'Подтвердить');
      expect(api.submits.single, {
        'revision': 1,
        'public_consent': true,
        'consent_version': publicPostConsentVersion,
      });
      await _visible(tester, find.text('На проверке редакции'));
      expect(find.text('На проверке редакции'), findsOneWidget);
      expect(find.text('Опубликовано'), findsNothing);
      await _unmount(tester);
    },
  );

  testWidgets(
    'declared topic with no published reader items remains selectable for authoring',
    (tester) async {
      final api = _BlogApi()..includeEmptyTopic = true;
      await _mount(tester, api, path: '/blogs/$_blogId/posts/$_postId');
      final topic = find.byKey(const ValueKey('blog-post-topic-fresh'));
      await _visible(tester, topic);
      expect(tester.widget<FilterChip>(topic).onSelected, isNotNull);
      await _tapFinder(tester, topic);
      await _tap(tester, 'Сохранить черновик');
      final changed = api.requests.singleWhere(
        (r) =>
            r.method == 'PATCH' && r.path == '/blogs/$_blogId/posts/$_postId',
      );
      expect((changed.data as Map)['topics'], ['fresh', 'home']);
      await _unmount(tester);
    },
  );

  testWidgets(
    'paused couple profile explains pause and blocks exact-version consent controls',
    (tester) async {
      final api = _BlogApi()
        ..kind = 'couple'
        ..profileStatus = 'pending_consent'
        ..myConsent = false
        ..paused = true;
      await _mount(tester, api, path: '/blogs/$_blogId/profile');
      await _visible(
        tester,
        find.text('Пара на паузе — подтверждение общего блога пока недоступно'),
      );
      expect(
        find.text('Пара на паузе — подтверждение общего блога пока недоступно'),
        findsOneWidget,
      );
      final consent = find.byKey(const ValueKey('blog-profile-consent'));
      await _visible(tester, consent);
      expect(tester.widget<CheckboxListTile>(consent).onChanged, isNull);
      await _visible(tester, find.text('Подтвердить публичный профиль'));
      expect(
        tester
            .widget<FilledButton>(
              find.widgetWithText(
                FilledButton,
                'Подтвердить публичный профиль',
              ),
            )
            .onPressed,
        isNull,
      );
      expect(api.profileConsents, isEmpty);
      await _unmount(tester);
    },
  );

  testWidgets(
    'partner approval names the exact revision and stale consent requires reload',
    (tester) async {
      final api = _BlogApi()
        ..kind = 'couple'
        ..postStatus = 'pending_partner'
        ..partnerApproved = true
        ..conflictSubmit = true;
      await _mount(tester, api, path: '/blogs/$_blogId/posts/$_postId');
      await _tap(tester, 'Подтвердить эту версию');
      expect(find.text('Версия 1 · видео и описание выше'), findsOneWidget);
      await _tapFinder(
        tester,
        find.byKey(const ValueKey('blog-post-public-consent')),
      );
      await _tap(tester, 'Подтвердить');
      expect(api.submits.single['revision'], 1);
      expect(find.textContaining('Данные изменились'), findsOneWidget);
      expect(api.postStatus, 'pending_partner');
      await _tap(tester, 'Загрузить актуальную версию');
      await _visible(tester, find.text('Версия 2'));
      expect(find.text('Версия 2'), findsOneWidget);
      expect(api.submits, hasLength(1));
      await _unmount(tester);
    },
  );

  testWidgets(
    'background preserves local text, stops status polling and resumes only in foreground',
    (tester) async {
      final api = _BlogApi()
        ..hasPost = false
        ..keepProcessing = true;
      await _mount(tester, api, path: '/blogs/$_blogId/posts/new');
      await tester.enterText(
        find.byKey(const ValueKey('blog-post-title')),
        'Локальный черновик',
      );
      await _tap(tester, 'Выбрать видео с устройства', settle: false);
      await _pump(tester);
      _background(tester);
      await _pump(tester);
      final before = api.polls;
      await tester.pump(const Duration(seconds: 30));
      expect(api.polls, before);
      _foreground(tester);
      await _pump(tester);
      expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('blog-post-title')))
            .controller!
            .text,
        'Локальный черновик',
      );
      await tester.pump(const Duration(seconds: 5));
      await _pump(tester);
      expect(api.polls, greaterThan(before));
      await _unmount(tester);
    },
  );

  testWidgets(
    'native picker background retains draft and manual upload cancellation retries same file',
    (tester) async {
      final picked = Completer<SelectedBlogVideo?>();
      final held = Completer<ResponseBody>();
      final api = _BlogApi()
        ..hasPost = false
        ..heldUpload = held;
      await _mount(
        tester,
        api,
        path: '/blogs/$_blogId/posts/new',
        picker: () => picked.future,
      );
      await tester.enterText(
        find.byKey(const ValueKey('blog-post-title')),
        'Сохранить во время выбора',
      );
      await _tap(tester, 'Выбрать видео с устройства', settle: false);
      _background(tester);
      await _pump(tester);
      _foreground(tester);
      await _pump(tester);
      picked.complete(_selected());
      await _pump(tester);
      expect(api.uploadedBytes, hasLength(1));
      await _tap(tester, 'Остановить загрузку');
      await _visible(tester, find.textContaining('Загрузка остановлена'));
      expect(find.textContaining('Загрузка остановлена'), findsOneWidget);
      await _visible(tester, find.byKey(const ValueKey('blog-post-title')));
      expect(
        tester
            .widget<TextField>(find.byKey(const ValueKey('blog-post-title')))
            .controller!
            .text,
        'Сохранить во время выбора',
      );
      api.heldUpload = null;
      held.complete(
        ResponseBody.fromString(
          jsonEncode({'id': _mediaId, 'status': 'queued'}),
          202,
          headers: {
            Headers.contentTypeHeader: [Headers.jsonContentType],
          },
        ),
      );
      await _tap(tester, 'Повторить загрузку видео', settle: false);
      expect(api.uploadedBytes, hasLength(2));
      expect(
        api.requests
            .where((r) => r.path.endsWith('/media'))
            .map((r) => r.headers['Idempotency-Key'])
            .toSet(),
        hasLength(1),
      );
      await _unmount(tester);
    },
  );

  testWidgets(
    'actual pair identity change clears old joint draft and cannot create from denied old profile',
    (tester) async {
      final api = _BlogApi()..kind = 'couple';
      final harness = await _mount(
        tester,
        api,
        path: '/blogs/$_blogId/posts/$_postId',
      );
      expect(find.text('Охота за деталями'), findsOneWidget);
      api.denyOwnership = true;
      harness.container.read(_pairState.notifier).state = 'other-pair';
      await tester.pumpAndSettle();
      expect(find.text('Охота за деталями'), findsNothing);
      expect(
        find.textContaining('Блог или публикация больше недоступны'),
        findsOneWidget,
      );
      expect(find.text('Отправить на проверку'), findsNothing);
      harness.router.go('/blogs/$_blogId/profile');
      await tester.pumpAndSettle();
      expect(find.text('Создать публичный профиль'), findsNothing);
      expect(api.createdProfiles, isEmpty);
      await _unmount(tester);
    },
  );

  testWidgets(
    'hosted playback attaches bearer only to API and closes under covered route/background',
    (tester) async {
      final api = _BlogApi();
      final harness = await _mount(tester, api, path: '/content/post-$_postId');
      expect(
        api.requests
            .where((r) => r.path == '/content/post-$_postId')
            .single
            .queryParameters['hosted_video'],
        isTrue,
      );
      await _tap(tester, 'Смотреть видео');
      expect(harness.players, hasLength(1));
      expect(
        harness.playerUris.single.toString(),
        'https://api.example.invalid/blogs/media/$_mediaId/video',
      );
      expect(harness.playerHeaders.single, {
        'Authorization': 'Bearer account-a',
      });
      expect(harness.players.single.playCalls, 1);
      harness.router.push('/covered');
      await tester.pumpAndSettle();
      expect(harness.players.single.closed, isTrue);
      harness.router.pop();
      await tester.pumpAndSettle();
      expect(harness.players, hasLength(1));
      await _tap(tester, 'Смотреть видео');
      _background(tester);
      await _pump(tester);
      expect(harness.players.last.closed, isTrue);
      _foreground(tester);
      await tester.pumpAndSettle();
      await _unmount(tester);
    },
  );

  testWidgets(
    'actual hosted publication projection renders full description only once',
    (tester) async {
      final api = _BlogApi();
      await _mount(tester, api, path: '/content/post-$_postId');
      await _visible(tester, find.text(api.description));
      expect(find.text(api.description), findsOneWidget);
      await _unmount(tester);
    },
  );

  testWidgets('late player initialization never starts after account changes', (
    tester,
  ) async {
    final api = _BlogApi();
    final initialized = Completer<void>();
    final harness = await _mount(
      tester,
      api,
      path: '/content/post-$_postId',
      playerInit: initialized.future,
    );
    await _tap(tester, 'Смотреть видео', settle: false);
    await _pump(tester);
    final revision = sessionChanges.begin();
    await _pump(tester);
    initialized.complete();
    await _pump(tester);
    expect(harness.players.single.playCalls, 0);
    expect(harness.players.single.closed, isTrue);
    sessionChanges.finish(revision);
    await tester.pumpAndSettle();
    await _unmount(tester);
  });

  testWidgets('reader reports chosen reason without sharing or auto-hiding', (
    tester,
  ) async {
    final api = _BlogApi();
    await _mount(tester, api, path: '/content/post-$_postId');
    await _tap(tester, 'Пожаловаться на публикацию');
    await _tap(tester, 'Публикация без согласия / личные данные');
    await _tap(tester, 'Отправить жалобу');
    expect(api.reports, [
      {'reason': 'privacy'},
    ]);
    expect(
      api.requests.where(
        (r) => r.path.endsWith('/state') || r.path.startsWith('/chat'),
      ),
      isEmpty,
    );
    await _unmount(tester);
  });

  testWidgets(
    'studio and populated editor fit narrow text-scaled phone with optional actual widget export',
    (tester) async {
      final directory = Platform.environment['VECHER_BLOGS_REVIEW_DIR'];
      if (directory != null) {
        await tester.runAsync(() async {
          await Directory(directory).create(recursive: true);
          final loader = FontLoader('Manrope')
            ..addFont(rootBundle.load('assets/fonts/Manrope-Regular.ttf'));
          await loader.load();
          await (FontLoader('MaterialIcons')
                ..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf')))
              .load();
          await (FontLoader(
                'Comfortaa',
              )..addFont(rootBundle.load('assets/fonts/Comfortaa-Regular.ttf')))
              .load();
        });
      }
      for (final size in [const Size(390, 844), const Size(320, 844)]) {
        final api = _BlogApi()
          ..kind = 'couple'
          ..postStatus = 'pending_partner'
          ..partnerApproved = true;
        final harness = await _mount(
          tester,
          api,
          path: '/entry',
          size: size,
          textScale: size.width == 320 ? 1.3 : 1,
          review: directory != null,
        );
        await _tap(tester, 'Интересное');
        await _tap(tester, 'Мои блоги');
        expect(tester.takeException(), isNull);
        if (directory != null) {
          await _capture(tester, '$directory/studio-${size.width.toInt()}.png');
        }
        harness.router.push('/blogs/$_blogId/posts/$_postId');
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull);
        if (directory != null) {
          await _capture(tester, '$directory/editor-${size.width.toInt()}.png');
        }
        await _visible(tester, find.text('Подтвердить эту версию'));
        expect(tester.takeException(), isNull);
        await _unmount(tester);
      }
    },
  );
}

SelectedBlogVideo _selected() => SelectedBlogVideo(
  file: PlatformFile(
    name: 'video.mp4',
    size: 4,
    bytes: Uint8List.fromList([1, 2, 3, 4]),
  ),
);

void _background(WidgetTester tester) {
  for (final state in [
    AppLifecycleState.inactive,
    AppLifecycleState.hidden,
    AppLifecycleState.paused,
  ]) {
    tester.binding.handleAppLifecycleStateChanged(state);
  }
}

void _foreground(WidgetTester tester) {
  final state = tester.binding.lifecycleState;
  if (state == AppLifecycleState.paused) {
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
  }
  if (state == AppLifecycleState.paused || state == AppLifecycleState.hidden) {
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
  }
  tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
}

Future<void> _pump(WidgetTester tester) async {
  for (var i = 0; i < 8; i++) {
    await tester.pump(const Duration(milliseconds: 40));
  }
}

Future<void> _unmount(WidgetTester tester) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pumpAndSettle();
}

Future<void> _visible(WidgetTester tester, Finder finder) async {
  if (finder.evaluate().isEmpty) {
    final scrollable = find
        .descendant(
          of: find.byType(ListView).last,
          matching: find.byType(Scrollable),
        )
        .first;
    tester.state<ScrollableState>(scrollable).position.jumpTo(0);
    await _pump(tester);
    for (var i = 0; i < 35 && finder.evaluate().isEmpty; i++) {
      final position = tester.state<ScrollableState>(scrollable).position;
      position.jumpTo(
        (position.pixels + 280).clamp(0, position.maxScrollExtent),
      );
      await _pump(tester);
    }
    expect(
      finder,
      findsWidgets,
      reason: tester
          .widgetList<Text>(find.byType(Text))
          .map((e) => e.data)
          .join(' | '),
    );
  }
  await tester.ensureVisible(finder.last);
  await _pump(tester);
}

Future<void> _tapFinder(
  WidgetTester tester,
  Finder finder, {
  bool settle = true,
}) async {
  await _visible(tester, finder);
  await tester.tap(finder.last);
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    await _pump(tester);
  }
}

Future<void> _tap(WidgetTester tester, String value, {bool settle = true}) =>
    _tapFinder(tester, find.text(value), settle: settle);
Future<void> _capture(WidgetTester tester, String path) async {
  final boundary = tester.renderObject<RenderRepaintBoundary>(
    find.byKey(const ValueKey('blogs-review')),
  );
  await tester.runAsync(() async {
    final image = await boundary.toImage(pixelRatio: 2);
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    await File(path).writeAsBytes(bytes!.buffer.asUint8List());
    image.dispose();
  });
}

final _pairState = StateProvider<String>((_) => 'pair-one');

class _Harness {
  _Harness(
    this.router,
    this.container,
    this.players,
    this.playerUris,
    this.playerHeaders,
  );
  final GoRouter router;
  final ProviderContainer container;
  final List<_FakePlayer> players;
  final List<Uri> playerUris;
  final List<Map<String, String>> playerHeaders;
}

Future<_Harness> _mount(
  WidgetTester tester,
  _BlogApi api, {
  required String path,
  Size size = const Size(390, 844),
  double textScale = 1,
  Future<void>? playerInit,
  Future<SelectedBlogVideo?> Function()? picker,
  bool review = false,
}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  _foreground(tester);
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
    ..httpClientAdapter = api;
  final realtime = FakeRealtime();
  final endpoints = ApiEndpoints(primary: 'https://api.example.invalid');
  final players = <_FakePlayer>[], uris = <Uri>[];
  final headers = <Map<String, String>>[];
  final router = GoRouter(
    initialLocation: path,
    observers: [chatRouteObserver],
    routes: [
      GoRoute(
        path: '/entry',
        builder: (context, _) => Scaffold(
          body: Center(
            child: TextButton(
              onPressed: () => context.push('/content'),
              child: const Text('Интересное'),
            ),
          ),
        ),
      ),
      GoRoute(
        path: '/covered',
        builder: (_, __) => const Scaffold(body: Text('Covered route')),
      ),
      GoRoute(path: '/content', builder: (_, __) => const ContentScreen()),
      GoRoute(
        path: '/content/:id',
        builder: (_, state) =>
            PublicationScreen(id: state.pathParameters['id']!),
      ),
      GoRoute(path: '/blogs', builder: (_, __) => const BlogStudioScreen()),
      GoRoute(
        path: '/blogs/new',
        builder: (_, state) => BlogProfileScreen(
          kind: state.uri.queryParameters['kind'] ?? 'individual',
        ),
      ),
      GoRoute(
        path: '/blogs/:id/profile',
        builder: (_, state) =>
            BlogProfileScreen(blogId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/blogs/:id/posts/new',
        builder: (_, state) =>
            BlogPostEditorScreen(blogId: state.pathParameters['id']!),
      ),
      GoRoute(
        path: '/blogs/:id/posts/:postId',
        builder: (_, state) => BlogPostEditorScreen(
          blogId: state.pathParameters['id']!,
          postId: state.pathParameters['postId']!,
        ),
      ),
      GoRoute(
        path: '/blogs/:id',
        builder: (_, state) =>
            BlogStudioScreen(blogId: state.pathParameters['id']!),
      ),
    ],
  );
  addTearDown(dio.close);
  addTearDown(realtime.controller.dispose);
  addTearDown(endpoints.dispose);
  addTearDown(router.dispose);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        dioProvider.overrideWithValue(dio),
        blogUploadDioProvider.overrideWithValue(dio),
        apiEndpointsProvider.overrideWithValue(endpoints),
        realtimeControllerProvider.overrideWithValue(realtime.controller),
        pairProvider.overrideWith(
          (ref) async => {
            'pair_id': ref.watch(_pairState),
            'status': 'active',
            'partner_name': 'private-name',
          },
        ),
        blogVideoPickerProvider.overrideWithValue(
          picker ?? () async => _selected(),
        ),
        hostedVideoPlayerFactoryProvider.overrideWithValue((uri, map) {
          final player = _FakePlayer(playerInit);
          players.add(player);
          uris.add(uri);
          headers.add(map);
          return player;
        }),
      ],
      child: MaterialApp.router(
        routerConfig: router,
        theme: review
            ? buildTheme()
            : ThemeData(brightness: Brightness.dark, useMaterial3: true),
        builder: (context, child) => RepaintBoundary(
          key: const ValueKey('blogs-review'),
          child: MediaQuery(
            data: MediaQuery.of(context)
                .copyWith(textScaler: TextScaler.linear(textScale)),
            child: PinGate(child: child!),
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  final container = ProviderScope.containerOf(
    tester.element(find.byType(MaterialApp)),
  );
  return _Harness(router, container, players, uris, headers);
}

class _FakePlayer extends ShortVideoPlayer {
  _FakePlayer(this.initializing);
  final Future<void>? initializing;
  bool initialized = false, isPlaying = false, closed = false;
  int playCalls = 0;
  @override
  bool get ready => initialized;
  @override
  bool get playing => isPlaying;
  @override
  bool get buffering => false;
  @override
  bool get failed => false;
  @override
  Duration get position => Duration.zero;
  @override
  Duration get duration => const Duration(seconds: 45);
  @override
  double get aspectRatio => 16 / 9;
  @override
  Future<void> initialize() async {
    await initializing;
    initialized = true;
  }

  @override
  Future<void> play() async {
    playCalls++;
    isPlaying = true;
    notifyListeners();
  }

  @override
  Future<void> pause() async {
    isPlaying = false;
  }

  @override
  Future<void> mute(bool muted) async {}
  @override
  Future<void> seekToStart() async {}
  @override
  Widget buildView() => const ColoredBox(
    color: Colors.black,
    child: Center(child: Text('Тестовое видео')),
  );
  @override
  Future<void> close() async {
    if (!closed) {
      closed = true;
      super.dispose();
    }
  }
}

Map<String, dynamic> _media() => {
  'id': _mediaId,
  'status': 'ready',
  'url': '/blogs/media/$_mediaId/video',
  'thumbnail_url': '/blogs/media/$_mediaId/thumbnail',
  'duration_seconds': 45,
  'width': 1280,
  'height': 720,
};

class _BlogApi implements HttpClientAdapter {
  bool enabled = true, hasBlog = true, hasPost = true, myConsent = true;
  bool paused = false, includeEmptyTopic = false;
  bool myApproved = false, partnerApproved = false, keepProcessing = false;
  bool failFirstUpload = false, conflictSubmit = false, denyOwnership = false;
  Completer<ResponseBody>? heldUpload;
  String kind = 'individual', profileStatus = 'active', postStatus = 'draft';
  String name = 'Маленькие открытия', bio = 'Находим радость в обычном вечере';
  String
  title = 'Охота за деталями',
  description =
      'Покажи партнёру маленький фрагмент любого предмета вокруг. Пусть попробует угадать. Потом поменяйтесь ролями.';
  int revision = 1, polls = 0;
  final requests = <RequestOptions>[];
  final uploadedBytes = <List<int>>[];
  final createdProfiles = <Map>[],
      profileConsents = <Map>[],
      createdPosts = <Map>[],
      submits = <Map>[],
      reports = <Map>[];
  Map<String, dynamic> get settings => {
    if (enabled) 'public_blogs_enabled': true,
    'unified_feed': true,
    'has_pair': true,
    'topics': [
      if (includeEmptyTopic)
        {'id': 'fresh', 'title': 'Новая тема', 'available': false},
      {'id': 'home', 'title': 'Дом и уют'},
      {'id': 'digital', 'title': 'Цифровая жизнь'},
    ],
    'languages': [
      {'id': 'ru', 'title': 'Русский'},
      {'id': 'en', 'title': 'Английский'},
      {'id': 'none', 'title': 'Без слов'},
      {'id': 'all', 'title': 'Все языки'},
    ],
    'personal_topics': ['digital'],
    'shared_topics': ['home'],
    'personal_language': 'ru',
    'shared_language': 'ru',
    'personal_revision': 0,
    'shared_revision': 0,
    'followed_author_ids': [],
    'hidden_author_ids': [],
  };
  Map<String, dynamic> get blog => {
    'id': _blogId,
    'kind': kind,
    'name': name,
    'bio': bio,
    'status': profileStatus,
    'revision': 1,
    'my_consent': myConsent,
    'partner_consent': kind == 'couple',
    'can_edit': !paused,
    'can_publish': profileStatus == 'active' && !paused,
    'paused': paused,
  };
  Map<String, dynamic> get post => {
    'id': _postId,
    'blog_id': _blogId,
    'content_id': 'post-$_postId',
    'title': title,
    'description': description,
    'topics': ['home'],
    'language': 'ru',
    'media_id': _mediaId,
    'media': _media(),
    'status': postStatus,
    'revision': revision,
    'my_approved': myApproved,
    'partner_approved': partnerApproved,
    'can_edit': true,
    'can_submit': ['draft', 'pending_partner'].contains(postStatus),
    'can_withdraw': postStatus != 'withdrawn',
  };
  Map<String, dynamic> get publication => {
    'id': 'post-$_postId',
    'kind': 'video',
    'title': title,
    'summary': description,
    'author': {'id': 'blog-$_blogId', 'kind': kind, 'name': name, 'bio': bio},
    'topics': ['home'],
    'language': 'ru',
    'blocks': [
      {'type': 'paragraph', 'text': description},
    ],
    'sources': [],
    'published_at': '2026-10-23T18:00:00Z',
    'updated_at': '2026-10-23T18:00:00Z',
    'reading_minutes': 0,
    'video_id': null,
    'playback_kind': 'hosted',
    'media': _media(),
    'duration_seconds': 45,
    'recommendation_reason': 'По теме «Дом и уют»',
  };
  ResponseBody _json(Object data, {int status = 200}) =>
      ResponseBody.fromString(
        jsonEncode(data),
        status,
        headers: {
          Headers.contentTypeHeader: [Headers.jsonContentType],
        },
      );
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? stream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    final path = options.path;
    if (options.method == 'POST' && path.endsWith('/media')) {
      final bytes = <int>[];
      if (stream != null) {
        await for (final chunk in stream) {
          bytes.addAll(chunk);
        }
      }
      uploadedBytes.add(bytes);
      if (failFirstUpload && uploadedBytes.length == 1) {
        throw DioException.connectionError(
          requestOptions: options,
          reason: 'lost response',
        );
      }
      if (heldUpload != null) return heldUpload!.future;
      return _json({
        'id': _mediaId,
        'status': 'queued',
        'source_bytes': bytes.length,
        'error_code': null,
        'media': null,
      }, status: 202);
    }
    if (stream != null) await stream.drain<void>();
    final body = options.data is Map
        ? Map<String, dynamic>.from(options.data as Map)
        : <String, dynamic>{};
    if (path == '/content/settings') return _json(settings);
    if (path == '/content/feed') {
      return _json({
        'items': [publication],
        'total': 1,
        'next_cursor': null,
      });
    }
    if (path == '/content/post-$_postId') return _json(publication);
    if (path.endsWith('/report')) {
      reports.add(body);
      return _json({'ok': true, 'can_hide': true});
    }
    if (path.startsWith('/blogs') && denyOwnership) {
      return _json({'detail': 'Недоступно'}, status: 404);
    }
    if (path == '/blogs/mine') {
      return _json({
        'items': hasBlog ? [blog] : [],
      });
    }
    if (path == '/blogs' && options.method == 'POST') {
      createdProfiles.add(body);
      hasBlog = true;
      name = body['name'] as String;
      bio = body['bio'] as String;
      kind = body['kind'] as String;
      profileStatus = kind == 'couple' ? 'pending_consent' : 'active';
      return _json(blog);
    }
    if (path == '/blogs/$_blogId') return _json(blog);
    if (path == '/blogs/$_blogId/consent') {
      profileConsents.add(body);
      myConsent = true;
      profileStatus = 'active';
      return _json(blog);
    }
    if (path == '/blogs/$_blogId/posts') {
      if (options.method == 'POST') {
        createdPosts.add(body);
        hasPost = true;
        title = body['title'] as String;
        description = body['description'] as String;
        return _json(post);
      }
      return _json({
        'items': hasPost ? [post] : [],
      });
    }
    if (path == '/blogs/$_blogId/posts/$_postId') return _json(post);
    if (path.endsWith('/submit')) {
      submits.add(body);
      if (conflictSubmit) {
        conflictSubmit = false;
        revision++;
        return _json({'detail': 'Revision conflict'}, status: 409);
      }
      myApproved = true;
      postStatus = kind == 'individual' || partnerApproved
          ? 'pending_review'
          : 'pending_partner';
      return _json(post);
    }
    if (path.endsWith('/withdraw')) {
      postStatus = 'withdrawn';
      revision++;
      return _json(post);
    }
    if (path == '/blogs/media/$_mediaId') {
      polls++;
      return _json({
        'id': _mediaId,
        'status': keepProcessing ? 'processing' : 'ready',
        'source_bytes': 4,
        'error_code': null,
        'media': keepProcessing ? null : _media(),
      });
    }
    return _json({'detail': 'Not found'}, status: 404);
  }

  @override
  void close({bool force = false}) {}
}
