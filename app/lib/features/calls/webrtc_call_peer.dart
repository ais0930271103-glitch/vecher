import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';

import 'call_controller.dart' show CallPeer;
import 'call_media_exception.dart';
import 'ice_configuration.dart';
import 'local_media_cleanup.dart';

const _relayOnly = bool.fromEnvironment('CALL_RELAY_ONLY', defaultValue: false);
const _callChannel = MethodChannel('vecher/call');

class WebRtcCallPeer extends CallPeer with ChangeNotifier {
  final RTCVideoRenderer localRenderer = RTCVideoRenderer();
  final RTCVideoRenderer remoteRenderer = RTCVideoRenderer();
  RTCPeerConnection? _pc;
  MediaStream? _local;
  MediaStream? _remote;
  MediaStream? _cameraCapture;
  RTCRtpSender? _videoSender;
  Future<void> _cameraQueue = Future<void>.value();
  Future<void>? _initializing;
  Future<void>? _closing;
  bool _closed = false;
  bool _localReady = false;
  bool _remoteReady = false;
  bool _background = false;
  bool _statsBusy = false;
  bool _foregroundStarted = false;
  bool microphoneMuted = false;
  bool cameraOff = false;
  bool frontCamera = true;
  bool speakerOn = true;
  bool remoteVideo = false;
  bool weakConnection = false;
  String? cameraNotice;
  Timer? _statsTimer;
  final Map<String, (num, num)> _previousPackets = {};

  bool get mediaReady => !_closed && _local != null && _localReady;
  bool get videoVisible => mediaReady && !cameraOff && !_background;

  void _changed() {
    if (!_closed) notifyListeners();
  }

  @override
  Future<void> initialize(List<Map<String, dynamic>> iceServers) =>
      _initializing ??= _initialize(iceServers);

  void _checkOpen() {
    if (_closed) throw const CallMediaException('Звонок завершён');
  }

  Future<T> _runStage<T>(
    String stage,
    String message,
    Future<T> Function() action,
  ) async {
    try {
      return await action();
    } on CallMediaException {
      rethrow;
    } catch (_) {
      // Native exception text can contain ICE URLs, credentials or SDP. Keep
      // only an explicit, safe stage code and a useful message for the person.
      throw CallMediaException(message, stage: stage);
    }
  }

  Future<void> _initialize(List<Map<String, dynamic>> iceServers) async {
    _checkOpen();
    if (_appInBackground) {
      throw const CallMediaException(
        'Вернись в приложение, чтобы начать звонок.',
      );
    }
    // Also normalize here for callers which construct CallConfiguration
    // directly instead of loading it through the HTTP API.
    final nativeIceServers = normalizeIceServers(iceServers);
    await _runStage(
      'rtc_initialize',
      'Не удалось подготовить звонок на этом телефоне. Попробуй ещё раз.',
      () async {
        await localRenderer.initialize();
        _localReady = true;
        _checkOpen();
        await remoteRenderer.initialize();
        _remoteReady = true;
        _checkOpen();
      },
    );
    _pc = await _runStage(
      'peer_connection',
      'Не удалось подготовить соединение для звонка. Попробуй ещё раз.',
      () => createPeerConnection({
        'iceServers': nativeIceServers,
        'iceTransportPolicy': _relayOnly ? 'relay' : 'all',
        'sdpSemantics': 'unified-plan',
        'bundlePolicy': 'max-bundle',
        'iceCandidatePoolSize': 2,
      }),
    );
    _checkOpen();
    _pc!.onIceCandidate = (candidate) {
      if (!_closed && candidate.candidate?.isNotEmpty == true) {
        onLocalIce?.call(Map<String, dynamic>.from(candidate.toMap()));
      }
    };
    _pc!.onConnectionState = (state) {
      if (_closed) return;
      switch (state) {
        case RTCPeerConnectionState.RTCPeerConnectionStateConnected:
          onConnection?.call('connected');
          unawaited(_configureSenders());
        case RTCPeerConnectionState.RTCPeerConnectionStateDisconnected:
          onConnection?.call('disconnected');
        case RTCPeerConnectionState.RTCPeerConnectionStateFailed:
          onConnection?.call('failed');
        default:
          break;
      }
    };
    _pc!.onTrack = (event) {
      if (_closed || event.streams.isEmpty) return;
      _remote = event.streams.first;
      remoteRenderer.srcObject = _remote;
      if (event.track.kind == 'video') {
        remoteVideo = true;
        event.track.onMute = () {
          remoteVideo = false;
          _changed();
        };
        event.track.onUnMute = () {
          remoteVideo = true;
          _changed();
        };
      }
      _changed();
    };

    try {
      _local = await navigator.mediaDevices.getUserMedia({
        'audio': {
          'echoCancellation': true,
          'noiseSuppression': true,
          'autoGainControl': true,
        },
        'video': {
          'facingMode': 'user',
          'width': 1280,
          'height': 720,
          'frameRate': 30,
        },
      });
    } catch (_) {
      throw const CallMediaException(
        'Не удалось включить камеру и микрофон. Разреши доступ к ним в настройках телефона.',
        stage: 'media_capture',
      );
    }
    _checkOpen();
    // Permission is already granted and the call was explicitly accepted.
    if (!kIsWeb && defaultTargetPlatform == TargetPlatform.android) {
      if (_appInBackground) {
        throw const CallMediaException(
          'Вернись в приложение, чтобы начать звонок.',
        );
      }
      try {
        await _callChannel.invokeMethod<void>('start');
        _foregroundStarted = true;
      } catch (_) {
        throw const CallMediaException(
          'Не удалось запустить звонок на этом телефоне. Открой приложение и попробуй снова.',
          stage: 'foreground_service',
        );
      }
    }
    _checkOpen();
    await _runStage(
      'local_tracks',
      'Не удалось подключить камеру и микрофон к звонку. Попробуй ещё раз.',
      () async {
        await localRenderer.setSrcObject(stream: _local);
        _checkOpen();
        for (final track in _local!.getTracks()) {
          final sender = await _pc!.addTrack(track, _local!);
          if (track.kind == 'video') _videoSender = sender;
          _checkOpen();
        }
      },
    );
    setBackground(_background);
    if (!kIsWeb) {
      try {
        await Helper.setSpeakerphoneOnButPreferBluetooth();
      } catch (_) {
        await _quiet(() => Helper.setSpeakerphoneOn(true));
      }
    }
    _checkOpen();
    _statsTimer = Timer.periodic(
      const Duration(seconds: 5),
      (_) => unawaited(_readStats()),
    );
    _changed();
  }

  Future<void> _configureSenders() async {
    if (_closed || _pc == null) return;
    try {
      for (final sender in await _pc!.getSenders()) {
        if (_closed) return;
        final parameters = sender.parameters;
        if (sender.track?.kind == 'video') {
          parameters.degradationPreference = RTCDegradationPreference.BALANCED;
          for (final encoding in parameters.encodings ?? <RTCRtpEncoding>[]) {
            encoding.maxBitrate = 2500000;
            encoding.maxFramerate = 30;
          }
        } else if (sender.track?.kind == 'audio') {
          for (final encoding in parameters.encodings ?? <RTCRtpEncoding>[]) {
            encoding.maxBitrate = 64000;
            encoding.priority = RTCPriorityType.high;
          }
        }
        await sender.setParameters(parameters);
      }
    } catch (_) {
      // Codec/platform defaults still provide congestion control if an option is unsupported.
    }
  }

  @override
  Future<Map<String, dynamic>> offer({bool iceRestart = false}) async {
    _checkOpen();
    if (iceRestart) {
      // Android consumes legacy mandatory/optional SDP constraints and ignores
      // a browser-style top-level iceRestart flag. Use the native method.
      await _runStage(
        'ice_restart',
        'Не удалось восстановить соединение. Позвони ещё раз.',
        () => _pc!.restartIce(),
      );
      _checkOpen();
    }
    final description = await _runStage(
      'offer',
      'Не удалось начать соединение для звонка. Попробуй ещё раз.',
      () => _pc!.createOffer(),
    );
    _checkOpen();
    await _runStage(
      'local_description',
      'Не удалось подготовить соединение для звонка. Попробуй ещё раз.',
      () => _pc!.setLocalDescription(description),
    );
    _checkOpen();
    return {'sdp': description.sdp, 'type': description.type};
  }

  @override
  Future<Map<String, dynamic>> answer(Map<String, dynamic> offer) async {
    _checkOpen();
    await _pc!.setRemoteDescription(
      RTCSessionDescription(offer['sdp'] as String, 'offer'),
    );
    final description = await _pc!.createAnswer();
    await _pc!.setLocalDescription(description);
    await _configureSenders();
    return {'sdp': description.sdp, 'type': description.type};
  }

  @override
  Future<void> setAnswer(Map<String, dynamic> answer) async {
    _checkOpen();
    await _pc!.setRemoteDescription(
      RTCSessionDescription(answer['sdp'] as String, 'answer'),
    );
    await _configureSenders();
  }

  @override
  Future<void> addIce(Map<String, dynamic> candidate) async {
    _checkOpen();
    await _pc!.addCandidate(
      RTCIceCandidate(
        candidate['candidate'] as String?,
        candidate['sdpMid'] as String?,
        (candidate['sdpMLineIndex'] as num?)?.toInt(),
      ),
    );
  }

  void setMicrophoneMuted(bool muted) {
    microphoneMuted = muted;
    for (final track in _local?.getAudioTracks() ?? <MediaStreamTrack>[]) {
      track.enabled = !muted && !_closed;
    }
    _changed();
  }

  void setCameraOff(bool off) {
    cameraOff = off;
    cameraNotice = null;
    setBackground(_background);
  }

  void setBackground(bool background) {
    _background = background;
    for (final track in _local?.getVideoTracks() ?? <MediaStreamTrack>[]) {
      track.enabled = !cameraOff && !_background && !_closed;
    }
    _cameraQueue = _cameraQueue.then((_) => _syncCameraCapture()).catchError((
      Object _,
    ) {
      if (!_closed) {
        cameraOff = true;
        cameraNotice =
            'Не удалось включить камеру. Разговор можно продолжить голосом.';
        _changed();
      }
    });
    _changed();
  }

  /// Stop the capturer, not just its outgoing frames, when video is switched off.
  /// Reuse the negotiated sender so resuming video does not create another call.
  Future<void> _syncCameraCapture() async {
    final stream = _local;
    final sender = _videoSender;
    if (stream == null || sender == null) return;
    final shouldCapture =
        !_closed && !cameraOff && !_background && !_appInBackground;
    final oldTrack = stream.getVideoTracks().firstOrNull;
    if (!shouldCapture && oldTrack != null) {
      oldTrack.enabled = false;
      await _quiet(() => sender.replaceTrack(null));
      await _quiet(() => stream.removeTrack(oldTrack));
      final capture = _cameraCapture;
      _cameraCapture = null;
      if (capture != null) {
        await disposeLocalMediaStreams([capture]);
      } else {
        await _quiet(oldTrack.stop);
      }
      if (!_closed) localRenderer.srcObject = stream;
      _changed();
    } else if (shouldCapture && oldTrack == null) {
      final capture = await navigator.mediaDevices.getUserMedia({
        'audio': false,
        'video': {
          'facingMode': frontCamera ? 'user' : 'environment',
          'width': 1280,
          'height': 720,
          'frameRate': 30,
        },
      });
      final track = capture.getVideoTracks().first;
      track.enabled = false;
      if (_closed || cameraOff || _background || _appInBackground) {
        await disposeLocalMediaStreams([capture]);
        return;
      }
      try {
        await stream.addTrack(track);
        if (_closed || cameraOff || _background || _appInBackground) {
          await _quiet(() => stream.removeTrack(track));
          await disposeLocalMediaStreams([capture]);
          return;
        }
        await sender.replaceTrack(track);
        _cameraCapture = capture;
        track.enabled =
            !_closed && !cameraOff && !_background && !_appInBackground;
        if (_closed) return;
        localRenderer.srcObject = stream;
        cameraNotice = null;
        await _configureSenders();
        _changed();
      } catch (_) {
        await _quiet(() => stream.removeTrack(track));
        await disposeLocalMediaStreams([capture]);
        rethrow;
      }
    }
  }

  Future<void> switchCamera() async {
    await _cameraQueue;
    final track = _local?.getVideoTracks().firstOrNull;
    if (_closed || cameraOff || _background || track == null) return;
    await Helper.switchCamera(track);
    frontCamera = !frontCamera;
    _changed();
  }

  Future<void> toggleSpeaker() async {
    if (_closed || kIsWeb) return;
    await Helper.setSpeakerphoneOn(!speakerOn);
    speakerOn = !speakerOn;
    _changed();
  }

  Future<void> _readStats() async {
    if (_closed || _pc == null || _statsBusy) return;
    _statsBusy = true;
    try {
      final reports = await _pc!.getStats();
      if (_closed) return;
      final byId = {for (final report in reports) report.id: report};
      bool weak = false;
      for (final report in reports) {
        final values = report.values;
        if (report.type == 'inbound-rtp') {
          final lost = (values['packetsLost'] as num?) ?? 0;
          final received = (values['packetsReceived'] as num?) ?? 0;
          final previous = _previousPackets[report.id];
          if (previous != null) {
            final lostDelta = lost - previous.$1;
            final total = lostDelta + received - previous.$2;
            if (total > 20 && lostDelta / total > .08) weak = true;
          }
          _previousPackets[report.id] = (lost, received);
        }
        if (report.type == 'transport') {
          final pair = byId[values['selectedCandidatePairId']];
          if (pair != null) {
            if (((pair.values['currentRoundTripTime'] as num?) ?? 0) > .4) {
              weak = true;
            }
            if (kDebugMode || _relayOnly) {
              final candidate = byId[pair.values['localCandidateId']];
              // Diagnostic route type only: never log IPs, SDP, JWT or TURN passwords.
              debugPrint(
                'Vecher call route: ${candidate?.values['candidateType'] ?? 'unknown'}',
              );
            }
          }
        }
      }
      if (weakConnection != weak) {
        weakConnection = weak;
        _changed();
      }
    } catch (_) {
      // Stats are advisory; failure must not interrupt a working call.
    } finally {
      _statsBusy = false;
    }
  }

  Future<void> _quiet(Future<void> Function() action) async {
    try {
      await action();
    } catch (_) {}
  }

  @override
  Future<void> close() {
    if (_closing != null) return _closing!;
    _closed = true;
    _statsTimer?.cancel();
    for (final track in _local?.getTracks() ?? <MediaStreamTrack>[]) {
      track.enabled = false;
    }
    return _closing = _closeResources();
  }

  Future<void> _closeResources() async {
    if (_initializing != null) await _quiet(() => _initializing!);
    await _cameraQueue;
    final pc = _pc;
    if (pc != null) {
      pc.onConnectionState = null;
      pc.onIceCandidate = null;
      pc.onTrack = null;
      await _quiet(pc.close);
      await _quiet(pc.dispose);
    }
    if (_localReady) {
      try {
        localRenderer.srcObject = null;
      } catch (_) {}
    }
    if (_remoteReady) {
      try {
        remoteRenderer.srcObject = null;
      } catch (_) {}
    }
    await disposeLocalMediaStreams([
      if (_local != null) _local!,
      if (_cameraCapture != null) _cameraCapture!,
    ]);
    if (_remote != null) await _quiet(_remote!.dispose);
    if (_localReady) await _quiet(localRenderer.dispose);
    if (_remoteReady) await _quiet(remoteRenderer.dispose);
    if (_foregroundStarted) {
      await _quiet(() => _callChannel.invokeMethod<void>('stop'));
    }
    if (_local != null &&
        !kIsWeb &&
        defaultTargetPlatform == TargetPlatform.android) {
      await _quiet(Helper.clearAndroidCommunicationDevice);
    }
    _local = null;
    _remote = null;
    _pc = null;
    _cameraCapture = null;
    _videoSender = null;
  }
}

bool get _appInBackground => const [
  AppLifecycleState.paused,
  AppLifecycleState.hidden,
  AppLifecycleState.detached,
].contains(WidgetsBinding.instance.lifecycleState);
