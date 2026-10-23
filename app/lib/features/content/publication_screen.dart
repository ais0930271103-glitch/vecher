import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/theme.dart';
import '../videos/videos.dart' show videoAccessProvider;
import 'content.dart';

class PublicationScreen extends ConsumerWidget {
  const PublicationScreen({super.key, required this.id});
  final String id;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final access = ref.watch(videoAccessProvider);
    if (!access.allowed) return const ContentUnavailable();
    return Scaffold(
      appBar: AppBar(title: const Text('Интересное')),
      body: SafeArea(
        child: ref
            .watch(publicationProvider(id))
            .when(
              skipLoadingOnReload: false,
              skipLoadingOnRefresh: true,
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (e, _) => ListView(
                padding: const EdgeInsets.all(20),
                children: [
                  ContentError(
                    error: e,
                    onRetry: () => ref.invalidate(publicationProvider(id)),
                  ),
                ],
              ),
              data: (item) => item == null
                  ? const SizedBox.shrink()
                  : _Reader(
                      key: ValueKey('${access.revision}:${item.id}'),
                      item: item,
                    ),
            ),
      ),
    );
  }
}

class _Reader extends ConsumerStatefulWidget {
  const _Reader({super.key, required this.item});
  final Publication item;
  @override
  ConsumerState<_Reader> createState() => _ReaderState();
}

class _ReaderState extends ConsumerState<_Reader>
    with ContentRequests<_Reader> {
  bool _busy = false;
  Object? _error;

  Future<void> _change(Map<String, dynamic> state) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final data = await contentRequest(
        'PATCH',
        '/content/${Uri.encodeComponent(widget.item.id)}/state',
        data: state,
      );
      if (!mounted || data == null) return;
      invalidateContent(ref);
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = error;
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
        });
      }
    }
  }

  Future<void> _source(ContentSource source) async {
    final uri = contentSourceUri(source.url);
    if (uri == null) return;
    try {
      final opened = await launchUrl(uri, mode: LaunchMode.externalApplication);
      if (!opened && mounted) {
        setState(() {
          _error = 'Не удалось открыть источник.';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _error = 'Не удалось открыть источник.';
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final item = widget.item;
    final settings = ref.watch(contentSettingsProvider);
    final hasPair =
        !settings.isLoading &&
        !settings.hasError &&
        settings.valueOrNull?.hasPair == true;
    return ListView(
      padding: const EdgeInsets.fromLTRB(22, 4, 22, 32),
      children: [
        Text(item.metadataLabel, style: const TextStyle(color: VColors.gold)),
        const SizedBox(height: 12),
        SelectableText(
          item.title,
          style: const TextStyle(
            fontSize: 29,
            fontWeight: FontWeight.w700,
            height: 1.22,
          ),
        ),
        const SizedBox(height: 10),
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton.icon(
            onPressed: () => context.push(
              '/content/authors/${Uri.encodeComponent(item.author.id)}',
            ),
            icon: const Icon(Icons.person_outline, size: 18),
            label: Text(item.author.name),
          ),
        ),
        if (item.publishedAt != null)
          Text(
            'Опубликовано ${_date(item.publishedAt!)}${item.updatedAt != null && _date(item.updatedAt!) != _date(item.publishedAt!) ? ' · обновлено ${_date(item.updatedAt!)}' : ''}',
            style: const TextStyle(color: VColors.muted, fontSize: 12),
          ),
        const SizedBox(height: 18),
        SelectableText(
          item.summary,
          style: const TextStyle(
            color: VColors.soft,
            fontSize: 18,
            height: 1.55,
          ),
        ),
        const Padding(
          padding: EdgeInsets.symmetric(vertical: 18),
          child: Divider(),
        ),
        for (final block in item.blocks) _block(block),
        if (item.videoId != null && item.kind == 'video')
          Padding(
            padding: const EdgeInsets.only(top: 14),
            child: OutlinedButton.icon(
              onPressed: () =>
                  context.push('/videos/${Uri.encodeComponent(item.videoId!)}'),
              icon: const Icon(Icons.play_arrow),
              label: const Text('Смотреть видео'),
            ),
          ),
        if (item.seriesId != null)
          Padding(
            padding: const EdgeInsets.only(top: 18),
            child: OutlinedButton.icon(
              onPressed: () => context.push(
                contentLocation(
                  seriesId: item.seriesId,
                  title: item.seriesTitle,
                ),
              ),
              icon: const Icon(Icons.library_books_outlined),
              label: Text('Серия: ${item.seriesTitle ?? 'Все части'}'),
            ),
          ),
        if (item.sources.isNotEmpty) ...[
          const SizedBox(height: 26),
          const Text(
            'Источники и материалы',
            style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 6),
          const Text(
            'Откроются в браузере по нажатию.',
            style: TextStyle(color: VColors.muted, fontSize: 12),
          ),
          for (final source in item.sources)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  onPressed: contentSourceUri(source.url) == null
                      ? null
                      : () => _source(source),
                  icon: const Icon(Icons.open_in_new, size: 16),
                  label: Text(source.title),
                ),
              ),
            ),
        ],
        const SizedBox(height: 24),
        const Text(
          'Оставить себе',
          style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 10),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            OutlinedButton.icon(
              onPressed: _busy ? null : () => _change({'useful': !item.useful}),
              icon: Icon(
                item.useful ? Icons.thumb_up : Icons.thumb_up_outlined,
              ),
              label: Text(item.useful ? 'Полезно · отмечено' : 'Полезно'),
            ),
            FilledButton.icon(
              onPressed: _busy ? null : () => _change({'saved': !item.saved}),
              icon: Icon(item.saved ? Icons.bookmark : Icons.bookmark_outline),
              label: Text(item.saved ? 'Убрать из сохранённого' : 'Сохранить'),
            ),
            TextButton(
              onPressed: _busy ? null : () => _change({'hidden': !item.hidden}),
              child: Text(
                item.hidden
                    ? 'Вернуть в подборки'
                    : 'Не показывать в подборках',
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        const Text(
          'Эти отметки помогают уточнять личную подборку. Партнёр их не видит.',
          style: TextStyle(color: VColors.muted, fontSize: 12),
        ),
        const SizedBox(height: 24),
        OutlinedButton.icon(
          onPressed: !hasPair || _busy
              ? null
              : () => context.push(
                  '/chat',
                  extra: {'draft_text': item.shareDraft},
                ),
          icon: const Icon(Icons.chat_bubble_outline),
          label: const Text('Обсудить с партнёром'),
        ),
        Text(
          hasPair
              ? 'В чат подставятся название и краткое описание. Отправку подтверждаешь ты.'
              : 'Обсуждение в чате станет доступно после соединения пары.',
          style: const TextStyle(
            color: VColors.muted,
            fontSize: 12,
            height: 1.5,
          ),
        ),
        if (_error != null)
          ContentError(
            error: _error!,
            onRetry: () {
              setState(() {
                _error = null;
              });
              ref.invalidate(publicationProvider(item.id));
            },
          ),
      ],
    );
  }

  Widget _block(ContentBlock block) {
    switch (block.type) {
      case 'heading':
        return Padding(
          padding: const EdgeInsets.only(top: 16, bottom: 10),
          child: SelectableText(
            block.text,
            style: const TextStyle(
              fontSize: 23,
              fontWeight: FontWeight.w600,
              height: 1.3,
            ),
          ),
        );
      case 'paragraph':
        return Padding(
          padding: const EdgeInsets.only(bottom: 16),
          child: SelectableText(
            block.text,
            style: const TextStyle(fontSize: 17, height: 1.6),
          ),
        );
      case 'list':
        return Padding(
          padding: const EdgeInsets.only(bottom: 16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (final text in block.items)
                Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Padding(
                        padding: EdgeInsets.only(top: 9, right: 12),
                        child: Icon(Icons.circle, size: 6, color: VColors.gold),
                      ),
                      Expanded(
                        child: SelectableText(
                          text,
                          style: const TextStyle(fontSize: 17, height: 1.6),
                        ),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        );
      default:
        return const SizedBox.shrink();
    }
  }

  String _date(DateTime value) =>
      '${value.day.toString().padLeft(2, '0')}.${value.month.toString().padLeft(2, '0')}.${value.year}';
}
