import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import 'video_player.dart';
import 'video_topics.dart';
import 'videos.dart';

/// Navigation card stays useful even when the catalogue cannot be fetched.
class VideoTogetherCard extends StatelessWidget {
  const VideoTogetherCard({super.key});
  @override
  Widget build(BuildContext context) => Container(
    padding: const EdgeInsets.all(18),
    decoration: BoxDecoration(
      gradient: VColors.cardGradient,
      borderRadius: BorderRadius.circular(24),
      border: Border.all(color: const Color(0x4DF3C98B)),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        const Row(
          children: [
            Icon(Icons.smart_display_outlined, color: VColors.gold, size: 28),
            SizedBox(width: 10),
            Expanded(
              child: Text(
                'Маленькая пауза — вместе',
                style: TextStyle(fontSize: 18, fontWeight: FontWeight.w600),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        const Text(
          'Животные, открытия, красивые места и идеи. Найди ролик для себя или отправь своему человеку.',
          style: TextStyle(color: VColors.soft, height: 1.4),
        ),
        const SizedBox(height: 12),
        Wrap(
          spacing: 8,
          runSpacing: 6,
          children: [
            OutlinedButton.icon(
              onPressed: () => context.push('/videos'),
              icon: const Icon(Icons.play_arrow),
              label: const Text('Смотреть видео'),
            ),
            TextButton(
              onPressed: () => showVideoTopics(context),
              child: const Text('Мои интересы'),
            ),
          ],
        ),
        const SizedBox(height: 6),
        const Text(
          'Первая подборка · видео с Wikimedia Commons',
          style: TextStyle(color: VColors.muted, fontSize: 11),
        ),
      ],
    ),
  );
}

class VideoTodayCard extends ConsumerWidget {
  const VideoTodayCard({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final access = ref.watch(videoAccessProvider);
    if (!access.allowed) return const SizedBox.shrink();
    final videos = ref.watch(videoFeedProvider(VideoMode.personal));
    return videos.when(
      skipLoadingOnRefresh: false,
      skipLoadingOnReload: false,
      loading: () => const SizedBox.shrink(),
      error: (_, __) => const SizedBox.shrink(),
      data: (data) {
        final video = data.items.firstOrNull;
        if (video == null) return const SizedBox.shrink();
        return Padding(
          padding: const EdgeInsets.only(bottom: 16),
          child: Material(
            color: VColors.card,
            borderRadius: BorderRadius.circular(22),
            child: InkWell(
              borderRadius: BorderRadius.circular(22),
              onTap: () => context.push('/videos'),
              child: Padding(
                padding: const EdgeInsets.all(14),
                child: Row(
                  children: [
                    ClipRRect(
                      borderRadius: BorderRadius.circular(14),
                      child: SizedBox(
                        width: 84,
                        height: 84,
                        child: Stack(
                          fit: StackFit.expand,
                          children: [
                            VideoPoster(video: video, fit: BoxFit.cover),
                            const Center(
                              child: Icon(
                                Icons.play_circle_fill,
                                color: Colors.white,
                                size: 30,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                    const SizedBox(width: 14),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          const Text(
                            'НА МИНУТКУ',
                            style: TextStyle(
                              color: VColors.label,
                              fontSize: 11,
                              letterSpacing: 1,
                            ),
                          ),
                          const SizedBox(height: 5),
                          Text(
                            video.title,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              fontSize: 15,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const SizedBox(height: 5),
                          const Text(
                            'Посмотреть свою подборку',
                            style: TextStyle(
                              color: VColors.muted,
                              fontSize: 12,
                            ),
                          ),
                        ],
                      ),
                    ),
                    const Icon(Icons.chevron_right, color: VColors.muted),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}

class VideoChatReference extends ConsumerWidget {
  const VideoChatReference({super.key, required this.id, required this.title});
  final String id, title;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final video = ref.watch(shortVideoProvider(id)).valueOrNull;
    return Semantics(
      button: true,
      label: 'Открыть видео: $title',
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => context.push('/videos/${Uri.encodeComponent(id)}'),
        child: Container(
          margin: const EdgeInsets.only(bottom: 6),
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: const Color(0x14FFFFFF),
            borderRadius: BorderRadius.circular(12),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (video != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 8),
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(8),
                    child: SizedBox(
                      height: 110,
                      width: double.infinity,
                      child: VideoPoster(video: video, fit: BoxFit.cover),
                    ),
                  ),
                ),
              const Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    Icons.play_circle_outline,
                    size: 20,
                    color: VColors.gold,
                  ),
                  SizedBox(width: 7),
                  Text(
                    'Короткое видео',
                    style: TextStyle(color: VColors.label, fontSize: 11),
                  ),
                ],
              ),
              const SizedBox(height: 5),
              Text(title, style: const TextStyle(fontSize: 14, height: 1.3)),
              const SizedBox(height: 5),
              const Text(
                'Смотреть',
                style: TextStyle(fontSize: 12, color: VColors.goldText),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
