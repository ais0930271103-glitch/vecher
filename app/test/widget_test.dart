import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:vecher_app/features/welcome/welcome_screen.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late ByteData testFont;
  late ByteData emblem;

  setUpAll(() async {
    // Use Flutter's normal test font. This smoke test checks welcome navigation,
    // so it should neither download Google Fonts nor depend on their CDN.
    final packageConfig = File('.dart_tool/package_config.json').absolute;
    final packages = jsonDecode(await packageConfig.readAsString()) as Map;
    final flutterTest = (packages['packages'] as List).cast<Map>().singleWhere(
      (package) => package['name'] == 'flutter_test',
    );
    final root = flutterTest['rootUri'] as String;
    final flutterTestRoot = packageConfig.uri.resolve(
      root.endsWith('/') ? root : '$root/',
    );
    final fontBytes = await File.fromUri(
      flutterTestRoot.resolve('../flutter_tools/static/Ahem.ttf'),
    ).readAsBytes();
    testFont = ByteData.sublistView(fontBytes);
    emblem = ByteData.sublistView(
      await File('assets/emblem.png').readAsBytes(),
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
              'assets/emblem.png': [
                {'asset': 'assets/emblem.png'},
              ],
            });
          }
          if (key == 'test/Comfortaa-Regular.ttf') return testFont;
          if (key == 'assets/emblem.png') return emblem;
          return null;
        });
  });

  tearDown(() {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger
        .setMockMessageHandler('flutter/assets', null);
    GoogleFonts.config.allowRuntimeFetching = true;
  });

  testWidgets('welcome renders and both entry actions open the login route', (
    tester,
  ) async {
    // Ahem has deliberately wide square glyphs. Use a roomy viewport for this
    // navigation smoke; device typography/layout are not golden-tested here.
    tester.view.physicalSize = const Size(600, 1000);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final router = GoRouter(
      routes: [
        GoRoute(path: '/', builder: (_, __) => const WelcomeScreen()),
        GoRoute(
          path: '/login',
          builder: (_, __) => const Scaffold(body: Text('Экран входа')),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(
      MaterialApp.router(theme: ThemeData.dark(), routerConfig: router),
    );
    await tester.pumpAndSettle();
    expect(find.text('Вечер'), findsOneWidget);
    expect(find.byType(Image), findsOneWidget);
    expect(
      tester.widget<Image>(find.byType(Image)).semanticLabel,
      'Эмблема Вечер',
    );
    expect(find.text('Начать'), findsOneWidget);
    expect(find.text('У меня есть приглашение'), findsOneWidget);
    expect(tester.takeException(), isNull);

    await tester.tap(find.text('Начать'));
    await tester.pumpAndSettle();
    expect(router.routeInformationProvider.value.uri.path, '/login');
    expect(find.text('Экран входа'), findsOneWidget);

    router.go('/');
    await tester.pumpAndSettle();
    await tester.tap(find.text('У меня есть приглашение'));
    await tester.pumpAndSettle();
    expect(router.routeInformationProvider.value.uri.path, '/login');
    expect(find.text('Экран входа'), findsOneWidget);
  });
}
