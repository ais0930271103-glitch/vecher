import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import '../videos/videos.dart' show videoAccessProvider;
import 'content.dart';

class ContentPreferencesScreen extends ConsumerWidget {
  const ContentPreferencesScreen({super.key, this.shared = false});
  final bool shared;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final access = ref.watch(videoAccessProvider);
    if (!access.allowed) return const ContentUnavailable();
    return _Preferences(
      key: ValueKey('${access.revision}:$shared'),
      shared: shared,
    );
  }
}

class _Preferences extends ConsumerStatefulWidget {
  const _Preferences({super.key, required this.shared});
  final bool shared;
  @override
  ConsumerState<_Preferences> createState() => _PreferencesState();
}

class _PreferencesState extends ConsumerState<_Preferences>
    with ContentRequests<_Preferences> {
  ContentSettings? _settings;
  Set<String> _selected = {};
  String _language = 'ru';
  int _revision = 0;
  bool _loading = true, _saving = false;
  Object? _error;
  String? _notice;

  @override
  void initState() {
    super.initState();
    Future.microtask(() {
      if (mounted) _load();
    });
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
      _settings = null;
    });
    try {
      final data = await contentRequest('GET', '/content/settings');
      if (!mounted || data == null) return;
      final settings = ContentSettings.fromJson(data as Map);
      setState(() {
        _settings = settings;
        _selected =
            (widget.shared ? settings.sharedTopics : settings.personalTopics)
                .toSet();
        _language = widget.shared
            ? settings.sharedLanguage
            : settings.personalLanguage;
        _revision = widget.shared
            ? settings.sharedRevision
            : settings.personalRevision;
        _loading = false;
      });
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = error;
          _loading = false;
        });
      }
    }
  }

  Future<void> _save() async {
    if (_saving || _settings == null) return;
    setState(() {
      _saving = true;
      _error = null;
      _notice = null;
    });
    try {
      final data = await contentRequest(
        'PUT',
        '/content/settings/${widget.shared ? 'shared' : 'personal'}',
        data: {
          'topics': _selected.toList()..sort(),
          'language': _language,
          'revision': _revision,
        },
      );
      if (!mounted || data == null) return;
      invalidateContent(ref);
      if (context.canPop()) {
        context.pop();
      } else {
        _notice = 'Интересы сохранены.';
        await _load();
      }
    } on DioException catch (error) {
      if (!mounted) return;
      if (error.response?.statusCode == 409) {
        _notice = 'Настройки уже изменились на другом устройстве. Загружен актуальный выбор — проверь его перед сохранением.';
        await _load();
      } else {
        setState(() {
          _error = error;
        });
      }
    } catch (error) {
      if (mounted) {
        setState(() {
          _error = error;
        });
      }
    } finally {
      if (mounted) {
        setState(() {
          _saving = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final settings = _settings;
    final allowed = !widget.shared || settings?.hasPair == true;
    return Scaffold(
      appBar: AppBar(
        title: Text(widget.shared ? 'Общие темы' : 'Мои интересы'),
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(22),
          children: [
            Text(
              widget.shared
                  ? 'Что интересно вам обоим?'
                  : 'Собери свою подборку',
              style: const TextStyle(
                fontSize: 26,
                fontWeight: FontWeight.w700,
                height: 1.25,
              ),
            ),
            const SizedBox(height: 14),
            Text(
              widget.shared
                  ? 'Эти темы и язык видны вам обоим. Каждый может их изменить. Личные интересы сюда не копируются.'
                  : 'Темы, язык, подписки и отметки остаются личными. Партнёр увидит только то, что ты сам(а) отправишь в чат.',
              style: const TextStyle(
                color: VColors.soft,
                height: 1.5,
                fontSize: 16,
              ),
            ),
            const SizedBox(height: 12),
            const Text(
              'Видео и статьи используют эти темы и язык. Твои отметки «Полезно» и сохранения уточняют личную подборку. Время чтения и просмотра не используется для скрытого профиля интересов.',
              style: TextStyle(
                color: VColors.muted,
                height: 1.45,
                fontSize: 13,
              ),
            ),
            if (_notice != null)
              Padding(
                padding: const EdgeInsets.only(top: 20),
                child: Text(
                  _notice!,
                  key: const ValueKey('content-settings-notice'),
                  style: const TextStyle(color: VColors.gold, height: 1.5),
                ),
              ),
            if (_loading)
              const Padding(
                padding: EdgeInsets.all(30),
                child: Center(child: CircularProgressIndicator()),
              ),
            if (!_loading && settings != null) ...[
              if (!allowed)
                const Padding(
                  padding: EdgeInsets.only(top: 24),
                  child: Text(
                    'Сначала соедини пару. Пока можно настроить личные интересы.',
                  ),
                ),
              if (allowed) ...[
                const SizedBox(height: 26),
                const Text(
                  'Темы',
                  style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final topic in settings.topics)
                      FilterChip(
                        key: ValueKey('content-topic-${topic.id}'),
                        label: Text(
                          topic.available
                              ? topic.title
                              : '${topic.title} · пока нет публикаций',
                        ),
                        selected: _selected.contains(topic.id),
                        onSelected: _saving
                            ? null
                            : (selected) => setState(() {
                                if (selected) {
                                  _selected.add(topic.id);
                                } else {
                                  _selected.remove(topic.id);
                                }
                              }),
                      ),
                  ],
                ),
                if (_selected.isEmpty)
                  const Padding(
                    padding: EdgeInsets.only(top: 10),
                    child: Text(
                      'Без выбранных тем эта подборка будет пустой. Подписки и сохранённое остаются доступны.',
                      style: TextStyle(
                        color: VColors.muted,
                        fontSize: 13,
                        height: 1.4,
                      ),
                    ),
                  ),
                const SizedBox(height: 26),
                const Text(
                  'Язык публикаций',
                  style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 10),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final language in settings.languages)
                      ChoiceChip(
                        key: ValueKey('content-language-${language.id}'),
                        label: Text(language.title),
                        selected: _language == language.id,
                        onSelected: _saving
                            ? null
                            : (_) => setState(() {
                                _language = language.id;
                              }),
                      ),
                  ],
                ),
                const SizedBox(height: 28),
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
            ],
            if (_error != null)
              ContentError(
                error: _error!,
                onRetry: _settings == null ? _load : _save,
                retryLabel: _settings == null
                    ? 'Повторить загрузку'
                    : 'Повторить сохранение',
              ),
          ],
        ),
      ),
    );
  }
}
