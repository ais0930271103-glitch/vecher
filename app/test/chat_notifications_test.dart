import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/chat_presence.dart';
import 'package:vecher_app/core/top_banner.dart';

void main() {
  test('covered/disposed chat owners do not hide a still-visible chat', () {
    final first = Object();
    final second = Object();
    setChatScreenVisible(first, true);
    setChatScreenVisible(second, true);
    setChatScreenVisible(first, false);
    expect(chatOnScreen, isTrue);
    setChatScreenVisible(second, false);
    expect(chatOnScreen, isFalse);
  });

  testWidgets('top feedback stays above the composer with the keyboard open', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetViewInsets);
    await tester.pumpWidget(_BannerHost());
    await tester.tap(find.text('Показать уведомление'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    final banner = tester.getRect(find.byKey(const ValueKey('top-banner')));
    final composer = tester.getRect(find.byKey(const ValueKey('composer')));
    expect(banner.top, greaterThanOrEqualTo(0));
    expect(banner.bottom, lessThan(composer.top));
    expect(find.byType(SnackBar), findsNothing);
    await tester.tap(find.byTooltip('Закрыть уведомление'));
    await tester.pump();
    expect(find.text('Файл сохранён'), findsNothing);
    hideTopBanner();
  });

  testWidgets('replacing a banner cancels the earlier dismissal timer', (
    tester,
  ) async {
    late BuildContext overlayContext;
    await tester.pumpWidget(
      MaterialApp(
        home: Builder(
          builder: (context) {
            overlayContext = context;
            return const Scaffold(body: SizedBox.expand());
          },
        ),
      ),
    );
    showTopBanner(
      Overlay.of(overlayContext),
      text: 'Первое',
      duration: const Duration(seconds: 1),
    );
    await tester.pump(const Duration(milliseconds: 600));
    showTopBanner(
      Overlay.of(overlayContext),
      text: 'Второе',
      duration: const Duration(seconds: 2),
    );
    await tester.pump(const Duration(milliseconds: 600));
    expect(find.text('Первое'), findsNothing);
    expect(find.text('Второе'), findsOneWidget);
    await tester.pump(const Duration(seconds: 2));
    expect(find.text('Второе'), findsNothing);
    hideTopBanner();
  });
}

class _BannerHost extends StatelessWidget {
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Builder(
        builder: (context) {
          return Scaffold(
            body: Column(
              children: [
                Expanded(
                  child: Center(
                    child: TextButton(
                      onPressed: () => showTopBanner(
                        Overlay.of(context),
                        text: 'Файл сохранён',
                      ),
                      child: const Text('Показать уведомление'),
                    ),
                  ),
                ),
                const TextField(
                  key: ValueKey('composer'),
                  decoration: InputDecoration(hintText: 'Сообщение'),
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}
