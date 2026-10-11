import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
import '../chat/chat_screen.dart';
import '../today/today_screen.dart' show pairProvider;

final placesProvider = FutureProvider.autoDispose.family<Map<String, dynamic>, int>((ref, cardId) async {
  final r = await ref.read(dioProvider).get('/plans/$cardId/places');
  return Map<String, dynamic>.from(r.data as Map);
});

/// Плитка в плане: «Выбрать конкретное».
class PlacesTile extends StatelessWidget {
  const PlacesTile({super.key, required this.cardId, required this.planTitle});
  final int cardId;
  final String planTitle;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(20),
        onTap: () => Navigator.of(context).push(MaterialPageRoute<void>(
          builder: (_) => PlacesScreen(cardId: cardId, planTitle: planTitle),
        )),
        child: Container(
          padding: const EdgeInsets.all(18),
          decoration: glowBox(radius: 20),
          child: const Row(children: [
            Icon(Icons.place_outlined, color: VColors.gold),
            SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text('Выбрать конкретное', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
                SizedBox(height: 3),
                Text('Места и события в вашем городе — с учётом того, что вы любите',
                    style: TextStyle(color: VColors.muted, fontSize: 13)),
              ]),
            ),
            Icon(Icons.chevron_right, color: VColors.muted),
          ]),
        ),
      ),
    );
  }
}

class PlacesScreen extends ConsumerStatefulWidget {
  const PlacesScreen({super.key, required this.cardId, required this.planTitle});
  final int cardId;
  final String planTitle;

  @override
  ConsumerState<PlacesScreen> createState() => _PlacesScreenState();
}

class _PlacesScreenState extends ConsumerState<PlacesScreen> {
  int? _busy;

  Future<void> _vote(int placeId) async {
    setState(() => _busy = placeId);
    try {
      await ref.read(dioProvider).post('/plans/${widget.cardId}/places/$placeId/vote');
      ref.invalidate(placesProvider(widget.cardId));
    } catch (e) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(errorText(e))));
    } finally {
      if (mounted) setState(() => _busy = null);
    }
  }

  Future<void> _open(String url) async {
    final ok = await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
    if (!ok && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Не получилось открыть ссылку')));
    }
  }

  @override
  Widget build(BuildContext context) {
    final data = ref.watch(placesProvider(widget.cardId));
    final partnerName = ref.watch(pairProvider).valueOrNull?['partner_name'] as String? ?? 'Партнёр';
    return Scaffold(
      appBar: AppBar(title: Text('Где провести вечер', style: display(20))),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () => ref.refresh(placesProvider(widget.cardId).future),
          child: data.when(
            skipLoadingOnRefresh: true,
            loading: () => const Center(child: CircularProgressIndicator()),
            error: (e, _) => ListView(padding: const EdgeInsets.all(24), children: [
              Text(errorText(e), style: const TextStyle(color: VColors.coral)),
            ]),
            data: (d) {
              final places = (d['places'] as List).map((e) => Map<String, dynamic>.from(e as Map)).toList();
              final cities = (d['cities'] as List).cast<String>();
              final chosen = (d['chosen'] as List).cast<int>().toSet();
              final hidden = d['hidden'] as int;
              final anyDemo = places.any((p) => p['demo'] == true);
              return ListView(padding: const EdgeInsets.fromLTRB(20, 4, 20, 32), children: [
                Text(widget.planTitle, style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w600, height: 1.3)),
                const SizedBox(height: 6),
                Text(
                  '${(d['kinds'] as List).join(' · ')} · '
                  '${cities.isEmpty ? 'город не указан — выбери его в настройках' : cities.join(' и ')}',
                  style: const TextStyle(color: VColors.muted, fontSize: 13),
                ),
                const SizedBox(height: 6),
                Text('Отмечайте, что нравится. Совпало у обоих — место выбрано.',
                    style: const TextStyle(color: VColors.soft, fontSize: 14)),
                if (anyDemo) ...[
                  const SizedBox(height: 12),
                  const _Note(
                    icon: Icons.info_outline,
                    text: 'Это примеры, чтобы показать, как всё работает. Настоящие места и события '
                        'твоего города появятся позже.',
                  ),
                ],
                if (hidden > 0) ...[
                  const SizedBox(height: 8),
                  _Note(
                    icon: Icons.health_and_safety_outlined,
                    text: 'Скрыто вариантов: $hidden — учли «Аллергии и что не ем».',
                  ),
                ],
                const SizedBox(height: 14),
                if (places.isEmpty)
                  const Padding(
                    padding: EdgeInsets.only(top: 24),
                    child: Text('Пока нечего предложить — скоро добавим места.',
                        textAlign: TextAlign.center, style: TextStyle(color: VColors.muted)),
                  ),
                for (final p in places)
                  _PlaceCard(
                    p: p,
                    partnerName: partnerName,
                    chosen: chosen.contains(p['id']),
                    busy: _busy == p['id'],
                    onVote: () => _vote(p['id'] as int),
                    onOpen: p['url'] == null ? null : () => _open(p['url'] as String),
                    onDiscuss: () => openChat(context,
                        kind: 'plan', id: '${widget.cardId}', title: '${widget.planTitle} — ${p['title']}'),
                  ),
              ]);
            },
          ),
        ),
      ),
    );
  }
}

class _Note extends StatelessWidget {
  const _Note({required this.icon, required this.text});
  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(12),
        decoration: cardBox(radius: 14),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Icon(icon, size: 18, color: VColors.gold),
          const SizedBox(width: 10),
          Expanded(child: Text(text, style: const TextStyle(color: VColors.soft, fontSize: 13, height: 1.35))),
        ]),
      );
}

class _PlaceCard extends StatelessWidget {
  const _PlaceCard({
    required this.p,
    required this.partnerName,
    required this.chosen,
    required this.busy,
    required this.onVote,
    required this.onDiscuss,
    this.onOpen,
  });

  final Map<String, dynamic> p;
  final String partnerName;
  final bool chosen;
  final bool busy;
  final VoidCallback onVote;
  final VoidCallback onDiscuss;
  final VoidCallback? onOpen;

  @override
  Widget build(BuildContext context) {
    final mine = p['mine'] == true;
    final partner = p['partner'] == true;
    final event = p['starts_at'] != null;
    final reasons = (p['reasons'] as List).cast<String>();
    return Container(
      margin: const EdgeInsets.only(bottom: 14),
      padding: const EdgeInsets.fromLTRB(18, 16, 18, 12),
      decoration: BoxDecoration(
        color: VColors.card,
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: chosen ? VColors.gold : const Color(0x14FFFFFF), width: chosen ? 1.5 : 1),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Text((p['kind_label'] as String).toUpperCase(),
              style: const TextStyle(letterSpacing: 1.2, fontSize: 11, color: VColors.label)),
          const Spacer(),
          Text('₽' * (p['price'] as int), style: const TextStyle(color: VColors.muted, fontSize: 13)),
        ]),
        const SizedBox(height: 6),
        Text(p['title'] as String, style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w600)),
        if (event) ...[
          const SizedBox(height: 4),
          Row(children: [
            const Icon(Icons.event, size: 15, color: VColors.gold),
            const SizedBox(width: 6),
            Text(formatWhen(DateTime.parse(p['starts_at'] as String).toLocal()),
                style: const TextStyle(color: VColors.soft, fontSize: 13)),
          ]),
        ],
        if (p['address'] != null) ...[
          const SizedBox(height: 4),
          Text(p['address'] as String, style: const TextStyle(color: VColors.muted, fontSize: 13)),
        ],
        const SizedBox(height: 8),
        Text(p['description'] as String, style: const TextStyle(color: VColors.text2, fontSize: 14, height: 1.4)),
        if (reasons.isNotEmpty) ...[
          const SizedBox(height: 10),
          Wrap(spacing: 6, runSpacing: 6, children: [
            for (final r in reasons)
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                decoration: BoxDecoration(color: const Color(0xFF2E2A26), borderRadius: BorderRadius.circular(12)),
                child: Row(mainAxisSize: MainAxisSize.min, children: [
                  const Icon(Icons.favorite, size: 12, color: VColors.gold),
                  const SizedBox(width: 5),
                  Flexible(child: Text(r, style: const TextStyle(color: VColors.goldText, fontSize: 12))),
                ]),
              ),
          ]),
        ],
        if (chosen) ...[
          const SizedBox(height: 10),
          const Row(children: [
            Icon(Icons.check_circle, size: 18, color: VColors.gold),
            SizedBox(width: 6),
            Text('Вы оба выбрали это место', style: TextStyle(color: VColors.goldText, fontWeight: FontWeight.w600)),
          ]),
        ] else if (partner) ...[
          const SizedBox(height: 10),
          Text('$partnerName отметил(а) это место', style: const TextStyle(color: VColors.lavenderText, fontSize: 13)),
        ],
        const SizedBox(height: 8),
        Row(children: [
          TextButton.icon(
            onPressed: busy ? null : onVote,
            icon: Icon(mine ? Icons.favorite : Icons.favorite_border, size: 18, color: VColors.gold),
            label: Text(mine ? 'Нравится' : 'Мне нравится', style: const TextStyle(color: VColors.goldText)),
          ),
          IconButton(
            tooltip: 'Обсудить в чате',
            onPressed: onDiscuss,
            icon: const Icon(Icons.chat_bubble_outline, size: 18, color: VColors.muted),
          ),
          const Spacer(),
          TextButton(
            onPressed: onOpen,
            child: Text(
              onOpen == null ? 'Бронь — скоро' : (event ? 'Купить билеты' : 'Забронировать'),
              style: TextStyle(color: onOpen == null ? VColors.muted : VColors.gold),
            ),
          ),
        ]),
      ]),
    );
  }
}
