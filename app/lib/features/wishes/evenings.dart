import 'dart:async';
import 'dart:math';

import 'package:dio/dio.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/privacy.dart';
import '../../core/realtime_provider.dart';
import '../../core/session_changes.dart';
import '../today/today_screen.dart' show pairProvider;

class EveningStep {
  const EveningStep({
    required this.index,
    required this.text,
    required this.status,
  });

  factory EveningStep.fromJson(Map data) => EveningStep(
    index: data['index'] as int,
    text: data['text'] as String,
    status: data['status'] as String,
  );

  final int index;
  final String text;
  final String status;
  bool get pending => status == 'pending';
}

class EveningFeedback {
  const EveningFeedback({required this.feeling, required this.wantRepeat});

  factory EveningFeedback.fromJson(Map data) => EveningFeedback(
    feeling: data['feeling'] as String,
    wantRepeat: data['want_repeat'] == true,
  );

  final String feeling;
  final bool wantRepeat;
}

class Evening {
  const Evening({
    required this.id,
    required this.cardId,
    required this.title,
    required this.optionIndex,
    required this.optionTitle,
    required this.mode,
    required this.status,
    required this.revision,
    required this.steps,
    required this.items,
    required this.note,
    required this.startedAt,
    required this.updatedAt,
    this.itemsTitle,
    this.completedAt,
    this.myFeedback,
  });

  factory Evening.fromJson(Map data) => Evening(
    id: data['id'] as String,
    cardId: data['card_id'] as int,
    title: data['title'] as String,
    optionIndex: data['option_index'] as int,
    optionTitle: data['option_title'] as String,
    mode: data['mode'] as String,
    status: data['status'] as String,
    revision: data['revision'] as int,
    steps: (data['steps'] as List)
        .map((s) => EveningStep.fromJson(s as Map))
        .toList(growable: false),
    items: (data['items'] as List? ?? const []).cast<String>(),
    itemsTitle: data['items_title'] as String?,
    note: data['note'] as String? ?? '',
    startedAt: DateTime.parse(data['started_at'] as String),
    updatedAt: DateTime.parse(data['updated_at'] as String),
    completedAt: data['completed_at'] == null
        ? null
        : DateTime.parse(data['completed_at'] as String),
    myFeedback: data['my_feedback'] == null
        ? null
        : EveningFeedback.fromJson(data['my_feedback'] as Map),
  );

  final String id, title, optionTitle, mode, status, note;
  final int cardId, optionIndex, revision;
  final List<EveningStep> steps;
  final List<String> items;
  final String? itemsTitle;
  final DateTime startedAt, updatedAt;
  final DateTime? completedAt;
  final EveningFeedback? myFeedback;

  bool get ongoing => status == 'active' || status == 'paused';
  bool get paused => status == 'paused';
  bool get completed => status == 'completed';
  int get done => steps.where((s) => s.status == 'done').length;
  int get skipped => steps.where((s) => s.status == 'skipped').length;
  EveningStep? get nextStep => steps.where((s) => s.pending).firstOrNull;
  String get modeLabel => mode == 'apart' ? 'Отдельно' : 'Рядом';
}

class EveningHistory {
  const EveningHistory({
    required this.items,
    this.nextBefore,
    this.supported = true,
  });
  final List<Evening> items;
  final String? nextBefore;
  final bool supported;
}

/// An epoch contains no private data. Every account, pair or PIN change cancels
/// in-flight reads and writes before a late response can reach another session.
class EveningAccess extends ChangeNotifier with WidgetsBindingObserver {
  EveningAccess(Stream<Set<String>> invalidations) {
    sessionChanges.addListener(changed);
    PinGate.lockedListenable.addListener(changed);
    PinGate.readyListenable.addListener(changed);
    WidgetsBinding.instance.addObserver(this);
    _updates = invalidations.listen((scopes) {
      if (scopes.any({'pair', 'all'}.contains)) {
        changed();
      } else if (scopes.contains('wishes')) {
        _revision++;
        notifyListeners();
      }
    });
  }

  int _revision = 0;
  int _sessionRevision = 0;
  late final StreamSubscription<Set<String>> _updates;
  int get revision => _revision;
  int get sessionRevision => _sessionRevision;
  bool get allowed =>
      PinGate.isReady &&
      !PinGate.isLocked &&
      !sessionChanges.isChanging &&
      (WidgetsBinding.instance.lifecycleState ?? AppLifecycleState.resumed) ==
          AppLifecycleState.resumed;

  void changed() {
    _revision++;
    _sessionRevision++;
    notifyListeners();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) => changed();

  @override
  void dispose() {
    sessionChanges.removeListener(changed);
    PinGate.lockedListenable.removeListener(changed);
    PinGate.readyListenable.removeListener(changed);
    WidgetsBinding.instance.removeObserver(this);
    unawaited(_updates.cancel());
    super.dispose();
  }
}

final eveningAccessProvider = ChangeNotifierProvider.autoDispose<EveningAccess>(
  (ref) {
    final access = EveningAccess(
      ref.watch(realtimeControllerProvider).invalidations,
    );
    ref.listen(pairProvider, (previous, next) {
      // A local leave/join invalidates pairProvider even without a live socket.
      if (previous != null && previous != next) access.changed();
    });
    return access;
  },
);

class EveningRequest {
  EveningRequest(this.access, {this.includeContentChanges = true})
    : revision = includeContentChanges
          ? access.revision
          : access.sessionRevision {
    access.addListener(_changed);
    if (!access.allowed) cancel.cancel('Private session unavailable');
  }
  final EveningAccess access;
  // Mutations never render their snapshot. A content invalidation may be the
  // acknowledgement of our own write; its response only triggers a new GET.
  final bool includeContentChanges;
  final int revision;
  final cancel = CancelToken();
  int get _currentRevision =>
      includeContentChanges ? access.revision : access.sessionRevision;
  bool get valid =>
      !cancel.isCancelled && access.allowed && _currentRevision == revision;
  void _changed() {
    if (!access.allowed || _currentRevision != revision) {
      cancel.cancel('Private session changed');
    }
  }

  void dispose() => access.removeListener(_changed);
}

Future<dynamic> _get(
  Ref ref,
  String path, {
  Map<String, dynamic>? query,
}) async {
  final access = ref.watch(eveningAccessProvider);
  if (!access.allowed) return null;
  final request = EveningRequest(access);
  ref.onDispose(() {
    request.cancel.cancel();
    request.dispose();
  });
  final response = await ref
      .read(dioProvider)
      .get(path, queryParameters: query, cancelToken: request.cancel);
  if (!request.valid) return null;
  return response.data;
}

final currentEveningProvider = FutureProvider.autoDispose<Evening?>((
  ref,
) async {
  try {
    final data = await _get(ref, '/evenings/current');
    return data == null ? null : Evening.fromJson(data as Map);
  } on DioException catch (e) {
    // APK9 can run on the previous server while the API rollout is in progress.
    if ({403, 404, 409}.contains(e.response?.statusCode)) return null;
    rethrow;
  }
});

final eveningProvider = FutureProvider.autoDispose.family<Evening?, String>((
  ref,
  id,
) async {
  final data = await _get(ref, '/evenings/$id');
  return data == null ? null : Evening.fromJson(data as Map);
});

final eveningHistoryProvider = FutureProvider.autoDispose
    .family<EveningHistory, String?>((ref, before) async {
      try {
        final data = await _get(
          ref,
          '/evenings/history',
          query: {'limit': 20, if (before != null) 'before': before},
        );
        if (data == null) return const EveningHistory(items: []);
        final page = data as Map;
        return EveningHistory(
          items: (page['items'] as List)
              .map((e) => Evening.fromJson(e as Map))
              .toList(growable: false),
          nextBefore: page['next_before'] as String?,
        );
      } on DioException catch (e) {
        if (e.response?.statusCode == 404) {
          return const EveningHistory(items: [], supported: false);
        }
        rethrow;
      }
    });

void invalidateEvenings(WidgetRef ref) {
  ref.invalidate(currentEveningProvider);
  ref.invalidate(eveningProvider);
  ref.invalidate(eveningHistoryProvider);
}

String newEveningClientId() {
  final random = Random.secure();
  final bytes = List<int>.generate(16, (_) => random.nextInt(256));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  final hex = bytes.map((v) => v.toRadixString(16).padLeft(2, '0')).join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}
