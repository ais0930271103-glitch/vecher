import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/chat_presence.dart';
import '../../core/session_changes.dart';
import '../../core/theme.dart';
import '../content/content.dart';
import '../videos/videos.dart'
    show VideoAccess, VideoRequest, videoAccessProvider;
import 'blog_video_upload.dart';
import 'blogs.dart';
import 'hosted_content_video.dart';

class BlogPostEditorScreen extends StatelessWidget {
  const BlogPostEditorScreen({super.key, required this.blogId, this.postId});
  final String blogId;
  final String? postId;
  @override
  Widget build(BuildContext context) => BlogGate(
    child: _Editor(blogId: blogId, postId: postId),
  );
}

class _Editor extends ConsumerStatefulWidget {
  const _Editor({required this.blogId, this.postId});
  final String blogId;
  final String? postId;
  @override
  ConsumerState<_Editor> createState() => _EditorState();
}

class _EditorState extends ConsumerState<_Editor>
    with ContentRequests<_Editor>, RouteAware {
  final _title = TextEditingController(),
      _description = TextEditingController();
  final _clientId = newBlogRequestId();
  final _topics = <String>{};
  String _language = 'ru';
  PublicBlog? _blog;
  BlogPost? _post;
  BlogMedia? _media;
  SelectedBlogVideo? _selected;
  ContentSettings? _settings;
  late final VideoAccess _access;
  VideoRequest? _upload;
  Timer? _pollTimer;
  int _pollCount = 0;
  bool _loading = true, _busy = false, _picking = false, _polling = false;
  bool _dirty = false, _allowPop = false, _visible = true;
  double? _progress;
  Object? _error;
  String? _notice;
  ModalRoute<dynamic>? _route;
  String get _base => '/blogs/${Uri.encodeComponent(widget.blogId)}';
  String get _postPath => '$_base/posts/${Uri.encodeComponent(_post!.id)}';
  bool get _canEditFields =>
      !_busy && (_post?.canEdit ?? _blog?.canPublish ?? false);
  bool get _editable =>
      !_busy &&
      !_picking &&
      _upload == null &&
      (_post?.canEdit ?? _blog?.canPublish ?? false);
  bool get _valid =>
      _title.text.trim().isNotEmpty &&
      _description.text.trim().isNotEmpty &&
      _topics.isNotEmpty &&
      _media?.ready == true;

  @override
  void initState() {
    super.initState();
    _access = ref.read(videoAccessProvider)..addListener(_accessChanged);
    Future.microtask(_load);
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final route = ModalRoute.of(context);
    if (route != _route) {
      // The same observer also covers incoming call routes and dialogs.
      if (_route != null) chatRouteObserver.unsubscribe(this);
      _route = route;
      if (route != null) chatRouteObserver.subscribe(this, route);
      _visible = route?.isCurrent ?? true;
    }
  }

  @override
  void didPushNext() {
    _visible = false;
    _pollTimer?.cancel();
  }

  @override
  void didPopNext() {
    _visible = true;
    _schedulePoll();
  }

  @override
  void didPop() {
    _visible = false;
    _pollTimer?.cancel();
  }

  Future<void> _load() async {
    if (!mounted || !_access.allowed) return;
    _pollTimer?.cancel();
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data = await contentRequest('GET', _base);
      if (!mounted || data == null) return;
      final settings = await ref.read(contentSettingsProvider.future);
      if (!mounted || settings == null || !_access.allowed) return;
      final id = _post?.id ?? widget.postId;
      BlogPost? post;
      if (id != null) {
        final result = await contentRequest(
          'GET',
          '$_base/posts/${Uri.encodeComponent(id)}',
        );
        if (!mounted || result == null) return;
        post = BlogPost.fromJson(result as Map);
      }
      setState(() {
        _blog = PublicBlog.fromJson(data as Map);
        _settings = settings;
        if (post != null) _acceptPost(post, syncFields: true);
        _loading = false;
      });
      _schedulePoll();
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = e;
          _loading = false;
        });
      }
    }
  }

  void _acceptPost(BlogPost post, {required bool syncFields}) {
    _post = post;
    _media = post.media;
    if (syncFields) {
      _title.text = post.title;
      _description.text = post.description;
      _topics
        ..clear()
        ..addAll(post.topics);
      _language = post.language;
      _dirty = false;
      _selected = null;
    }
  }

  void _accessChanged() {
    _pollTimer?.cancel();
    if (!_access.allowed) {
      _upload?.cancel.cancel('Blog editor suspended');
    } else {
      _schedulePoll();
    }
  }

  Future<void> _pick() async {
    if (!_editable) return;
    final session = sessionChanges.revision;
    setState(() {
      _picking = true;
      _error = null;
      _notice = null;
    });
    try {
      final selected = await ref.read(blogVideoPickerProvider)();
      if (!mounted || session != sessionChanges.revision || selected == null) {
        return;
      }
      if (!_access.allowed) {
        setState(
          () => _notice = 'Выбор видео прерван. Разблокируй приложение и выбери файл снова.',
        );
        return;
      }
      final old = _media;
      _pollTimer?.cancel();
      setState(() {
        _selected = selected;
        _media = null;
        _dirty = true;
      });
      // Only unattached media may be removed. A current post's asset is retained
      // until an explicit server-side revision change replaces it.
      if (old != null && old.id != _post?.mediaId) await _deleteOrphan(old.id);
      if (mounted && _access.allowed) await _uploadSelected();
    } catch (e) {
      if (mounted) {
        setState(() => _error = e is BlogVideoException ? e.message : e);
      }
    } finally {
      if (mounted) setState(() => _picking = false);
    }
  }

  Future<void> _deleteOrphan(String id) async {
    try {
      await contentRequest('DELETE', '/blogs/media/${Uri.encodeComponent(id)}');
    } catch (_) {
      /* Unattached files also have a bounded server cleanup TTL. */
    }
  }

  Future<void> _uploadSelected() async {
    final selected = _selected;
    if (selected == null || _upload != null || !_access.allowed) return;
    final request = VideoRequest(_access);
    setState(() {
      _upload = request;
      _progress = 0;
      _error = null;
      _notice = null;
    });
    try {
      final media = await uploadBlogVideo(
        ref.read(blogUploadDioProvider),
        widget.blogId,
        selected,
        cancelToken: request.cancel,
        onProgress: (sent, total) {
          if (mounted && request.valid) {
            setState(() => _progress = total > 0 ? sent / total : null);
          }
        },
      );
      if (!mounted || !request.valid) return;
      setState(() {
        _media = media;
        _dirty = true;
        _pollCount = 0;
      });
      _schedulePoll();
    } catch (e) {
      if (!mounted) return;
      if ((e is DioException && CancelToken.isCancel(e)) || !request.valid) {
        setState(
          () => _notice = 'Загрузка остановлена. Можно продолжить тем же файлом, пока этот экран открыт.',
        );
      } else {
        setState(() => _error = e is BlogVideoException ? e.message : e);
      }
    } finally {
      request.dispose();
      if (mounted && identical(_upload, request)) {
        setState(() {
          _upload = null;
          _progress = null;
        });
      }
    }
  }

  Future<void> _retryUpload() async {
    if (_selected == null || _upload != null) return;
    if (_media?.status == 'failed') {
      await _deleteOrphan(_media!.id);
      final old = _selected!;
      _selected = SelectedBlogVideo(
        file: old.file,
        browserBytes: old.browserBytes,
      );
      _media = null;
    }
    if (mounted) await _uploadSelected();
  }

  void _schedulePoll() {
    _pollTimer?.cancel();
    if (!mounted ||
        !_access.allowed ||
        !_visible ||
        _media?.processing != true ||
        _polling ||
        _pollCount >= 120) {
      return;
    }
    _pollTimer = Timer(const Duration(seconds: 5), _pollMedia);
  }

  Future<void> _pollMedia() async {
    final media = _media;
    if (media == null ||
        _polling ||
        !mounted ||
        !_access.allowed ||
        !_visible) {
      return;
    }
    _polling = true;
    try {
      final data = await contentRequest(
        'GET',
        '/blogs/media/${Uri.encodeComponent(media.id)}',
      );
      if (!mounted || data == null || _media?.id != media.id) return;
      setState(() {
        _media = BlogMedia.fromJson(data as Map);
        _pollCount++;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = e;
          _pollCount = 120;
        });
      }
    } finally {
      _polling = false;
      if (mounted) _schedulePoll();
    }
  }

  Future<void> _save() async {
    if (!_editable || !_valid) return;
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      final post = _post;
      final data = await contentRequest(
        post == null ? 'POST' : 'PATCH',
        post == null ? '$_base/posts' : _postPath,
        data: {
          if (post == null) 'client_id': _clientId,
          if (post != null) 'revision': post.revision,
          'title': _title.text.trim(),
          'description': _description.text.trim(),
          'topics': _topics.toList()..sort(),
          'language': _language,
          'media_id': _media!.id,
        },
      );
      if (!mounted || data == null) return;
      setState(() {
        _acceptPost(BlogPost.fromJson(data as Map), syncFields: true);
        _notice = 'Черновик сохранён. Отправка на проверку — отдельный шаг.';
      });
      invalidateBlogs(ref);
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _submit() async {
    final post = _post;
    if (post == null || !post.canSubmit || _dirty || _busy) return;
    final accepted = await showDialog<bool>(
      context: context,
      builder: (context) => _SubmissionConsent(
        title: _blog!.couple
            ? (post.partnerApproved
                  ? 'Подтвердить эту версию?'
                  : 'Предложить партнёру?')
            : 'Отправить на проверку?',
        post: post,
        couple: _blog!.couple,
      ),
    );
    if (accepted != true || !mounted || !_access.allowed) return;
    await _postAction('submit', {
      'revision': post.revision,
      'public_consent': true,
      'consent_version': publicPostConsentVersion,
    });
  }

  Future<void> _withdraw() async {
    final post = _post;
    if (post == null || !post.canWithdraw || _busy) return;
    final yes = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Снять публикацию?'),
        content: const Text(
          'Видео исчезнет из общих подборок и страницы автора. В блоге пары это может сделать любой из вас.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Оставить'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Снять публикацию'),
          ),
        ],
      ),
    );
    if (yes == true && mounted) {
      await _postAction('withdraw', {'revision': post.revision});
    }
  }

  Future<void> _postAction(String action, Map<String, dynamic> data) async {
    setState(() {
      _busy = true;
      _error = null;
      _notice = null;
    });
    try {
      final result = await contentRequest(
        'POST',
        '$_postPath/$action',
        data: data,
      );
      if (!mounted || result == null) return;
      setState(
        () => _acceptPost(BlogPost.fromJson(result as Map), syncFields: true),
      );
      invalidateBlogs(ref);
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _leave() async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Выйти из редактора?'),
        content: const Text(
          'Несохранённые изменения потеряются. Начатая загрузка остановится.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Остаться'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Выйти'),
          ),
        ],
      ),
    );
    if (yes == true && mounted) {
      setState(() => _allowPop = true);
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) context.pop();
      });
    }
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    _upload?.cancel.cancel('Blog editor closed');
    _access.removeListener(_accessChanged);
    chatRouteObserver.unsubscribe(this);
    _title.dispose();
    _description.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final post = _post;
    final media = _media;
    return PopScope(
      canPop: _allowPop || (!_dirty && _upload == null && !_busy),
      onPopInvokedWithResult: (didPop, result) {
        if (!didPop) _leave();
      },
      child: Scaffold(
        appBar: AppBar(
          title: Text(post == null ? 'Новое видео' : 'Публикация'),
          actions: [
            if (post != null)
              IconButton(
                tooltip: 'Обновить публикацию',
                onPressed: _busy || _dirty || _upload != null ? null : _load,
                icon: const Icon(Icons.refresh),
              ),
          ],
        ),
        body: SafeArea(
          child: _loading
              ? const Center(child: CircularProgressIndicator())
              : ListView(
                  padding: const EdgeInsets.fromLTRB(20, 8, 20, 36),
                  children: [
                    if (_blog != null)
                      Text(
                        _blog!.name,
                        style: const TextStyle(color: VColors.gold),
                      ),
                    if (post != null) ...[
                      const SizedBox(height: 8),
                      Text(
                        post.statusLabel,
                        style: const TextStyle(
                          fontSize: 23,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 8),
                      Text(
                        'Версия ${post.revision}',
                        style: const TextStyle(color: VColors.muted),
                      ),
                    ],
                    if (post?.reviewNote?.isNotEmpty == true) ...[
                      const SizedBox(height: 12),
                      Text(
                        post!.reviewNote!,
                        style: const TextStyle(color: VColors.gold),
                      ),
                    ],
                    const SizedBox(height: 16),
                    const Text(
                      'Видео до 3 минут · MP4 или MOV · до 100 МБ',
                      style: TextStyle(color: VColors.soft),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      _blog?.couple == true
                          ? 'Черновик доступен вам обоим. Другие пользователи увидят его только после ваших согласий и проверки.'
                          : 'Черновик доступен только тебе. Другие пользователи увидят его после твоего подтверждения и проверки.',
                      style: const TextStyle(color: VColors.muted, height: 1.5),
                    ),
                    const SizedBox(height: 12),
                    if (media?.ready == true)
                      HostedContentVideo(
                        key: ValueKey(media!.id),
                        media: media,
                      ),
                    if (_selected != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 12),
                        child: Text(
                          _selected!.name,
                          maxLines: 2,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    if (_upload != null) ...[
                      const SizedBox(height: 12),
                      LinearProgressIndicator(value: _progress),
                      const SizedBox(height: 6),
                      Text(
                        'Загружаем видео${_progress == null ? '…' : ' · ${(_progress! * 100).round()}%'}',
                      ),
                      TextButton(
                        onPressed: () =>
                            _upload?.cancel.cancel('User cancelled upload'),
                        child: const Text('Остановить загрузку'),
                      ),
                    ] else ...[
                      if (media != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 12),
                          child: Text(
                            media.statusLabel,
                            style: const TextStyle(color: VColors.gold),
                          ),
                        ),
                      if (media?.processing == true) ...[
                        const SizedBox(height: 8),
                        const LinearProgressIndicator(),
                        TextButton(
                          onPressed: _polling
                              ? null
                              : () {
                                  _pollCount = 0;
                                  _pollMedia();
                                },
                          child: const Text('Обновить готовность видео'),
                        ),
                      ],
                      if (_selected != null &&
                          (media == null || media.status == 'failed'))
                        OutlinedButton.icon(
                          onPressed: _busy || _picking ? null : _retryUpload,
                          icon: const Icon(Icons.refresh),
                          label: const Text('Повторить загрузку видео'),
                        ),
                      if (_post?.canEdit ?? _blog?.canPublish ?? false)
                        OutlinedButton.icon(
                          onPressed: _editable ? _pick : null,
                          icon: const Icon(Icons.video_file_outlined),
                          label: Text(
                            media == null && _selected == null
                                ? 'Выбрать видео с устройства'
                                : 'Выбрать другое видео',
                          ),
                        ),
                    ],
                    if (_picking && _upload == null)
                      const LinearProgressIndicator(),
                    const SizedBox(height: 20),
                    TextField(
                      key: const ValueKey('blog-post-title'),
                      controller: _title,
                      enabled: _canEditFields,
                      maxLength: 200,
                      onChanged: (_) => setState(() => _dirty = true),
                      decoration: const InputDecoration(
                        labelText: 'Название видео',
                      ),
                    ),
                    const SizedBox(height: 12),
                    TextField(
                      key: const ValueKey('blog-post-description'),
                      controller: _description,
                      enabled: _canEditFields,
                      minLines: 4,
                      maxLines: 10,
                      maxLength: 4000,
                      onChanged: (_) => setState(() => _dirty = true),
                      decoration: const InputDecoration(
                        labelText: 'Описание',
                        hintText:
                            'Что происходит в видео и чем хочешь поделиться?',
                      ),
                    ),
                    const SizedBox(height: 18),
                    const Text(
                      'Темы публикации',
                      style: TextStyle(fontWeight: FontWeight.w600),
                    ),
                    const SizedBox(height: 6),
                    const Text(
                      'Выбери от 1 до 6 тем именно этого видео. Личные интересы здесь не подставляются.',
                      style: TextStyle(color: VColors.muted, fontSize: 12),
                    ),
                    const SizedBox(height: 10),
                    Wrap(
                      spacing: 8,
                      runSpacing: 6,
                      children: [
                        for (final topic
                            in _settings?.topics ?? <ContentChoice>[])
                          FilterChip(
                            key: ValueKey('blog-post-topic-${topic.id}'),
                            label: Text(topic.title),
                            selected: _topics.contains(topic.id),
                            onSelected:
                                !_canEditFields ||
                                    (_topics.length >= 6 &&
                                        !_topics.contains(topic.id))
                                ? null
                                : (value) => setState(() {
                                    if (value) {
                                      _topics.add(topic.id);
                                    } else {
                                      _topics.remove(topic.id);
                                    }
                                    _dirty = true;
                                  }),
                          ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    DropdownButtonFormField<String>(
                      initialValue: _language,
                      decoration: const InputDecoration(
                        labelText: 'Язык видео',
                      ),
                      items: [
                        for (final language
                            in _settings?.languages ?? <ContentChoice>[])
                          if (language.id != 'all')
                            DropdownMenuItem(
                              value: language.id,
                              child: Text(language.title),
                            ),
                      ],
                      onChanged: !_canEditFields
                          ? null
                          : (value) {
                              if (value != null) {
                                setState(() {
                                  _language = value;
                                  _dirty = true;
                                });
                              }
                            },
                    ),
                    const SizedBox(height: 22),
                    if (_dirty && post != null && post.status != 'draft') ...[
                      const Text(
                        'Изменения снимут прежние согласия и отправят видео обратно в черновик. Опубликованная версия станет недоступна до повторного подтверждения и проверки.',
                        style: TextStyle(color: VColors.gold, height: 1.45),
                      ),
                      const SizedBox(height: 14),
                    ],
                    if (_post?.canEdit ?? _blog?.canPublish ?? false)
                      FilledButton(
                        onPressed:
                            _editable && _valid && (_dirty || post == null)
                            ? _save
                            : null,
                        child: const Text('Сохранить черновик'),
                      ),
                    if (!_valid &&
                        (_post?.canEdit ?? _blog?.canPublish ?? false))
                      const Padding(
                        padding: EdgeInsets.only(top: 8),
                        child: Text(
                          'Для сохранения нужны готовое видео, название, описание и хотя бы одна тема.',
                          style: TextStyle(color: VColors.muted, fontSize: 12),
                        ),
                      ),
                    if (post?.canSubmit == true &&
                        post?.myApproved != true) ...[
                      const SizedBox(height: 12),
                      FilledButton.icon(
                        onPressed: _busy || _dirty ? null : _submit,
                        icon: const Icon(Icons.fact_check_outlined),
                        label: Text(
                          _blog!.couple
                              ? (post!.partnerApproved
                                    ? 'Подтвердить эту версию'
                                    : 'Предложить публикацию партнёру')
                              : 'Отправить на проверку',
                        ),
                      ),
                    ],
                    if (post?.status == 'pending_partner' ||
                        post?.status == 'pending_review')
                      Padding(
                        padding: const EdgeInsets.only(top: 12),
                        child: Text(
                          post!.status == 'pending_partner'
                              ? 'Сейчас другие пользователи это видео не видят. Нужны согласия обоих на одну и ту же версию.'
                              : 'Видео ещё не опубликовано. Редакция проверяет первую пилотную подборку; решение появится здесь.',
                          style: const TextStyle(
                            color: VColors.muted,
                            height: 1.45,
                          ),
                        ),
                      ),
                    if (post?.status == 'published' && post?.contentId != null)
                      TextButton(
                        onPressed: () => context.push(
                          '/content/${Uri.encodeComponent(post!.contentId!)}',
                        ),
                        child: const Text('Открыть опубликованное видео'),
                      ),
                    if (_busy)
                      const Padding(
                        padding: EdgeInsets.all(12),
                        child: LinearProgressIndicator(),
                      ),
                    if (_notice != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 14),
                        child: Text(
                          _notice!,
                          style: const TextStyle(color: VColors.gold),
                        ),
                      ),
                    if (_error != null)
                      ContentError(
                        error: blogErrorText(_error!),
                        onRetry: _busy
                            ? () {}
                            : () {
                                if (_post != null || widget.postId != null) {
                                  _load();
                                } else {
                                  setState(() => _error = null);
                                }
                              },
                        retryLabel: _post != null || widget.postId != null
                            ? 'Загрузить актуальную версию'
                            : 'Вернуться к черновику',
                      ),
                    if (post?.canWithdraw == true) ...[
                      const SizedBox(height: 26),
                      OutlinedButton(
                        onPressed: _busy ? null : _withdraw,
                        child: const Text('Снять публикацию'),
                      ),
                    ],
                  ],
                ),
        ),
      ),
    );
  }
}

class _SubmissionConsent extends StatefulWidget {
  const _SubmissionConsent({
    required this.title,
    required this.post,
    required this.couple,
  });
  final String title;
  final BlogPost post;
  final bool couple;
  @override
  State<_SubmissionConsent> createState() => _SubmissionConsentState();
}

class _SubmissionConsentState extends State<_SubmissionConsent> {
  bool _agreed = false;
  @override
  Widget build(BuildContext context) => AlertDialog(
    title: Text(widget.title),
    content: SingleChildScrollView(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            widget.post.title,
            style: const TextStyle(fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 8),
          Text('Версия ${widget.post.revision} · видео и описание выше'),
          const SizedBox(height: 12),
          Text(
            widget.couple
                ? 'После согласий обоих эту версию проверит редакция. После одобрения её смогут увидеть другие пользователи «Вечера».'
                : 'После одобрения редакцией это видео и описание смогут увидеть другие пользователи «Вечера».',
          ),
          const SizedBox(height: 12),
          CheckboxListTile(
            key: const ValueKey('blog-post-public-consent'),
            contentPadding: EdgeInsets.zero,
            value: _agreed,
            onChanged: (value) => setState(() => _agreed = value == true),
            title: const Text(
              'Я вправе публиковать это видео; люди в кадре согласны. Подтверждаю публичное видео и описание именно этой версии.',
            ),
          ),
        ],
      ),
    ),
    actions: [
      TextButton(
        onPressed: () => Navigator.pop(context, false),
        child: const Text('Вернуться'),
      ),
      FilledButton(
        onPressed: _agreed ? () => Navigator.pop(context, true) : null,
        child: const Text('Подтвердить'),
      ),
    ],
  );
}
