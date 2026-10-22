import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart' show ValueListenable;
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';

import '../../core/theme.dart';
import 'videos.dart';

/// The native boundary is replaceable in widget tests; no test claims to
/// exercise an actual Android decoder or a public network.
abstract class ShortVideoPlayer extends ChangeNotifier {
  bool get ready;
  bool get playing;
  bool get buffering;
  bool get failed;
  Duration get position;
  Duration get duration;
  double get aspectRatio;
  Future<void> initialize();
  Future<void> play();
  Future<void> pause();
  Future<void> mute(bool muted);
  Future<void> seekToStart();
  Widget buildView();
  Future<void> close();
}

class NativeShortVideoPlayer extends ShortVideoPlayer {
  NativeShortVideoPlayer(Uri uri)
    : _controller = VideoPlayerController.networkUrl(
        uri,
        videoPlayerOptions: VideoPlayerOptions(mixWithOthers: false),
      ) {
    _controller.addListener(_changed);
  }
  final VideoPlayerController _controller;
  bool _closed = false;
  Future<void>? _closing;
  void _changed() {
    if (!_closed) notifyListeners();
  }

  @override
  bool get ready => _controller.value.isInitialized;
  @override
  bool get playing => _controller.value.isPlaying;
  @override
  bool get buffering => _controller.value.isBuffering;
  @override
  bool get failed => _controller.value.hasError;
  @override
  Duration get position => _controller.value.position;
  @override
  Duration get duration => _controller.value.duration;
  @override
  double get aspectRatio => _controller.value.aspectRatio;
  @override
  Future<void> initialize() => _controller.initialize();
  @override
  Future<void> play() async {
    if (!_closed) await _controller.play();
  }

  @override
  Future<void> pause() async {
    if (!_closed) await _controller.pause();
  }

  @override
  Future<void> mute(bool muted) async {
    if (!_closed) await _controller.setVolume(muted ? 0 : 1);
  }

  @override
  Future<void> seekToStart() async {
    if (!_closed) await _controller.seekTo(Duration.zero);
  }

  @override
  Widget buildView() => VideoPlayer(_controller);
  @override
  Future<void> close() => _closing ??= _close();
  Future<void> _close() async {
    _closed = true;
    _controller.removeListener(_changed);
    try {
      await _controller.dispose();
    } finally {
      super.dispose();
    }
  }
}

final shortVideoPlayerFactoryProvider =
    Provider<ShortVideoPlayer Function(Uri)>((_) => NativeShortVideoPlayer.new);

/// Only the current visible page owns a native player. A hidden, locked,
/// background or covered route closes it, including initialization in flight.
class ShortVideoSurface extends ConsumerStatefulWidget {
  const ShortVideoSurface({
    super.key,
    required this.video,
    required this.active,
    required this.muted,
    required this.onMute,
    required this.onViewed,
    required this.onSource,
    this.routeVisible,
  });
  final ShortVideo video;
  final bool active, muted;
  final ValueChanged<bool> onMute;
  final VoidCallback onViewed, onSource;
  final ValueListenable<bool>? routeVisible;
  @override
  ConsumerState<ShortVideoSurface> createState() => _ShortVideoSurfaceState();
}

class _ShortVideoSurfaceState extends ConsumerState<ShortVideoSurface> {
  late final VideoAccess _access;
  ShortVideoPlayer? _player;
  int _generation = 0;
  bool _failed = false;
  bool _viewed = false;
  Future<void> _commands = Future<void>.value();

  @override
  void initState() {
    super.initState();
    _access = ref.read(videoAccessProvider);
    _access.addListener(_accessChanged);
    widget.routeVisible?.addListener(_routeChanged);
    if (widget.active) unawaited(_load());
  }

  void _accessChanged() {
    // Paused applications may not produce another frame. Stop audio directly
    // from the lifecycle/PIN/account signal, without waiting for build().
    if (!_access.allowed) _release();
  }

  void _routeChanged() {
    if (widget.routeVisible?.value == false) _release();
  }

  @override
  void didUpdateWidget(ShortVideoSurface old) {
    super.didUpdateWidget(old);
    if (old.routeVisible != widget.routeVisible) {
      old.routeVisible?.removeListener(_routeChanged);
      widget.routeVisible?.addListener(_routeChanged);
    }
    if (old.video.mediaUrl != widget.video.mediaUrl ||
        old.active != widget.active) {
      _release();
      if (old.video.id != widget.video.id) _viewed = false;
      if (widget.active) unawaited(_load());
    } else if (old.muted != widget.muted) {
      _command((p) => p.mute(widget.muted));
    }
  }

  bool _current(ShortVideoPlayer p, int generation) =>
      mounted &&
      _access.allowed &&
      widget.routeVisible?.value != false &&
      widget.active &&
      identical(_player, p) &&
      generation == _generation;
  Future<void> _load() async {
    final uri = safeVideoUri(widget.video.mediaUrl);
    if (uri == null) {
      setState(() => _failed = true);
      return;
    }
    final generation = ++_generation;
    final player = ref.read(shortVideoPlayerFactoryProvider)(uri);
    _player = player;
    _failed = false;
    player.addListener(_changed);
    try {
      await player.initialize().timeout(const Duration(seconds: 20));
      if (!_current(player, generation)) return;
      await player.mute(widget.muted);
      if (!_current(player, generation)) return;
      _command((p) => p.play());
      setState(() {});
    } catch (_) {
      if (!_current(player, generation)) return;
      _release();
      setState(() => _failed = true);
    }
  }

  void _changed() {
    if (!mounted || !widget.active || !_access.allowed) return;
    final p = _player;
    if (p == null) return;
    if (p.failed && !_failed) {
      _release();
      setState(() => _failed = true);
      return;
    }
    if (!_viewed &&
        p.playing &&
        !p.buffering &&
        p.duration > Duration.zero &&
        p.position.inMilliseconds >=
            (p.duration.inMilliseconds < 6000
                ? p.duration.inMilliseconds ~/ 2
                : 3000) &&
        p.position > Duration.zero) {
      _viewed = true;
      widget.onViewed();
    }
    setState(() {});
  }

  void _command(Future<void> Function(ShortVideoPlayer) action) {
    final player = _player;
    final generation = _generation;
    if (player == null) return;
    _commands = _commands
        .then((_) async {
          if (_current(player, generation)) await action(player);
        })
        .catchError((Object _) {
          if (_current(player, generation)) {
            _release();
            setState(() => _failed = true);
          }
        });
  }

  void _release() {
    ++_generation;
    final old = _player;
    _player = null;
    if (old == null) return;
    old.removeListener(_changed);
    // Stop immediately; close remains ordered after an already issued command.
    unawaited(old.pause().catchError((Object _) {}));
    _commands = _commands.then((_) => old.close()).catchError((Object _) {});
  }

  @override
  void dispose() {
    _access.removeListener(_accessChanged);
    widget.routeVisible?.removeListener(_routeChanged);
    _release();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final p = _player;
    if (!widget.active) return VideoPoster(video: widget.video);
    if (_failed) {
      return ColoredBox(
        color: Colors.black,
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(
                  Icons.wifi_off_rounded,
                  size: 34,
                  color: VColors.muted,
                ),
                const SizedBox(height: 12),
                const Text(
                  'Не удалось воспроизвести видео',
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 6),
                const Text(
                  'Проверь соединение или открой источник.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: VColors.muted),
                ),
                TextButton.icon(
                  onPressed: () {
                    setState(() => _failed = false);
                    unawaited(_load());
                  },
                  icon: const Icon(Icons.refresh),
                  label: const Text('Повторить видео'),
                ),
                TextButton(
                  onPressed: widget.onSource,
                  child: const Text('Открыть источник'),
                ),
              ],
            ),
          ),
        ),
      );
    }
    if (p == null || !p.ready) {
      return const ColoredBox(
        color: Colors.black,
        child: Center(child: CircularProgressIndicator()),
      );
    }
    final ratio = p.aspectRatio > 0 && p.aspectRatio.isFinite
        ? p.aspectRatio
        : 1.0;
    return ColoredBox(
      color: Colors.black,
      child: Column(
        children: [
          Expanded(
            child: Center(
              child: AspectRatio(aspectRatio: ratio, child: p.buildView()),
            ),
          ),
          if (p.buffering) const LinearProgressIndicator(minHeight: 2),
          Row(
            children: [
              IconButton(
                tooltip: p.playing ? 'Пауза' : 'Смотреть',
                onPressed: () => _command((p) async {
                  if (p.playing) {
                    await p.pause();
                  } else {
                    if (p.duration > Duration.zero &&
                        p.position >= p.duration) {
                      await p.seekToStart();
                    }
                    await p.play();
                  }
                }),
                icon: Icon(
                  p.playing ? Icons.pause_rounded : Icons.play_arrow_rounded,
                ),
              ),
              Expanded(
                child: LinearProgressIndicator(
                  value: p.duration.inMilliseconds <= 0
                      ? 0
                      : (p.position.inMilliseconds / p.duration.inMilliseconds)
                            .clamp(0, 1),
                  minHeight: 2,
                  backgroundColor: Colors.white12,
                  color: VColors.gold,
                ),
              ),
              const SizedBox(width: 12),
              Text(
                widget.video.durationLabel,
                style: const TextStyle(fontSize: 12, color: VColors.muted),
              ),
              IconButton(
                tooltip: widget.muted ? 'Включить звук' : 'Выключить звук',
                onPressed: () => widget.onMute(!widget.muted),
                icon: Icon(
                  widget.muted
                      ? Icons.volume_off_outlined
                      : Icons.volume_up_outlined,
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class VideoPoster extends StatelessWidget {
  const VideoPoster({
    super.key,
    required this.video,
    this.fit = BoxFit.contain,
  });
  final ShortVideo video;
  final BoxFit fit;
  @override
  Widget build(BuildContext context) {
    final thumb = safeVideoUri(video.thumbnailUrl, thumbnail: true);
    const empty = ColoredBox(
      color: VColors.card2,
      child: Center(
        child: Icon(
          Icons.smart_display_outlined,
          size: 40,
          color: VColors.lavender,
        ),
      ),
    );
    if (thumb == null) return empty;
    return Image.network(
      thumb.toString(),
      fit: fit,
      errorBuilder: (_, __, ___) => empty,
      loadingBuilder: (_, child, progress) => progress == null ? child : empty,
    );
  }
}
