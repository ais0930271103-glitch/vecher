import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/privacy.dart';
import '../../core/realtime_provider.dart';
import '../../core/session_changes.dart';
import '../today/today_screen.dart' show pairProvider;

const videoPlaybackQuery = <String, String>{'playback': 'platform-v1'};

enum VideoMode {
  personal('Для меня'),
  shared('Для нас'),
  saved('Сохранённые'),
  sharedCollection('Отправили друг другу');

  const VideoMode(this.title);
  final String title;
  String get query => this == sharedCollection ? 'shared_collection' : name;
  static VideoMode parse(String? value) =>
      values.firstWhere((m) => m.query == value, orElse: () => personal);
}

/// Only approved public hosts can reach the native media player. Bearer headers
/// from the API client are never copied to a third-party media request.
Uri? safeVideoUri(String? value, {bool thumbnail = false}) {
  final uri = value == null ? null : Uri.tryParse(value);
  final hosts = thumbnail
      ? {'upload.wikimedia.org', 'thumb.wikimedia.org'}
      : {'upload.wikimedia.org'};
  if (uri == null ||
      uri.scheme != 'https' ||
      !hosts.contains(uri.host) ||
      uri.userInfo.isNotEmpty ||
      uri.hasPort && uri.port != 443 ||
      uri.fragment.isNotEmpty) {
    return null;
  }
  return uri;
}

Uri? safeVideoSourceUri(String? value, {bool license = false}) {
  final uri = value == null ? null : Uri.tryParse(value);
  final hosts = license
      ? {
          'creativecommons.org',
          'www.creativecommons.org',
          'commons.wikimedia.org',
        }
      : {'commons.wikimedia.org', 'rutube.ru'};
  if (uri == null ||
      uri.scheme != 'https' ||
      !hosts.contains(uri.host) ||
      uri.userInfo.isNotEmpty ||
      uri.hasPort && uri.port != 443 ||
      uri.fragment.isNotEmpty) {
    return null;
  }
  if (uri.host == 'rutube.ru' &&
      (!RegExp(r'^/video/[0-9a-f]{32}/?$').hasMatch(uri.path) ||
          uri.hasQuery)) {
    return null;
  }
  return uri;
}

/// An embed is constructed from one public provider ID, never arbitrary HTML,
/// a media URL, a session token or a catalogue-supplied script/query string.
Uri? safeRutubeEmbedUri(String? id) =>
    id != null && RegExp(r'^[0-9a-f]{32}$').hasMatch(id)
    ? Uri.https('rutube.ru', '/play/embed/$id', {
        'autoplay': 'false',
        'q': 'auto',
      })
    : null;

class ShortVideo {
  const ShortVideo({
    required this.id,
    required this.title,
    required this.description,
    required this.author,
    required this.sourceUrl,
    required this.mediaUrl,
    required this.licenseName,
    required this.licenseUrl,
    required this.topics,
    required this.language,
    required this.durationSeconds,
    required this.aspectRatio,
    this.playbackKind = 'direct',
    this.providerVideoId,
    this.thumbnailUrl,
    this.planCardId,
    this.liked = false,
    this.saved = false,
    this.hidden = false,
  });
  factory ShortVideo.fromJson(Map data) => ShortVideo(
    id: data['id'] as String,
    title: data['title'] as String,
    description: data['description'] as String? ?? '',
    author: data['author'] as String,
    sourceUrl: data['source_url'] as String,
    mediaUrl: data['media_url'] as String?,
    thumbnailUrl: data['thumbnail_url'] as String?,
    licenseName: data['license_name'] as String?,
    licenseUrl: data['license_url'] as String?,
    topics: (data['topics'] as List).cast<String>(),
    language: data['language'] as String? ?? 'none',
    durationSeconds: (data['duration_seconds'] as num).round(),
    aspectRatio: (data['aspect_ratio'] as num).toDouble(),
    playbackKind: data['playback_kind'] as String? ?? 'direct',
    providerVideoId: data['provider_video_id'] as String?,
    planCardId: data['plan_card_id'] as int?,
    liked: data['liked'] == true,
    saved: data['saved'] == true,
    hidden: data['hidden'] == true,
  );
  final String id, title, description, author, sourceUrl, language;
  final String playbackKind;
  final String? mediaUrl, licenseName, licenseUrl, providerVideoId;
  final List<String> topics;
  final String? thumbnailUrl;
  final int durationSeconds;
  final double aspectRatio;
  final int? planCardId;
  final bool liked, saved, hidden;
  bool get isRutube => playbackKind == 'rutube_embed';
  Uri? get rutubeEmbedUri {
    final uri = safeRutubeEmbedUri(providerVideoId);
    final source = safeVideoSourceUri(sourceUrl);
    if (!isRutube ||
        uri == null ||
        source?.host != 'rutube.ru' ||
        source?.path != '/video/$providerVideoId/' ||
        id != 'rutube-$providerVideoId') {
      return null;
    }
    return uri;
  }

  String get attribution => isRutube
      ? '$author · RUTUBE'
      : [author, licenseName].whereType<String>().join(' · ');
  String get durationLabel =>
      '${durationSeconds ~/ 60}:${(durationSeconds % 60).toString().padLeft(2, '0')}';
}

class VideoTopic {
  const VideoTopic(this.id, this.title, {this.available = true});
  final String id, title;
  final bool available;
}

class VideoSettings {
  const VideoSettings({
    required this.topics,
    required this.personalTopics,
    required this.sharedTopics,
    required this.personalRevision,
    required this.sharedRevision,
    required this.hasPair,
  });
  factory VideoSettings.fromJson(Map data) => VideoSettings(
    topics: (data['topics'] as List)
        .map(
          (t) => VideoTopic(
            t['id'] as String,
            t['title'] as String,
            available: t['available'] != false,
          ),
        )
        .toList(),
    personalTopics: (data['personal_topics'] as List).cast<String>(),
    sharedTopics: (data['shared_topics'] as List).cast<String>(),
    personalRevision: data['personal_revision'] as int,
    sharedRevision: data['shared_revision'] as int,
    hasPair: data['has_pair'] == true,
  );
  final List<VideoTopic> topics;
  final List<String> personalTopics, sharedTopics;
  final int personalRevision, sharedRevision;
  final bool hasPair;
}

class VideoFeed {
  const VideoFeed(this.items, {this.total = 0});
  factory VideoFeed.fromJson(Map data) => VideoFeed(
    (data['items'] as List).map((v) => ShortVideo.fromJson(v as Map)).toList(),
    total: data['total'] as int? ?? 0,
  );
  final List<ShortVideo> items;
  final int total;
}

/// Cancels account-scoped work before asynchronous token/route changes. No
/// private data is held here, and a late response cannot enter a new account.
class VideoAccess extends ChangeNotifier with WidgetsBindingObserver {
  VideoAccess(Stream<Set<String>> invalidations) {
    sessionChanges.addListener(changed);
    PinGate.lockedListenable.addListener(changed);
    PinGate.readyListenable.addListener(changed);
    WidgetsBinding.instance.addObserver(this);
    _updates = invalidations.listen((scopes) {
      if (scopes.any({'pair', 'all'}.contains)) changed();
    });
  }
  late final StreamSubscription<Set<String>> _updates;
  int _revision = 0;
  int get revision => _revision;
  bool get allowed =>
      PinGate.isReady &&
      !PinGate.isLocked &&
      !sessionChanges.isChanging &&
      (WidgetsBinding.instance.lifecycleState ?? AppLifecycleState.resumed) ==
          AppLifecycleState.resumed;
  void changed() {
    _revision++;
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

final videoAccessProvider = ChangeNotifierProvider.autoDispose<VideoAccess>((
  ref,
) {
  final access = VideoAccess(
    ref.watch(realtimeControllerProvider).invalidations,
  );
  ref.listen(pairProvider, (previous, next) {
    if (previous != null && previous != next) access.changed();
  });
  return access;
});

class VideoRequest {
  VideoRequest(this.access) : revision = access.revision {
    access.addListener(_changed);
    if (!access.allowed) cancel.cancel('Private session unavailable');
  }
  final VideoAccess access;
  final int revision;
  final cancel = CancelToken();
  bool get valid =>
      !cancel.isCancelled && access.allowed && revision == access.revision;
  void _changed() {
    if (!valid) cancel.cancel('Private session changed');
  }

  void dispose() => access.removeListener(_changed);
}

Future<dynamic> _get(
  Ref ref,
  String path, {
  Map<String, dynamic>? query,
}) async {
  final access = ref.watch(videoAccessProvider);
  if (!access.allowed) return null;
  final request = VideoRequest(access);
  ref.onDispose(() {
    request.cancel.cancel();
    request.dispose();
  });
  final response = await ref
      .read(dioProvider)
      .get(
        path,
        queryParameters: {...?query, ...videoPlaybackQuery},
        cancelToken: request.cancel,
      );
  return request.valid ? response.data : null;
}

final videoSettingsProvider = FutureProvider.autoDispose<VideoSettings?>((
  ref,
) async {
  final data = await _get(ref, '/videos/settings');
  return data == null ? null : VideoSettings.fromJson(data as Map);
});

final videoFeedProvider = FutureProvider.autoDispose
    .family<VideoFeed, VideoMode>((ref, mode) async {
      final data = await _get(
        ref,
        mode == VideoMode.sharedCollection ? '/videos/shared' : '/videos/feed',
        query: mode == VideoMode.sharedCollection
            ? null
            : {'mode': mode.query, 'limit': 48},
      );
      return data == null
          ? const VideoFeed([])
          : VideoFeed.fromJson(data as Map);
    });

final shortVideoProvider = FutureProvider.autoDispose
    .family<ShortVideo?, String>((ref, id) async {
      if (!RegExp(r'^[a-zA-Z0-9_-]{1,64}$').hasMatch(id)) return null;
      final data = await _get(ref, '/videos/${Uri.encodeComponent(id)}');
      return data == null ? null : ShortVideo.fromJson(data as Map);
    });

final singleVideoFeedProvider = FutureProvider.autoDispose
    .family<VideoFeed, String>((ref, id) async {
      final video = await ref.watch(shortVideoProvider(id).future);
      return VideoFeed(video == null ? [] : [video]);
    });

void invalidateVideos(WidgetRef ref) {
  ref.invalidate(videoFeedProvider);
  ref.invalidate(videoSettingsProvider);
  ref.invalidate(shortVideoProvider);
}

String videoErrorText(Object error, {bool detail = false}) {
  if (error is DioException && error.response?.statusCode == 404) {
    return detail
        ? 'Этот ролик уже недоступен. Открой другую подборку.'
        : 'Видео появятся после обновления сервера. Остальные разделы доступны.';
  }
  return errorText(error);
}
