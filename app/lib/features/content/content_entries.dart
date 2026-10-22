import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import '../videos/videos.dart' show videoAccessProvider;
import 'content.dart';

class ContentTogetherCard extends StatelessWidget {
  const ContentTogetherCard({super.key});
  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(20),
    decoration: BoxDecoration(
      gradient: VColors.cardGradient,
      borderRadius: BorderRadius.circular(24),
      border: Border.all(color: const Color(0x4DF3C98B)),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Читать. Пробовать. Обсуждать.',
          style: TextStyle(
            fontSize: 22,
            fontWeight: FontWeight.w600,
            height: 1.3,
          ),
        ),
        const SizedBox(height: 10),
        const Text(
          'Публикации и практикумы по твоим интересам: полезное для жизни и идеи для времени вдвоём.',
          style: TextStyle(color: VColors.soft, height: 1.5),
        ),
        const SizedBox(height: 16),
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            FilledButton.icon(
              onPressed: () => context.push('/content'),
              icon: const Icon(Icons.menu_book_outlined),
              label: const Text('Найти интересное'),
            ),
            OutlinedButton(
              onPressed: () => context.push('/content?mode=shared'),
              child: const Text('Для нас'),
            ),
            TextButton(
              onPressed: () => context.push('/content/preferences'),
              child: const Text('Мои интересы'),
            ),
          ],
        ),
        TextButton.icon(
          onPressed: () => context.push('/videos'),
          icon: const Icon(Icons.smart_display_outlined, size: 18),
          label: const Text('Прежняя видеоподборка'),
        ),
      ],
    ),
  );
}

class ContentTodayCard extends ConsumerWidget {
  const ContentTodayCard({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (!ref.watch(videoAccessProvider).allowed) return const SizedBox.shrink();
    final preview = ref.watch(contentPreviewProvider);
    return Padding(
      padding: const EdgeInsets.only(bottom: 16),
      child: Material(
        color: VColors.card,
        borderRadius: BorderRadius.circular(22),
        child: InkWell(
          borderRadius: BorderRadius.circular(22),
          onTap: () {
            final item = preview.isLoading || preview.hasError
                ? null
                : preview.valueOrNull;
            context.push(
              item == null
                  ? '/content'
                  : '/content/${Uri.encodeComponent(item.id)}',
            );
          },
          child: Padding(
            padding: const EdgeInsets.all(18),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  'НА СВОЕЙ ВОЛНЕ',
                  style: TextStyle(
                    color: VColors.gold,
                    fontSize: 11,
                    letterSpacing: 1.1,
                  ),
                ),
                const SizedBox(height: 8),
                preview.when(
                  skipLoadingOnReload: false,
                  skipLoadingOnRefresh: false,
                  loading: () => const Text(
                    'Публикации по интересам',
                    style: TextStyle(fontSize: 19, fontWeight: FontWeight.w600),
                  ),
                  error: (_, __) => const Text(
                    'Открыть публикации',
                    style: TextStyle(fontSize: 19, fontWeight: FontWeight.w600),
                  ),
                  data: (item) => Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        item?.title ?? 'Что интересно тебе сегодня?',
                        style: const TextStyle(
                          fontSize: 19,
                          fontWeight: FontWeight.w600,
                          height: 1.3,
                        ),
                      ),
                      const SizedBox(height: 8),
                      Text(
                        item?.summary ?? 'Выбери темы — соберём подборку практикумов и публикаций.',
                        maxLines: 3,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: VColors.soft,
                          height: 1.4,
                        ),
                      ),
                      if (item != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 10),
                          child: Text(
                            '${item.author.name} · ${item.readingMinutes} мин',
                            style: const TextStyle(
                              color: VColors.muted,
                              fontSize: 12,
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                const Text('Открыть →', style: TextStyle(color: VColors.gold)),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class ContentPersonalCard extends StatelessWidget {
  const ContentPersonalCard({super.key});
  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(18),
    decoration: BoxDecoration(
      color: VColors.card,
      borderRadius: BorderRadius.circular(22),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Мои публикации',
          style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 6),
        const Text(
          'Личная подборка, любимые авторы и полезное на потом.',
          style: TextStyle(color: VColors.soft, height: 1.4),
        ),
        const SizedBox(height: 10),
        Wrap(
          spacing: 8,
          runSpacing: 4,
          children: [
            TextButton(
              onPressed: () => context.push('/content'),
              child: const Text('Для меня'),
            ),
            TextButton(
              onPressed: () => context.push('/content?mode=saved'),
              child: const Text('Сохранённое'),
            ),
            TextButton(
              onPressed: () => context.push('/content?mode=following'),
              child: const Text('Подписки'),
            ),
            TextButton(
              onPressed: () => context.push('/content/preferences'),
              child: const Text('Мои интересы'),
            ),
          ],
        ),
      ],
    ),
  );
}
