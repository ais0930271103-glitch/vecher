import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'theme.dart';

/// Уведомление внутри приложения — плашка сверху (не мешает писать в чате и нажимать кнопки внизу).
/// Само скрывается через несколько секунд; смахнуть вверх — скрыть; нажать — открыть.
OverlayEntry? _current;
Timer? _timer;

void showTopBanner(
  OverlayState? overlay, {
  required String text,
  String? title,
  VoidCallback? onTap,
  Duration duration = const Duration(seconds: 4),
}) {
  if (overlay == null) return;
  hideTopBanner();
  HapticFeedback.lightImpact();
  late final OverlayEntry entry;
  // A disappearing old banner must not close the newer one that replaced it.
  void dismissThisBanner() {
    if (identical(_current, entry)) hideTopBanner();
  }

  entry = OverlayEntry(
    builder: (_) => _TopBanner(
      title: title,
      text: text,
      onTap: onTap == null
          ? null
          : () {
              dismissThisBanner();
              onTap();
            },
      onDismiss: dismissThisBanner,
    ),
  );
  _current = entry;
  overlay.insert(entry);
  _timer = Timer(duration, dismissThisBanner);
}

void hideTopBanner() {
  _timer?.cancel();
  _timer = null;
  final entry = _current;
  _current = null;
  entry?.remove();
  entry?.dispose();
}

class _TopBanner extends StatefulWidget {
  const _TopBanner({
    required this.text,
    this.title,
    this.onTap,
    required this.onDismiss,
  });
  final String? title;
  final String text;
  final VoidCallback? onTap;
  final VoidCallback onDismiss;

  @override
  State<_TopBanner> createState() => _TopBannerState();
}

class _TopBannerState extends State<_TopBanner>
    with SingleTickerProviderStateMixin {
  late final AnimationController _c = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 260),
  )..forward();
  late final Animation<Offset> _slide = Tween(
    begin: const Offset(0, -1.2),
    end: Offset.zero,
  ).animate(CurvedAnimation(parent: _c, curve: Curves.easeOutCubic));

  @override
  void dispose() {
    _c.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Positioned(
      top: 0,
      left: 0,
      right: 0,
      child: SafeArea(
        bottom: false,
        child: SlideTransition(
          position: _slide,
          child: Dismissible(
            key: const ValueKey('top-banner'),
            direction: DismissDirection.up,
            onDismissed: (_) => widget.onDismiss(),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
              child: Material(
                color: VColors.card2,
                elevation: 8,
                shadowColor: Colors.black54,
                borderRadius: BorderRadius.circular(16),
                child: InkWell(
                  borderRadius: BorderRadius.circular(16),
                  onTap: widget.onTap,
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
                    child: Row(
                      children: [
                        const Icon(
                          Icons.nightlight_round,
                          color: VColors.gold,
                          size: 22,
                        ),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Semantics(
                            liveRegion: true,
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                if (widget.title != null &&
                                    widget.title!.isNotEmpty)
                                  Text(
                                    widget.title!,
                                    style: const TextStyle(
                                      color: VColors.goldText,
                                      fontWeight: FontWeight.w600,
                                      fontSize: 14,
                                    ),
                                  ),
                                Text(
                                  widget.text,
                                  maxLines: 2,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    color: VColors.text,
                                    fontSize: 14,
                                    height: 1.3,
                                  ),
                                ),
                              ],
                            ),
                          ),
                        ),
                        if (widget.onTap != null) ...[
                          const SizedBox(width: 8),
                          const Text(
                            'Открыть',
                            style: TextStyle(
                              color: VColors.gold,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ],
                        IconButton(
                          tooltip: 'Закрыть уведомление',
                          visualDensity: VisualDensity.compact,
                          icon: const Icon(
                            Icons.close,
                            color: VColors.muted,
                            size: 18,
                          ),
                          onPressed: widget.onDismiss,
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
