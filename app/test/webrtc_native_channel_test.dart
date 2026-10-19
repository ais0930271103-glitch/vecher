import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/features/calls/call_media_exception.dart';
import 'package:vecher_app/features/calls/webrtc_call_peer.dart';

void main() {
  final binding = TestWidgetsFlutterBinding.ensureInitialized();
  late _NativeCallHarness native;
  late WebRtcCallPeer peer;

  setUp(() {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    native = _NativeCallHarness()..install();
    peer = WebRtcCallPeer();
    addTearDown(() async {
      native.releaseRestart();
      await peer.close();
      peer.dispose();
      // Track.enabled setters dispatch asynchronous platform messages.
      await Future<void>.delayed(Duration.zero);
      debugDefaultTargetPlatformOverride = null;
    });
  });

  test(
    'native channel receives STUN without nullable auth and exact TURN credentials',
    () async {
      // Feed the peer the original API shape directly, so this also protects
      // callers which bypass CallConfiguration.fromMap.
      await peer.initialize(_apiIceServers);

      final configuration =
          native.argumentsFor('createPeerConnection')['configuration'] as Map;
      expect(configuration['iceServers'], [
        {
          'urls': ['stun:stun.example.test:3478'],
        },
        {
          'urls': ['turn:relay.example.test:3478?transport=udp'],
          'username': '1234567890:user',
          'credential': 'SyntheticCaseSensitive+/Auth=',
        },
      ]);
      expect(peer.mediaReady, isTrue);
      expect(
        native.calls
            .where((call) => call.method == 'addTrack')
            .map((call) => (call.arguments as Map)['trackId']),
        ['audio-local', 'video-local'],
      );
      expect(native.foregroundCalls, ['start']);
    },
  );

  test('TURN without credentials fails before touching native media', () async {
    await expectLater(
      peer.initialize([
        {
          'urls': ['turns:relay.example.test:5349'],
          'username': null,
          'credential': null,
        },
      ]),
      throwsA(
        isA<CallMediaException>().having(
          (e) => e.stage,
          'stage',
          'ice_configuration',
        ),
      ),
    );
    expect(native.calls, isEmpty);
    expect(native.foregroundCalls, isEmpty);
  });

  test(
    'restartIce completes before the next native offer and local SDP',
    () async {
      await peer.initialize(_apiIceServers);
      await peer.offer();
      expect(
        native.calls.where((call) => call.method == 'restartIce'),
        isEmpty,
      );

      native.calls.clear();
      native.restartGate = Completer<void>();
      final pending = peer.offer(iceRestart: true);
      await Future<void>.delayed(Duration.zero);
      expect(native.negotiationMethods, ['restartIce']);

      native.releaseRestart();
      final description = await pending;
      expect(native.negotiationMethods, [
        'restartIce',
        'createOffer',
        'setLocalDescription',
      ]);
      expect(description, {'sdp': _localSdp, 'type': 'offer'});
      // These are the actual legacy constraints understood by the pinned native
      // plugin; a top-level browser-style iceRestart flag would be ignored.
      expect(native.argumentsFor('createOffer')['constraints'], {
        'mandatory': {'OfferToReceiveAudio': true, 'OfferToReceiveVideo': true},
        'optional': [],
      });
    },
  );

  test('closing during native ICE restart prevents a late offer', () async {
    await peer.initialize(_apiIceServers);
    native.calls.clear();
    native.restartGate = Completer<void>();
    final pending = peer.offer(iceRestart: true);
    await Future<void>.delayed(Duration.zero);
    expect(native.negotiationMethods, ['restartIce']);

    await peer.close();
    native.releaseRestart();
    await expectLater(pending, throwsA(isA<CallMediaException>()));
    expect(native.negotiationMethods, ['restartIce']);
  });

  for (final failure in [
    (
      method: 'createVideoRenderer',
      stage: 'rtc_initialize',
      afterInitialize: false,
    ),
    (
      method: 'createPeerConnection',
      stage: 'peer_connection',
      afterInitialize: false,
    ),
    (method: 'getUserMedia', stage: 'media_capture', afterInitialize: false),
    (method: 'addTrack', stage: 'local_tracks', afterInitialize: false),
    (method: 'createOffer', stage: 'offer', afterInitialize: true),
    (
      method: 'setLocalDescription',
      stage: 'local_description',
      afterInitialize: true,
    ),
  ]) {
    test(
      'native ${failure.method} failure preserves a safe ${failure.stage} stage',
      () async {
        if (failure.afterInitialize) await peer.initialize(_apiIceServers);
        native.failMethod = failure.method;
        final attempt = failure.afterInitialize
            ? peer.offer()
            : peer.initialize(_apiIceServers);

        await expectLater(
          attempt,
          throwsA(
            isA<CallMediaException>()
                .having((e) => e.stage, 'stage', failure.stage)
                .having(
                  (e) => e.message,
                  'no credentials',
                  isNot(contains(_sensitiveNativeError)),
                )
                .having((e) => e.message, 'no SDP', isNot(contains(_localSdp))),
          ),
        );
        if (failure.stage == 'peer_connection') {
          expect(
            native.calls.where((call) => call.method == 'getUserMedia'),
            isEmpty,
          );
        }
      },
    );
  }
}

const _localSdp = 'v=0\r\na=synthetic-test-description\r\n';
const _sensitiveNativeError = 'synthetic-private-turn-password';
const _apiIceServers = <Map<String, dynamic>>[
  {
    'urls': ['stun:stun.example.test:3478'],
    'username': null,
    'credential': null,
  },
  {
    'urls': ['turn:relay.example.test:3478?transport=udp'],
    'username': '1234567890:user',
    'credential': 'SyntheticCaseSensitive+/Auth=',
  },
];

/// Mocks only the platform boundary. The app uses the pinned flutter_webrtc
/// Dart implementation, including its native payloads and response decoding.
class _NativeCallHarness {
  final calls = <MethodCall>[];
  final foregroundCalls = <String>[];
  String? failMethod;
  Completer<void>? restartGate;
  int _texture = 100;

  Iterable<String> get negotiationMethods => calls
      .map((call) => call.method)
      .where(
        (method) => [
          'restartIce',
          'createOffer',
          'setLocalDescription',
        ].contains(method),
      );

  Map argumentsFor(String method) =>
      calls.singleWhere((call) => call.method == method).arguments as Map;

  void releaseRestart() {
    final gate = restartGate;
    if (gate != null && !gate.isCompleted) gate.complete();
  }

  void install() {
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    for (final name in [
      'FlutterWebRTC.Event',
      'FlutterWebRTC/Texture101',
      'FlutterWebRTC/Texture102',
      'FlutterWebRTC/peerConnectionEventpeer-test',
    ]) {
      final channel = MethodChannel(name);
      messenger.setMockMethodCallHandler(channel, (_) async => null);
      addTearDown(() => messenger.setMockMethodCallHandler(channel, null));
    }
    const media = MethodChannel('FlutterWebRTC.Method');
    messenger.setMockMethodCallHandler(media, _handle);
    addTearDown(() => messenger.setMockMethodCallHandler(media, null));
    const foreground = MethodChannel('vecher/call');
    messenger.setMockMethodCallHandler(foreground, (call) async {
      foregroundCalls.add(call.method);
      return null;
    });
    addTearDown(() => messenger.setMockMethodCallHandler(foreground, null));
  }

  Future<Object?> _handle(MethodCall call) async {
    calls.add(call);
    if (failMethod == call.method) {
      failMethod = null;
      throw PlatformException(
        code: 'native-test-error',
        message: '$_sensitiveNativeError $_localSdp',
      );
    }
    switch (call.method) {
      case 'createVideoRenderer':
        return {'textureId': ++_texture};
      case 'createPeerConnection':
        return {'peerConnectionId': 'peer-test'};
      case 'getUserMedia':
        return {
          'streamId': 'local-stream',
          'audioTracks': [_track('audio')],
          'videoTracks': [_track('video')],
        };
      case 'addTrack':
        final id = (call.arguments as Map)['trackId'];
        return {
          'senderId': 'sender-$id',
          'track': _track(id == 'audio-local' ? 'audio' : 'video'),
          'rtpParameters': {
            'transactionId': 'initial',
            'rtcp': {'cname': 'test', 'reducedSize': true},
            'encodings': [],
            'headerExtensions': [],
            'codecs': [],
          },
          'ownsTrack': false,
        };
      case 'restartIce':
        final gate = restartGate;
        if (gate != null) await gate.future;
        return null;
      case 'createOffer':
        return {'sdp': _localSdp, 'type': 'offer'};
      default:
        return null;
    }
  }

  Map<String, dynamic> _track(String kind) => {
    'id': '$kind-local',
    'label': kind,
    'kind': kind,
    'enabled': true,
    'settings': <String, dynamic>{},
  };
}
