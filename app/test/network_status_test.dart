import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/network_client.dart';
import 'package:vecher_app/core/network_endpoints.dart';
import 'package:vecher_app/core/network_status.dart';
import 'package:dio/dio.dart';

import 'dart:typed_data';

class _Health implements HttpClientAdapter {
  int requests = 0;
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    expect(options.method, 'GET');
    expect(options.uri.path, '/health');
    requests++;
    return ResponseBody.fromString(
      '{"ok":true}',
      200,
      headers: {
        Headers.contentTypeHeader: ['application/json'],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

class _Draft extends StatefulWidget {
  const _Draft();
  @override
  State<_Draft> createState() => _DraftState();
}

class _DraftState extends State<_Draft> {
  final controller = TextEditingController();
  @override
  Widget build(BuildContext context) =>
      Scaffold(body: TextField(controller: controller));
  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }
}

void main() {
  testWidgets(
    'reachability row preserves an open draft and retries only a safe read',
    (tester) async {
      final endpoints = ApiEndpoints(primary: 'https://api.example.org');
      final adapter = _Health();
      final dio = createApiDio(endpoints: endpoints, adapter: adapter);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            apiEndpointsProvider.overrideWithValue(endpoints),
            dioProvider.overrideWithValue(dio),
          ],
          child: MaterialApp(
            builder: (context, child) => NetworkStatus(child: child!),
            home: const _Draft(),
          ),
        ),
      );
      await tester.enterText(find.byType(TextField), 'Сохранённый черновик');
      endpoints.unavailable(startingRevision: endpoints.revision);
      await tester.pump();
      expect(find.text('Нет связи с сервером'), findsOneWidget);
      expect(find.text('Сохранённый черновик'), findsOneWidget);
      expect(
        tester.getBottomLeft(find.text('Нет связи с сервером')).dy,
        lessThan(tester.getTopLeft(find.byType(TextField)).dy),
      );
      await tester.tap(find.text('Повторить'));
      await tester.pumpAndSettle();
      expect(adapter.requests, 1);
      expect(find.text('Нет связи с сервером'), findsNothing);
      expect(find.text('Сохранённый черновик'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
      dio.close(force: true);
      endpoints.dispose();
    },
  );
}
