import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';

import '../../core/api.dart';
import '../../core/chat_presence.dart';
import '../../core/network_client.dart';
import '../../core/network_endpoints.dart';
import '../../core/theme.dart';
import '../videos/video_player.dart' show ShortVideoPlayer;
import '../videos/videos.dart'
    show VideoAccess, VideoRequest, videoAccessProvider;
import 'blogs.dart';

/// Credentials may only accompany the fixed media route of the trusted API.
/// Absolute URLs, query tokens and server-provided origins are not used to
/// construct a native player URL. The API media contract serves bytes directly.
Uri? hostedBlogVideoUri(BlogMedia media, ApiEndpoints endpoints) {
  if (!RegExp(r'^[a-zA-Z0-9_-]{1,128}$').hasMatch(media.id) ||
      media.url != '/blogs/media/${media.id}/video' ||
      !media.ready) {
    return null;
  }
  final uri = endpoints.selected.resolve(media.url!);
  return endpoints.trusts(uri) ? uri : null;
}

class NativeHostedVideoPlayer extends ShortVideoPlayer {
  NativeHostedVideoPlayer(Uri uri, Map<String, String> headers)
    : _controller = VideoPlayerController.networkUrl(
        uri,
        httpHeaders: headers,
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

final hostedVideoPlayerFactoryProvider =
    Provider<ShortVideoPlayer Function(Uri, Map<String, String>)>(
      (_) => NativeHostedVideoPlayer.new,
    );

class HostedContentVideo extends ConsumerStatefulWidget {
  const HostedContentVideo({super.key, required this.media});
  final BlogMedia media;
  @override
  ConsumerState<HostedContentVideo> createState() => _HostedContentVideoState();
}

class _HostedContentVideoState extends ConsumerState<HostedContentVideo>
    with RouteAware {
  late final VideoAccess _access;
  ModalRoute<dynamic>? _route;
  ShortVideoPlayer? _player;
  VideoRequest? _request;
  Future<void> _closing = Future<void>.value();
  bool _visible = true, _busy = false, _muted = false, _commandBusy = false;
  String? _error;
  int _generation = 0;

  @override
  void initState() {
    super.initState();
    _access = ref.read(videoAccessProvider)..addListener(_accessChanged);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final route = ModalRoute.of(context);
    if (route != _route) {
      chatRouteObserver.unsubscribe(this);
      _route = route;
      if (route != null) chatRouteObserver.subscribe(this, route);
      _visible = route?.isCurrent ?? true;
    }
  }

  @override
  void didUpdateWidget(HostedContentVideo old) {
    super.didUpdateWidget(old);
    if (old.media.id != widget.media.id || old.media.url != widget.media.url) {
      _release();
    }
  }

  @override
  void didPushNext() {
    _visible = false;
    _release();
  }

  @override
  void didPopNext() {
    _visible = true;
    if (mounted) setState(() {});
  }

  @override
  void didPop() {
    _visible = false;
    _release();
  }

  void _accessChanged() {
    _release();
  }

  bool _valid(int generation) =>
      mounted && _visible && _access.allowed && generation == _generation;

  Future<void> _load() async {
    if (_busy || !_visible || !_access.allowed) return;
    final generation = ++_generation;
    setState(() {
      _busy = true;
      _error = null;
    });
    final request = VideoRequest(_access);
    _request = request;
    try {
      await _closing.timeout(const Duration(seconds: 10));
      if (!_valid(generation) || !request.valid) return;
      final ApiEndpoints endpoints =
          endpointsOf(ref.read(dioProvider)) ?? ref.read(apiEndpointsProvider);
      final uri = hostedBlogVideoUri(widget.media, endpoints);
      if (uri == null) throw const FormatException('Untrusted media path');
      final token = await readToken().timeout(const Duration(seconds: 10));
      if (!_valid(generation) || !request.valid) return;
      if (token == null || token.isEmpty) {
        throw const FormatException('No session');
      }
      final player = ref.read(hostedVideoPlayerFactoryProvider)(uri, {
        'Authorization': 'Bearer $token',
      });
      _player = player;
      player.addListener(_changed);
      await player.initialize().timeout(const Duration(seconds: 20));
      if (!_valid(generation) || !request.valid) return;
      await player.mute(_muted).timeout(const Duration(seconds: 5));
      if (!_valid(generation) || !request.valid) return;
      await player.play().timeout(const Duration(seconds: 5));
      if (_valid(generation)) setState(() => _busy = false);
    } catch (_) {
      if (_valid(generation)) {
        _release();
        setState(
          () => _error = 'Не удалось открыть видео. Проверь подключение и попробуй ещё раз.',
        );
      }
    } finally {
      if (identical(_request, request)) _request = null;
      request.dispose();
    }
  }

  void _changed() {
    if (!mounted || !_access.allowed || !_visible) return;
    if (_player?.failed == true) {
      _release();
      setState(
        () => _error =
            'Воспроизведение прервалось. Попробуй открыть видео снова.',
      );
    } else {
      setState(() {});
    }
  }

  Future<void> _command(Future<void> Function(ShortVideoPlayer) action) async {
    final player = _player;
    final generation = _generation;
    if (player == null || _commandBusy || !_valid(generation)) return;
    setState(() => _commandBusy = true);
    try {
      await action(player).timeout(const Duration(seconds: 8));
    } catch (_) {
      if (_valid(generation)) {
        _release();
        setState(
          () => _error = 'Видео не отвечает. Попробуй открыть его снова.',
        );
      }
    } finally {
      if (_valid(generation)) setState(() => _commandBusy = false);
    }
  }

  void _release({bool notify = true}) {
    ++_generation;
    _request?.cancel.cancel();
    _request = null;
    _busy = _commandBusy = false;
    final player = _player;
    _player = null;
    if (player != null) {
      player.removeListener(_changed);
      unawaited(
        player
            .pause()
            .timeout(const Duration(seconds: 5))
            .catchError((Object _) {}),
      );
      _closing = _closing.catchError((Object _) {}).then((_) => player.close());
      unawaited(_closing.catchError((Object _) {}));
    }
    if (notify && mounted) setState(() {});
  }

  @override
  void dispose() {
    _access.removeListener(_accessChanged);
    chatRouteObserver.unsubscribe(this);
    _release(notify: false);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final player = _player;
    final ready = player?.ready == true;
    final atEnd =
        ready &&
        player!.duration > Duration.zero &&
        player.position >= player.duration;
    final ratio =
        ready && player!.aspectRatio.isFinite && player.aspectRatio > 0
        ? player.aspectRatio.clamp(0.55, 1.8)
        : ((widget.media.width ?? 16) / (widget.media.height ?? 9)).clamp(
            0.55,
            1.8,
          );
    return ClipRRect(
      borderRadius: BorderRadius.circular(18),
      child: ColoredBox(
        color: Colors.black,
        child: Column(
          children: [
            AspectRatio(
              aspectRatio: ratio,
              child: ready
                  ? Center(
                      child: AspectRatio(
                        aspectRatio: player!.aspectRatio > 0
                            ? player.aspectRatio
                            : 1,
                        child: player.buildView(),
                      ),
                    )
                  : Center(
                      child: Padding(
                        padding: const EdgeInsets.all(20),
                        child: _busy
                            ? const CircularProgressIndicator()
                            : Column(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  if (_error != null)
                                    Text(_error!, textAlign: TextAlign.center),
                                  const SizedBox(height: 8),
                                  FilledButton.icon(
                                    onPressed: _access.allowed && _visible
                                        ? _load
                                        : null,
                                    icon: const Icon(Icons.play_arrow),
                                    label: Text(
                                      _error == null
                                          ? 'Смотреть видео'
                                          : 'Повторить видео',
                                    ),
                                  ),
                                ],
                              ),
                      ),
                    ),
            ),
            if (ready) ...[
              if (player!.buffering)
                const LinearProgressIndicator(minHeight: 2),
              Row(
                children: [
                  IconButton(
                    tooltip: player.playing ? 'Пауза' : 'Смотреть',
                    onPressed: _commandBusy
                        ? null
                        : () => _command((p) async {
                            if (p.playing) {
                              await p.pause();
                            } else {
                              if (atEnd) await p.seekToStart();
                              await p.play();
                            }
                          }),
                    icon: Icon(player.playing ? Icons.pause : Icons.play_arrow),
                  ),
                  Expanded(
                    child: LinearProgressIndicator(
                      value: player.duration.inMilliseconds <= 0
                          ? 0
                          : (player.position.inMilliseconds /
                                    player.duration.inMilliseconds)
                                .clamp(0, 1),
                      backgroundColor: Colors.white12,
                      color: VColors.gold,
                      minHeight: 2,
                    ),
                  ),
                  IconButton(
                    tooltip: _muted ? 'Включить звук' : 'Выключить звук',
                    onPressed: _commandBusy
                        ? null
                        : () => _command((p) async {
                            _muted = !_muted;
                            await p.mute(_muted);
                          }),
                    icon: Icon(
                      _muted
                          ? Icons.volume_off_outlined
                          : Icons.volume_up_outlined,
                    ),
                  ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}
