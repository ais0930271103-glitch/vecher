import 'dart:async';

import 'package:flutter/foundation.dart' show ValueListenable;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:webview_flutter/webview_flutter.dart';
import 'package:webview_flutter_android/webview_flutter_android.dart';

import '../../core/theme.dart';
import 'rutube_embed.dart';
import 'videos.dart';

/// Replaceable native boundary. Widget tests verify our lifecycle and error
/// handling, not the remote player, advertisements or Android media codecs.
abstract class RutubePlayer extends ChangeNotifier {
  bool get ready;
  bool get failed;
  bool get playing;
  Duration get position;
  Widget? get fullscreen;
  void exitFullscreen();
  Future<void> initialize();
  Future<void> pause();
  Widget buildView();
  Future<void> close();
}

class NativeRutubePlayer extends RutubePlayer {
  NativeRutubePlayer(this.providerId);
  final String providerId;
  WebViewController? _controller;
  bool _closed = false;
  bool _ready = false;
  bool _failed = false;
  bool _playing = false;
  Duration _position = Duration.zero;
  Widget? _fullscreen;
  VoidCallback? _exitFullscreen;
  Future<void>? _initializing;
  Future<void>? _closing;

  @override
  bool get ready => _ready;
  @override
  bool get failed => _failed;
  @override
  bool get playing => _playing;
  @override
  Duration get position => _position;
  @override
  Widget? get fullscreen => _fullscreen;
  @override
  void exitFullscreen() {
    final callback = _exitFullscreen;
    _exitFullscreen = null;
    _fullscreen = null;
    callback?.call();
  }

  void _error() {
    if (_closed) return;
    _failed = true;
    notifyListeners();
  }

  void _message(JavaScriptMessage message) {
    if (_closed) return;
    final event = RutubeEvent.parse(message.message);
    if (event == null) return;
    switch (event.kind) {
      case 'ready':
        _ready = true;
      case 'error':
        _failed = true;
      case 'state':
        _playing = event.state == 'playing';
      case 'time':
        _position = Duration(milliseconds: (event.seconds! * 1000).round());
    }
    notifyListeners();
  }

  @override
  Future<void> initialize() => _initializing ??= _initialize();

  Future<void> _initialize() async {
    final html = rutubeEmbedHtml(providerId);
    final controller = WebViewController(
      onPermissionRequest: (request) => unawaited(request.deny()),
    );
    _controller = controller;
    await controller.setJavaScriptMode(JavaScriptMode.unrestricted);
    if (_closed) return;
    await controller.setBackgroundColor(Colors.black);
    if (_closed) return;
    await controller.addJavaScriptChannel(
      'VecherVideo',
      onMessageReceived: _message,
    );
    if (_closed) return;
    await controller.setNavigationDelegate(
      NavigationDelegate(
        onNavigationRequest: (request) =>
            allowedRutubeNavigation(request.url, mainFrame: request.isMainFrame)
            ? NavigationDecision.navigate
            : NavigationDecision.prevent,
        onWebResourceError: (error) {
          // An unavailable ad/thumbnail is not proof that the video is broken.
          if (error.isForMainFrame == true) _error();
        },
        onSslAuthError: (error) {
          unawaited(error.cancel());
          // This callback also covers secondary analytics/advertising resources.
          // Always reject invalid TLS, but let the provider error/ready deadline
          // decide whether the actual player is unavailable.
        },
      ),
    );
    if (_closed) return;
    final platform = controller.platform;
    if (platform is AndroidWebViewController) {
      await platform.setMediaPlaybackRequiresUserGesture(true);
      if (_closed) return;
      await platform.setMixedContentMode(MixedContentMode.neverAllow);
      if (_closed) return;
      await platform.setAllowFileAccess(false);
      if (_closed) return;
      await platform.setAllowContentAccess(false);
      if (_closed) return;
      await platform.setOnShowFileSelector((_) async => <String>[]);
      if (_closed) return;
      await platform.setCustomWidgetCallbacks(
        onShowCustomWidget: (widget, onHidden) {
          if (_closed) {
            onHidden();
            return;
          }
          exitFullscreen();
          _fullscreen = widget;
          _exitFullscreen = onHidden;
          notifyListeners();
        },
        onHideCustomWidget: () {
          _fullscreen = null;
          _exitFullscreen = null;
          if (!_closed) notifyListeners();
        },
      );
    }
    if (_closed) return;
    notifyListeners();
    try {
      await controller.loadHtmlString(html, baseUrl: rutubeWrapperOrigin);
    } finally {
      // A late native load completion must not revive a hidden player.
      if (_closed) await _blank(controller);
    }
  }

  @override
  Future<void> pause() async {
    if (!_closed && _ready) {
      await _controller?.runJavaScript('window.vecherPause?.();');
    }
  }

  @override
  Widget buildView() => _controller == null
      ? const SizedBox.shrink()
      : WebViewWidget(controller: _controller!);

  Future<void> _blank(WebViewController controller) async {
    // Removing the iframe stops its media even if a pause event is ignored.
    try {
      await controller
          .runJavaScript('window.vecherClose?.(); window.stop();')
          .timeout(const Duration(seconds: 2));
    } catch (_) {
      // Before document initialization the bridge may not exist.
    }
    await controller
        .loadHtmlString('<html><body></body></html>')
        .timeout(const Duration(seconds: 3));
  }

  @override
  Future<void> close() => _closing ??= _close();

  Future<void> _close() async {
    _closed = true;
    exitFullscreen();
    final controller = _controller;
    try {
      if (controller != null) {
        await _blank(controller);
        // Wait for an in-flight configure/load before the final blank page.
        // Closing is bounded by the shared gate below; uncertain cleanup never
        // permits a second player to start.
        try {
          await _initializing;
        } catch (_) {
          // A previous initialization error does not invalidate a successful
          // teardown. A still-pending initialization remains bounded by gate.
        }
        await _blank(controller);
      }
    } finally {
      super.dispose();
    }
  }
}

final rutubePlayerFactoryProvider = Provider<RutubePlayer Function(String)>(
  (_) => NativeRutubePlayer.new,
);

/// Shared by adjacent pages and reopened routes. Disposal must finish before
/// another WebView is created, including when the previous widget is gone.
class _RutubeCleanup {
  Future<void> settled = Future<void>.value();
  bool failed = false;
  Object? _owner;
  VoidCallback? _revoke;

  void reserve(Object owner, VoidCallback revoke) {
    if (identical(owner, _owner)) return;
    final previous = _revoke;
    _owner = null;
    _revoke = null;
    previous?.call();
    _owner = owner;
    _revoke = revoke;
  }

  bool owns(Object owner) => identical(_owner, owner);

  void release(Object owner, RutubePlayer? player) {
    if (owns(owner)) {
      _owner = null;
      _revoke = null;
    }
    if (player == null) return;
    final closing = (() async {
      try {
        await player.close().timeout(const Duration(seconds: 9));
      } catch (_) {
        failed = true;
      }
    })();
    settled = Future.wait([settled, closing]).then((_) {});
  }
}

final _rutubeCleanupProvider = Provider<_RutubeCleanup>(
  (_) => _RutubeCleanup(),
);

class RutubeVideoSurface extends ConsumerStatefulWidget {
  const RutubeVideoSurface({
    super.key,
    required this.video,
    required this.active,
    required this.onViewed,
    required this.onSource,
    this.suspended = false,
    this.routeVisible,
  });
  final ShortVideo video;
  final bool active, suspended;
  final VoidCallback onViewed, onSource;
  final ValueListenable<bool>? routeVisible;
  @override
  ConsumerState<RutubeVideoSurface> createState() => _RutubeVideoSurfaceState();
}

class _RutubeVideoSurfaceState extends ConsumerState<RutubeVideoSurface> {
  late final VideoAccess _access;
  late final _RutubeCleanup _cleanup;
  RutubePlayer? _player;
  Timer? _deadline;
  int _generation = 0;
  String? _failure;
  bool _viewed = false;
  bool get _cleanupFailed => _cleanup.failed;
  OverlayEntry? _fullscreenOverlay;
  LocalHistoryEntry? _fullscreenHistory;
  bool _restoreScheduled = false;

  bool get _eligible =>
      mounted &&
      widget.active &&
      _access.allowed &&
      widget.routeVisible?.value != false;

  @override
  void initState() {
    super.initState();
    _access = ref.read(videoAccessProvider);
    _cleanup = ref.read(_rutubeCleanupProvider);
    _access.addListener(_accessChanged);
    widget.routeVisible?.addListener(_routeChanged);
    if (_eligible) unawaited(_load());
  }

  void _accessChanged() {
    if (!_access.allowed) _release();
  }

  void _routeChanged() {
    if (widget.routeVisible?.value == false) {
      _release();
      if (mounted) setState(() {});
    } else if (!_restoreScheduled) {
      _restoreScheduled = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _restoreScheduled = false;
        if (_eligible && _player == null && _failure == null) {
          unawaited(_load());
        }
      });
      WidgetsBinding.instance.scheduleFrame();
    }
  }

  @override
  void didUpdateWidget(RutubeVideoSurface old) {
    super.didUpdateWidget(old);
    if (old.routeVisible != widget.routeVisible) {
      old.routeVisible?.removeListener(_routeChanged);
      widget.routeVisible?.addListener(_routeChanged);
    }
    if (old.active != widget.active || old.video.id != widget.video.id) {
      _release();
      if (old.video.id != widget.video.id) _viewed = false;
      if (_eligible) unawaited(_load());
    } else if (!old.suspended && widget.suspended) {
      // Never resume automatically: the provider's own play button controls
      // user intent and preserves the platform's gesture requirement.
      unawaited(_pause());
    }
  }

  Future<void> _pause() async {
    final player = _player;
    if (player == null) return;
    try {
      await player.pause().timeout(const Duration(seconds: 3));
    } catch (_) {
      if (identical(player, _player)) _fail('pause_failed');
    }
  }

  Future<void> _load() async {
    if (!_eligible || _player != null) return;
    final generation = ++_generation;
    // PageView may build the newly visible card before rebuilding the old one.
    // Revoke synchronously so native audio is stopped even in that ordering.
    _cleanup.reserve(this, _release);
    Future<void> pending;
    do {
      pending = _cleanup.settled;
      await pending;
    } while (!identical(pending, _cleanup.settled));
    if (!_eligible ||
        generation != _generation ||
        _player != null ||
        !_cleanup.owns(this)) {
      return;
    }
    if (_cleanupFailed) {
      setState(() => _failure = 'cleanup_failed');
      return;
    }
    if (widget.video.rutubeEmbedUri == null) {
      setState(() => _failure = 'invalid_source');
      return;
    }
    RutubePlayer player;
    try {
      player = ref.read(rutubePlayerFactoryProvider)(
        widget.video.providerVideoId!,
      );
    } catch (_) {
      setState(() => _failure = 'webview_unavailable');
      return;
    }
    _player = player;
    _failure = null;
    player.addListener(_changed);
    _deadline = Timer(const Duration(seconds: 25), () {
      if (generation == _generation && !player.ready) _fail('load_timeout');
    });
    if (mounted) setState(() {});
    try {
      await player.initialize().timeout(const Duration(seconds: 20));
      if (_eligible &&
          identical(player, _player) &&
          generation == _generation) {
        _changed();
      }
    } catch (_) {
      if (identical(player, _player) && generation == _generation) {
        _fail('webview_load_failed');
      }
    }
  }

  void _changed() {
    if (!_eligible || _player == null) return;
    final player = _player!;
    if (player.failed) {
      _fail('provider_error');
      return;
    }
    if (player.ready) _deadline?.cancel();
    if (!_viewed &&
        player.playing &&
        player.position >= const Duration(seconds: 3)) {
      _viewed = true;
      widget.onViewed();
    }
    if (player.fullscreen != null && _fullscreenOverlay == null) {
      _showFullscreen(player);
    } else if (player.fullscreen == null) {
      _hideFullscreen();
    }
    setState(() {});
  }

  void _showFullscreen(RutubePlayer player) {
    final view = player.fullscreen;
    if (view == null) return;
    _fullscreenHistory = LocalHistoryEntry(
      onRemove: () {
        _fullscreenHistory = null;
        player.exitFullscreen();
        _hideFullscreen();
      },
    );
    ModalRoute.of(context)?.addLocalHistoryEntry(_fullscreenHistory!);
    _fullscreenOverlay = OverlayEntry(
      builder: (context) => Material(
        color: Colors.black,
        child: SafeArea(
          child: Stack(
            children: [
              Positioned.fill(child: view),
              Positioned(
                top: 8,
                left: 8,
                child: IconButton.filledTonal(
                  tooltip: 'Закрыть полный экран',
                  onPressed: () {
                    player.exitFullscreen();
                    _hideFullscreen();
                  },
                  icon: const Icon(Icons.close),
                ),
              ),
            ],
          ),
        ),
      ),
    );
    Overlay.of(context, rootOverlay: true).insert(_fullscreenOverlay!);
  }

  void _hideFullscreen() {
    final overlay = _fullscreenOverlay;
    _fullscreenOverlay = null;
    overlay?.remove();
    overlay?.dispose();
    final history = _fullscreenHistory;
    _fullscreenHistory = null;
    history?.remove();
  }

  void _fail(String code) {
    _release();
    if (mounted) setState(() => _failure = code);
  }

  void _release() {
    ++_generation;
    _deadline?.cancel();
    _deadline = null;
    final player = _player;
    _player = null;
    player?.removeListener(_changed);
    player?.exitFullscreen();
    _hideFullscreen();
    _cleanup.release(this, player);
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
    final player = _player;
    if (_failure != null) {
      return Center(
        child: SingleChildScrollView(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(
                  Icons.smart_display_outlined,
                  color: VColors.muted,
                  size: 34,
                ),
                const SizedBox(height: 8),
                const Text(
                  'Плеер RUTUBE не загрузился',
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 5),
                Text(
                  _cleanupFailed
                      ? 'Закрой и снова открой «Вечер» или посмотри видео на площадке.'
                      : 'Попробуй ещё раз или открой видео на площадке.',
                  textAlign: TextAlign.center,
                  style: TextStyle(color: VColors.muted),
                ),
                Wrap(
                  alignment: WrapAlignment.center,
                  children: [
                    if (!_cleanupFailed)
                      TextButton(
                        onPressed: _eligible ? () => unawaited(_load()) : null,
                        child: const Text('Повторить'),
                      ),
                    TextButton(
                      onPressed: widget.onSource,
                      child: const Text('Открыть на RUTUBE'),
                    ),
                  ],
                ),
                Text(
                  'RUTUBE: $_failure',
                  style: const TextStyle(fontSize: 10, color: VColors.muted),
                ),
              ],
            ),
          ),
        ),
      );
    }
    if (player == null || !_eligible) {
      return const Center(
        child: Icon(Icons.play_circle_outline, size: 52, color: VColors.muted),
      );
    }
    return Center(
      child: AspectRatio(
        aspectRatio: 16 / 9,
        child: Stack(
          fit: StackFit.expand,
          children: [
            player.buildView(),
            if (!player.ready)
              const IgnorePointer(
                child: ColoredBox(
                  color: Color(0x88000000),
                  child: Center(child: CircularProgressIndicator()),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
