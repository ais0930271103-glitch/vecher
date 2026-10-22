import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/theme.dart';
import '../videos/videos.dart' show videoAccessProvider;
import 'content.dart';

class ContentAuthorsScreen extends ConsumerWidget {
  const ContentAuthorsScreen({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final access = ref.watch(videoAccessProvider);
    if (!access.allowed) return const ContentUnavailable();
    final authors = ref.watch(contentAuthorsProvider);
    return Scaffold(
      appBar: AppBar(title: const Text('Авторы')),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () async {
            ref.invalidate(contentAuthorsProvider);
            try {
              await ref.read(contentAuthorsProvider.future);
            } catch (_) {}
          },
          child: ListView(
            padding: const EdgeInsets.all(20),
            physics: const AlwaysScrollableScrollPhysics(),
            children: [
              const Text(
                'Чей голос тебе близок?',
                style: TextStyle(fontSize: 25, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 10),
              const Text(
                'Здесь указаны реальные авторы каталога. Подписки помогают собрать свою подборку и видны только тебе.',
                style: TextStyle(color: VColors.soft, height: 1.5),
              ),
              const SizedBox(height: 20),
              authors.when(
                skipLoadingOnReload: false,
                skipLoadingOnRefresh: false,
                loading: () => const Center(child: CircularProgressIndicator()),
                error: (e, _) => ContentError(
                  error: e,
                  onRetry: () => ref.invalidate(contentAuthorsProvider),
                ),
                data: (items) => Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    if (items.isEmpty)
                      const Text(
                        'Авторы появятся вместе с первыми публикациями.',
                      ),
                    for (final author in items)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 14),
                        child: Material(
                          color: VColors.card,
                          borderRadius: BorderRadius.circular(22),
                          child: InkWell(
                            borderRadius: BorderRadius.circular(22),
                            onTap: () => context.push(
                              '/content/authors/${Uri.encodeComponent(author.id)}',
                            ),
                            child: Padding(
                              padding: const EdgeInsets.all(18),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    author.name,
                                    style: const TextStyle(
                                      fontSize: 21,
                                      fontWeight: FontWeight.w600,
                                    ),
                                  ),
                                  const SizedBox(height: 8),
                                  Text(
                                    author.bio,
                                    style: const TextStyle(
                                      color: VColors.soft,
                                      height: 1.45,
                                    ),
                                  ),
                                  const SizedBox(height: 12),
                                  Text(
                                    '${author.publishedCount} публикаций${author.followed ? ' · Ты подписан(а)' : ''}${author.hidden ? ' · Скрыт из подборок' : ''}',
                                    style: const TextStyle(
                                      color: VColors.lavender,
                                      fontSize: 12,
                                    ),
                                  ),
                                  if (author.hidden)
                                    const Padding(
                                      padding: EdgeInsets.only(top: 8),
                                      child: Text(
                                        'Открой страницу, чтобы снова показывать автора.',
                                        style: TextStyle(color: VColors.muted),
                                      ),
                                    ),
                                ],
                              ),
                            ),
                          ),
                        ),
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
}

class ContentAuthorHeader extends ConsumerStatefulWidget {
  const ContentAuthorHeader({super.key, required this.authorId});
  final String authorId;
  @override
  ConsumerState<ContentAuthorHeader> createState() =>
      _ContentAuthorHeaderState();
}

class _ContentAuthorHeaderState extends ConsumerState<ContentAuthorHeader>
    with ContentRequests<ContentAuthorHeader> {
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
        '/content/authors/${Uri.encodeComponent(widget.authorId)}/state',
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

  Future<void> _source(String url) async {
    final uri = contentSourceUri(url);
    if (uri == null) return;
    try {
      final opened = await launchUrl(uri, mode: LaunchMode.externalApplication);
      if (!opened && mounted) {
        setState(() {
          _error = 'Не удалось открыть сайт автора.';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _error = 'Не удалось открыть сайт автора.';
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => ref
      .watch(contentAuthorProvider(widget.authorId))
      .when(
        skipLoadingOnReload: false,
        skipLoadingOnRefresh: true,
        loading: () => const Padding(
          padding: EdgeInsets.all(20),
          child: Center(child: CircularProgressIndicator()),
        ),
        error: (e, _) => ContentError(
          error: e,
          onRetry: () => ref.invalidate(contentAuthorProvider(widget.authorId)),
        ),
        data: (author) => author == null
            ? const SizedBox.shrink()
            : Padding(
                padding: const EdgeInsets.only(bottom: 20),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      'АВТОР',
                      style: TextStyle(
                        color: VColors.gold,
                        letterSpacing: 1.2,
                        fontSize: 12,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      author.name,
                      style: const TextStyle(
                        fontSize: 27,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 12),
                    Text(
                      author.bio,
                      style: const TextStyle(
                        color: VColors.soft,
                        height: 1.5,
                        fontSize: 16,
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      '${author.publishedCount} публикаций',
                      style: const TextStyle(color: VColors.muted),
                    ),
                    const SizedBox(height: 12),
                    Wrap(
                      spacing: 8,
                      runSpacing: 6,
                      children: [
                        FilledButton.icon(
                          onPressed: _busy
                              ? null
                              : () => _change({'followed': !author.followed}),
                          icon: Icon(author.followed ? Icons.check : Icons.add),
                          label: Text(
                            author.followed ? 'Отписаться' : 'Подписаться',
                          ),
                        ),
                        OutlinedButton(
                          onPressed: _busy
                              ? null
                              : () => _change({'hidden': !author.hidden}),
                          child: Text(
                            author.hidden
                                ? 'Вернуть в подборки'
                                : 'Скрыть автора',
                          ),
                        ),
                        if (contentSourceUri(author.url) != null)
                          TextButton.icon(
                            onPressed: () => _source(author.url!),
                            icon: const Icon(Icons.open_in_new, size: 16),
                            label: const Text('Сайт автора'),
                          ),
                      ],
                    ),
                    const SizedBox(height: 8),
                    Text(
                      author.hidden
                          ? 'Автор скрыт из твоих подборок. На этой странице публикации доступны по твоему выбору.'
                          : 'Подписка и скрытие — твой личный выбор. Партнёр его не увидит.',
                      style: const TextStyle(
                        color: VColors.muted,
                        fontSize: 12,
                        height: 1.45,
                      ),
                    ),
                    if (_error != null)
                      ContentError(
                        error: _error!,
                        onRetry: () {
                          setState(() {
                            _error = null;
                          });
                          ref.invalidate(
                            contentAuthorProvider(widget.authorId),
                          );
                        },
                      ),
                  ],
                ),
              ),
      );
}
