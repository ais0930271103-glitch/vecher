import 'dart:math';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api.dart';
import '../../core/session_changes.dart';
import '../content/content.dart';
import '../today/today_screen.dart' show pairProvider;
import '../videos/videos.dart' show VideoRequest, videoAccessProvider;
import 'blog_media.dart';
export 'blog_media.dart';

const publicBlogConsentVersion = 'public-blog-v1';
const publicPostConsentVersion = 'public-post-v1';
const maxBlogVideoBytes = 100 * 1024 * 1024;

String newBlogRequestId() => List.generate(
  16,
  (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0'),
).join();

class PublicBlog {
  PublicBlog.fromJson(Map data)
    : id = data['id'] as String,
      kind = data['kind'] as String,
      name = data['name'] as String,
      bio = data['bio'] as String? ?? '',
      status = data['status'] as String,
      revision = data['revision'] as int,
      myConsent = data['my_consent'] == true,
      partnerConsent = data['partner_consent'] == true,
      paused = data['paused'] == true,
      canEdit = data['can_edit'] == true,
      canPublish = data['can_publish'] == true;
  final String id, kind, name, bio, status;
  final int revision;
  final bool myConsent, partnerConsent, paused, canEdit, canPublish;
  bool get couple => kind == 'couple';
  String get kindLabel => couple ? 'Блог пары' : 'Мой блог';
  String get statusLabel => paused
      ? 'Пара на паузе — подтверждение общего блога пока недоступно'
      : switch (status) {
          'active' => 'Публичный профиль подтверждён',
          'pending_consent' =>
            myConsent
                ? 'Ждём согласия партнёра на профиль'
                : 'Нужно твоё согласие на профиль',
          'withdrawn' => 'Профиль снят с публикации',
          _ => 'Профиль недоступен',
        };
}

class BlogPost {
  BlogPost.fromJson(Map data)
    : id = data['id'] as String,
      blogId = data['blog_id'] as String,
      contentId = data['content_id'] as String?,
      title = data['title'] as String,
      description = data['description'] as String,
      topics = (data['topics'] as List).cast<String>(),
      language = data['language'] as String,
      mediaId = data['media_id'] as String?,
      media = data['media'] is Map
          ? BlogMedia.fromJson(data['media'] as Map)
          : null,
      status = data['status'] as String,
      revision = data['revision'] as int,
      myApproved = data['my_approved'] == true,
      partnerApproved = data['partner_approved'] == true,
      reviewNote = data['review_note'] as String?,
      canSubmit = data['can_submit'] == true,
      canEdit = data['can_edit'] == true,
      canWithdraw = data['can_withdraw'] == true;
  final String id, blogId, title, description, language, status;
  final String? mediaId, contentId, reviewNote;
  final BlogMedia? media;
  final List<String> topics;
  final int revision;
  final bool myApproved, partnerApproved, canSubmit, canEdit, canWithdraw;
  String get statusLabel => switch (status) {
    'draft' => 'Черновик',
    'pending_partner' =>
      myApproved ? 'Ждём согласия партнёра' : 'Партнёр предлагает публикацию',
    'pending_review' => 'На проверке редакции',
    'published' => 'Опубликовано',
    'rejected' => 'Нужны изменения',
    'withdrawn' => 'Снято с публикации',
    _ => 'Недоступно',
  };
}

Future<dynamic> _get(Ref ref, String path) async {
  final access = ref.watch(videoAccessProvider);
  if (!access.allowed) return null;
  final request = VideoRequest(access);
  ref.onDispose(() {
    request.cancel.cancel();
    request.dispose();
  });
  final response = await ref
      .read(dioProvider)
      .get(path, cancelToken: request.cancel);
  return request.valid ? response.data : null;
}

final myBlogsProvider = FutureProvider.autoDispose<List<PublicBlog>>((
  ref,
) async {
  final data = await _get(ref, '/blogs/mine');
  return data == null
      ? []
      : (data['items'] as List)
            .map((e) => PublicBlog.fromJson(e as Map))
            .toList();
});
final blogProvider = FutureProvider.autoDispose.family<PublicBlog?, String>((
  ref,
  id,
) async {
  final data = await _get(ref, '/blogs/${Uri.encodeComponent(id)}');
  return data == null ? null : PublicBlog.fromJson(data as Map);
});
final blogPostsProvider = FutureProvider.autoDispose
    .family<List<BlogPost>, String>((ref, id) async {
      final data = await _get(ref, '/blogs/${Uri.encodeComponent(id)}/posts');
      return data == null
          ? []
          : (data['items'] as List)
                .map((e) => BlogPost.fromJson(e as Map))
                .toList();
    });

void invalidateBlogs(WidgetRef ref) {
  ref.invalidate(myBlogsProvider);
  ref.invalidate(blogProvider);
  ref.invalidate(blogPostsProvider);
  invalidateContent(ref);
}

String blogErrorText(Object error) {
  if (error is String) return error;
  if (error is DioException) {
    final body = error.response?.data;
    final detail = body is Map && body['detail'] is String
        ? body['detail'] as String
        : null;
    final mediaError = blogMediaErrorMessage(detail);
    if (mediaError != null) return mediaError;
    if (error.response?.statusCode == 409) {
      if (detail == null ||
          detail.contains('Версия изменилась') ||
          detail.toLowerCase().contains('revision')) {
        return 'Данные изменились. Обнови публикацию и проверь её актуальную версию перед подтверждением.';
      }
      return RegExp('[А-Яа-яЁё]').hasMatch(detail)
          ? detail
          : 'Действие пока недоступно. Обнови экран и проверь состояние публикации.';
    }
    if (error.response?.statusCode == 413) {
      return 'Видео больше 100 МБ. Выбери файл поменьше.';
    }
    if (error.response?.statusCode == 403) {
      return 'Сейчас это действие недоступно. Проверь состояние профиля и согласие пары.';
    }
    if (error.response?.statusCode == 404) {
      return 'Блог или публикация больше недоступны.';
    }
  }
  return errorText(error);
}

/// Native file selection backgrounds the app. Preserve the local form while
/// hidden, but never keep it across an account change or expose it behind PIN.
class BlogGate extends ConsumerStatefulWidget {
  const BlogGate({super.key, required this.child});
  final Widget child;
  @override
  ConsumerState<BlogGate> createState() => _BlogGateState();
}

class _BlogGateState extends ConsumerState<BlogGate> {
  String? _pairIdentity;
  @override
  Widget build(BuildContext context) {
    final access = ref.watch(videoAccessProvider);
    final pair = ref.watch(pairProvider);
    // Keep the stable identity through refresh/background, but discard every
    // local draft and old consent projection when pairing actually changes.
    if (!pair.isLoading && !pair.hasError) {
      final data = pair.valueOrNull;
      _pairIdentity = data == null
          ? 'solo'
          : '${data['pair_id']}:${data['status']}:${data['paused'] == true}';
    }
    if (_pairIdentity == null) {
      return Scaffold(
        appBar: AppBar(title: const Text('Мои блоги')),
        body: Center(
          child: pair.hasError
              ? ContentError(
                  error: pair.error!,
                  onRetry: () => ref.invalidate(pairProvider),
                )
              : const CircularProgressIndicator(),
        ),
      );
    }
    return KeyedSubtree(
      key: ValueKey('${sessionChanges.revision}:$_pairIdentity'),
      child: _CapabilityGate(allowed: access.allowed, child: widget.child),
    );
  }
}

class _CapabilityGate extends ConsumerStatefulWidget {
  const _CapabilityGate({required this.allowed, required this.child});
  final bool allowed;
  final Widget child;
  @override
  ConsumerState<_CapabilityGate> createState() => _CapabilityGateState();
}

class _CapabilityGateState extends ConsumerState<_CapabilityGate> {
  bool _enabled = false;
  @override
  Widget build(BuildContext context) {
    final settings = ref.watch(contentSettingsProvider);
    final value = settings.valueOrNull;
    if (value != null) _enabled = value.publicBlogsEnabled;
    if (!_enabled) {
      return Scaffold(
        appBar: AppBar(title: const Text('Мои блоги')),
        body: Center(
          child: Padding(
            padding: const EdgeInsets.all(24),
            child: !widget.allowed
                ? const Text('Блоги откроются после разблокировки')
                : settings.isLoading
                ? const CircularProgressIndicator()
                : settings.hasError
                ? ContentError(
                    error: settings.error!,
                    onRetry: () => ref.invalidate(contentSettingsProvider),
                  )
                : !hostedContentSupported
                ? const Text(
                    'Публикация и просмотр видео из блогов доступны в Android-приложении «Вечер». Обычные подборки и подписки работают здесь как прежде.',
                  )
                : const Text(
                    'Публикация блогов пока недоступна. Подборки и подписки продолжают работать.',
                  ),
          ),
        ),
      );
    }
    return Stack(
      children: [
        Offstage(
          offstage: !widget.allowed,
          child: TickerMode(enabled: widget.allowed, child: widget.child),
        ),
        if (!widget.allowed) const ContentUnavailable(),
      ],
    );
  }
}
