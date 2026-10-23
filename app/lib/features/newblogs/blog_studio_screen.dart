import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import '../content/content.dart';
import 'blogs.dart';

class BlogStudioScreen extends ConsumerWidget {
  const BlogStudioScreen({super.key, this.blogId});
  final String? blogId;
  @override
  Widget build(BuildContext context, WidgetRef ref) =>
      BlogGate(child: _Studio(blogId: blogId));
}

class _Studio extends ConsumerWidget {
  const _Studio({this.blogId});
  final String? blogId;
  Future<void> _open(BuildContext context, WidgetRef ref, String path) async {
    await context.push(path);
    if (context.mounted) invalidateBlogs(ref);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final id = blogId;
    return Scaffold(
      appBar: AppBar(
        title: Text(id == null ? 'Мои блоги' : 'Публикации блога'),
      ),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () async {
            invalidateBlogs(ref);
            if (id == null) {
              await ref.read(myBlogsProvider.future);
            } else {
              await ref.read(blogPostsProvider(id).future);
            }
          },
          child: ListView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 32),
            children: (id == null)
                ? <Widget>[
                    const Text(
                      'Делиться тем, что нравится',
                      style: TextStyle(
                        fontSize: 25,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    const SizedBox(height: 10),
                    const Text(
                      'Видео из блога могут увидеть другие пользователи «Вечера». Публичное имя выбираешь отдельно. Личные интересы, переписка и данные пары сюда не переносятся.',
                      style: TextStyle(color: VColors.soft, height: 1.5),
                    ),
                    const SizedBox(height: 20),
                    ref
                        .watch(myBlogsProvider)
                        .when(
                          loading: () =>
                              const Center(child: CircularProgressIndicator()),
                          error: (e, _) => ContentError(
                            error: blogErrorText(e),
                            onRetry: () => ref.invalidate(myBlogsProvider),
                          ),
                          data: (blogs) {
                            final hasPair =
                                ref
                                    .watch(contentSettingsProvider)
                                    .valueOrNull
                                    ?.hasPair ==
                                true;
                            return Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                for (final kind in const [
                                  'individual',
                                  'couple',
                                ]) ...[
                                  Builder(
                                    builder: (context) {
                                      final blog = blogs
                                          .where((item) => item.kind == kind)
                                          .firstOrNull;
                                      final couple = kind == 'couple';
                                      return Card(
                                        child: Padding(
                                          padding: const EdgeInsets.all(18),
                                          child: Column(
                                            crossAxisAlignment:
                                                CrossAxisAlignment.start,
                                            children: [
                                              Icon(
                                                couple
                                                    ? Icons.people_outline
                                                    : Icons.person_outline,
                                                color: VColors.gold,
                                              ),
                                              const SizedBox(height: 10),
                                              Text(
                                                couple
                                                    ? 'Блог пары'
                                                    : 'Мой блог',
                                                style: const TextStyle(
                                                  fontSize: 22,
                                                  fontWeight: FontWeight.w600,
                                                ),
                                              ),
                                              const SizedBox(height: 8),
                                              Text(
                                                blog?.name ??
                                                    (couple
                                                        ? 'Общий профиль и каждое видео — только с согласия обоих.'
                                                        : 'Личный публичный профиль. Публикации выбираешь ты.'),
                                              ),
                                              if (blog != null) ...[
                                                const SizedBox(height: 8),
                                                Text(
                                                  blog.statusLabel,
                                                  style: const TextStyle(
                                                    color: VColors.muted,
                                                  ),
                                                ),
                                              ],
                                              if (couple &&
                                                  !hasPair &&
                                                  blog == null) ...[
                                                const SizedBox(height: 8),
                                                const Text(
                                                  'Сначала соединитесь в пару в основном «Вечере».',
                                                  style: TextStyle(
                                                    color: VColors.muted,
                                                  ),
                                                ),
                                              ],
                                              const SizedBox(height: 14),
                                              FilledButton(
                                                onPressed:
                                                    couple &&
                                                        !hasPair &&
                                                        blog == null
                                                    ? null
                                                    : () => _open(
                                                        context,
                                                        ref,
                                                        blog == null
                                                            ? '/blogs/new?kind=$kind'
                                                            : '/blogs/${Uri.encodeComponent(blog.id)}',
                                                      ),
                                                child: Text(
                                                  blog == null
                                                      ? (couple
                                                            ? 'Создать блог пары'
                                                            : 'Создать мой блог')
                                                      : 'Открыть блог',
                                                ),
                                              ),
                                            ],
                                          ),
                                        ),
                                      );
                                    },
                                  ),
                                  const SizedBox(height: 12),
                                ],
                              ],
                            );
                          },
                        ),
                  ]
                : [
                    ref
                        .watch(blogProvider(id))
                        .when(
                          loading: () =>
                              const Center(child: CircularProgressIndicator()),
                          error: (e, _) => ContentError(
                            error: blogErrorText(e),
                            onRetry: () => ref.invalidate(blogProvider(id)),
                          ),
                          data: (blog) => blog == null
                              ? const SizedBox.shrink()
                              : Column(
                                  crossAxisAlignment: CrossAxisAlignment.start,
                                  children: [
                                    Text(
                                      blog.kindLabel,
                                      style: const TextStyle(
                                        color: VColors.gold,
                                      ),
                                    ),
                                    const SizedBox(height: 8),
                                    Text(
                                      blog.name,
                                      style: const TextStyle(
                                        fontSize: 27,
                                        fontWeight: FontWeight.w700,
                                      ),
                                    ),
                                    if (blog.bio.isNotEmpty) ...[
                                      const SizedBox(height: 10),
                                      Text(blog.bio),
                                    ],
                                    const SizedBox(height: 12),
                                    Text(
                                      blog.statusLabel,
                                      style: const TextStyle(
                                        color: VColors.gold,
                                      ),
                                    ),
                                    const SizedBox(height: 10),
                                    Wrap(
                                      spacing: 8,
                                      runSpacing: 8,
                                      children: [
                                        OutlinedButton(
                                          onPressed: () => _open(
                                            context,
                                            ref,
                                            '/blogs/${Uri.encodeComponent(id)}/profile',
                                          ),
                                          child: Text(
                                            blog.status == 'pending_consent' &&
                                                    !blog.myConsent
                                                ? 'Проверить профиль и дать согласие'
                                                : 'Профиль и согласие',
                                          ),
                                        ),
                                        if (blog.canPublish)
                                          FilledButton.icon(
                                            onPressed: () => _open(
                                              context,
                                              ref,
                                              '/blogs/${Uri.encodeComponent(id)}/posts/new',
                                            ),
                                            icon: const Icon(Icons.add),
                                            label: const Text('Добавить видео'),
                                          ),
                                      ],
                                    ),
                                    const SizedBox(height: 12),
                                    Text(
                                      blog.couple
                                          ? 'Оба подтверждают одну и ту же версию каждого видео и описания. Затем публикацию проверяет редакция.'
                                          : 'Сначала сохраняешь черновик и подтверждаешь отправку. Видео появится в подборках после проверки редакцией.',
                                      style: const TextStyle(
                                        color: VColors.muted,
                                        height: 1.45,
                                      ),
                                    ),
                                  ],
                                ),
                        ),
                    const SizedBox(height: 24),
                    ref
                        .watch(blogPostsProvider(id))
                        .when(
                          loading: () =>
                              const Center(child: CircularProgressIndicator()),
                          error: (e, _) => ContentError(
                            error: blogErrorText(e),
                            onRetry: () =>
                                ref.invalidate(blogPostsProvider(id)),
                          ),
                          data: (posts) => posts.isEmpty
                              ? const Padding(
                                  padding: EdgeInsets.symmetric(vertical: 20),
                                  child: Text(
                                    'Публикаций пока нет. Начни с короткого видео и описания.',
                                  ),
                                )
                              : Column(
                                  children: [
                                    for (final post in posts)
                                      Card(
                                        child: ListTile(
                                          contentPadding:
                                              const EdgeInsets.symmetric(
                                                horizontal: 18,
                                                vertical: 10,
                                              ),
                                          title: Text(post.title),
                                          subtitle: Padding(
                                            padding: const EdgeInsets.only(
                                              top: 6,
                                            ),
                                            child: Text(post.statusLabel),
                                          ),
                                          trailing: const Icon(
                                            Icons.chevron_right,
                                          ),
                                          onTap: () => _open(
                                            context,
                                            ref,
                                            '/blogs/${Uri.encodeComponent(id)}/posts/${Uri.encodeComponent(post.id)}',
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
