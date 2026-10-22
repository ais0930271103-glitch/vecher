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

enum _PlaybackFailure {
  source('source', 'unapproved_source'),
  initializationTimeout('initialization', 'timeout'),
  initialization('initialization', 'player_error'),
  stalled('playback', 'no_progress'),
  commandTimeout('playback', 'command_timeout'),
  cleanup('cleanup', 'not_confirmed'),
  playback('playback', 'player_error');

  const _PlaybackFailure(this.stage, this.code);
  final String stage, code;
}

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
    this.suspended = false,
    this.routeVisible,
  });
  final ShortVideo video;
  final bool active, muted;
  // A scroll gesture pauses the visible clip without discarding its buffer.
  // Leaving the page/route/session still releases the native player.
  final bool suspended;
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
  _PlaybackFailure? _failure;
  bool _showDiagnostics = false;
  bool _viewed = false;
  bool _userPaused = false;
  bool _playRequested = false;
  bool _prepared = false;
  Timer? _progressTimer;
  Duration? _lastProgress;
  bool _restoreScheduled = false;
  bool _cleanupFailed = false;
  final _unclosedPlayers = <ShortVideoPlayer>{};
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
    if (widget.routeVisible?.value == false) {
      _release();
    } else if (!_restoreScheduled) {
      // A route can cover and uncover this page before its next build. In that
      // case active never changes, so didUpdateWidget cannot restore the player.
      _restoreScheduled = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _restoreScheduled = false;
        if (mounted &&
            _access.allowed &&
            widget.active &&
            widget.routeVisible?.value != false &&
            _player == null &&
            _failure == null) {
          unawaited(_load());
        }
      });
      WidgetsBinding.instance.scheduleFrame();
    }
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
      if (old.video.id != widget.video.id) {
        _viewed = false;
        _userPaused = false;
      }
      if (widget.active) unawaited(_load());
    } else {
      if (old.suspended != widget.suspended) _syncPlayback();
      if (old.muted != widget.muted && _prepared) {
        _command((p) => p.mute(widget.muted));
      }
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
    if (_cleanupFailed && _unclosedPlayers.isNotEmpty) {
      setState(() => _failure = _PlaybackFailure.cleanup);
      return;
    }
    final uri = safeVideoUri(widget.video.mediaUrl);
    if (uri == null) {
      setState(() => _failure = _PlaybackFailure.source);
      return;
    }
    final generation = ++_generation;
    final player = ref.read(shortVideoPlayerFactoryProvider)(uri);
    _player = player;
    _failure = null;
    _showDiagnostics = false;
    _playRequested = false;
    _prepared = false;
    _stopWatchdog();
    player.addListener(_changed);
    try {
      await (() async {
        await player.initialize();
        while (_current(player, generation)) {
          final muted = widget.muted;
          await player.mute(muted);
          if (!_current(player, generation)) return;
          // The parent may change sound while the native mute call is pending.
          // Keep the whole synchronization inside the initialization deadline.
          if (widget.muted == muted) return;
        }
      })().timeout(const Duration(seconds: 20));
      if (!_current(player, generation)) return;
      _prepared = true;
      _syncPlayback();
      setState(() {});
    } on TimeoutException {
      if (_current(player, generation)) {
        _fail(_PlaybackFailure.initializationTimeout);
      }
    } catch (_) {
      if (!_current(player, generation)) return;
      _fail(_PlaybackFailure.initialization);
    }
  }

  void _changed() {
    if (!mounted || !widget.active || !_access.allowed) return;
    final p = _player;
    if (p == null) return;
    if (p.failed && _failure == null) {
      _fail(
        p.ready ? _PlaybackFailure.playback : _PlaybackFailure.initialization,
      );
      return;
    }
    _watchProgress(p);
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

  bool _atEnd(ShortVideoPlayer p) =>
      p.duration > Duration.zero && p.position >= p.duration;

  bool get _wantsPlayback => !_userPaused && !widget.suspended;

  void _syncPlayback({bool restart = false}) {
    final p = _player;
    if (p == null || !p.ready || !_prepared) return;
    if (!_wantsPlayback) {
      _playRequested = false;
      _stopWatchdog();
    }
    final generation = _generation;
    _command((player) async {
      if (restart && _wantsPlayback && _atEnd(player)) {
        _stopWatchdog();
        await player.seekToStart();
        if (!_current(player, generation)) return;
      }
      if (_wantsPlayback && !_atEnd(player)) {
        _playRequested = true;
        _watchProgress(player);
        await player.play();
      } else {
        _playRequested = false;
        _stopWatchdog();
        await player.pause();
      }
    });
  }

  void _stopWatchdog() {
    _progressTimer?.cancel();
    _progressTimer = null;
    _lastProgress = null;
  }

  void _watchProgress(ShortVideoPlayer p) {
    if (!_playRequested || !_wantsPlayback || !p.ready || _atEnd(p)) {
      _stopWatchdog();
      return;
    }
    if (_progressTimer != null &&
        _lastProgress != null &&
        p.position <= _lastProgress!) {
      return;
    }
    _progressTimer?.cancel();
    _lastProgress = p.position;
    final generation = _generation;
    _progressTimer = Timer(const Duration(seconds: 20), () {
      if (!_current(p, generation)) return;
      if (!_playRequested || !_wantsPlayback) {
        _stopWatchdog();
        return;
      }
      if (_atEnd(p) || p.position > _lastProgress!) {
        _progressTimer = null;
        _watchProgress(p);
        return;
      }
      _fail(_PlaybackFailure.stalled);
    });
  }

  void _fail(_PlaybackFailure failure) {
    _release();
    setState(() {
      _failure = failure;
      _showDiagnostics = false;
    });
  }

  void _command(Future<void> Function(ShortVideoPlayer) action) {
    final player = _player;
    final generation = _generation;
    if (player == null) return;
    _commands = _commands
        .then((_) async {
          if (!_current(player, generation)) return;
          if (_unclosedPlayers.isNotEmpty) {
            _fail(_PlaybackFailure.cleanup);
            return;
          }
          await action(player).timeout(const Duration(seconds: 10));
        })
        .catchError((Object error) {
          if (_current(player, generation)) {
            _fail(
              error is TimeoutException
                  ? _PlaybackFailure.commandTimeout
                  : _PlaybackFailure.playback,
            );
          }
        });
  }

  void _release() {
    ++_generation;
    _playRequested = false;
    _prepared = false;
    _stopWatchdog();
    final old = _player;
    _player = null;
    if (old == null) return;
    old.removeListener(_changed);
    _unclosedPlayers.add(old);
    // Stop immediately; close remains ordered after an already issued command.
    unawaited(
      old
          .pause()
          .timeout(const Duration(seconds: 10))
          .catchError((Object _) {}),
    );
    _commands = _commands
        .then((_) async {
          final closing = old.close();
          // A late cleanup acknowledgement may enable a later explicit retry, but
          // it never starts playback. No new player may play before this succeeds.
          unawaited(
            closing
                .then((_) {
                  _unclosedPlayers.remove(old);
                  if (_unclosedPlayers.isEmpty) _cleanupFailed = false;
                })
                .catchError((Object _) {}),
          );
          await closing.timeout(const Duration(seconds: 10));
        })
        .catchError((Object _) {
          _cleanupFailed = true;
        });
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
    final failure = _failure;
    if (failure != null) {
      return ColoredBox(
        color: Colors.black,
        child: Center(
          child: SingleChildScrollView(
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(
                    Icons.error_outline_rounded,
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
                    'Попробуй ещё раз или открой другой ролик.',
                    textAlign: TextAlign.center,
                    style: TextStyle(color: VColors.muted),
                  ),
                  TextButton.icon(
                    onPressed: () {
                      setState(() => _failure = null);
                      unawaited(_load());
                    },
                    icon: const Icon(Icons.refresh),
                    label: const Text('Повторить видео'),
                  ),
                  TextButton(
                    onPressed: widget.onSource,
                    child: const Text('Открыть источник'),
                  ),
                  TextButton(
                    onPressed: () =>
                        setState(() => _showDiagnostics = !_showDiagnostics),
                    child: Text(
                      _showDiagnostics
                          ? 'Скрыть сведения'
                          : 'Сведения об ошибке',
                    ),
                  ),
                  if (_showDiagnostics)
                    SelectableText(
                      'Ролик: ${RegExp(r'^[a-zA-Z0-9_-]{1,64}$').hasMatch(widget.video.id) ? widget.video.id : 'unknown'}\n'
                      'Этап: ${failure.stage}\nКод: ${failure.code}',
                      style: const TextStyle(
                        fontSize: 12,
                        color: VColors.muted,
                      ),
                    ),
                ],
              ),
            ),
          ),
        ),
      );
    }
    if (p == null || !p.ready || !_prepared) {
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
          if (p.buffering && _wantsPlayback)
            const LinearProgressIndicator(minHeight: 2),
          Row(
            children: [
              IconButton(
                tooltip: _userPaused || _atEnd(p) ? 'Смотреть' : 'Пауза',
                onPressed: () {
                  setState(() => _userPaused = !(_userPaused || _atEnd(p)));
                  _syncPlayback(restart: true);
                },
                icon: Icon(
                  _userPaused || _atEnd(p)
                      ? Icons.play_arrow_rounded
                      : Icons.pause_rounded,
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
