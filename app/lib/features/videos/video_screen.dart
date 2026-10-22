import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/api.dart';
import '../../core/chat_presence.dart';
import '../../core/theme.dart';
import '../chat/chat_screen.dart' show openChat;
import 'video_player.dart';
import 'video_topics.dart';
import 'videos.dart';

class VideoScreen extends ConsumerStatefulWidget {
  const VideoScreen({super.key, this.mode = VideoMode.personal, this.videoId});
  final VideoMode mode;
  final String? videoId;
  @override
  ConsumerState<VideoScreen> createState() => _VideoScreenState();
}

class _VideoScreenState extends ConsumerState<VideoScreen> with RouteAware {
  ModalRoute<dynamic>? _route;
  bool _visible = true;
  final _visibility = ValueNotifier<bool>(true);
  bool _muted = true;
  int _refreshNumber = 0;
  late VideoMode _mode;
  @override
  void initState() {
    super.initState();
    _mode = widget.mode;
  }

  @override
  void didUpdateWidget(VideoScreen old) {
    super.didUpdateWidget(old);
    if (old.mode != widget.mode || old.videoId != widget.videoId) {
      _mode = widget.mode;
      _refreshNumber++;
    }
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final route = ModalRoute.of(context);
    if (route != _route) {
      if (_route != null) chatRouteObserver.unsubscribe(this);
      _route = route;
      if (route != null) chatRouteObserver.subscribe(this, route);
      _visible = route?.isCurrent ?? true;
      _visibility.value = _visible;
    }
  }

  @override
  void didPushNext() {
    _visibility.value = false;
    if (mounted) setState(() => _visible = false);
  }

  @override
  void didPopNext() {
    _visibility.value = true;
    if (mounted) setState(() => _visible = true);
  }

  @override
  void didPush() {
    _visible = true;
    _visibility.value = true;
  }

  @override
  void didPop() {
    _visible = false;
    _visibility.value = false;
  }

  @override
  void dispose() {
    chatRouteObserver.unsubscribe(this);
    _visibility.dispose();
    super.dispose();
  }

  void _refresh() {
    setState(() => _refreshNumber++);
    if (widget.videoId != null) {
      ref.invalidate(shortVideoProvider(widget.videoId!));
    } else {
      ref.invalidate(videoFeedProvider(_mode));
    }
  }

  @override
  Widget build(BuildContext context) {
    final access = ref.watch(videoAccessProvider);
    final settings = ref.watch(videoSettingsProvider);
    final AsyncValue<VideoFeed> feed = widget.videoId == null
        ? ref.watch(videoFeedProvider(_mode))
        : ref.watch(singleVideoFeedProvider(widget.videoId!));
    return Scaffold(
      backgroundColor: VColors.bgDeep,
      appBar: AppBar(
        title: Text(
          widget.videoId != null ? 'Короткое видео' : 'Короткие видео',
          style: display(20),
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          if (widget.videoId == null &&
              (_mode == VideoMode.personal || _mode == VideoMode.shared))
            IconButton(
              tooltip: _mode == VideoMode.shared
                  ? 'Общие темы'
                  : 'Мои интересы',
              onPressed: access.allowed
                  ? () async {
                      await showVideoTopics(
                        context,
                        shared: _mode == VideoMode.shared,
                      );
                      if (mounted) _refresh();
                    }
                  : null,
              icon: const Icon(Icons.tune_rounded),
            ),
          IconButton(
            tooltip: 'Обновить подборку',
            onPressed: access.allowed ? _refresh : null,
            icon: const Icon(Icons.refresh_rounded),
          ),
        ],
      ),
      body: SafeArea(
        top: false,
        child: Column(
          children: [
            if (widget.videoId == null &&
                (_mode == VideoMode.personal || _mode == VideoMode.shared))
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                child: Row(
                  children: [
                    for (final mode in [VideoMode.personal, VideoMode.shared])
                      Expanded(
                        child: Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 4),
                          child: ChoiceChip(
                            label: SizedBox(
                              width: double.infinity,
                              child: Text(
                                mode.title,
                                textAlign: TextAlign.center,
                              ),
                            ),
                            selected: _mode == mode,
                            showCheckmark: false,
                            onSelected: access.allowed
                                ? (_) => setState(() => _mode = mode)
                                : null,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            if (widget.videoId == null &&
                _mode != VideoMode.personal &&
                _mode != VideoMode.shared)
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
                child: Text(
                  _mode.title,
                  style: const TextStyle(color: VColors.goldText),
                ),
              ),
            Expanded(
              child: !access.allowed
                  ? const Center(
                      child: Text(
                        'Видео на паузе',
                        style: TextStyle(color: VColors.muted),
                      ),
                    )
                  : feed.when(
                      skipLoadingOnRefresh: true,
                      skipLoadingOnReload: false,
                      loading: () =>
                          const Center(child: CircularProgressIndicator()),
                      error: (e, _) => _VideoEmpty(
                        icon: Icons.cloud_off_outlined,
                        title: videoErrorText(
                          e,
                          detail: widget.videoId != null,
                        ),
                        action: 'Повторить',
                        onAction: _refresh,
                      ),
                      data: (data) => _VideoPager(
                        key: ValueKey(
                          '${access.revision}:${_mode.query}:${widget.videoId ?? 'feed'}:$_refreshNumber',
                        ),
                        initial: data.items,
                        mode: _mode,
                        visible: _visible,
                        visibility: _visibility,
                        muted: _muted,
                        hasPair: settings.valueOrNull?.hasPair == true,
                        single: widget.videoId != null,
                        onMute: (muted) => setState(() => _muted = muted),
                        onRefresh: _refresh,
                      ),
                    ),
            ),
            const Padding(
              padding: EdgeInsets.fromLTRB(12, 2, 12, 6),
              child: Text(
                'Видео загружаются с Wikimedia Commons',
                style: TextStyle(color: VColors.muted, fontSize: 10),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _VideoPager extends ConsumerStatefulWidget {
  const _VideoPager({
    super.key,
    required this.initial,
    required this.mode,
    required this.visible,
    required this.visibility,
    required this.muted,
    required this.hasPair,
    required this.single,
    required this.onMute,
    required this.onRefresh,
  });
  final List<ShortVideo> initial;
  final VideoMode mode;
  final bool visible, muted, hasPair, single;
  final ValueNotifier<bool> visibility;
  final ValueChanged<bool> onMute;
  final VoidCallback onRefresh;
  @override
  ConsumerState<_VideoPager> createState() => _VideoPagerState();
}

class _VideoPagerState extends ConsumerState<_VideoPager> {
  late List<ShortVideo> _items;
  final _pages = PageController();
  final _busy = <String>{};
  final _seen = <String>{};
  final _requests = <VideoRequest>{};
  int _index = 0;
  bool _scrolling = false;
  String? _notice;
  ShortVideo? _undo;
  @override
  void initState() {
    super.initState();
    _items = List.of(widget.initial);
  }

  @override
  void didUpdateWidget(_VideoPager old) {
    super.didUpdateWidget(old);
    if (!identical(old.initial, widget.initial)) {
      final available = {for (final video in widget.initial) video.id: video};
      // A realtime acknowledgement updates marks without reordering the clip
      // under the viewer's finger. Manual refresh starts a new ordered session.
      _items = [
        for (final video in _items)
          if (available.containsKey(video.id)) available.remove(video.id)!,
        ...available.values,
      ];
      if (_index >= _items.length && _index > 0) _index = _items.length - 1;
      if (_index < 0) _index = 0;
    }
  }

  @override
  void dispose() {
    for (final request in _requests) {
      request.cancel.cancel();
      request.dispose();
    }
    _pages.dispose();
    super.dispose();
  }

  Future<void> _state(ShortVideo video, Map<String, bool> change) async {
    if (_busy.contains(video.id)) return;
    final request = VideoRequest(ref.read(videoAccessProvider));
    if (!request.valid) {
      request.dispose();
      return;
    }
    _requests.add(request);
    setState(() {
      _busy.add(video.id);
      _notice = null;
    });
    try {
      final response = await ref
          .read(dioProvider)
          .patch(
            '/videos/${Uri.encodeComponent(video.id)}/state',
            data: change,
            cancelToken: request.cancel,
          );
      if (!mounted || !request.valid) return;
      final next = ShortVideo.fromJson(response.data as Map);
      setState(() {
        final oldIndex = _items.indexWhere((item) => item.id == video.id);
        if (widget.mode == VideoMode.saved && !next.saved) {
          _items.removeWhere((item) => item.id == video.id);
          _notice = 'Ролик удалён из личных сохранений.';
        } else if (next.hidden &&
            change['hidden'] == true &&
            widget.mode != VideoMode.saved) {
          _undo = video;
          _items.removeWhere((item) => item.id == video.id);
          _notice = 'Ролик скрыт из твоей подборки.';
        } else if (oldIndex >= 0) {
          _items[oldIndex] = next;
        } else if (change['hidden'] == false) {
          _items.insert(_index.clamp(0, _items.length), next);
          _undo = null;
          _notice = 'Ролик снова в подборке.';
        }
        if (change['hidden'] == false) {
          _undo = null;
          _notice = 'Ролик снова в подборке.';
        }
        if (_index >= _items.length && _index > 0) _index = _items.length - 1;
        if (_index < 0) _index = 0;
      });
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && _pages.hasClients && _items.isNotEmpty) {
          _pages.jumpToPage(_index);
        }
      });
      // Other entry points fetch current marks when reopened. The active feed
      // keeps its order so a like cannot unexpectedly switch to another clip.
      ref.invalidate(shortVideoProvider(video.id));
    } catch (e) {
      if (mounted && request.valid) {
        setState(() => _notice = videoErrorText(e, detail: true));
      }
    } finally {
      _requests.remove(request);
      request.dispose();
      if (mounted) setState(() => _busy.remove(video.id));
    }
  }

  Future<void> _viewed(ShortVideo video) async {
    if (!_seen.add(video.id)) return;
    final request = VideoRequest(ref.read(videoAccessProvider));
    _requests.add(request);
    try {
      if (request.valid) {
        await ref
            .read(dioProvider)
            .post(
              '/videos/${Uri.encodeComponent(video.id)}/viewed',
              cancelToken: request.cancel,
            );
      }
    } catch (_) {
      // A view mark is optional and never interrupts actual playback.
    } finally {
      _requests.remove(request);
      request.dispose();
    }
  }

  Future<void> _external(String value, {bool license = false}) async {
    final uri = safeVideoSourceUri(value, license: license);
    try {
      if (uri == null ||
          !await launchUrl(uri, mode: LaunchMode.externalApplication)) {
        if (mounted) {
          setState(
            () => _notice = 'Не удалось открыть ссылку. Попробуй позже.',
          );
        }
      }
    } catch (_) {
      if (mounted) {
        setState(() => _notice = 'Не удалось открыть ссылку. Попробуй позже.');
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final access = ref.watch(videoAccessProvider);
    return Column(
      children: [
        if (_notice != null)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    _notice!,
                    style: const TextStyle(color: VColors.soft, fontSize: 13),
                  ),
                ),
                if (_undo != null)
                  TextButton(
                    onPressed: _busy.contains(_undo!.id)
                        ? null
                        : () => _state(_undo!, {'hidden': false}),
                    child: const Text('Вернуть'),
                  ),
                IconButton(
                  tooltip: 'Закрыть сообщение',
                  onPressed: () => setState(() => _notice = null),
                  icon: const Icon(Icons.close, size: 18),
                ),
              ],
            ),
          ),
        Expanded(
          child: _items.isEmpty
              ? _VideoEmpty(
                  icon: Icons.video_library_outlined,
                  title: switch (widget.mode) {
                    VideoMode.saved =>
                      'Здесь появятся ролики, которые ты сохранишь.',
                    VideoMode.sharedCollection => 'Отправьте друг другу видео в чате — они останутся здесь.',
                    _ => 'В этой подборке пока нет доступных роликов. Можно выбрать другие темы или вернуться позже.',
                  },
                  action:
                      widget.mode == VideoMode.personal ||
                          widget.mode == VideoMode.shared
                      ? 'Обновить подборку'
                      : 'Открыть подборку',
                  onAction:
                      widget.mode == VideoMode.personal ||
                          widget.mode == VideoMode.shared
                      ? widget.onRefresh
                      : () => context.push('/videos'),
                )
              : NotificationListener<ScrollNotification>(
                  onNotification: (n) {
                    if (n.depth != 0) return false;
                    if (n is ScrollStartNotification && !_scrolling) {
                      setState(() => _scrolling = true);
                    }
                    if (n is ScrollEndNotification && _scrolling) {
                      setState(() => _scrolling = false);
                    }
                    return false;
                  },
                  child: PageView.builder(
                    controller: _pages,
                    scrollDirection: Axis.vertical,
                    itemCount: _items.length + (widget.single ? 0 : 1),
                    onPageChanged: (value) => setState(() => _index = value),
                    itemBuilder: (context, index) {
                      if (index == _items.length) {
                        return _VideoEmpty(
                          icon: Icons.nights_stay_outlined,
                          title: 'Подборка закончилась',
                          subtitle: 'Доступные ролики этой подборки закончились. Новые появятся после пополнения каталога. Можно выбрать другие темы.',
                          action: 'Обновить подборку',
                          onAction: widget.onRefresh,
                        );
                      }
                      final video = _items[index];
                      final busy = _busy.contains(video.id);
                      return LayoutBuilder(
                        builder: (context, box) {
                          // Small screens and large system fonts can scroll the details;
                          // the media never stretches/crops a horizontal source.
                          final height = box.maxHeight;
                          final mediaHeight = (height * 0.55).clamp(
                            170.0,
                            540.0,
                          );
                          return Column(
                            children: [
                              SizedBox(
                                height: mediaHeight,
                                width: double.infinity,
                                child: ShortVideoSurface(
                                  key: ValueKey(video.id),
                                  video: video,
                                  routeVisible: widget.visibility,
                                  active:
                                      widget.visible &&
                                      access.allowed &&
                                      index == _index,
                                  suspended: _scrolling,
                                  muted: widget.muted,
                                  onMute: widget.onMute,
                                  onViewed: () => unawaited(_viewed(video)),
                                  onSource: () =>
                                      unawaited(_external(video.sourceUrl)),
                                ),
                              ),
                              Expanded(
                                child: SingleChildScrollView(
                                  padding: const EdgeInsets.fromLTRB(
                                    16,
                                    12,
                                    16,
                                    12,
                                  ),
                                  child: Column(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.stretch,
                                    children: [
                                      Text(
                                        video.title,
                                        style: const TextStyle(
                                          fontSize: 18,
                                          fontWeight: FontWeight.w600,
                                        ),
                                      ),
                                      if (video.description.isNotEmpty)
                                        Padding(
                                          padding: const EdgeInsets.only(
                                            top: 5,
                                          ),
                                          child: Text(
                                            video.description,
                                            style: const TextStyle(
                                              color: VColors.soft,
                                              fontSize: 13,
                                              height: 1.35,
                                            ),
                                          ),
                                        ),
                                      const SizedBox(height: 8),
                                      Wrap(
                                        spacing: 6,
                                        runSpacing: 2,
                                        children: [
                                          _Action(
                                            label: 'Нравится',
                                            icon: video.liked
                                                ? Icons.favorite
                                                : Icons.favorite_border,
                                            selected: video.liked,
                                            onTap: busy
                                                ? null
                                                : () => _state(video, {
                                                    'liked': !video.liked,
                                                  }),
                                          ),
                                          _Action(
                                            label: video.saved
                                                ? 'Сохранено'
                                                : 'Сохранить',
                                            icon: video.saved
                                                ? Icons.bookmark
                                                : Icons.bookmark_border,
                                            selected: video.saved,
                                            onTap: busy
                                                ? null
                                                : () => _state(video, {
                                                    'saved': !video.saved,
                                                  }),
                                          ),
                                          _Action(
                                            label: video.hidden
                                                ? 'Вернуть в подборку'
                                                : 'Не интересно',
                                            icon: video.hidden
                                                ? Icons.visibility_outlined
                                                : Icons.visibility_off_outlined,
                                            onTap: busy
                                                ? null
                                                : () => _state(video, {
                                                    'hidden': !video.hidden,
                                                  }),
                                          ),
                                        ],
                                      ),
                                      if (widget.hasPair)
                                        TextButton.icon(
                                          icon: const Icon(
                                            Icons.send_outlined,
                                            size: 18,
                                          ),
                                          onPressed: () => openChat(
                                            context,
                                            kind: 'video',
                                            id: video.id,
                                            title: video.title,
                                          ),
                                          label: const Text(
                                            'Отправить партнёру',
                                          ),
                                        ),
                                      if (video.planCardId != null)
                                        TextButton.icon(
                                          icon: const Icon(
                                            Icons.nights_stay_outlined,
                                            size: 18,
                                          ),
                                          onPressed: () => context.push(
                                            '/wishes/plan/${video.planCardId}',
                                          ),
                                          label: const Text(
                                            'Попробовать вместе — открыть план',
                                          ),
                                        ),
                                      Text(
                                        '${video.author} · ${video.licenseName}',
                                        style: const TextStyle(
                                          color: VColors.muted,
                                          fontSize: 11,
                                        ),
                                      ),
                                      Wrap(
                                        spacing: 12,
                                        children: [
                                          TextButton(
                                            onPressed: () =>
                                                _external(video.sourceUrl),
                                            child: const Text('Источник'),
                                          ),
                                          TextButton(
                                            onPressed: () => _external(
                                              video.licenseUrl,
                                              license: true,
                                            ),
                                            child: const Text('Лицензия'),
                                          ),
                                        ],
                                      ),
                                      Text(
                                        widget.mode == VideoMode.shared
                                            ? 'Общие темы выбираете явно. Личные отметки партнёру не видны.'
                                            : 'Твои отметки и сохранения видны только тебе.',
                                        style: const TextStyle(
                                          color: VColors.muted,
                                          fontSize: 11,
                                        ),
                                      ),
                                      const SizedBox(height: 5),
                                      Text(
                                        '${index + 1} / ${_items.length} · Листай вверх',
                                        style: const TextStyle(
                                          color: VColors.muted,
                                          fontSize: 11,
                                        ),
                                      ),
                                    ],
                                  ),
                                ),
                              ),
                            ],
                          );
                        },
                      );
                    },
                  ),
                ),
        ),
      ],
    );
  }
}

class _Action extends StatelessWidget {
  const _Action({
    required this.label,
    required this.icon,
    required this.onTap,
    this.selected = false,
  });
  final String label;
  final IconData icon;
  final VoidCallback? onTap;
  final bool selected;
  @override
  Widget build(BuildContext context) => ActionChip(
    avatar: Icon(icon, size: 18, color: selected ? VColors.gold : VColors.soft),
    label: Text(label),
    onPressed: onTap,
    backgroundColor: selected ? VColors.segment : VColors.card,
  );
}

class _VideoEmpty extends StatelessWidget {
  const _VideoEmpty({
    required this.icon,
    required this.title,
    this.subtitle,
    required this.action,
    required this.onAction,
  });
  final IconData icon;
  final String title, action;
  final String? subtitle;
  final VoidCallback onAction;
  @override
  Widget build(BuildContext context) => Center(
    child: SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: VColors.lavender, size: 42),
          const SizedBox(height: 16),
          Text(
            title,
            textAlign: TextAlign.center,
            style: const TextStyle(fontSize: 18),
          ),
          if (subtitle != null)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Text(
                subtitle!,
                textAlign: TextAlign.center,
                style: const TextStyle(color: VColors.muted),
              ),
            ),
          const SizedBox(height: 16),
          OutlinedButton(onPressed: onAction, child: Text(action)),
        ],
      ),
    ),
  );
}
