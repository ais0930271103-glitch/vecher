import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import '../videos/videos.dart' show videoAccessProvider;
import 'content.dart';
import 'content_author_screen.dart';

class ContentScreen extends ConsumerWidget {
  const ContentScreen({
    super.key,
    this.mode = ContentMode.personal,
    this.format = ContentFormat.all,
    this.discover = false,
    this.authorId,
    this.seriesId,
    this.title,
  });
  final ContentMode mode;
  final ContentFormat format;
  final bool discover;
  final String? authorId, seriesId, title;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final access = ref.watch(videoAccessProvider);
    if (!access.allowed) return const ContentUnavailable();
    return _ContentFeed(
      key: ValueKey(
        '${access.revision}:${mode.name}:${format.name}:$discover:$authorId:$seriesId',
      ),
      mode: mode,
      format: format,
      discover: discover,
      authorId: authorId,
      seriesId: seriesId,
      title: title,
    );
  }
}

class _ContentFeed extends ConsumerStatefulWidget {
  const _ContentFeed({
    super.key,
    required this.mode,
    required this.format,
    required this.discover,
    this.authorId,
    this.seriesId,
    this.title,
  });
  final ContentMode mode;
  final ContentFormat format;
  final bool discover;
  final String? authorId, seriesId, title;
  @override
  ConsumerState<_ContentFeed> createState() => _ContentFeedState();
}

class _ContentFeedState extends ConsumerState<_ContentFeed>
    with ContentRequests<_ContentFeed> {
  final _search = TextEditingController();
  final _scroll = ScrollController();
  final _items = <Publication>[];
  late ContentMode _mode;
  late ContentFormat _format;
  late bool _discover;
  String _query = '';
  String? _cursor, _notice;
  Object? _error;
  bool _loading = true, _loadingMore = false, _noPair = false;
  int _generation = 0;
  bool get _filtered => widget.authorId != null || widget.seriesId != null;

  @override
  void initState() {
    super.initState();
    _mode = widget.mode;
    _format = widget.format;
    _discover = widget.discover && _mode == ContentMode.personal && !_filtered;
    Future.microtask(() {
      if (mounted) _load();
    });
  }

  Future<void> _load({bool more = false}) async {
    if (more && (_loading || _loadingMore || _cursor == null)) return;
    final generation = more ? _generation : ++_generation;
    if (!more) cancelContentRequests();
    setState(() {
      _error = null;
      if (more) {
        _loadingMore = true;
      } else {
        _loading = true;
        _loadingMore = false;
        _noPair = false;
        _items.clear();
        _cursor = null;
      }
    });
    try {
      final settings = await ref.read(contentSettingsProvider.future);
      if (!mounted || generation != _generation || settings == null) return;
      if (!settings.unifiedFeed &&
          (_format != ContentFormat.all || _discover)) {
        setState(() {
          _format = ContentFormat.all;
          _discover = false;
          _notice =
              'Общая подборка обновляется. Пока доступны прежние материалы.';
        });
      }
      final data = await contentRequest(
        'GET',
        '/content/feed',
        query: {
          'mode': _mode.name,
          if (settings.unifiedFeed) 'format': _format.name,
          if (_discover) 'discover': true,
          'limit': 20,
          if (more) 'cursor': _cursor,
          if (_query.isNotEmpty) 'q': _query,
          if (widget.authorId != null) 'author_id': widget.authorId,
          if (widget.seriesId != null) 'series_id': widget.seriesId,
        },
      );
      if (!mounted || data == null || generation != _generation) return;
      final page = ContentPage.fromJson(data as Map);
      setState(() {
        final known = _items.map((e) => e.id).toSet();
        _items.addAll(page.items.where((e) => known.add(e.id)));
        _cursor = page.nextCursor;
        _loading = _loadingMore = false;
      });
    } catch (error) {
      if (!mounted || generation != _generation) return;
      if (!more &&
          _mode == ContentMode.shared &&
          error is DioException &&
          error.response?.statusCode == 409) {
        setState(() {
          _noPair = true;
          _loading = _loadingMore = false;
        });
        return;
      }
      if (more && error is DioException && error.response?.statusCode == 409) {
        _notice = 'Подборка изменилась. Показываем её с начала.';
        ref.invalidate(contentSettingsProvider);
        if (_scroll.hasClients) _scroll.jumpTo(0);
        await _load();
        return;
      }
      setState(() {
        _error = error;
        _loading = _loadingMore = false;
      });
    }
  }

  void _changeMode(ContentMode mode) {
    if (_mode == mode) return;
    setState(() {
      _mode = mode;
      _discover = false;
      _notice = null;
    });
    if (_scroll.hasClients) _scroll.jumpTo(0);
    _load();
  }

  void _changeFormat(ContentFormat format) {
    if (_format == format) return;
    setState(() {
      _format = format;
      _notice = null;
    });
    if (_scroll.hasClients) _scroll.jumpTo(0);
    _load();
  }

  void _toggleDiscover() {
    if (_mode != ContentMode.personal || _filtered) return;
    setState(() {
      _discover = !_discover;
      _notice = null;
    });
    if (_scroll.hasClients) _scroll.jumpTo(0);
    _load();
  }

  Future<void> _open(Publication item) async {
    await context.push(item.openLocation);
    if (mounted && item.isVideo) invalidateContent(ref);
  }

  void _submitSearch() {
    final query = _search.text.trim();
    if (query == _query) return;
    setState(() {
      _query = query;
      _notice = null;
    });
    _load();
  }

  Future<void> _hide(Publication item) async {
    try {
      final result = await contentRequest(
        'PATCH',
        '/content/${Uri.encodeComponent(item.id)}/state',
        data: {'hidden': true},
      );
      if (!mounted || result == null) return;
      setState(() {
        _notice =
            'Публикация скрыта из подборок. Сохранённое остаётся доступно.';
      });
      invalidateContent(ref);
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = error;
        });
      }
    }
  }

  @override
  void dispose() {
    _generation++;
    _search.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    ref.listen(contentChangeProvider, (_, __) {
      _load();
    });
    final settings = ref.watch(contentSettingsProvider);
    final current = settings.isLoading || settings.hasError
        ? null
        : settings.valueOrNull;
    final shared = _mode == ContentMode.shared;
    final canChoose = !_filtered && (_mode == ContentMode.personal || shared);
    final selectedTopics = shared
        ? current?.sharedTopics
        : current?.personalTopics;
    return Scaffold(
      appBar: AppBar(
        title: Text(
          widget.title ??
              (widget.authorId != null
                  ? 'Автор'
                  : _mode == ContentMode.saved
                  ? 'Сохранённое'
                  : 'Интересное'),
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          if (_mode != ContentMode.saved)
            IconButton(
              tooltip: 'Сохранённое',
              icon: const Icon(Icons.bookmark_outline),
              onPressed: () => context.push(
                contentLocation(mode: ContentMode.saved, format: _format),
              ),
            ),
        ],
      ),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () async {
            ref.invalidate(contentSettingsProvider);
            await _load();
          },
          child: ListView(
            controller: _scroll,
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
            children: [
              if (widget.authorId != null)
                ContentAuthorHeader(authorId: widget.authorId!),
              if (!_filtered) ...[
                if (_mode == ContentMode.saved) ...[
                  const Text(
                    'Полезное на потом',
                    style: TextStyle(fontSize: 23, fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 8),
                ],
                Text(
                  _mode == ContentMode.saved
                      ? 'Видео и статьи, которые ты сохранил(а). Эта коллекция видна только тебе.'
                      : 'Видео, статьи и полезные идеи в одной подборке.',
                  style: const TextStyle(color: VColors.soft, height: 1.5),
                ),
                const SizedBox(height: 14),
                if (_mode == ContentMode.saved)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: TextButton.icon(
                      onPressed: () => _changeMode(ContentMode.personal),
                      icon: const Icon(Icons.arrow_back, size: 18),
                      label: const Text('Вернуться в «Интересное»'),
                    ),
                  )
                else
                  Wrap(
                    spacing: 8,
                    runSpacing: 6,
                    children: [
                      for (final mode in const [
                        ContentMode.personal,
                        ContentMode.following,
                        ContentMode.shared,
                      ])
                        ChoiceChip(
                          key: ValueKey('content-mode-${mode.name}'),
                          label: Text(mode.title),
                          selected: _mode == mode,
                          onSelected: (_) => _changeMode(mode),
                        ),
                    ],
                  ),
                const SizedBox(height: 12),
                Text(
                  shared
                      ? 'Здесь общие темы. Личные интересы партнёра сюда не попадают.'
                      : _mode == ContentMode.following
                      ? 'Материалы выбранных авторов на любые темы. Подписки видишь только ты.'
                      : 'Личные интересы и отметки видишь только ты.',
                  style: const TextStyle(
                    fontSize: 12,
                    color: VColors.muted,
                    height: 1.45,
                  ),
                ),
                if (_mode == ContentMode.following)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: TextButton.icon(
                      onPressed: () => context.push('/content/authors'),
                      icon: const Icon(Icons.people_outline, size: 18),
                      label: const Text('Все авторы'),
                    ),
                  ),
              ],
              const SizedBox(height: 14),
              Wrap(
                spacing: 8,
                runSpacing: 6,
                children: [
                  for (final format in ContentFormat.values)
                    ChoiceChip(
                      key: ValueKey('content-format-${format.name}'),
                      label: Text(format.title),
                      selected: _format == format,
                      onSelected:
                          current?.unifiedFeed == true ||
                              format == ContentFormat.all
                          ? (_) => _changeFormat(format)
                          : null,
                    ),
                ],
              ),
              if (current != null && !current.unifiedFeed)
                const Padding(
                  padding: EdgeInsets.only(top: 10),
                  child: Text(
                    'Видео и новые фильтры появятся после обновления подборки. Сейчас можно читать и сохранять доступные материалы.',
                    key: ValueKey('content-compatibility-notice'),
                    style: TextStyle(color: VColors.muted, height: 1.45),
                  ),
                ),
              if (!_filtered &&
                  _mode == ContentMode.personal &&
                  current?.unifiedFeed == true) ...[
                const SizedBox(height: 4),
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton.icon(
                    key: const ValueKey('content-discover'),
                    onPressed: _toggleDiscover,
                    icon: Icon(
                      _discover ? Icons.arrow_back : Icons.explore_outlined,
                      size: 18,
                    ),
                    label: Text(
                      _discover ? 'Вернуться к моим темам' : 'Открыть новое',
                    ),
                  ),
                ),
                if (_discover)
                  const Text(
                    'Сейчас показываем и другие темы. Это временный просмотр: твои интересы не меняются.',
                    key: ValueKey('content-discovery-notice'),
                    style: TextStyle(color: VColors.gold, height: 1.45),
                  ),
              ],
              if (widget.seriesId != null) ...[
                const Text(
                  'СЕРИЯ',
                  style: TextStyle(color: VColors.gold, letterSpacing: 1.2),
                ),
                const SizedBox(height: 6),
                Text(
                  widget.title ?? 'Публикации по порядку',
                  style: const TextStyle(
                    fontSize: 22,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 6),
                const Text(
                  'Все части серии показаны по порядку. Поиск работает внутри этой серии.',
                  style: TextStyle(color: VColors.soft),
                ),
              ],
              if (canChoose && current != null) ...[
                const SizedBox(height: 8),
                TextButton.icon(
                  onPressed: shared && !current.hasPair
                      ? null
                      : () => context.push(
                          '/content/preferences?scope=${shared ? 'shared' : 'personal'}',
                        ),
                  icon: const Icon(Icons.tune, size: 18),
                  label: Text(
                    selectedTopics!.isEmpty
                        ? (shared
                              ? 'Выбрать общие темы'
                              : 'Выбрать мои интересы')
                        : '${shared ? 'Общие темы' : 'Мои интересы'} (${selectedTopics.length})',
                  ),
                ),
              ],
              const SizedBox(height: 14),
              TextField(
                key: const ValueKey('content-search'),
                controller: _search,
                maxLength: 120,
                textInputAction: TextInputAction.search,
                onSubmitted: (_) => _submitSearch(),
                decoration: InputDecoration(
                  labelText: 'Поиск',
                  hintText: 'Название, тема или автор',
                  counterText: '',
                  prefixIcon: const Icon(Icons.search),
                  suffixIcon: IconButton(
                    tooltip: 'Искать',
                    onPressed: _submitSearch,
                    icon: const Icon(Icons.arrow_forward),
                  ),
                ),
              ),
              if (_query.isNotEmpty)
                Align(
                  alignment: Alignment.centerLeft,
                  child: TextButton(
                    onPressed: () {
                      _search.clear();
                      _submitSearch();
                    },
                    child: const Text('Сбросить поиск'),
                  ),
                ),
              if (_notice != null)
                Padding(
                  padding: const EdgeInsets.only(top: 14),
                  child: Text(
                    _notice!,
                    key: const ValueKey('content-notice'),
                    style: const TextStyle(color: VColors.gold),
                  ),
                ),
              if (_loading)
                const Padding(
                  padding: EdgeInsets.all(36),
                  child: Center(child: CircularProgressIndicator()),
                ),
              if (!_loading && _items.isEmpty && _error == null)
                _empty(current),
              for (final item in _items)
                Padding(
                  padding: const EdgeInsets.only(top: 16),
                  child: PublicationCard(
                    item: item,
                    onOpen: () => _open(item),
                    onHide: item.hidden ? null : () => _hide(item),
                  ),
                ),
              if (_error != null)
                ContentError(
                  error: _error!,
                  onRetry: () {
                    ref.invalidate(contentSettingsProvider);
                    _load(more: _items.isNotEmpty && _cursor != null);
                  },
                ),
              if (_loadingMore)
                const Padding(
                  padding: EdgeInsets.all(24),
                  child: Center(child: CircularProgressIndicator()),
                ),
              if (!_loading && !_loadingMore && _cursor != null)
                Padding(
                  padding: const EdgeInsets.only(top: 20),
                  child: OutlinedButton(
                    onPressed: () => _load(more: true),
                    child: const Text('Показать ещё'),
                  ),
                ),
              if (!_loading &&
                  _error == null &&
                  _items.isNotEmpty &&
                  _cursor == null)
                const Padding(
                  padding: EdgeInsets.only(top: 24),
                  child: Text(
                    'Это всё в этой подборке. Можно выбрать другие темы или открыть автора.',
                    style: TextStyle(color: VColors.muted, height: 1.4),
                  ),
                ),
              if (!_filtered && _mode == ContentMode.saved)
                Padding(
                  padding: const EdgeInsets.only(top: 18),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Divider(),
                      const Text(
                        'Ролики из старой подборки, которые ты сохранил(а) раньше, доступны в архиве.',
                        style: TextStyle(color: VColors.muted, height: 1.4),
                      ),
                      TextButton.icon(
                        key: const ValueKey('content-legacy-saved'),
                        onPressed: () =>
                            context.push('/videos?mode=saved&legacy=true'),
                        icon: const Icon(Icons.history, size: 18),
                        label: const Text('Архив сохранённых видео'),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _empty(ContentSettings? settings) {
    String title, text;
    if (_query.isNotEmpty) {
      title = 'По запросу ничего не найдено';
      text = _discover
          ? 'Попробуй другое слово, сбрось поиск или выбери другой формат.'
          : 'Попробуй другое слово или сбрось поиск. Поиск учитывает выбранные темы и формат.';
    } else if (_mode == ContentMode.shared &&
        (_noPair || settings?.hasPair == false)) {
      title = 'Подборка для двоих';
      text = 'После соединения пары здесь можно отдельно выбрать общие темы. Личная подборка уже доступна.';
    } else if (!_filtered &&
        !_discover &&
        (_mode == ContentMode.personal &&
                settings?.personalTopics.isEmpty == true ||
            _mode == ContentMode.shared &&
                settings?.sharedTopics.isEmpty == true)) {
      title = _mode == ContentMode.shared
          ? 'Что интересно вам обоим?'
          : 'Что интересно тебе?';
      text = 'Выбери темы выше. Подборка следует этому выбору и не подменяет его случайными публикациями.';
    } else if (_mode == ContentMode.following) {
      title = settings?.followedAuthorIds.isNotEmpty == true
          ? 'У этих авторов пока нет подходящих материалов'
          : 'Материалы любимых авторов';
      text = settings?.followedAuthorIds.isNotEmpty == true
          ? 'Попробуй выбрать «Всё» или вернись позже. Подписки сохранены.'
          : 'Подпишись на автора на его странице. Подписка видна только тебе.';
    } else if (_mode == ContentMode.saved) {
      title = 'То, к чему хочется вернуться';
      text = 'Нажми «Сохранить» у видео или статьи — материал появится здесь. Для всех форматов выбери «Всё».';
    } else {
      title = _format == ContentFormat.video
          ? 'Пока нет видео по этим условиям'
          : _format == ContentFormat.article
          ? 'Пока нет статей по этим условиям'
          : 'Пока нет подходящих материалов';
      text = 'Каталог пополняется. Можно выбрать другой формат, изменить темы и язык или открыть автора.';
    }
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 30),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: const TextStyle(fontSize: 21, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 10),
          Text(text, style: const TextStyle(color: VColors.soft, height: 1.5)),
          if (_mode == ContentMode.following)
            TextButton(
              onPressed: () => context.push('/content/authors'),
              child: const Text('Посмотреть авторов'),
            ),
        ],
      ),
    );
  }
}

class PublicationCard extends StatelessWidget {
  const PublicationCard({
    super.key,
    required this.item,
    required this.onOpen,
    this.onHide,
  });
  final Publication item;
  final VoidCallback onOpen;
  final VoidCallback? onHide;
  @override
  Widget build(BuildContext context) => Material(
    color: VColors.card,
    borderRadius: BorderRadius.circular(22),
    child: InkWell(
      borderRadius: BorderRadius.circular(22),
      onTap: onOpen,
      child: Padding(
        padding: const EdgeInsets.all(18),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    item.metadataLabel,
                    style: const TextStyle(color: VColors.gold, fontSize: 12),
                  ),
                ),
                if (item.saved)
                  const Padding(
                    padding: EdgeInsets.only(right: 8),
                    child: Icon(
                      Icons.bookmark,
                      size: 18,
                      color: VColors.lavender,
                    ),
                  ),
                if (onHide != null)
                  PopupMenuButton<String>(
                    tooltip: 'Действия с материалом',
                    padding: EdgeInsets.zero,
                    onSelected: (_) => onHide!(),
                    itemBuilder: (_) => [
                      const PopupMenuItem(
                        value: 'hide',
                        child: Text('Не показывать в подборках'),
                      ),
                    ],
                  ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              item.title,
              style: const TextStyle(
                fontSize: 21,
                height: 1.25,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 10),
            Text(
              item.summary,
              style: const TextStyle(color: VColors.soft, height: 1.5),
            ),
            if (item.recommendationReason?.isNotEmpty == true)
              Padding(
                padding: const EdgeInsets.only(top: 10),
                child: Text(
                  item.recommendationReason!,
                  key: ValueKey('content-reason-${item.id}'),
                  style: const TextStyle(
                    color: VColors.muted,
                    fontSize: 12,
                    height: 1.4,
                  ),
                ),
              ),
            const SizedBox(height: 14),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton(
                onPressed: () => context.push(
                  '/content/authors/${Uri.encodeComponent(item.author.id)}',
                ),
                style: TextButton.styleFrom(
                  foregroundColor: VColors.lavender,
                  padding: EdgeInsets.zero,
                  alignment: Alignment.centerLeft,
                ),
                child: Text(item.author.name),
              ),
            ),
            if (item.seriesTitle != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  'Серия «${item.seriesTitle}»${item.seriesOrder == null ? '' : ' · часть ${item.seriesOrder}'}',
                  style: const TextStyle(color: VColors.muted, fontSize: 12),
                ),
              ),
            const SizedBox(height: 14),
            Row(
              children: [
                Text(
                  item.isVideo ? 'Смотреть' : 'Читать',
                  style: const TextStyle(color: VColors.gold),
                ),
                const SizedBox(width: 6),
                Icon(
                  item.isVideo ? Icons.play_arrow : Icons.arrow_forward,
                  size: 17,
                  color: VColors.gold,
                ),
              ],
            ),
          ],
        ),
      ),
    ),
  );
}
