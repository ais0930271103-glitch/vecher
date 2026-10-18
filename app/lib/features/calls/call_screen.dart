import 'dart:async';

import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/privacy.dart';
import '../../core/theme.dart';
import '../../core/top_banner.dart';
import 'call_api.dart';
import 'call_controller.dart';
import 'call_navigation.dart';
import 'webrtc_call_peer.dart';

export 'call_navigation.dart' show openVideoCall;

class CallScreen extends ConsumerStatefulWidget {
  const CallScreen({super.key, this.callId});
  final String? callId;

  @override
  ConsumerState<CallScreen> createState() => _CallScreenState();
}

class _CallScreenState extends ConsumerState<CallScreen>
    with WidgetsBindingObserver {
  late final WebRtcCallPeer _peer;
  late final CallController _call;
  Timer? _clock;
  bool _background = false;
  bool _changingCamera = false;
  bool _alerted = false;

  @override
  void initState() {
    super.initState();
    setCallScreenPresent(this, true);
    hideTopBanner();
    WidgetsBinding.instance.addObserver(this);
    PinGate.lockedListenable.addListener(_applyCameraPrivacy);
    _peer = WebRtcCallPeer();
    _call = CallController(api: DioCallApi(ref.read(dioProvider)), peer: _peer);
    _call.addListener(_callChanged);
    _applyCameraPrivacy();
    _clock = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && _call.connectedAt != null && !_call.finished) {
        setState(() {});
      }
    });
    unawaited(_call.start(callId: widget.callId));
  }

  void _callChanged() {
    if (!_alerted && _call.incoming && _call.phase == CallPhase.ringing) {
      _alerted = true;
      unawaited(HapticFeedback.mediumImpact());
      unawaited(SystemSound.play(SystemSoundType.alert));
    }
  }

  void _applyCameraPrivacy() =>
      _peer.setBackground(_background || PinGate.isLocked);

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused ||
        state == AppLifecycleState.hidden ||
        state == AppLifecycleState.detached) {
      _background = true;
      _applyCameraPrivacy();
    } else if (state == AppLifecycleState.resumed) {
      _background = false;
      _applyCameraPrivacy();
      unawaited(_call.poll());
    }
  }

  @override
  void dispose() {
    _clock?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    PinGate.lockedListenable.removeListener(_applyCameraPrivacy);
    setCallScreenPresent(this, false);
    _call.removeListener(_callChanged);
    _call.dispose();
    final cleanup = _peer.close().whenComplete(_peer.dispose);
    waitForCallCleanup(cleanup);
    unawaited(cleanup);
    super.dispose();
  }

  void _back() {
    if (context.canPop()) {
      context.pop();
    } else {
      context.go('/today');
    }
  }

  Future<void> _switchCamera() async {
    if (_changingCamera) return;
    setState(() => _changingCamera = true);
    try {
      await _peer.switchCamera();
    } catch (_) {
      if (mounted) _toast('Не удалось переключить камеру');
    } finally {
      if (mounted) setState(() => _changingCamera = false);
    }
  }

  void _toast(String text) => showTopBanner(Overlay.of(context), text: text);

  String get _duration {
    final start = _call.connectedAt;
    if (start == null) return '';
    final elapsed = DateTime.now().difference(start);
    final seconds = elapsed.inSeconds.remainder(60).toString().padLeft(2, '0');
    return '${elapsed.inMinutes}:$seconds';
  }

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: Listenable.merge([_call, _peer]),
    builder: (context, _) {
      final incoming = _call.phase == CallPhase.ringing && _call.incoming;
      final name = _call.session?.partnerName ?? 'Партнёр';
      final media = _peer.mediaReady && !_call.finished;
      return Scaffold(
        backgroundColor: VColors.bg,
        appBar: AppBar(
          title: const Text('Видеозвонок'),
          leading: IconButton(
            tooltip: 'Закрыть звонок',
            onPressed: _back,
            icon: const Icon(Icons.arrow_back),
          ),
        ),
        body: SafeArea(
          top: false,
          child: Column(
            children: [
              Expanded(
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(24),
                    child: Stack(
                      fit: StackFit.expand,
                      children: [
                        Container(
                          decoration: const BoxDecoration(
                            gradient: LinearGradient(
                              begin: Alignment.topLeft,
                              end: Alignment.bottomRight,
                              colors: [VColors.card2, VColors.bg],
                            ),
                          ),
                        ),
                        if (media && _peer.remoteVideo)
                          RTCVideoView(
                            _peer.remoteRenderer,
                            objectFit: RTCVideoViewObjectFit
                                .RTCVideoViewObjectFitContain,
                          )
                        else
                          Center(
                            child: Padding(
                              padding: const EdgeInsets.all(28),
                              child: Column(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Container(
                                    padding: const EdgeInsets.all(30),
                                    decoration: BoxDecoration(
                                      color: VColors.gold.withValues(alpha: .1),
                                      shape: BoxShape.circle,
                                    ),
                                    child: Icon(
                                      _call.finished
                                          ? Icons.nightlight_outlined
                                          : Icons.videocam_outlined,
                                      color: VColors.gold,
                                      size: 52,
                                    ),
                                  ),
                                  const SizedBox(height: 24),
                                  Text(
                                    name,
                                    style: display(26),
                                    textAlign: TextAlign.center,
                                  ),
                                  const SizedBox(height: 12),
                                  Text(
                                    _call.statusText,
                                    textAlign: TextAlign.center,
                                    style: TextStyle(
                                      color: _call.error == null
                                          ? VColors.soft
                                          : VColors.coral,
                                      height: 1.5,
                                    ),
                                  ),
                                  if (_call.phase == CallPhase.loading ||
                                      _call.phase == CallPhase.connecting) ...[
                                    const SizedBox(height: 24),
                                    const SizedBox(
                                      width: 24,
                                      height: 24,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                      ),
                                    ),
                                  ],
                                ],
                              ),
                            ),
                          ),
                        if (media)
                          Positioned(
                            top: 12,
                            left: 12,
                            right: 120,
                            child: DecoratedBox(
                              decoration: BoxDecoration(
                                color: Colors.black54,
                                borderRadius: BorderRadius.circular(14),
                              ),
                              child: Padding(
                                padding: const EdgeInsets.symmetric(
                                  horizontal: 12,
                                  vertical: 8,
                                ),
                                child: Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      name,
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                        fontWeight: FontWeight.w600,
                                        color: Colors.white,
                                      ),
                                    ),
                                    const SizedBox(height: 4),
                                    Text(
                                      _call.phase == CallPhase.connected
                                          ? _duration
                                          : _call.statusText,
                                      style: const TextStyle(
                                        fontSize: 12,
                                        color: Colors.white70,
                                      ),
                                    ),
                                  ],
                                ),
                              ),
                            ),
                          ),
                        if (media)
                          Positioned(
                            top: 12,
                            right: 12,
                            width: 96,
                            height: 136,
                            child: ClipRRect(
                              borderRadius: BorderRadius.circular(16),
                              child: ColoredBox(
                                color: VColors.card,
                                child: _peer.videoVisible
                                    ? RTCVideoView(
                                        _peer.localRenderer,
                                        mirror: _peer.frontCamera,
                                        objectFit: RTCVideoViewObjectFit
                                            .RTCVideoViewObjectFitCover,
                                      )
                                    : const Center(
                                        child: Icon(
                                          Icons.videocam_off_outlined,
                                          color: VColors.muted,
                                        ),
                                      ),
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
                ),
              ),
              if (_call.networkNotice != null ||
                  _peer.weakConnection ||
                  _peer.cameraNotice != null)
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 0, 20, 10),
                  child: Text(
                    _peer.cameraNotice ??
                        _call.networkNotice ??
                        'Связь слабая. Можно отключить видео, чтобы лучше слышать друг друга.',
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 12,
                      color: VColors.goldText,
                    ),
                  ),
                ),
              if (_call.finished)
                Padding(
                  padding: const EdgeInsets.all(20),
                  child: FilledButton(
                    onPressed: _back,
                    child: const Text('Вернуться в чат'),
                  ),
                )
              else if (incoming)
                Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 28,
                    vertical: 20,
                  ),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: [
                      _CallButton(
                        icon: Icons.call_end,
                        label: 'Отклонить',
                        color: VColors.coral,
                        onPressed: _call.accepting
                            ? null
                            : () => unawaited(_call.decline()),
                      ),
                      _CallButton(
                        icon: Icons.videocam,
                        label: _call.accepting ? 'Подключаем…' : 'Принять',
                        color: const Color(0xFF93CBB2),
                        onPressed: _call.accepting
                            ? null
                            : () => unawaited(_call.accept()),
                      ),
                    ],
                  ),
                )
              else
                Padding(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 12,
                  ),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      if (media)
                        Row(
                          mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                          children: [
                            _CallButton(
                              icon: _peer.microphoneMuted
                                  ? Icons.mic_off
                                  : Icons.mic,
                              label: _peer.microphoneMuted
                                  ? 'Вкл. микрофон'
                                  : 'Микрофон',
                              selected: _peer.microphoneMuted,
                              onPressed: () => _peer.setMicrophoneMuted(
                                !_peer.microphoneMuted,
                              ),
                            ),
                            _CallButton(
                              icon: _peer.cameraOff
                                  ? Icons.videocam_off
                                  : Icons.videocam,
                              label: _peer.cameraOff ? 'Вкл. камеру' : 'Камера',
                              selected: _peer.cameraOff,
                              onPressed: () =>
                                  _peer.setCameraOff(!_peer.cameraOff),
                            ),
                            if (!kIsWeb)
                              _CallButton(
                                icon: Icons.cameraswitch_outlined,
                                label: 'Повернуть',
                                onPressed: _changingCamera || _peer.cameraOff
                                    ? null
                                    : _switchCamera,
                              ),
                            if (!kIsWeb)
                              _CallButton(
                                icon: _peer.speakerOn
                                    ? Icons.volume_up_outlined
                                    : Icons.hearing_outlined,
                                label: 'Звук',
                                selected: !_peer.speakerOn,
                                onPressed: () async {
                                  try {
                                    await _peer.toggleSpeaker();
                                  } catch (_) {
                                    if (mounted) {
                                      _toast(
                                        'Выбери устройство звука в настройках телефона',
                                      );
                                    }
                                  }
                                },
                              ),
                          ],
                        ),
                      if (media) const SizedBox(height: 12),
                      _CallButton(
                        icon: Icons.call_end,
                        label: 'Завершить',
                        color: VColors.coral,
                        onPressed: () => unawaited(_call.hangUp()),
                      ),
                    ],
                  ),
                ),
              if (!_call.finished && !media)
                const Padding(
                  padding: EdgeInsets.fromLTRB(24, 0, 24, 16),
                  child: Text(
                    'Камера и микрофон включатся после принятия звонка.',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 12, color: VColors.muted),
                  ),
                ),
            ],
          ),
        ),
      );
    },
  );
}

class _CallButton extends StatelessWidget {
  const _CallButton({
    required this.icon,
    required this.label,
    this.onPressed,
    this.color,
    this.selected = false,
  });
  final IconData icon;
  final String label;
  final VoidCallback? onPressed;
  final Color? color;
  final bool selected;

  @override
  Widget build(BuildContext context) => SizedBox(
    width: 76,
    child: Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        IconButton.filled(
          tooltip: label,
          iconSize: 26,
          padding: const EdgeInsets.all(15),
          style: IconButton.styleFrom(
            backgroundColor: color ?? (selected ? VColors.gold : VColors.card2),
            foregroundColor: color != null || selected
                ? VColors.ink
                : VColors.text,
          ),
          onPressed: onPressed,
          icon: Icon(icon),
        ),
        const SizedBox(height: 6),
        Text(
          label,
          textAlign: TextAlign.center,
          maxLines: 2,
          style: const TextStyle(fontSize: 11, color: VColors.soft),
        ),
      ],
    ),
  );
}
