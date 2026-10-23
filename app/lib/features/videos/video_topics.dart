import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/theme.dart';
import 'videos.dart';

Future<void> showVideoTopics(BuildContext context, {bool shared = false}) =>
    showModalBottomSheet<void>(
      context: context,
      useRootNavigator: true,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => VideoTopicsSheet(shared: shared),
    );

class VideoTopicsSheet extends ConsumerStatefulWidget {
  const VideoTopicsSheet({super.key, this.shared = false});
  final bool shared;
  @override
  ConsumerState<VideoTopicsSheet> createState() => _VideoTopicsSheetState();
}

class _VideoTopicsSheetState extends ConsumerState<VideoTopicsSheet> {
  Set<String>? _selected;
  int? _revision;
  int? _accessRevision;
  bool _saving = false;
  String? _message;
  VideoRequest? _request;
  @override
  void dispose() {
    _request?.cancel.cancel();
    _request?.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_saving || _revision == null) return;
    final request = VideoRequest(ref.read(videoAccessProvider));
    if (!request.valid) {
      request.dispose();
      return;
    }
    _request = request;
    setState(() {
      _saving = true;
      _message = null;
    });
    try {
      await ref
          .read(dioProvider)
          .put(
            '/videos/settings/${widget.shared ? 'shared' : 'personal'}',
            queryParameters: videoPlaybackQuery,
            data: {
              'topics': _selected!.toList()..sort(),
              'revision': _revision,
            },
            cancelToken: request.cancel,
          );
      if (!mounted || !request.valid) return;
      invalidateVideos(ref);
      Navigator.of(context).pop();
    } catch (e) {
      if (!mounted || !request.valid) return;
      if (e is DioException && e.response?.statusCode == 409) {
        setState(() {
          _selected = null;
          _revision = null;
          _message = 'Темы изменились на другом телефоне. Мы загрузили новый выбор — проверь его перед сохранением.';
        });
        ref.invalidate(videoSettingsProvider);
      } else {
        setState(() => _message = videoErrorText(e));
      }
    } finally {
      if (identical(_request, request)) _request = null;
      request.dispose();
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final access = ref.watch(videoAccessProvider);
    if (_accessRevision != access.revision) {
      _accessRevision = access.revision;
      _selected = null;
      _revision = null;
      _message = null;
    }
    final settings = ref.watch(videoSettingsProvider);
    return SafeArea(
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.sizeOf(context).height * 0.85,
        ),
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(22, 0, 22, 22),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                widget.shared ? 'Что смотрим вместе?' : 'Что тебе интересно?',
                style: display(22),
              ),
              const SizedBox(height: 10),
              Text(
                widget.shared
                    ? 'В подборке «Для нас» будут только ролики выбранных общих тем. Эти темы видите и меняете вы оба. Личные интересы и отметки сюда не переносятся.'
                    : 'В подборке «Для меня» будут только ролики выбранных тем. Партнёр не увидит этот выбор, лайки и личные сохранения.',
                style: const TextStyle(color: VColors.soft, height: 1.4),
              ),
              const SizedBox(height: 18),
              if (_message != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Text(
                    _message!,
                    style: const TextStyle(color: VColors.coral),
                  ),
                ),
              if (!access.allowed)
                const Text('Сначала вернись в приложение.')
              else
                settings.when(
                  skipLoadingOnRefresh: false,
                  skipLoadingOnReload: false,
                  loading: () =>
                      const Center(child: CircularProgressIndicator()),
                  error: (e, _) => Column(
                    children: [
                      Text(videoErrorText(e)),
                      TextButton(
                        onPressed: () => ref.invalidate(videoSettingsProvider),
                        child: const Text('Повторить'),
                      ),
                    ],
                  ),
                  data: (settings) {
                    if (settings == null) return const SizedBox.shrink();
                    if (widget.shared && !settings.hasPair) {
                      return const Text(
                        'Общие темы появятся, когда вы соединитесь в пару.',
                      );
                    }
                    _selected ??=
                        (widget.shared
                                ? settings.sharedTopics
                                : settings.personalTopics)
                            .toSet();
                    _revision ??= widget.shared
                        ? settings.sharedRevision
                        : settings.personalRevision;
                    return Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            for (final topic in settings.topics)
                              FilterChip(
                                label: Text(
                                  topic.available
                                      ? topic.title
                                      : '${topic.title} · пока нет видео',
                                ),
                                selected: _selected!.contains(topic.id),
                                onSelected:
                                    _saving ||
                                        (!topic.available &&
                                            !_selected!.contains(topic.id))
                                    ? null
                                    : (selected) => setState(() {
                                        if (selected) {
                                          _selected!.add(topic.id);
                                        } else {
                                          _selected!.remove(topic.id);
                                        }
                                      }),
                              ),
                          ],
                        ),
                        const SizedBox(height: 12),
                        const Text(
                          'Можно оставить всё без выбора — покажем разные темы.',
                          style: TextStyle(color: VColors.muted, fontSize: 12),
                        ),
                        const SizedBox(height: 18),
                        FilledButton(
                          onPressed: _saving ? null : _save,
                          child: Text(
                            _saving
                                ? 'Сохраняем…'
                                : widget.shared
                                ? 'Сохранить общие темы'
                                : 'Сохранить мои интересы',
                          ),
                        ),
                      ],
                    );
                  },
                ),
              const SizedBox(height: 16),
              const Text(
                'В новой подборке — официальный плеер RUTUBE. '
                'Площадка получает обычные данные просмотра и может показывать рекламу. '
                'Личные отметки остаются в «Вечере». Ранее сохранённые видео сохраняют свой источник.',
                style: TextStyle(
                  color: VColors.muted,
                  fontSize: 11,
                  height: 1.4,
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
