import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../newblogs/blog_media.dart';
import '../videos/videos.dart' show VideoRequest, videoAccessProvider;

// The Android player sends protected media headers. HTML video cannot use the
// same bearer transport, so the first hosted-content client opts in natively.
const hostedContentSupported = !kIsWeb;

enum ContentMode {
  personal('Для меня'),
  following('Подписки'),
  shared('Для нас'),
  saved('Сохранённое');

  const ContentMode(this.title);
  final String title;
  static ContentMode parse(String? value) =>
      values.firstWhere((m) => m.name == value, orElse: () => personal);
}

enum ContentFormat {
  all('Всё'),
  video('Видео'),
  article('Статьи');

  const ContentFormat(this.title);
  final String title;
  static ContentFormat parse(String? value) =>
      values.firstWhere((format) => format.name == value, orElse: () => all);
}

class ContentChoice {
  const ContentChoice(this.id, this.title, {this.available = true});
  factory ContentChoice.fromJson(Map data) => ContentChoice(
    data['id'] as String,
    data['title'] as String,
    available: data['available'] != false,
  );
  final String id, title;
  final bool available;
}

class ContentSettings {
  ContentSettings.fromJson(Map data)
    : topics = (data['topics'] as List)
          .map((e) => ContentChoice.fromJson(e as Map))
          .toList(),
      languages = (data['languages'] as List)
          .map((e) => ContentChoice.fromJson(e as Map))
          .toList(),
      personalTopics = (data['personal_topics'] as List).cast<String>(),
      sharedTopics = (data['shared_topics'] as List).cast<String>(),
      personalLanguage = data['personal_language'] as String,
      sharedLanguage = data['shared_language'] as String,
      personalRevision = data['personal_revision'] as int,
      sharedRevision = data['shared_revision'] as int,
      followedAuthorIds = (data['followed_author_ids'] as List).cast<String>(),
      hiddenAuthorIds = (data['hidden_author_ids'] as List).cast<String>(),
      unifiedFeed = data['unified_feed'] == true,
      publicBlogsEnabled =
          hostedContentSupported && data['public_blogs_enabled'] == true,
      hasPair = data['has_pair'] == true;
  final List<ContentChoice> topics, languages;
  final List<String> personalTopics,
      sharedTopics,
      followedAuthorIds,
      hiddenAuthorIds;
  final String personalLanguage, sharedLanguage;
  final int personalRevision, sharedRevision;
  final bool hasPair, unifiedFeed, publicBlogsEnabled;
}

class ContentAuthor {
  ContentAuthor.fromJson(Map data)
    : id = data['id'] as String,
      name = data['name'] as String,
      bio = data['bio'] as String? ?? '',
      kind = data['kind'] as String? ?? 'editorial',
      url = data['url'] as String?,
      publishedCount = data['published_count'] as int? ?? 0,
      followed = data['followed'] == true,
      hidden = data['hidden'] == true;
  final String id, name, bio, kind;
  final String? url;
  final int publishedCount;
  final bool followed, hidden;
}

class ContentBlock {
  ContentBlock.fromJson(Map data)
    : type = data['type'] as String,
      text = data['text'] as String? ?? '',
      items = (data['items'] as List? ?? const []).cast<String>();
  final String type, text;
  final List<String> items;
}

class ContentSource {
  ContentSource.fromJson(Map data)
    : title = data['title'] as String,
      url = data['url'] as String;
  final String title, url;
}

class Publication {
  Publication.fromJson(Map data)
    : id = data['id'] as String,
      kind = data['kind'] as String,
      title = data['title'] as String,
      summary = data['summary'] as String,
      author = ContentAuthor.fromJson(data['author'] as Map),
      topics = (data['topics'] as List).cast<String>(),
      language = data['language'] as String,
      blocks = (data['blocks'] as List)
          .map((e) => ContentBlock.fromJson(e as Map))
          .toList(),
      sources = (data['sources'] as List)
          .map((e) => ContentSource.fromJson(e as Map))
          .toList(),
      publishedAt = DateTime.tryParse(data['published_at'] as String? ?? ''),
      updatedAt = DateTime.tryParse(data['updated_at'] as String? ?? ''),
      seriesId = data['series_id'] as String?,
      seriesTitle = data['series_title'] as String?,
      seriesOrder = data['series_order'] as int?,
      readingMinutes = data['reading_minutes'] as int,
      videoId = data['video_id'] as String?,
      hostedMedia = data['playback_kind'] == 'hosted' && data['media'] is Map
          ? BlogMedia.fromJson(data['media'] as Map)
          : null,
      durationSeconds = (data['duration_seconds'] as num?)?.round(),
      recommendationReason = data['recommendation_reason'] as String?,
      useful = data['useful'] == true,
      saved = data['saved'] == true,
      hidden = data['hidden'] == true,
      authorFollowed = data['author_followed'] == true,
      authorHidden = data['author_hidden'] == true;
  final String id, kind, title, summary, language;
  final ContentAuthor author;
  final List<String> topics;
  final List<ContentBlock> blocks;
  final List<ContentSource> sources;
  final DateTime? publishedAt, updatedAt;
  final String? seriesId, seriesTitle, videoId, recommendationReason;
  final int? seriesOrder, durationSeconds;
  final int readingMinutes;
  final BlogMedia? hostedMedia;
  final bool useful, saved, hidden, authorFollowed, authorHidden;
  String get kindLabel => kind == 'guide'
      ? 'Практикум'
      : kind == 'video'
      ? 'Видео'
      : 'Статья';
  bool get isVideo =>
      kind == 'video' && (videoId != null || hostedMedia != null);
  bool get isPublicBlog =>
      author.kind == 'individual' || author.kind == 'couple';
  String get metadataLabel {
    if (kind != 'video') return '$kindLabel · $readingMinutes мин чтения';
    final seconds = durationSeconds;
    if (seconds == null || seconds <= 0) return kindLabel;
    final minutes = seconds ~/ 60;
    final remainder = (seconds % 60).toString().padLeft(2, '0');
    return '$kindLabel · $minutes:$remainder';
  }

  String get openLocation =>
      kind == 'video' && videoId != null && hostedMedia == null
      ? '/videos/${Uri.encodeComponent(videoId!)}'
      : '/content/${Uri.encodeComponent(id)}';

  // Ordinary message text is deliberately compatible with already installed
  // clients. It is a draft, never a new unsupported reference or an auto-send.
  String get shareDraft => '$title\n\n$summary';
}

class ContentPage {
  const ContentPage(this.items, {this.nextCursor, this.total = 0});
  factory ContentPage.fromJson(Map data) => ContentPage(
    (data['items'] as List).map((e) => Publication.fromJson(e as Map)).toList(),
    nextCursor: data['next_cursor'] as String?,
    total: data['total'] as int? ?? 0,
  );
  final List<Publication> items;
  final String? nextCursor;
  final int total;
}

/// Source links open only after an explicit tap in the system browser. No
/// third-party page receives the authenticated Dio client or its headers.
Uri? contentSourceUri(String? value) {
  final uri = value == null ? null : Uri.tryParse(value);
  if (uri == null ||
      uri.scheme != 'https' ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      (uri.hasPort && uri.port != 443)) {
    return null;
  }
  return uri;
}

String contentLocation({
  ContentMode mode = ContentMode.personal,
  ContentFormat format = ContentFormat.all,
  String? authorId,
  String? seriesId,
  String? title,
}) => Uri(
  path: '/content',
  queryParameters: {
    if (mode != ContentMode.personal) 'mode': mode.name,
    if (format != ContentFormat.all) 'format': format.name,
    if (authorId != null) 'author_id': authorId,
    if (seriesId != null) 'series_id': seriesId,
    if (title != null) 'title': title,
  },
).toString();

Future<dynamic> _get(
  Ref ref,
  String path, {
  Map<String, dynamic>? query,
}) async {
  // Content and legacy video share the same account/PIN/pair/lifecycle gate.
  final access = ref.watch(videoAccessProvider);
  if (!access.allowed) return null;
  final request = VideoRequest(access);
  ref.onDispose(() {
    request.cancel.cancel();
    request.dispose();
  });
  final response = await ref
      .read(dioProvider)
      .get(path, queryParameters: query, cancelToken: request.cancel);
  return request.valid ? response.data : null;
}

final contentSettingsProvider = FutureProvider.autoDispose<ContentSettings?>((
  ref,
) async {
  final data = await _get(ref, '/content/settings');
  return data == null ? null : ContentSettings.fromJson(data as Map);
});
final publicationProvider = FutureProvider.autoDispose
    .family<Publication?, String>((ref, id) async {
      final data = await _get(
        ref,
        '/content/${Uri.encodeComponent(id)}',
        query: {'hosted_video': hostedContentSupported},
      );
      return data == null ? null : Publication.fromJson(data as Map);
    });
final contentAuthorProvider = FutureProvider.autoDispose
    .family<ContentAuthor?, String>((ref, id) async {
      final data = await _get(
        ref,
        '/content/authors/${Uri.encodeComponent(id)}',
      );
      return data == null ? null : ContentAuthor.fromJson(data as Map);
    });
final contentAuthorsProvider = FutureProvider.autoDispose<List<ContentAuthor>>((
  ref,
) async {
  final data = await _get(ref, '/content/authors');
  return data == null
      ? []
      : (data['items'] as List)
            .map((e) => ContentAuthor.fromJson(e as Map))
            .toList();
});
final contentPreviewProvider = FutureProvider.autoDispose<Publication?>((
  ref,
) async {
  final data = await _get(
    ref,
    '/content/feed',
    query: {
      'mode': 'personal',
      'limit': 1,
      'hosted_video': hostedContentSupported,
    },
  );
  return data == null
      ? null
      : ContentPage.fromJson(data as Map).items.firstOrNull;
});
final contentChangeProvider = StateProvider.autoDispose<int>((ref) => 0);

void invalidateContent(WidgetRef ref) {
  ref.invalidate(contentSettingsProvider);
  ref.invalidate(publicationProvider);
  ref.invalidate(contentAuthorProvider);
  ref.invalidate(contentAuthorsProvider);
  ref.invalidate(contentPreviewProvider);
  ref.read(contentChangeProvider.notifier).state++;
}

/// All page actions are cancelled when disposed as well as when the session
/// gate closes. Returning null means the caller must discard the response.
mixin ContentRequests<T extends ConsumerStatefulWidget> on ConsumerState<T> {
  final _requests = <VideoRequest>{};
  Future<dynamic> contentRequest(
    String method,
    String path, {
    Map<String, dynamic>? query,
    Map<String, dynamic>? data,
  }) async {
    final request = VideoRequest(ref.read(videoAccessProvider));
    _requests.add(request);
    try {
      final response = await ref
          .read(dioProvider)
          .request(
            path,
            options: Options(method: method),
            queryParameters: query,
            data: data,
            cancelToken: request.cancel,
          );
      return mounted && request.valid ? response.data : null;
    } on DioException catch (e) {
      if (!mounted || !request.valid || CancelToken.isCancel(e)) return null;
      rethrow;
    } finally {
      _requests.remove(request);
      request.dispose();
    }
  }

  void cancelContentRequests() {
    for (final request in _requests) {
      request.cancel.cancel('Content page changed');
    }
  }

  @override
  void dispose() {
    cancelContentRequests();
    super.dispose();
  }
}

class ContentUnavailable extends StatelessWidget {
  const ContentUnavailable({super.key});
  @override
  Widget build(BuildContext context) => const Scaffold(
    body: SafeArea(
      child: Center(child: Text('Интересное откроется после разблокировки')),
    ),
  );
}

class ContentError extends StatelessWidget {
  const ContentError({
    super.key,
    required this.error,
    required this.onRetry,
    this.retryLabel = 'Повторить загрузку',
  });
  final Object error;
  final VoidCallback onRetry;
  final String retryLabel;
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 16),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(error is String ? error as String : errorText(error)),
        const SizedBox(height: 8),
        OutlinedButton.icon(
          onPressed: onRetry,
          icon: const Icon(Icons.refresh),
          label: Text(retryLabel),
        ),
      ],
    ),
  );
}
