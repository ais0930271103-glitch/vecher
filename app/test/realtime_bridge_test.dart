import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/realtime_bridge.dart';
import 'package:vecher_app/core/realtime_provider.dart';
import 'package:vecher_app/features/today/paired_today.dart';

import 'support/fake_realtime.dart';

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({'vecher_token': 'account'});
    PinGate.unlock();
  });

  testWidgets('initial PIN check blocks realtime until unlock', (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'vecher_token': 'account',
      'vecher_pin': '1234',
    });
    final fake = FakeRealtime();
    addTearDown(fake.controller.dispose);
    await _mount(tester, fake);
    expect(PinGate.isReady, isTrue);
    expect(PinGate.isLocked, isTrue);
    expect(fake.loads, 0);
    // Unlock before the next frame: this test checks the gate, not font rendering.
    PinGate.unlock();
    await tester.pump();
    expect(fake.loads, 1);
    expect(fake.transports, hasLength(1));
    await tester.pumpWidget(const SizedBox.shrink());
    expect(fake.transport.closed, isTrue);
  });

  testWidgets(
    'background closes synchronously; resume resyncs; logout stops before token deletion',
    (tester) async {
      final fake = FakeRealtime();
      addTearDown(fake.controller.dispose);
      await _mount(tester, fake);
      await tester.pump();
      fake.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      final initial = fake.transport;
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      expect(initial.closed, isTrue);
      expect(fake.controller.connected, isFalse);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.paused);
      await tester.pump(const Duration(seconds: 20));
      expect(fake.loads, 1);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.hidden);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await tester.pump();
      expect(fake.loads, 2);
      final resumed = fake.transport;
      resumed.subscribed();
      final clear = clearToken();
      expect(resumed.closed, isTrue);
      expect(fake.controller.active, isFalse);
      await clear;
      await tester.pump();
      expect(fake.loads, 2);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'scope invalidation refetches current REST provider without reconnecting',
    (tester) async {
      final fake = FakeRealtime();
      var todayLoads = 0;
      addTearDown(fake.controller.dispose);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            realtimeControllerProvider.overrideWithValue(fake.controller),
            todayProvider.overrideWith((_) async => {'load': ++todayLoads}),
          ],
          child: MaterialApp(
            home: PinGate(
              child: RealtimeBridge(
                child: Consumer(
                  builder: (context, ref, _) {
                    return Text('${ref.watch(todayProvider).valueOrNull}');
                  },
                ),
              ),
            ),
          ),
        ),
      );
      await tester.pump();
      fake.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pump();
      final before = todayLoads;
      fake.transport.publish(1, ['chat']);
      await tester.pump(const Duration(milliseconds: 200));
      expect(todayLoads, before);
      fake.transport.publish(2, ['today']);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pump();
      expect(todayLoads, before + 1);
      fake.transport.publish(3, ['pair']);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pump();
      expect(todayLoads, before + 2);
      expect(fake.transports, hasLength(1));
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
}

Future<void> _mount(WidgetTester tester, FakeRealtime fake) =>
    tester.pumpWidget(
      ProviderScope(
        overrides: [
          realtimeControllerProvider.overrideWithValue(fake.controller),
        ],
        child: const MaterialApp(
          home: PinGate(child: RealtimeBridge(child: SizedBox.shrink())),
        ),
      ),
    );
