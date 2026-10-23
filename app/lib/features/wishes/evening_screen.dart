import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/realtime_polling.dart';
import '../../core/realtime_provider.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
import '../chat/chat_screen.dart';
import '../today/paired_today.dart' show upcomingProvider;
import 'evenings.dart';

/// A short choice belongs to this evening, never to the pair's permanent profile.
Future<String?> _chooseMode(
  BuildContext context, {
  DateTime? scheduled,
}) => showModalBottomSheet<String>(
  context: context,
  backgroundColor: VColors.card,
  showDragHandle: true,
  builder: (context) => SafeArea(
    child: Padding(
      padding: const EdgeInsets.fromLTRB(24, 8, 24, 24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text(
            'Как вы сегодня?',
            style: TextStyle(fontSize: 22, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 8),
          const Text(
            'Сохраним это только для этого вечера. Шаги можно пропускать, а вечер — поставить на паузу.',
            style: TextStyle(color: VColors.soft),
          ),
          if (scheduled != null) ...[
            const SizedBox(height: 12),
            Text(
              'В плане: ${formatWhen(scheduled)}. Начнём сейчас и уберём прежнюю дату из ближайших планов.',
              style: const TextStyle(color: VColors.goldText),
            ),
          ],
          const SizedBox(height: 20),
          FilledButton.icon(
            onPressed: () => Navigator.pop(context, 'together'),
            icon: const Icon(Icons.people_outline),
            label: const Text('Рядом — начать'),
          ),
          const SizedBox(height: 8),
          OutlinedButton.icon(
            onPressed: () => Navigator.pop(context, 'apart'),
            icon: const Icon(Icons.video_call_outlined),
            label: const Text('Отдельно — начать'),
          ),
        ],
      ),
    ),
  ),
);

class EveningStartCard extends ConsumerStatefulWidget {
  const EveningStartCard({
    super.key,
    required this.cardId,
    required this.option,
    required this.onPlanChanged,
    this.scheduled,
  });
  final int cardId, option;
  final VoidCallback onPlanChanged;
  final DateTime? scheduled;

  @override
  ConsumerState<EveningStartCard> createState() => _EveningStartCardState();
}

class _EveningStartCardState extends ConsumerState<EveningStartCard> {
  bool _busy = false;
  String? _error, _retryId, _retryMode;
  int? _retryOption, _retryCard;
  int? _retryEpoch;

  Future<void> _start() async {
    if (_busy) return;
    final access = ref.read(eveningAccessProvider);
    final epoch = access.revision;
    final cardId = widget.cardId;
    final option = widget.option;
    if (!access.allowed) return;
    final mode = await _chooseMode(context, scheduled: widget.scheduled);
    if (!mounted ||
        mode == null ||
        !access.allowed ||
        access.revision != epoch) {
      return;
    }
    final request = EveningRequest(access, includeContentChanges: false);
    if (_retryMode != mode ||
        _retryCard != cardId ||
        _retryOption != option ||
        _retryEpoch != epoch) {
      _retryId = newEveningClientId();
      _retryMode = mode;
      _retryOption = option;
      _retryCard = cardId;
      _retryEpoch = epoch;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final result = await ref
          .read(dioProvider)
          .post(
            '/plans/$cardId/start',
            data: {'client_id': _retryId, 'option': option, 'mode': mode},
            cancelToken: request.cancel,
          );
      if (!mounted ||
          !request.valid ||
          widget.cardId != cardId ||
          widget.option != option) {
        return;
      }
      final evening = Evening.fromJson(result.data as Map);
      _retryId = _retryMode = null;
      _retryOption = _retryEpoch = _retryCard = null;
      invalidateEvenings(ref);
      ref.invalidate(upcomingProvider);
      widget.onPlanChanged();
      await context.push('/evenings/${evening.id}');
    } catch (e) {
      if (!mounted || !request.valid) return;
      if (e is DioException &&
          {403, 404, 409}.contains(e.response?.statusCode)) {
        widget.onPlanChanged();
        invalidateEvenings(ref);
        _error = '${errorText(e)} Обновили состояние — попробуй снова.';
      } else {
        _error = errorText(e);
      }
    } finally {
      request.dispose();
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    if (!ref.watch(eveningAccessProvider).allowed) {
      return const SizedBox.shrink();
    }
    final current = ref.watch(currentEveningProvider);
    final evening = current.isLoading ? null : current.valueOrNull;
    return Padding(
      padding: const EdgeInsets.only(bottom: 18),
      child: _EveningCard(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              evening == null
                  ? 'От плана — к вашему вечеру'
                  : 'У вас уже начат вечер',
              style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 6),
            Text(
              evening == null
                  ? 'Пройдите выбранный вариант в своём темпе. Прогресс сохранится у вас обоих.'
                  : '${evening.optionTitle}. Можно продолжить его или завершить перед новым.',
              style: const TextStyle(color: VColors.soft, height: 1.35),
            ),
            const SizedBox(height: 14),
            GoldButton(
              onPressed: _busy || current.isLoading
                  ? null
                  : evening == null
                  ? _start
                  : () => context.push('/evenings/${evening.id}'),
              child: _busy || current.isLoading
                  ? const SizedBox(
                      width: 22,
                      height: 22,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : Text(
                      evening == null
                          ? 'Начать вечер'
                          : 'Перейти к начатому вечеру',
                    ),
            ),
            if (_error != null) ...[
              const SizedBox(height: 10),
              Text(_error!, style: const TextStyle(color: VColors.coral)),
            ],
          ],
        ),
      ),
    );
  }
}

/// Kept near the top of Today so returning has an obvious, useful destination.
class CurrentEveningCard extends ConsumerWidget {
  const CurrentEveningCard({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (!ref.watch(eveningAccessProvider).allowed) {
      return const SizedBox.shrink();
    }
    final current = ref.watch(currentEveningProvider);
    final evening = current.isLoading ? null : current.valueOrNull;
    if (evening == null) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: _EveningCard(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              evening.paused ? 'ВАШ ВЕЧЕР НА ПАУЗЕ' : 'ВАШ ВЕЧЕР ПРОДОЛЖАЕТСЯ',
              style: const TextStyle(
                color: VColors.goldText,
                fontSize: 12,
                letterSpacing: 1.2,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              evening.optionTitle,
              style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 6),
            Text(
              '${evening.modeLabel} · ${evening.done + evening.skipped} из ${evening.steps.length} шагов',
              style: const TextStyle(color: VColors.soft),
            ),
            const SizedBox(height: 14),
            GoldButton(
              onPressed: () => context.push('/evenings/${evening.id}'),
              child: const Text('Продолжить вечер'),
            ),
          ],
        ),
      ),
    );
  }
}

class EveningScreen extends ConsumerStatefulWidget {
  const EveningScreen({super.key, required this.eveningId});
  final String eveningId;

  @override
  ConsumerState<EveningScreen> createState() => _EveningScreenState();
}

class _EveningScreenState extends ConsumerState<EveningScreen> {
  RealtimePoll? _poll;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _poll = RealtimePoll(
      controller: ref.read(realtimeControllerProvider),
      interval: const Duration(seconds: 15),
      onPoll: () => ref.invalidate(eveningProvider(widget.eveningId)),
    );
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _change(
    Evening evening,
    String action, [
    Map<String, dynamic> fields = const {},
  ]) async {
    if (_busy) return;
    final request = EveningRequest(
      ref.read(eveningAccessProvider),
      includeContentChanges: false,
    );
    if (!request.valid) {
      request.dispose();
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref
          .read(dioProvider)
          .post(
            '/evenings/${evening.id}/$action',
            data: {
              if (action != 'feedback') 'revision': evening.revision,
              ...fields,
            },
            cancelToken: request.cancel,
          );
      if (!mounted || !request.valid) return;
      invalidateEvenings(ref);
    } catch (e) {
      if (!mounted || !request.valid) return;
      if (e is DioException && e.response?.statusCode == 409) {
        invalidateEvenings(ref);
        _error = 'Вечер уже изменился на другом телефоне. Обновили шаги — выбери действие ещё раз.';
      } else {
        _error = errorText(e);
        if (e is DioException &&
            {401, 403, 404}.contains(e.response?.statusCode)) {
          invalidateEvenings(ref);
        }
      }
    } finally {
      request.dispose();
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _finish(Evening evening, {bool cancel = false}) async {
    final request = EveningRequest(ref.read(eveningAccessProvider));
    if (!request.valid) {
      request.dispose();
      return;
    }
    final accepted = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: VColors.card,
        title: Text(cancel ? 'Остановить этот вечер?' : 'Завершить вечер?'),
        content: Text(
          cancel
              ? 'Сохраним сделанные шаги в истории с отметкой «Остановлен». Позже можно выбрать другой вечер.'
              : evening.nextStep != null
              ? 'Оставшиеся шаги будут пропущены. Вечер и ваш прогресс сохранятся в истории.'
              : 'Сохраним ваш вечер в общей истории. Оставить личную оценку можно по желанию.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Вернуться'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: Text(cancel ? 'Остановить' : 'Завершить'),
          ),
        ],
      ),
    );
    final valid = request.valid;
    request.dispose();
    if (mounted && valid && accepted == true) {
      await _change(evening, cancel ? 'cancel' : 'complete');
    }
  }

  @override
  Widget build(BuildContext context) {
    final allowed = ref.watch(eveningAccessProvider).allowed;
    final state = ref.watch(eveningProvider(widget.eveningId));
    return Scaffold(
      body: SafeArea(
        child: !allowed
            ? const Center(
                child: Text('Вечер скрыт, пока приложение заблокировано.'),
              )
            : state.when(
                skipLoadingOnReload: false,
                loading: () => const Center(child: CircularProgressIndicator()),
                error: (e, _) => _EveningError(
                  error: e,
                  retry: () =>
                      ref.invalidate(eveningProvider(widget.eveningId)),
                ),
                data: (evening) => evening == null
                    ? const Center(
                        child: Text(
                          'Не удалось открыть этот вечер. Вернись к плану.',
                        ),
                      )
                    : RefreshIndicator(
                        onRefresh: () async {
                          ref.invalidate(eveningProvider(widget.eveningId));
                          await ref.read(
                            eveningProvider(widget.eveningId).future,
                          );
                        },
                        child: ListView(
                          padding: const EdgeInsets.fromLTRB(20, 8, 20, 32),
                          children: [
                            TopBar(
                              title: 'Наш вечер',
                              trailing: IconButton(
                                tooltip: 'Обсудить в чате',
                                onPressed: () => openChat(
                                  context,
                                  kind: 'plan',
                                  id: '${evening.cardId}',
                                  title: evening.title,
                                ),
                                icon: const Icon(
                                  Icons.chat_bubble_outline,
                                  color: VColors.goldText,
                                ),
                              ),
                            ),
                            const SizedBox(height: 16),
                            _EveningCard(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.stretch,
                                children: [
                                  Text(
                                    '${evening.modeLabel} · ${formatWhen(evening.startedAt)}',
                                    style: const TextStyle(
                                      color: VColors.goldText,
                                      fontSize: 13,
                                    ),
                                  ),
                                  const SizedBox(height: 10),
                                  Text(
                                    evening.optionTitle,
                                    style: const TextStyle(
                                      fontSize: 24,
                                      fontWeight: FontWeight.w600,
                                    ),
                                  ),
                                  const SizedBox(height: 6),
                                  Text(
                                    evening.title,
                                    style: const TextStyle(color: VColors.soft),
                                  ),
                                  if (evening.note.isNotEmpty) ...[
                                    const SizedBox(height: 10),
                                    Text(
                                      evening.note,
                                      style: const TextStyle(
                                        color: VColors.muted,
                                      ),
                                    ),
                                  ],
                                  const SizedBox(height: 18),
                                  if (evening.steps.isNotEmpty)
                                    LinearProgressIndicator(
                                      value:
                                          (evening.done + evening.skipped) /
                                          evening.steps.length,
                                      color: VColors.gold,
                                      backgroundColor: VColors.card2,
                                      borderRadius: BorderRadius.circular(4),
                                    ),
                                  const SizedBox(height: 8),
                                  Text(
                                    'Сделано: ${evening.done} · Пропущено: ${evening.skipped} · Всего: ${evening.steps.length}',
                                    style: const TextStyle(
                                      color: VColors.muted,
                                      fontSize: 13,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                            if (_error != null)
                              Padding(
                                padding: const EdgeInsets.symmetric(
                                  vertical: 12,
                                ),
                                child: Text(
                                  _error!,
                                  style: const TextStyle(color: VColors.coral),
                                ),
                              ),
                            const SizedBox(height: 18),
                            if (evening.ongoing)
                              ..._ongoing(evening)
                            else
                              ..._finished(evening),
                            if (evening.items.isNotEmpty) ...[
                              const SizedBox(height: 16),
                              ExpansionTile(
                                tilePadding: EdgeInsets.zero,
                                title: Text(
                                  evening.itemsTitle ?? 'Понадобится',
                                ),
                                children: [
                                  for (final item in evening.items)
                                    ListTile(
                                      leading: const Icon(
                                        Icons.checklist,
                                        color: VColors.lavender,
                                      ),
                                      title: Text(item),
                                      contentPadding: EdgeInsets.zero,
                                    ),
                                ],
                              ),
                            ],
                            if (evening.steps.any((s) => !s.pending)) ...[
                              const SizedBox(height: 12),
                              ExpansionTile(
                                tilePadding: EdgeInsets.zero,
                                title: const Text('Пройденные шаги'),
                                children: [
                                  for (final step in evening.steps.where(
                                    (s) => !s.pending,
                                  ))
                                    ListTile(
                                      contentPadding: EdgeInsets.zero,
                                      leading: Icon(
                                        step.status == 'done'
                                            ? Icons.check_circle_outline
                                            : Icons.skip_next_outlined,
                                        color: step.status == 'done'
                                            ? VColors.gold
                                            : VColors.muted,
                                      ),
                                      title: Text(step.text),
                                      subtitle: Text(
                                        step.status == 'done'
                                            ? 'Сделали'
                                            : 'Пропустили',
                                      ),
                                      trailing: evening.status == 'active'
                                          ? TextButton(
                                              onPressed: _busy
                                                  ? null
                                                  : () => _change(
                                                      evening,
                                                      'step',
                                                      {
                                                        'index': step.index,
                                                        'status': 'pending',
                                                      },
                                                    ),
                                              child: const Text('Вернуть'),
                                            )
                                          : null,
                                    ),
                                ],
                              ),
                            ],
                          ],
                        ),
                      ),
              ),
      ),
    );
  }

  List<Widget> _ongoing(Evening evening) {
    final step = evening.nextStep;
    return [
      if (evening.paused)
        _EveningCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Icon(
                Icons.pause_circle_outline,
                color: VColors.gold,
                size: 42,
              ),
              const SizedBox(height: 10),
              const Text(
                'Можно не торопиться',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 6),
              const Text(
                'Прогресс сохранён. Продолжите, когда обоим будет удобно.',
                textAlign: TextAlign.center,
                style: TextStyle(color: VColors.soft),
              ),
              const SizedBox(height: 16),
              GoldButton(
                onPressed: _busy ? null : () => _change(evening, 'resume'),
                child: const Text('Возобновить вечер'),
              ),
            ],
          ),
        )
      else if (step != null)
        _EveningCard(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                'ШАГ ${step.index + 1} ИЗ ${evening.steps.length}',
                style: const TextStyle(
                  color: VColors.goldText,
                  fontSize: 12,
                  letterSpacing: 1.2,
                ),
              ),
              const SizedBox(height: 14),
              Text(
                step.text,
                style: const TextStyle(fontSize: 20, height: 1.45),
              ),
              const SizedBox(height: 20),
              GoldButton(
                onPressed: _busy
                    ? null
                    : () => _change(evening, 'step', {
                        'index': step.index,
                        'status': 'done',
                      }),
                child: const Text('Сделали'),
              ),
              TextButton(
                onPressed: _busy
                    ? null
                    : () => _change(evening, 'step', {
                        'index': step.index,
                        'status': 'skipped',
                      }),
                child: const Text('Пропустить этот шаг'),
              ),
              const Text(
                'Можно пропустить всё, что кому-то из вас не подходит.',
                textAlign: TextAlign.center,
                style: TextStyle(color: VColors.muted, fontSize: 12),
              ),
            ],
          ),
        )
      else if (evening.steps.isEmpty)
        const _EveningCard(
          child: Column(
            children: [
              Icon(Icons.wb_twilight_outlined, size: 42, color: VColors.gold),
              SizedBox(height: 10),
              Text(
                'Вечер в вашем ритме',
                style: TextStyle(fontSize: 21, fontWeight: FontWeight.w600),
              ),
              SizedBox(height: 6),
              Text(
                'В этом варианте нет отдельных шагов. Следуйте описанию и завершите вечер, когда будете готовы сохранить его в истории.',
                style: TextStyle(color: VColors.soft),
              ),
            ],
          ),
        )
      else
        const _EveningCard(
          child: Column(
            children: [
              Icon(Icons.done_all, size: 42, color: VColors.gold),
              SizedBox(height: 10),
              Text(
                'Все шаги пройдены',
                style: TextStyle(fontSize: 21, fontWeight: FontWeight.w600),
              ),
              SizedBox(height: 6),
              Text(
                'Завершите вечер, чтобы сохранить его в истории.',
                style: TextStyle(color: VColors.soft),
              ),
            ],
          ),
        ),
      const SizedBox(height: 16),
      if (!evening.paused)
        OutlinedButton.icon(
          onPressed: _busy ? null : () => _change(evening, 'pause'),
          icon: const Icon(Icons.pause),
          label: const Text('Поставить на паузу'),
        ),
      FilledButton.icon(
        onPressed: _busy ? null : () => _finish(evening),
        icon: const Icon(Icons.check),
        label: const Text('Завершить вечер'),
      ),
      TextButton(
        onPressed: _busy ? null : () => _finish(evening, cancel: true),
        child: const Text('Остановить без завершения'),
      ),
    ];
  }

  List<Widget> _finished(Evening evening) => [
    _EveningCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Icon(
            evening.completed
                ? Icons.favorite_outline
                : Icons.stop_circle_outlined,
            color: VColors.gold,
            size: 40,
          ),
          const SizedBox(height: 12),
          Text(
            evening.completed ? 'Ваш вечер сохранён' : 'Вечер остановлен',
            textAlign: TextAlign.center,
            style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 8),
          const Text(
            'Эта история остаётся только у вашей пары.',
            textAlign: TextAlign.center,
            style: TextStyle(color: VColors.soft),
          ),
          const SizedBox(height: 16),
          OutlinedButton(
            onPressed: () => context.push('/wishes/plan/${evening.cardId}'),
            child: const Text('Выбрать этот план ещё раз'),
          ),
          const Text(
            'Откроем план: новый вечер начнётся только после вашего выбора.',
            textAlign: TextAlign.center,
            style: TextStyle(color: VColors.muted, fontSize: 12),
          ),
        ],
      ),
    ),
    if (evening.completed) ...[
      const SizedBox(height: 18),
      _FeedbackCard(
        key: ValueKey(evening.id),
        feedback: evening.myFeedback,
        busy: _busy,
        onSave: (feeling, repeat) => _change(evening, 'feedback', {
          'feeling': feeling,
          'want_repeat': repeat,
        }),
      ),
    ],
  ];
}

class _FeedbackCard extends StatefulWidget {
  const _FeedbackCard({
    super.key,
    required this.feedback,
    required this.busy,
    required this.onSave,
  });
  final EveningFeedback? feedback;
  final bool busy;
  final void Function(String, bool) onSave;

  @override
  State<_FeedbackCard> createState() => _FeedbackCardState();
}

class _FeedbackCardState extends State<_FeedbackCard> {
  String? _feeling;
  bool _repeat = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _load() {
    _feeling = widget.feedback?.feeling;
    _repeat = widget.feedback?.wantRepeat ?? false;
  }

  @override
  void didUpdateWidget(_FeedbackCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.feedback?.feeling != widget.feedback?.feeling ||
        oldWidget.feedback?.wantRepeat != widget.feedback?.wantRepeat) {
      _load();
    }
  }

  @override
  Widget build(BuildContext context) {
    final unchanged =
        _feeling == widget.feedback?.feeling &&
        _repeat == widget.feedback?.wantRepeat;
    return _EveningCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text(
            'Как тебе этот вечер?',
            style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 6),
          const Text(
            'По желанию. Оценка видна только тебе, партнёр её не увидит.',
            style: TextStyle(color: VColors.muted),
          ),
          const SizedBox(height: 14),
          Wrap(
            spacing: 8,
            runSpacing: 6,
            children: [
              for (final choice in const [
                ('liked', 'Понравилось'),
                ('neutral', 'Нормально'),
                ('not_for_me', 'Не моё'),
              ])
                ChoiceChip(
                  label: Text(choice.$2),
                  selected: _feeling == choice.$1,
                  onSelected: widget.busy
                      ? null
                      : (_) => setState(() => _feeling = choice.$1),
                ),
            ],
          ),
          CheckboxListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('Мне хочется повторить'),
            value: _repeat,
            onChanged: widget.busy
                ? null
                : (value) => setState(() => _repeat = value ?? false),
          ),
          OutlinedButton(
            onPressed: widget.busy || _feeling == null || unchanged
                ? null
                : () => widget.onSave(_feeling!, _repeat),
            child: Text(
              unchanged && widget.feedback != null
                  ? 'Твоя оценка сохранена'
                  : 'Сохранить только для себя',
            ),
          ),
        ],
      ),
    );
  }
}

class EveningHistoryScreen extends ConsumerStatefulWidget {
  const EveningHistoryScreen({super.key});

  @override
  ConsumerState<EveningHistoryScreen> createState() =>
      _EveningHistoryScreenState();
}

class _EveningHistoryScreenState extends ConsumerState<EveningHistoryScreen> {
  final List<String?> _pages = [null];
  int? _epoch;
  RealtimePoll? _poll;

  @override
  void initState() {
    super.initState();
    _poll = RealtimePoll(
      controller: ref.read(realtimeControllerProvider),
      interval: const Duration(seconds: 30),
      onPoll: () => ref.invalidate(eveningHistoryProvider),
    );
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final access = ref.watch(eveningAccessProvider);
    if (_epoch != access.revision) {
      _epoch = access.revision;
      _pages.clear();
      _pages.add(null);
    }
    final history = ref.watch(eveningHistoryProvider(_pages.last));
    return Scaffold(
      appBar: AppBar(title: const Text('Наши вечера')),
      body: !access.allowed
          ? const SizedBox.shrink()
          : history.when(
              skipLoadingOnReload: false,
              loading: () => const Center(child: CircularProgressIndicator()),
              error: (e, _) => _EveningError(
                error: e,
                retry: () =>
                    ref.invalidate(eveningHistoryProvider(_pages.last)),
              ),
              data: (page) => RefreshIndicator(
                onRefresh: () async {
                  setState(() {
                    _pages.clear();
                    _pages.add(null);
                  });
                  ref.invalidate(eveningHistoryProvider);
                  await ref.read(eveningHistoryProvider(null).future);
                },
                child: ListView(
                  padding: const EdgeInsets.all(20),
                  children: [
                    const Text(
                      'Моменты, которые вы прожили вместе — рядом или отдельно.',
                      style: TextStyle(color: VColors.soft),
                    ),
                    const SizedBox(height: 18),
                    if (!page.supported)
                      const Text(
                        'История станет доступна после обновления сервиса. Ваши планы уже есть в разделе «Вместе».',
                        style: TextStyle(color: VColors.muted),
                      )
                    else if (page.items.isEmpty)
                      const _EveningCard(
                        child: Column(
                          children: [
                            Icon(
                              Icons.nights_stay_outlined,
                              color: VColors.gold,
                              size: 42,
                            ),
                            SizedBox(height: 12),
                            Text(
                              'Здесь будут ваши вечера',
                              style: TextStyle(
                                fontSize: 20,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                            SizedBox(height: 8),
                            Text(
                              'Выберите совпадение во «Вместе», откройте план и начните его. Завершённые и остановленные вечера сохранятся здесь.',
                              textAlign: TextAlign.center,
                              style: TextStyle(color: VColors.soft),
                            ),
                          ],
                        ),
                      )
                    else
                      for (final evening in page.items)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 12),
                          child: _EveningCard(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                Text(
                                  '${formatWhen(evening.completedAt ?? evening.updatedAt)} · ${evening.modeLabel}',
                                  style: const TextStyle(
                                    color: VColors.muted,
                                    fontSize: 13,
                                  ),
                                ),
                                const SizedBox(height: 8),
                                Text(
                                  evening.optionTitle,
                                  style: const TextStyle(
                                    fontSize: 19,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                                const SizedBox(height: 6),
                                Text(
                                  evening.completed
                                      ? 'Завершён · Сделано ${evening.done} шагов'
                                      : 'Остановлен · Сделано ${evening.done} шагов',
                                  style: const TextStyle(color: VColors.soft),
                                ),
                                const SizedBox(height: 8),
                                TextButton(
                                  onPressed: () =>
                                      context.push('/evenings/${evening.id}'),
                                  child: const Text('Открыть этот вечер'),
                                ),
                              ],
                            ),
                          ),
                        ),
                    if (_pages.length > 1)
                      OutlinedButton(
                        onPressed: () => setState(() => _pages.removeLast()),
                        child: const Text('Более новые вечера'),
                      ),
                    if (page.nextBefore != null)
                      OutlinedButton(
                        onPressed: () =>
                            setState(() => _pages.add(page.nextBefore)),
                        child: const Text('Более ранние вечера'),
                      ),
                  ],
                ),
              ),
            ),
    );
  }
}

class _EveningCard extends StatelessWidget {
  const _EveningCard({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Material(
    color: VColors.card,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(22),
      side: const BorderSide(color: Color(0x33F3C98B)),
    ),
    child: Padding(padding: const EdgeInsets.all(20), child: child),
  );
}

class _EveningError extends StatelessWidget {
  const _EveningError({required this.error, required this.retry});
  final Object error;
  final VoidCallback retry;

  @override
  Widget build(BuildContext context) => ListView(
    padding: const EdgeInsets.all(24),
    children: [
      const SizedBox(height: 24),
      Text(errorText(error), style: const TextStyle(color: VColors.coral)),
      const SizedBox(height: 16),
      OutlinedButton(
        onPressed: retry,
        child: const Text('Попробовать ещё раз'),
      ),
      TextButton(
        onPressed: () => Navigator.maybePop(context),
        child: const Text('Вернуться'),
      ),
    ],
  );
}
