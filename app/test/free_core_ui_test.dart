import 'dart:convert';
import 'dart:io';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/realtime_provider.dart';
import 'package:vecher_app/core/ui.dart';
import 'package:vecher_app/features/auth/consent_screen.dart';
import 'package:vecher_app/features/wishes/plan_screen.dart';
import 'package:vecher_app/features/wishes/wishes_screen.dart';

import 'support/fake_realtime.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late ByteData testFont;

  setUpAll(() async {
    // The behavior checks use Flutter's normal test font, never a CDN request.
    final packageConfig = File('.dart_tool/package_config.json').absolute;
    final packages = jsonDecode(await packageConfig.readAsString()) as Map;
    final flutterTest = (packages['packages'] as List).cast<Map>().singleWhere(
      (package) => package['name'] == 'flutter_test',
    );
    final root = flutterTest['rootUri'] as String;
    final flutterTestRoot = packageConfig.uri.resolve(
      root.endsWith('/') ? root : '$root/',
    );
    testFont = ByteData.sublistView(
      await File.fromUri(
        flutterTestRoot.resolve('../flutter_tools/static/Ahem.ttf'),
      ).readAsBytes(),
    );
  });

  setUp(() {
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
  });

  for (final legacy in [true, false]) {
    testWidgets(
      '${legacy ? 'exhausted legacy quota' : 'unlimited core'} opens and votes without a subscription',
      (tester) async {
        final adapter = _FreeCoreAdapter(legacy: legacy);
        await _mount(tester, adapter, const PlanScreen(cardId: 42));

        expect(find.text('Открыть план — бесплатно'), findsOneWidget);
        expect(find.text('Вечер+'), findsNothing);
        expect(find.textContaining('тестовую подписку'), findsNothing);
        expect(find.text('Подготовьте чай и выберите музыку.'), findsNothing);

        await tester.ensureVisible(find.text('Открыть план — бесплатно'));
        await tester.tap(find.text('Открыть план — бесплатно'));
        await tester.pumpAndSettle();

        expect(adapter.opened, isTrue);
        expect(find.text('Открыть план — бесплатно'), findsNothing);
        expect(find.text('Подготовьте чай и выберите музыку.'), findsOneWidget);
        await tester.ensureVisible(find.text('Выбрать этот вариант'));
        await tester.tap(find.text('Выбрать этот вариант'));
        await tester.pumpAndSettle();

        expect(adapter.posts.map((r) => r.path), [
          '/plans/42/open',
          '/plans/42/vote',
        ]);
        expect(adapter.posts.last.data, {'option': 0});
        expect(find.text('Ты выбрал(а) этот вариант'), findsOneWidget);
        expect(tester.takeException(), isNull);
        await _unmount(tester);
      },
    );
  }

  testWidgets(
    'an old API payment response asks to wait for the service update',
    (tester) async {
      final adapter = _FreeCoreAdapter(legacy: true, openStatus: 402);
      await _mount(tester, adapter, const PlanScreen(cardId: 42));
      await tester.ensureVisible(find.text('Открыть план — бесплатно'));
      await tester.tap(find.text('Открыть план — бесплатно'));
      await tester.pumpAndSettle();

      expect(adapter.opened, isFalse);
      expect(
        find.text(
          'Сервис ещё не обновлён для бесплатных планов. Попробуйте позже.',
        ),
        findsOneWidget,
      );
      expect(find.textContaining('Вечер+'), findsNothing);
      expect(find.text('Подготовьте чай и выберите музыку.'), findsNothing);
      expect(adapter.posts.map((r) => r.path), ['/plans/42/open']);
      await _unmount(tester);
    },
  );

  testWidgets('free access never treats a permission failure as an open plan', (
    tester,
  ) async {
    final adapter = _FreeCoreAdapter(openStatus: 403);
    await _mount(tester, adapter, const PlanScreen(cardId: 42));
    await tester.ensureVisible(find.text('Открыть план — бесплатно'));
    await tester.tap(find.text('Открыть план — бесплатно'));
    await tester.pumpAndSettle();

    expect(find.text('Нужно согласие обоих.'), findsOneWidget);
    expect(adapter.opened, isFalse);
    expect(find.text('Подготовьте чай и выберите музыку.'), findsNothing);
    expect(adapter.posts.map((r) => r.path), ['/plans/42/open']);
    await _unmount(tester);
  });

  testWidgets(
    'mutual hot preference shows an unavailable future in-app section',
    (tester) async {
      final adapter = _FreeCoreAdapter(nightOpen: true);
      await _mount(tester, adapter, const WishesScreen());

      expect(find.text('Вечер 18+'), findsOneWidget);
      expect(
        find.textContaining('Дополнительный платный раздел внутри приложения'),
        findsOneWidget,
      );
      expect(find.textContaining('Он пока недоступен.'), findsOneWidget);
      expect(
        find.textContaining('Основной «Вечер» остаётся бесплатным.'),
        findsOneWidget,
      );
      expect(find.textContaining('отдельное приложение'), findsNothing);
      expect(find.textContaining('режим «Ночь»'), findsNothing);
      expect(find.textContaining('Вам открыто: «Остро».'), findsOneWidget);
      final placeholder = tester.widget<GoldButton>(
        find.widgetWithText(GoldButton, 'В разработке'),
      );
      expect(placeholder.onPressed, isNull);
      expect(adapter.posts, isEmpty);
      expect(tester.takeException(), isNull);
      await _unmount(tester);
    },
  );

  testWidgets('one hot preference does not expose the future section as open', (
    tester,
  ) async {
    final adapter = _FreeCoreAdapter(nightOpen: false);
    await _mount(tester, adapter, const WishesScreen());

    expect(
      find.text(
        'Уровень «Жарко» относится к будущему дополнительному разделу «Вечер 18+».',
      ),
      findsOneWidget,
    );
    expect(find.text('В разработке'), findsNothing);
    expect(find.textContaining('Вам открыто: «Остро».'), findsOneWidget);
    expect(find.textContaining('отдельное приложение'), findsNothing);
    expect(adapter.posts, isEmpty);
    expect(tester.takeException(), isNull);
    await _unmount(tester);
  });
}

Future<void> _mount(
  WidgetTester tester,
  _FreeCoreAdapter adapter,
  Widget screen,
) async {
  // Room for the intentionally wide Ahem glyphs; these are behavior tests.
  tester.view.physicalSize = const Size(600, 1000);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
    ..httpClientAdapter = adapter;
  final realtime = FakeRealtime();
  addTearDown(dio.close);
  addTearDown(realtime.controller.dispose);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        dioProvider.overrideWithValue(dio),
        realtimeControllerProvider.overrideWithValue(realtime.controller),
        meProvider.overrideWith((_) async => {'special_ok': true}),
      ],
      child: MaterialApp(theme: ThemeData.dark(), home: screen),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> _unmount(WidgetTester tester) async {
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
}

class _FreeCoreAdapter implements HttpClientAdapter {
  _FreeCoreAdapter({
    this.legacy = false,
    this.openStatus = 200,
    this.nightOpen = false,
  });

  final bool legacy;
  final int openStatus;
  final bool nightOpen;
  final posts = <RequestOptions>[];
  bool opened = false;
  int? vote;

  Map<String, dynamic> get plan => {
    'card_id': 42,
    'text': 'Вечер дома',
    'deck_title': 'Дома',
    'kind': 'yes',
    'intro': 'Выберите подходящий вариант.',
    'locked': !opened,
    'premium': false,
    'free_plans_left': legacy ? 0 : 1,
    if (!legacy) 'core_plans_unlimited': true,
    'options': [
      {
        'index': 0,
        'title': 'Чай и музыка',
        'note': '',
        'items': <String>[],
        'steps': opened ? ['Подготовьте чай и выберите музыку.'] : <String>[],
      },
    ],
    'my_vote': vote,
    'partner_vote': null,
    'chosen': null,
    'places_available': false,
    'scheduled_at': null,
  };

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    if (requestStream != null) await requestStream.drain<void>();
    if (options.method == 'POST') posts.add(options);
    if (options.method == 'GET' && options.path == '/pairs/me') {
      return _json({'my_name': 'Аня', 'partner_name': 'Саша'});
    }
    if (options.method == 'GET' && options.path == '/plans/42') {
      return _json(plan);
    }
    if (options.method == 'POST' && options.path == '/plans/42/open') {
      if (openStatus != 200) {
        return _json({
          'detail': openStatus == 402
              ? 'Нужен «Вечер+»'
              : 'Нужно согласие обоих.',
        }, status: openStatus);
      }
      opened = true;
      return _json(plan);
    }
    if (options.method == 'POST' && options.path == '/plans/42/vote') {
      if (!opened) throw StateError('Cannot vote before opening');
      vote = (options.data as Map)['option'] as int;
      return _json(plan);
    }
    if (options.method == 'GET' && options.path == '/wishes/decks') {
      return _json({
        'matches': 0,
        'new_matches': 0,
        'decks': <Object>[],
        'level': {
          'mine': 'hot',
          'effective': nightOpen ? 'hot' : 'spicy',
          'night_open': nightOpen,
        },
      });
    }
    throw StateError('Unexpected request ${options.method} ${options.path}');
  }

  ResponseBody _json(Object data, {int status = 200}) =>
      ResponseBody.fromString(
        jsonEncode(data),
        status,
        headers: {
          Headers.contentTypeHeader: [Headers.jsonContentType],
        },
      );

  @override
  void close({bool force = false}) {}
}
