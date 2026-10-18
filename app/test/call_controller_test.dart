import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/features/calls/call_api.dart';
import 'package:vecher_app/features/calls/call_controller.dart';

void main() {
  for (final mine in [true, false]) {
    test(
      '${mine ? 'outgoing' : 'incoming'} unanswered call never opens media',
      () async {
        final api = _FakeCallApi(_session(mine: mine));
        final peer = _FakeCallPeer();
        final controller = _controller(api, peer);
        await controller.start(callId: mine ? null : api.current.id);
        await controller.poll();
        expect(controller.phase, CallPhase.ringing);
        expect(peer.initializeCalls, 0);
        expect(peer.offerCalls, 0);
        expect(peer.isOpen, isFalse);
        expect(api.sent, isEmpty);
      },
    );
  }

  test('only accepting an incoming call initializes its media', () async {
    final api = _FakeCallApi(_session(mine: false));
    final peer = _FakeCallPeer();
    final controller = _controller(api, peer);
    await controller.start(callId: api.current.id);
    expect(peer.initializeCalls, 0);
    await controller.accept();
    expect(api.acceptCalls, 1);
    expect(peer.initializeCalls, 1);
    expect(peer.iceServers, _configuration.iceServers);
    expect(peer.isOpen, isTrue);
    expect(peer.offerCalls, 0);
    expect(controller.phase, CallPhase.connecting);
  });

  test(
    'an offer is sent before ICE emitted synchronously inside offer()',
    () async {
      final api = _FakeCallApi(_session(mine: true, status: 'active'));
      final peer = _FakeCallPeer()..emitIceDuringOffer = true;
      final controller = _controller(api, peer);
      await controller.start();
      await controller.poll();
      expect(api.sent.map((s) => s.kind), ['offer', 'ice']);
      expect(api.sent.first.data['type'], 'offer');
      expect(api.sent.last.data['candidate'], 'local-candidate');
      expect(api.sent.first.clientId, isNot(api.sent.last.clientId));
    },
  );

  for (final mine in [true, false]) {
    test(
      'early remote ICE waits for ${mine ? 'answer' : 'offer'} SDP',
      () async {
        final api = _FakeCallApi(_session(mine: mine, status: 'active'));
        api.received.addAll([
          const CallSignal(
            seq: 1,
            kind: 'ice',
            data: {'candidate': 'remote-candidate'},
          ),
          CallSignal(
            seq: 2,
            kind: mine ? 'answer' : 'offer',
            data: {
              'type': mine ? 'answer' : 'offer',
              'sdp': 'remote-description',
            },
          ),
        ]);
        final peer = _FakeCallPeer();
        final controller = _controller(api, peer);
        await controller.start(callId: mine ? null : api.current.id);
        expect(peer.addedIce, [
          {'candidate': 'remote-candidate'},
        ]);
        final sdpIndex = peer.events.indexOf(mine ? 'setAnswer' : 'answer');
        expect(sdpIndex, greaterThanOrEqualTo(0));
        expect(peer.events.indexOf('addIce'), greaterThan(sdpIndex));
        expect(peer.iceBeforeDescription, isFalse);
        await controller.poll();
        expect(peer.addedIce, hasLength(1));
        expect(api.requestedCursors.last, 2);
      },
    );
  }

  test(
    'ambiguous HTTP signal failures retry one stable client_id and payload',
    () async {
      final api = _FakeCallApi(_session(mine: true, status: 'active'))
        ..failSending = true;
      final peer = _FakeCallPeer();
      final controller = _controller(api, peer);
      await controller.start();
      expect(api.sent, isNotEmpty);
      final first = api.sent.first;
      expect(controller.finished, isFalse);

      api.failSending = false;
      await controller.poll();
      expect(api.sent.length, greaterThan(1));
      for (final attempt in api.sent) {
        expect(attempt.kind, first.kind);
        expect(attempt.clientId, first.clientId);
        expect(attempt.data, first.data);
      }
      final confirmedAttempts = api.sent.length;
      await controller.poll();
      expect(api.sent.length, confirmedAttempts);
    },
  );

  test(
    'remote end closes media and ignores later connection callbacks',
    () async {
      final api = _FakeCallApi(_session(mine: true, status: 'active'));
      final peer = _FakeCallPeer();
      final controller = _controller(api, peer);
      await controller.start();
      peer.onConnection!('connected');
      expect(controller.phase, CallPhase.connected);
      api.current = _session(mine: true, status: 'ended', endReason: 'hangup');
      await controller.poll();
      expect(controller.phase, CallPhase.ended);
      expect(peer.isOpen, isFalse);
      expect(peer.closeCalls, greaterThan(0));
      peer.onConnection!('connected');
      expect(controller.phase, CallPhase.ended);
    },
  );

  test(
    'disposing while configuration is pending does not create a call',
    () async {
      final api = _FakeCallApi(_session(mine: true))
        ..configurationGate = Completer<CallConfiguration>();
      final peer = _FakeCallPeer();
      final controller = _controller(api, peer);
      final starting = controller.start();
      controller.dispose();
      api.configurationGate!.complete(_configuration);
      await starting;
      expect(api.startCalls, 0);
      expect(peer.initializeCalls, 0);
      expect(api.sent, isEmpty);
    },
  );

  test('disposing during call creation ends the late server call', () async {
    final api = _FakeCallApi(_session(mine: true))
      ..startGate = Completer<CallSession>();
    final peer = _FakeCallPeer();
    final controller = _controller(api, peer);
    final starting = controller.start();
    await api.startEntered.future;
    controller.dispose();
    api.startGate!.complete(api.current);
    await starting;
    expect(api.endedIds, [api.current.id]);
    expect(peer.initializeCalls, 0);
    expect(api.sent, isEmpty);
  });

  test('disposing during accept does not initialize late media', () async {
    final api = _FakeCallApi(_session(mine: false))
      ..acceptGate = Completer<CallSession>();
    final peer = _FakeCallPeer();
    final controller = _controller(api, peer);
    await controller.start(callId: api.current.id);
    final accepting = controller.accept();
    await api.acceptEntered.future;
    controller.dispose();
    api.acceptGate!.complete(_session(mine: false, status: 'active'));
    await accepting;
    expect(peer.initializeCalls, 0);
    expect(api.endedIds, contains(api.current.id));
    expect(api.sent, isEmpty);
  });

  test(
    'disposing during media initialization closes late media without offer',
    () async {
      final api = _FakeCallApi(_session(mine: true, status: 'active'));
      final peer = _FakeCallPeer()..initializeGate = Completer<void>();
      final controller = _controller(api, peer);
      final starting = controller.start();
      await peer.initializeEntered.future;
      controller.dispose();
      peer.initializeGate!.complete();
      await starting;
      expect(peer.isOpen, isFalse);
      expect(peer.closeCalls, greaterThan(0));
      expect(peer.offerCalls, 0);
      expect(api.sent, isEmpty);
      expect(peer.onLocalIce, isNull);
      expect(peer.onConnection, isNull);
    },
  );

  test(
    'disposing during offer creation never publishes the late SDP',
    () async {
      final api = _FakeCallApi(_session(mine: true, status: 'active'));
      final peer = _FakeCallPeer()..offerGate = Completer<void>();
      final controller = _controller(api, peer);
      final starting = controller.start();
      await peer.offerEntered.future;
      controller.dispose();
      peer.offerGate!.complete();
      await starting;
      expect(peer.isOpen, isFalse);
      expect(api.sent, isEmpty);
    },
  );

  test(
    'disposing while applying remote SDP does not add ICE to a closed peer',
    () async {
      final api = _FakeCallApi(_session(mine: true, status: 'active'));
      api.received.addAll([
        const CallSignal(
          seq: 1,
          kind: 'ice',
          data: {'candidate': 'remote-candidate'},
        ),
        const CallSignal(
          seq: 2,
          kind: 'answer',
          data: {'type': 'answer', 'sdp': 'remote-sdp'},
        ),
      ]);
      final peer = _FakeCallPeer()..setAnswerGate = Completer<void>();
      final controller = _controller(api, peer);
      final starting = controller.start();
      await peer.setAnswerEntered.future;
      controller.dispose();
      peer.setAnswerGate!.complete();
      await starting;
      expect(peer.isOpen, isFalse);
      expect(peer.addedIce, isEmpty);
    },
  );

  test(
    'transient signaling outages recover without restarting media',
    () async {
      final api = _FakeCallApi(_session(mine: true, status: 'active'));
      final peer = _FakeCallPeer();
      final controller = _controller(api, peer);
      await controller.start();
      peer.onConnection!('connected');
      api.failPolling = true;
      for (var i = 0; i < 3; i++) {
        await controller.poll();
      }
      expect(controller.networkNotice, isNotNull);
      expect(controller.phase, CallPhase.connected);
      expect(peer.initializeCalls, 1);
      expect(peer.isOpen, isTrue);

      api.failPolling = false;
      await controller.poll();
      expect(controller.networkNotice, isNull);
      expect(controller.phase, CallPhase.connected);
      expect(peer.initializeCalls, 1);
    },
  );
  test(
    'a fresh accepted session overrides an expired local ringing deadline',
    () async {
      final api = _FakeCallApi(_session(mine: true));
      final peer = _FakeCallPeer();
      final controller = _controller(api, peer);
      await controller.start();
      // Accepting just before ringing expires extends the server deadline. Fetch
      // the authoritative session before deciding that an unanswered call ended.
      controller.session = CallSession(
        id: api.current.id,
        status: 'ringing',
        mine: true,
        partnerName: 'Партнёр',
        expiresAt: DateTime.now().toUtc().subtract(const Duration(seconds: 1)),
      );
      api.current = _session(mine: true, status: 'active');
      await controller.poll();
      expect(controller.finished, isFalse);
      expect(controller.session!.active, isTrue);
      expect(peer.initializeCalls, 1);
      expect(api.endedIds, isEmpty);
    },
  );

  test(
    'native media initialization failure during polling ends the call',
    () async {
      final api = _FakeCallApi(_session(mine: true));
      final peer = _FakeCallPeer()
        ..initializeError = StateError('Native renderer failed');
      final controller = _controller(api, peer);
      await controller.start();
      api.current = _session(mine: true, status: 'active');
      await controller.poll();
      expect(controller.phase, CallPhase.failed);
      expect(controller.error, isNotEmpty);
      expect(controller.networkNotice, isNull);
      expect(peer.closeCalls, greaterThan(0));
      expect(peer.isOpen, isFalse);
      expect(api.endedIds, contains(api.current.id));
      expect(api.sent, isEmpty);
    },
  );
  test('expired offline ringing stops after repeated failed polls', () async {
    final api = _FakeCallApi(_session(mine: true));
    final peer = _FakeCallPeer();
    final controller = _controller(api, peer);
    await controller.start();
    controller.session = CallSession(
      id: api.current.id,
      status: 'ringing',
      mine: true,
      partnerName: 'Партнёр',
      expiresAt: DateTime.now().toUtc().subtract(const Duration(seconds: 20)),
    );
    api.failPolling = true;
    await controller.poll();
    expect(controller.finished, isFalse);
    await controller.poll();
    await controller.poll();
    expect(controller.finished, isTrue);
    expect(peer.closeCalls, greaterThan(0));
    expect(api.endedIds, contains(api.current.id));
    expect(peer.initializeCalls, 0);
    expect(peer.isOpen, isFalse);
  });
}

CallController _controller(_FakeCallApi api, _FakeCallPeer peer) {
  final controller = CallController(api: api, peer: peer, autoPoll: false);
  addTearDown(controller.dispose);
  return controller;
}

CallSession _session({
  required bool mine,
  String status = 'ringing',
  String? endReason,
}) => CallSession(
  id: 'call-1',
  status: status,
  mine: mine,
  partnerName: 'Партнёр',
  expiresAt: DateTime.now().toUtc().add(const Duration(hours: 1)),
  acceptedAt: status == 'active' ? DateTime.now().toUtc() : null,
  endReason: endReason,
);

const _configuration = CallConfiguration(
  enabled: true,
  relayConfigured: true,
  iceServers: [
    {
      'urls': ['stun:example.invalid:3478'],
    },
  ],
);

class _SentSignal {
  _SentSignal(this.kind, this.data, this.clientId);
  final String kind;
  final Map<String, dynamic> data;
  final String clientId;
}

class _FakeCallApi implements CallApi {
  _FakeCallApi(this.current);
  CallSession current;
  Completer<CallConfiguration>? configurationGate;
  Completer<CallSession>? startGate;
  Completer<CallSession>? acceptGate;
  final startEntered = Completer<void>();
  final acceptEntered = Completer<void>();
  final List<CallSignal> received = [];
  final List<_SentSignal> sent = [];
  final List<int> requestedCursors = [];
  final List<String> endedIds = [];
  bool failSending = false;
  bool failPolling = false;
  int startCalls = 0;
  int acceptCalls = 0;

  @override
  Future<CallConfiguration> configuration() async => configurationGate == null
      ? _configuration
      : await configurationGate!.future;

  @override
  Future<CallSession> start() async {
    startCalls++;
    if (!startEntered.isCompleted) startEntered.complete();
    return startGate == null ? current : await startGate!.future;
  }

  @override
  Future<CallSession> get(String id) async => current;

  @override
  Future<CallSession?> active() async => current.ended ? null : current;

  @override
  Future<CallSession> accept(String id) async {
    acceptCalls++;
    if (!acceptEntered.isCompleted) acceptEntered.complete();
    current = acceptGate == null
        ? _session(mine: current.mine, status: 'active')
        : await acceptGate!.future;
    return current;
  }

  @override
  Future<void> decline(String id) async {}

  @override
  Future<void> end(String id) async => endedIds.add(id);

  @override
  Future<CallSignalBatch> signals(String id, int after) async {
    requestedCursors.add(after);
    if (failPolling) throw _timeout('/calls/$id/signals');
    return CallSignalBatch(
      call: current,
      signals: received.where((s) => s.seq > after).toList(),
      cursor: received.isEmpty ? 0 : received.last.seq,
    );
  }

  @override
  Future<void> sendSignal(
    String id,
    String kind,
    Map<String, dynamic> data,
    String clientId,
  ) async {
    sent.add(_SentSignal(kind, Map<String, dynamic>.from(data), clientId));
    if (failSending) throw _timeout('/calls/$id/signals');
  }

  DioException _timeout(String path) => DioException(
    requestOptions: RequestOptions(path: path),
    type: DioExceptionType.receiveTimeout,
  );
}

class _FakeCallPeer extends CallPeer {
  Object? initializeError;
  Completer<void>? initializeGate;
  Completer<void>? offerGate;
  Completer<void>? setAnswerGate;
  final initializeEntered = Completer<void>();
  final offerEntered = Completer<void>();
  final setAnswerEntered = Completer<void>();
  final List<String> events = [];
  final List<Map<String, dynamic>> addedIce = [];
  List<Map<String, dynamic>>? iceServers;
  bool emitIceDuringOffer = false;
  bool isOpen = false;
  bool descriptionSet = false;
  bool iceBeforeDescription = false;
  int initializeCalls = 0;
  int offerCalls = 0;
  int closeCalls = 0;

  @override
  Future<void> initialize(List<Map<String, dynamic>> servers) async {
    initializeCalls++;
    iceServers = servers;
    events.add('initialize');
    if (!initializeEntered.isCompleted) initializeEntered.complete();
    if (initializeGate != null) await initializeGate!.future;
    if (initializeError != null) throw initializeError!;
    isOpen = true;
  }

  @override
  Future<Map<String, dynamic>> offer({bool iceRestart = false}) async {
    offerCalls++;
    events.add('offer');
    if (!offerEntered.isCompleted) offerEntered.complete();
    if (emitIceDuringOffer) {
      onLocalIce?.call({'candidate': 'local-candidate', 'sdpMLineIndex': 0});
    }
    if (offerGate != null) await offerGate!.future;
    return {'type': 'offer', 'sdp': 'local-offer'};
  }

  @override
  Future<Map<String, dynamic>> answer(Map<String, dynamic> offer) async {
    events.add('answer');
    descriptionSet = true;
    return {'type': 'answer', 'sdp': 'local-answer'};
  }

  @override
  Future<void> setAnswer(Map<String, dynamic> answer) async {
    events.add('setAnswer');
    if (!setAnswerEntered.isCompleted) setAnswerEntered.complete();
    if (setAnswerGate != null) await setAnswerGate!.future;
    descriptionSet = true;
  }

  @override
  Future<void> addIce(Map<String, dynamic> candidate) async {
    events.add('addIce');
    addedIce.add(Map<String, dynamic>.from(candidate));
    if (!descriptionSet) iceBeforeDescription = true;
  }

  @override
  Future<void> close() async {
    closeCalls++;
    events.add('close');
    isOpen = false;
  }
}
