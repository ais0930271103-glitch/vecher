import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_secure_storage/test/test_flutter_secure_storage_platform.dart';
import 'package:flutter_secure_storage_platform_interface/flutter_secure_storage_platform_interface.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/core/api.dart';
import 'package:vecher_app/core/chat_presence.dart';
import 'package:vecher_app/core/privacy.dart';
import 'package:vecher_app/core/realtime_provider.dart';
import 'package:vecher_app/core/session_changes.dart';
import 'package:vecher_app/core/top_banner.dart';
import 'package:vecher_app/features/chat/chat_screen.dart';
import 'package:vecher_app/features/today/today_screen.dart' show pairProvider;

import 'support/fake_realtime.dart';

void main() {
  setUp(() {
    FlutterSecureStorage.setMockInitialValues({'vecher_chat_hint_seen': '1'});
    PinGate.unlock();
  });
  tearDown(() {
    if (sessionChanges.isChanging) {
      sessionChanges.finish(sessionChanges.revision);
    }
  });

  for (final publication in [true, false]) {
    final attachment = publication
        ? {'draft_text': 'Практический план\n\nПопробуем вечером?'}
        : {'kind': 'video', 'id': 'saved-video', 'title': 'Наше видео'};
    final kind = publication ? 'publication text' : 'legacy video reference';

    testWidgets('$kind and user edits survive all/resume with the same pair', (
      tester,
    ) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      addTearDown(fake.controller.dispose);
      await _mountChat(
        tester,
        adapter,
        realtime: fake,
        attach: attachment,
        usePairApi: true,
      );
      await _connect(tester, fake);
      await tester.enterText(find.byType(TextField), 'Мой изменённый черновик');
      final pairLoads = adapter.pairLoads;
      fake.transport.publish(101, ['all']);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      expect(adapter.pairLoads, greaterThan(pairLoads));
      expect(_draftText(tester), 'Мой изменённый черновик');
      if (!publication) expect(find.text('Наше видео'), findsOneWidget);

      for (final state in [
        AppLifecycleState.inactive,
        AppLifecycleState.hidden,
        AppLifecycleState.paused,
      ]) {
        tester.binding.handleAppLifecycleStateChanged(state);
      }
      await tester.pump(const Duration(seconds: 2));
      for (final state in [
        AppLifecycleState.hidden,
        AppLifecycleState.inactive,
        AppLifecycleState.resumed,
      ]) {
        tester.binding.handleAppLifecycleStateChanged(state);
      }
      // Reconnected realtime emits a full resync after returning from another
      // network/VPN. Exercise the real ChatScreen subscription, not a draft stub.
      fake.transport.publish(102, ['all']);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      expect(_draftText(tester), 'Мой изменённый черновик');
      if (!publication) expect(find.text('Наше видео'), findsOneWidget);
      expect(adapter.posts, isEmpty);
      await _unmountChat(tester);
    });

    for (final removed in [false, true]) {
      testWidgets(
        '$kind clears only after REST confirms ${removed ? 'pair removal' : 'a different pair_id'}',
        (tester) async {
          final fake = FakeRealtime();
          final adapter = _ChatAdapter();
          addTearDown(fake.controller.dispose);
          await _mountChat(
            tester,
            adapter,
            realtime: fake,
            attach: attachment,
            usePairApi: true,
          );
          await _connect(tester, fake);
          await tester.enterText(
            find.byType(TextField),
            'Черновик прежней пары',
          );
          final delayed = adapter.delayedPair = Completer<ResponseBody>();
          fake.transport.publish(111, ['pair']);
          await tester.pump(const Duration(milliseconds: 200));
          expect(adapter.delayedPair, isNull);
          expect(_draftText(tester), 'Черновик прежней пары');
          adapter.pair = removed
              ? null
              : {
                  ...adapter.pair!,
                  'pair_id': 'pair-two',
                  'partner_id': 'partner-two',
                };
          delayed.complete(adapter._json(adapter.pair));
          await tester.pumpAndSettle();
          expect(_draftText(tester), isEmpty);
          if (!publication) expect(find.text('Наше видео'), findsNothing);
          expect(adapter.posts, isEmpty);
          await _unmountChat(tester);
        },
      );
    }

    testWidgets('$kind clears immediately on an account session change', (
      tester,
    ) async {
      final adapter = _ChatAdapter();
      await _mountChat(tester, adapter, attach: attachment, usePairApi: true);
      await tester.enterText(
        find.byType(TextField),
        'Личный черновик аккаунта',
      );
      final revision = sessionChanges.begin();
      await tester.pump();
      expect(_draftText(tester), isEmpty);
      if (!publication) expect(find.text('Наше видео'), findsNothing);
      sessionChanges.finish(revision);
      await tester.pumpAndSettle();
      expect(adapter.posts, isEmpty);
      await _unmountChat(tester);
    });
  }

  testWidgets(
    'pair refresh with the same pair_id preserves the publication draft',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      addTearDown(fake.controller.dispose);
      await _mountChat(
        tester,
        adapter,
        realtime: fake,
        usePairApi: true,
        attach: {'draft_text': 'Название публикации\n\nОписание'},
      );
      await _connect(tester, fake);
      await tester.enterText(find.byType(TextField), 'Моё дополнение');
      final delayed = adapter.delayedPair = Completer<ResponseBody>();
      fake.transport.publish(121, ['pair']);
      await tester.pump(const Duration(milliseconds: 200));
      expect(_draftText(tester), 'Моё дополнение');
      delayed.complete(
        adapter._json({
          ...adapter.pair!,
          'paused': true,
          'partner_name': 'Новое имя',
        }),
      );
      await tester.pumpAndSettle();
      expect(_draftText(tester), 'Моё дополнение');
      expect(adapter.posts, isEmpty);
      await _unmountChat(tester);
    },
  );

  testWidgets(
    'pending and failed pair checks preserve edits and never send automatically',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      addTearDown(fake.controller.dispose);
      await _mountChat(
        tester,
        adapter,
        realtime: fake,
        attach: {'draft_text': 'Публикация для нашей пары'},
      );
      await _connect(tester, fake);
      final delayed = adapter.delayedPair = Completer<ResponseBody>();
      fake.transport.publish(130, ['all']);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.enterText(find.byType(TextField), 'Дополненный черновик');
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pump();
      expect(adapter.posts, isEmpty);
      delayed.completeError(
        DioException(
          requestOptions: RequestOptions(path: '/pairs/me'),
          type: DioExceptionType.receiveTimeout,
        ),
      );
      await tester.pumpAndSettle();
      expect(
        find.text('Не удалось проверить пару. Черновик сохранён.'),
        findsOneWidget,
      );
      expect(_draftText(tester), 'Дополненный черновик');
      expect(
        tester.widget<TextField>(find.byType(TextField)).readOnly,
        isFalse,
      );
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pump();
      expect(adapter.posts, isEmpty);
      await tester.tap(find.text('Проверить'));
      await tester.pumpAndSettle();
      expect(adapter.posts, isEmpty);
      expect(_draftText(tester), 'Дополненный черновик');
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pumpAndSettle();
      expect(adapter.posts.single['expected_pair_id'], 'pair-one');
      expect(adapter.posts.single['text'], 'Дополненный черновик');
      await _unmountChat(tester);
    },
  );

  testWidgets('resume rechecks the pair before Send without a realtime event', (
    tester,
  ) async {
    final adapter = _ChatAdapter();
    await _mountChat(
      tester,
      adapter,
      attach: {'draft_text': 'После смены сети'},
    );
    for (final state in [
      AppLifecycleState.inactive,
      AppLifecycleState.hidden,
      AppLifecycleState.paused,
    ]) {
      tester.binding.handleAppLifecycleStateChanged(state);
    }
    await tester.pump();
    final delayed = adapter.delayedPair = Completer<ResponseBody>();
    for (final state in [
      AppLifecycleState.hidden,
      AppLifecycleState.inactive,
      AppLifecycleState.resumed,
    ]) {
      tester.binding.handleAppLifecycleStateChanged(state);
    }
    await tester.pump();
      await tester.pump(const Duration(milliseconds: 20));
    expect(adapter.delayedPair, isNull);
    await tester.tap(find.byTooltip('Отправить'));
    await tester.pump();
    expect(adapter.posts, isEmpty);
    delayed.complete(adapter._json(adapter.pair));
    await tester.pumpAndSettle();
    expect(_draftText(tester), 'После смены сети');
    expect(adapter.posts, isEmpty);
    await _unmountChat(tester);
  });

  testWidgets(
    'late pair response from a previous account cannot authorize its draft',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      addTearDown(fake.controller.dispose);
      await _mountChat(
        tester,
        adapter,
        realtime: fake,
        attach: {'draft_text': 'Старый аккаунт'},
      );
      await _connect(tester, fake);
      final oldPair = adapter._json(adapter.pair);
      final delayed = adapter.delayedPair = Completer<ResponseBody>();
      fake.transport.publish(131, ['all']);
      await tester.pump(const Duration(milliseconds: 200));
      final revision = sessionChanges.begin();
      adapter.pair = {...adapter.pair!, 'pair_id': 'pair-two'};
      sessionChanges.finish(revision);
      await tester.pumpAndSettle();
      expect(_draftText(tester), isEmpty);
      delayed.complete(oldPair);
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), 'Новый аккаунт');
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pumpAndSettle();
      expect(adapter.posts.single['expected_pair_id'], 'pair-two');
      expect(adapter.posts.single['text'], 'Новый аккаунт');
      await _unmountChat(tester);
    },
  );

  testWidgets(
    'server pair_changed blocks retries until REST confirms the new context',
    (tester) async {
      final adapter = _ChatAdapter()..rejectNextPairPost = true;
      await _mountChat(tester, adapter, attach: {'draft_text': 'Прежней паре'});
      final delayed = adapter.delayedPair = Completer<ResponseBody>();
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pumpAndSettle();
      expect(adapter.posts.single['expected_pair_id'], 'pair-one');
      expect(adapter.messages, isEmpty);
      expect(_draftText(tester), 'Прежней паре');
      expect(adapter.delayedPair, isNull);
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pump();
      expect(adapter.posts, hasLength(1));
      adapter.pair = {...adapter.pair!, 'pair_id': 'pair-two'};
      delayed.complete(adapter._json(adapter.pair));
      await tester.pumpAndSettle();
      expect(_draftText(tester), isEmpty);
      await tester.enterText(find.byType(TextField), 'Новой паре');
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pumpAndSettle();
      expect(adapter.messages.single['expected_pair_id'], 'pair-two');
      expect(adapter.messages.single['text'], 'Новой паре');
      await _unmountChat(tester);
    },
  );

  testWidgets(
    'late native file selection is discarded after a confirmed pair change',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      final picked = Completer<List<Map<String, Object>>>();
      _mockFilePicker(tester, () => picked.future);
      addTearDown(fake.controller.dispose);
      await _mountChat(tester, adapter, realtime: fake);
      await _connect(tester, fake);
      await tester.tap(find.byTooltip('Прикрепить файл до 25 МБ'));
      await tester.pump();
      adapter.pair = {...adapter.pair!, 'pair_id': 'pair-two'};
      fake.transport.publish(132, ['pair']);
      await tester.pump(const Duration(milliseconds: 200));
      picked.complete(_pickedFile());
      await tester.pumpAndSettle();
      expect(find.text('Личный файл.txt'), findsNothing);
      expect(adapter.uploads, 0);
      expect(adapter.posts, isEmpty);
      await _unmountChat(tester);
    },
  );

  testWidgets(
    'completed upload waits locally while pair validation fails then needs explicit Send',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      _mockFilePicker(tester, () async => _pickedFile());
      addTearDown(fake.controller.dispose);
      await _mountChat(tester, adapter, realtime: fake);
      await _connect(tester, fake);
      await tester.tap(find.byTooltip('Прикрепить файл до 25 МБ'));
      await tester.pumpAndSettle();
      final upload = adapter.delayedUpload = Completer<ResponseBody>();
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pump();
      final pair = adapter.delayedPair = Completer<ResponseBody>();
      fake.transport.publish(133, ['all']);
      await tester.pump(const Duration(milliseconds: 200));
      upload.complete(adapter._json(_uploadedFile));
      await tester.pumpAndSettle();
      expect(adapter.posts, isEmpty);
      expect(find.text('Личный файл.txt'), findsOneWidget);
      pair.completeError(
        DioException(
          requestOptions: RequestOptions(path: '/pairs/me'),
          type: DioExceptionType.receiveTimeout,
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pump();
      expect(adapter.posts, isEmpty);
      await tester.tap(find.text('Проверить'));
      await tester.pumpAndSettle();
      expect(adapter.posts, isEmpty);
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pumpAndSettle();
      expect(adapter.uploads, 1);
      expect(adapter.posts.single['attachment_ids'], ['private-upload-one']);
      expect(adapter.posts.single['expected_pair_id'], 'pair-one');
      await _unmountChat(tester);
    },
  );

  for (final accountChange in [false, true]) {
    testWidgets(
      'late upload cannot enter a new ${accountChange ? 'account' : 'pair'} composer',
      (tester) async {
        final fake = FakeRealtime();
        final adapter = _ChatAdapter();
        _mockFilePicker(tester, () async => _pickedFile());
        addTearDown(fake.controller.dispose);
        await _mountChat(tester, adapter, realtime: fake);
        await _connect(tester, fake);
        await tester.tap(find.byTooltip('Прикрепить файл до 25 МБ'));
        await tester.pumpAndSettle();
        final upload = adapter.delayedUpload = Completer<ResponseBody>();
        await tester.tap(find.byTooltip('Отправить'));
        await tester.pump();
        adapter.pair = {...adapter.pair!, 'pair_id': 'pair-two'};
        if (accountChange) {
          final revision = sessionChanges.begin();
          sessionChanges.finish(revision);
        } else {
          fake.transport.publish(134, ['pair']);
        }
        await tester.pump(const Duration(milliseconds: 200));
        upload.complete(adapter._json(_uploadedFile));
        await tester.pumpAndSettle();
        expect(adapter.posts, isEmpty);
        expect(find.text('Личный файл.txt'), findsNothing);
        expect(adapter.deletedUploads, isEmpty);
        await tester.enterText(find.byType(TextField), 'Новый контекст');
        await tester.tap(find.byTooltip('Отправить'));
        await tester.pumpAndSettle();
        expect(adapter.posts.single['expected_pair_id'], 'pair-two');
        expect(adapter.posts.single.containsKey('attachment_ids'), isFalse);
        await _unmountChat(tester);
      },
    );
  }

  testWidgets(
    'covered or background chat does not suppress push or mark read',
    (tester) async {
      final adapter = _ChatAdapter();
      final navigator = GlobalKey<NavigatorState>();
      await _mountChat(tester, adapter, navigator: navigator);
      expect(chatOnScreen, isTrue);
      expect(adapter.reads, greaterThan(0));

      navigator.currentState!.push(
        MaterialPageRoute<void>(
          builder: (_) => const Scaffold(body: Text('Другой экран')),
        ),
      );
      await tester.pumpAndSettle();
      expect(chatOnScreen, isFalse);
      final coveredReads = adapter.reads;
      final coveredLoads = adapter.loads;
      await tester.pump(const Duration(seconds: 5));
      expect(adapter.reads, coveredReads);
      expect(adapter.loads, coveredLoads);

      navigator.currentState!.pop();
      await tester.pumpAndSettle();
      expect(chatOnScreen, isTrue);
      expect(adapter.reads, greaterThan(coveredReads));

      for (final state in [
        AppLifecycleState.inactive,
        AppLifecycleState.hidden,
        AppLifecycleState.paused,
      ]) {
        tester.binding.handleAppLifecycleStateChanged(state);
      }
      await tester.pump();
      expect(chatOnScreen, isFalse);
      final backgroundReads = adapter.reads;
      final backgroundLoads = adapter.loads;
      await tester.pump(const Duration(seconds: 5));
      expect(adapter.reads, backgroundReads);
      expect(adapter.loads, backgroundLoads);

      for (final state in [
        AppLifecycleState.hidden,
        AppLifecycleState.inactive,
        AppLifecycleState.resumed,
      ]) {
        tester.binding.handleAppLifecycleStateChanged(state);
      }
      await tester.pumpAndSettle();
      expect(chatOnScreen, isTrue);
      expect(adapter.reads, greaterThan(backgroundReads));
      await _unmountChat(tester);
      expect(chatOnScreen, isFalse);
    },
  );

  testWidgets('a timed-out message retries the same id and immutable payload', (
    tester,
  ) async {
    final adapter = _ChatAdapter(failFirstPost: true);
    await _mountChat(tester, adapter);
    await tester.enterText(find.byType(TextField), 'Привет из чата');
    await tester.tap(find.byTooltip('Отправить'));
    await tester.pumpAndSettle();

    expect(adapter.posts, hasLength(1));
    final original = adapter.posts.single;
    expect(original['client_message_id'], matches(RegExp(r'^[a-f0-9]{32}$')));
    expect(tester.widget<TextField>(find.byType(TextField)).readOnly, isTrue);
    expect(find.byTooltip('Повторить отправку'), findsOneWidget);
    expect(find.byType(SnackBar), findsNothing);

    await tester.tap(find.byTooltip('Повторить отправку'));
    await tester.pumpAndSettle();
    expect(adapter.posts, hasLength(2));
    expect(adapter.posts.last, original);
    expect(adapter.messages, hasLength(1));
    expect(
      tester.widget<TextField>(find.byType(TextField)).controller!.text,
      '',
    );
    expect(find.byTooltip('Повторить отправку'), findsNothing);
    expect(find.text('Привет из чата'), findsOneWidget);
    await _unmountChat(tester);
  });

  testWidgets(
    'history acknowledges a committed message after lost POST reply',
    (tester) async {
      final adapter = _ChatAdapter(
        failFirstPost: true,
        commitBeforeFailure: true,
      );
      await _mountChat(tester, adapter);
      await tester.enterText(find.byType(TextField), 'Сообщение доставлено');
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pumpAndSettle();
      expect(find.byTooltip('Повторить отправку'), findsOneWidget);

      await tester.pump(const Duration(seconds: 4));
      await tester.pumpAndSettle();
      expect(adapter.posts, hasLength(1));
      expect(find.text('Сообщение доставлено'), findsOneWidget);
      expect(find.byTooltip('Повторить отправку'), findsNothing);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        '',
      );
      await _unmountChat(tester);
    },
  );

  testWidgets('a narrow chat shows an attachment with a long Unicode name', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 740);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    final adapter = _ChatAdapter();
    adapter.messages.add({
      'id': 'message-file',
      'mine': false,
      'created_at': '2026-10-18T19:00:00Z',
      'attachments': [
        {
          'id': 'file-1',
          'name': 'Очень длинное имя нашего общего плана на вечер 🌙.pdf',
          'mime': 'application/pdf',
          'size': 1536,
        },
      ],
    });
    await _mountChat(tester, adapter);
    expect(find.textContaining('Очень длинное имя'), findsOneWidget);
    expect(find.text('1,5 КБ · открыть'), findsOneWidget);
    expect(find.byTooltip('Скачать и открыть'), findsOneWidget);
    expect(tester.takeException(), isNull);
    await _unmountChat(tester);
  });

  testWidgets(
    'delayed startup PIN lookup never loads chat or marks messages read',
    (tester) async {
      final storage = _DelayedPinStorage();
      FlutterSecureStoragePlatform.instance = storage;
      final adapter = _ChatAdapter();
      await _mountChat(tester, adapter, settle: false);
      expect(PinGate.isReady, isFalse);
      expect(chatOnScreen, isFalse);
      await tester.pump(const Duration(seconds: 5));
      expect(adapter.loads, 0);
      expect(adapter.reads, 0);
      storage.pin.complete('1234');
      await tester.idle();
      expect(PinGate.isReady, isTrue);
      expect(PinGate.isLocked, isTrue);
      expect(adapter.loads, 0);
      expect(adapter.reads, 0);
      PinGate.unlock();
      await tester.pumpAndSettle();
      expect(adapter.loads, greaterThan(0));
      expect(adapter.reads, greaterThan(0));
      await _unmountChat(tester);
    },
  );

  for (final older in [false, true]) {
    testWidgets(
      'full resync discards delayed ${older ? 'older history' : 'chat refresh'} from prior pair',
      (tester) async {
        final fake = FakeRealtime();
        final adapter = _ChatAdapter()..hasMore = true;
        final oldMessage = {
          'id': 'old-pair-message',
          'mine': false,
          'text': 'Сообщение прежней пары',
          'created_at': '2026-10-21T18:00:00Z',
          'attachments': <Object>[],
        };
        adapter.messages.add(oldMessage);
        addTearDown(fake.controller.dispose);
        await _mountChat(tester, adapter, realtime: fake);
        fake.controller.activate('account');
        await tester.pump();
        fake.transport.subscribed();
        await tester.pump(const Duration(milliseconds: 200));
        await tester.pumpAndSettle();
        expect(find.text('Сообщение прежней пары'), findsOneWidget);

        final delayed = adapter.delayedLoad = Completer<ResponseBody>();
        if (older) {
          await tester.tap(find.text('Показать ранние'));
          await tester.pump(const Duration(milliseconds: 20));
        } else {
          fake.transport.publish(10, ['chat']);
          await tester.pump(const Duration(milliseconds: 200));
        }
        expect(adapter.delayedLoad, isNull);
        adapter.messages
          ..clear()
          ..add({
            ...oldMessage,
            'id': 'new-pair-message',
            'text': 'Сообщение новой пары',
          });
        adapter.hasMore = false;
        fake.transport.publish(11, ['all']);
        await tester.pump(const Duration(milliseconds: 200));
        await tester.pumpAndSettle();
        expect(find.text('Сообщение прежней пары'), findsNothing);
        expect(find.text('Сообщение новой пары'), findsOneWidget);
        delayed.complete(
          adapter._json({
            'messages': [oldMessage],
            'has_more': true,
            'partner_read_at': null,
          }),
        );
        await tester.pumpAndSettle();
        expect(find.text('Сообщение прежней пары'), findsNothing);
        expect(find.text('Сообщение новой пары'), findsOneWidget);
        await _unmountChat(tester);
      },
    );
  }

  testWidgets(
    'late send response cannot restore messages cleared by full resync',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      addTearDown(fake.controller.dispose);
      await _mountChat(tester, adapter, realtime: fake);
      fake.controller.activate('account');
      await tester.pump();
      fake.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      final delayed = adapter.delayedPost = Completer<ResponseBody>();
      await tester.enterText(find.byType(TextField), 'Отправлено прежней паре');
      await tester.tap(find.byTooltip('Отправить'));
      await tester.pump(const Duration(milliseconds: 20));
      expect(adapter.delayedPost, isNull);
      final response = adapter._json(adapter.messages.single);
      adapter.messages.clear();
      fake.transport.publish(21, ['all']);
      await tester.pump(const Duration(milliseconds: 200));
      delayed.complete(response);
      await tester.pumpAndSettle();
      expect(find.text('Отправлено прежней паре'), findsNothing);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        '',
      );
      await _unmountChat(tester);
    },
  );

  testWidgets(
    'realtime refreshes visible chat immediately but cannot mark covered chat read',
    (tester) async {
      final fake = FakeRealtime();
      final adapter = _ChatAdapter();
      final navigator = GlobalKey<NavigatorState>();
      addTearDown(fake.controller.dispose);
      await _mountChat(tester, adapter, navigator: navigator, realtime: fake);
      fake.controller.activate('account');
      await tester.pump();
      fake.transport.subscribed();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      final loads = adapter.loads;
      adapter.messages.add({
        'id': 'partner-realtime',
        'mine': false,
        'text': 'Новое сообщение',
        'created_at': '2026-10-21T19:00:00Z',
        'attachments': <Object>[],
      });
      fake.transport.publish(1, ['chat']);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pumpAndSettle();
      expect(adapter.loads, greaterThan(loads));
      expect(find.text('Новое сообщение'), findsOneWidget);
      navigator.currentState!.push(
        MaterialPageRoute<void>(
          builder: (_) => const Scaffold(body: Text('План')),
        ),
      );
      await tester.pumpAndSettle();
      final coveredLoads = adapter.loads;
      final coveredReads = adapter.reads;
      fake.transport.publish(2, ['chat']);
      await tester.pump(const Duration(milliseconds: 200));
      expect(adapter.loads, coveredLoads);
      expect(adapter.reads, coveredReads);
      navigator.currentState!.pop();
      await tester.pumpAndSettle();
      expect(adapter.loads, greaterThan(coveredLoads));
      await _unmountChat(tester);
    },
  );
}

Future<void> _mountChat(
  WidgetTester tester,
  _ChatAdapter adapter, {
  GlobalKey<NavigatorState>? navigator,
  FakeRealtime? realtime,
  bool settle = true,
  Map? attach,
  bool usePairApi = true,
}) async {
  final dio = Dio(BaseOptions(baseUrl: 'https://api.example.invalid'))
    ..httpClientAdapter = adapter;
  addTearDown(dio.close);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        dioProvider.overrideWithValue(dio),
        if (!usePairApi) pairProvider.overrideWith((_) async => null),
        if (realtime != null)
          realtimeControllerProvider.overrideWithValue(realtime.controller),
      ],
      child: MaterialApp(
        theme: ThemeData.dark(),
        navigatorKey: navigator,
        navigatorObservers: [chatRouteObserver],
        home: PinGate(child: ChatScreen(attach: attach)),
      ),
    ),
  );
  if (settle) await tester.pumpAndSettle();
}

String _draftText(WidgetTester tester) =>
    tester.widget<TextField>(find.byType(TextField)).controller!.text;

Future<void> _connect(WidgetTester tester, FakeRealtime fake) async {
  fake.controller.activate('account');
  await tester.pump();
  fake.transport.subscribed();
  await tester.pump(const Duration(milliseconds: 200));
  await tester.pumpAndSettle();
}

Future<void> _unmountChat(WidgetTester tester) async {
  hideTopBanner();
  await tester.pumpWidget(const SizedBox.shrink());
  await tester.pump();
}

const _uploadedFile = {
  'id': 'private-upload-one',
  'name': 'Личный файл.txt',
  'mime': 'text/plain',
  'size': 3,
};

List<Map<String, Object>> _pickedFile() => [
  {
    'path': '/unused-test-file.txt',
    'name': 'Личный файл.txt',
    'size': 3,
    'bytes': Uint8List.fromList([1, 2, 3]),
  },
];

void _mockFilePicker(
  WidgetTester tester,
  Future<List<Map<String, Object>>> Function() pick,
) {
  const channel = MethodChannel('miguelruivo.flutter.plugins.filepicker');
  tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
    channel,
    (_) => pick(),
  );
  addTearDown(() {
    tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
      channel,
      null,
    );
  });
}

class _DelayedPinStorage extends TestFlutterSecureStoragePlatform {
  _DelayedPinStorage() : super({'vecher_chat_hint_seen': '1'});
  final pin = Completer<String?>();

  @override
  Future<String?> read({
    required String key,
    required Map<String, String> options,
  }) {
    if (key == 'vecher_pin') return pin.future;
    return super.read(key: key, options: options);
  }
}

class _ChatAdapter implements HttpClientAdapter {
  _ChatAdapter({this.failFirstPost = false, this.commitBeforeFailure = false});
  final bool failFirstPost;
  final bool commitBeforeFailure;
  final List<Map<String, dynamic>> posts = [];
  final List<Map<String, dynamic>> messages = [];
  int reads = 0;
  int loads = 0;
  int pairLoads = 0;
  int uploads = 0;
  final List<String> deletedUploads = [];
  bool rejectNextPairPost = false;
  Map<String, dynamic>? pair = {
    'pair_id': 'pair-one',
    'partner_id': 'partner-one',
    'status': 'active',
    'partner_name': 'Саша',
    'paused': false,
  };
  bool hasMore = false;
  Completer<ResponseBody>? delayedLoad;
  Completer<ResponseBody>? delayedPost;
  Completer<ResponseBody>? delayedPair;
  Completer<ResponseBody>? delayedUpload;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    if (requestStream != null) await requestStream.drain<void>();
    if (options.path == '/pairs/me' && options.method == 'GET') {
      pairLoads++;
      final delayed = delayedPair;
      delayedPair = null;
      return delayed == null ? _json(pair) : delayed.future;
    }
    if (options.path == '/chat' && options.method == 'GET') {
      loads++;
      final delayed = delayedLoad;
      delayedLoad = null;
      if (delayed != null) return delayed.future;
      return _json({
        'messages': messages,
        'has_more': hasMore,
        'partner_read_at': null,
      });
    }
    if (options.path == '/chat/read') {
      reads++;
      return _json({'ok': true});
    }
    if (options.path == '/chat/attachments' && options.method == 'POST') {
      uploads++;
      final delayed = delayedUpload;
      delayedUpload = null;
      return delayed == null ? _json(_uploadedFile) : delayed.future;
    }
    if (options.path.startsWith('/chat/attachments/') &&
        options.method == 'DELETE') {
      deletedUploads.add(options.path);
      return _json({'ok': true});
    }
    if (options.path == '/chat' && options.method == 'POST') {
      final body = Map<String, dynamic>.from(options.data as Map);
      posts.add(body);
      if (rejectNextPairPost) {
        rejectNextPairPost = false;
        return _json({'detail': 'pair_changed'}, statusCode: 409);
      }
      final message = {
        'id': 'message-${body['client_message_id']}',
        'mine': true,
        'created_at': '2026-10-18T19:30:00Z',
        'attachments': <Object>[],
        ...body,
      };
      if (posts.length == 1 && failFirstPost) {
        if (commitBeforeFailure) messages.add(message);
        throw DioException(
          requestOptions: options,
          type: DioExceptionType.receiveTimeout,
        );
      }
      if (!messages.any((m) => m['id'] == message['id'])) messages.add(message);
      final delayed = delayedPost;
      delayedPost = null;
      if (delayed != null) return delayed.future;
      return _json(message);
    }
    if (options.path == '/chat/unread') return _json({'count': 0});
    throw StateError('Unexpected request ${options.method} ${options.path}');
  }

  ResponseBody _json(Object? data, {int statusCode = 200}) =>
      ResponseBody.fromString(
        jsonEncode(data),
        statusCode,
        headers: {
          Headers.contentTypeHeader: [Headers.jsonContentType],
        },
      );

  @override
  void close({bool force = false}) {}
}
