import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/format.dart';
import '../../core/push.dart' show chatOnScreen;
import '../../core/theme.dart';
import '../../core/ui.dart';
import '../today/today_screen.dart' show pairProvider;

/// Непрочитанные сообщения партнёра — для значка в шапке «Сегодня».
final chatUnreadProvider = FutureProvider.autoDispose<int>((ref) async {
  final r = await ref.read(dioProvider).get('/chat/unread');
  return (r.data as Map)['count'] as int;
});

/// Открыть чат; с вложением — «Обсудить» совпадение, план, приглашение или письмо.
void openChat(BuildContext context, {String? kind, String? id, String? title}) {
  context.push('/chat', extra: kind == null ? null : {'kind': kind, 'id': id, 'title': title});
}

const _refIcons = <String, IconData>{
  'match': Icons.favorite_border,
  'plan': Icons.event_note_outlined,
  'invite': Icons.nightlight_outlined,
  'letter': Icons.mail_outline,
};
const _refLabels = <String, String>{
  'match': 'Совпадение',
  'plan': 'План',
  'invite': 'Приглашение',
  'letter': 'Письмо',
};
const _reactions = ['❤️', '😂', '😮', '😢', '👍', '🔥'];
const _hintKey = 'vecher_chat_hint_seen';

class ChatScreen extends ConsumerStatefulWidget {
  const ChatScreen({super.key, this.attach});
  final Map? attach;

  @override
  ConsumerState<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends ConsumerState<ChatScreen> {
  final _text = TextEditingController();
  final _focus = FocusNode();
  final Map<String, Map<String, dynamic>> _byId = {};
  bool _showHint = false;
  DateTime? _partnerReadAt;
  bool _hasMore = false;
  bool _loading = true;
  bool _sending = false;
  String? _error;
  Map? _attach;
  Map<String, dynamic>? _replyTo;
  Timer? _poll;

  @override
  void initState() {
    super.initState();
    chatOnScreen = true;
    _attach = widget.attach;
    if (_attach != null) WidgetsBinding.instance.addPostFrameCallback((_) => _focus.requestFocus());
    secureStorage.read(key: _hintKey).then((v) {
      if (mounted && v == null) setState(() => _showHint = true);
    });
    _refresh(markRead: true);
    // Мгновенная доставка появится вместе с сервером (Centrifugo); пока — раз в 4 секунды.
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _refresh());
  }

  @override
  void dispose() {
    chatOnScreen = false;
    _poll?.cancel();
    _text.dispose();
    _focus.dispose();
    super.dispose();
  }

  List<Map<String, dynamic>> get _messages {
    final list = _byId.values.toList()
      ..sort((a, b) => (a['created_at'] as String).compareTo(b['created_at'] as String));
    return list;
  }

  void _absorb(Map data) {
    for (final m in (data['messages'] as List)) {
      final msg = Map<String, dynamic>.from(m as Map);
      // Время сравниваем как текст: приводим к UTC, чтобы порядок был верным.
      msg['created_at'] = DateTime.parse(msg['created_at'] as String).toUtc().toIso8601String();
      _byId[msg['id'] as String] = msg;
    }
    final read = data['partner_read_at'] as String?;
    _partnerReadAt = read == null ? null : DateTime.parse(read).toUtc();
  }

  Future<void> _refresh({bool markRead = false}) async {
    try {
      final before = _byId.length;
      final lastPartner = _messages.where((m) => m['mine'] != true).lastOrNull?['id'];
      final r = await ref.read(dioProvider).get('/chat');
      if (!mounted) return;
      final data = r.data as Map;
      setState(() {
        if (before == 0) _hasMore = data['has_more'] == true;
        _absorb(data);
        _loading = false;
        _error = null;
      });
      final newLastPartner = _messages.where((m) => m['mine'] != true).lastOrNull?['id'];
      if (markRead || newLastPartner != lastPartner) {
        await ref.read(dioProvider).post('/chat/read');
        ref.invalidate(chatUnreadProvider);
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          _loading = false;
          _error = errorText(e);
        });
      }
    }
  }

  Future<void> _loadOlder() async {
    final first = _messages.firstOrNull;
    if (first == null) return;
    try {
      final r = await ref.read(dioProvider).get('/chat', queryParameters: {'before': first['created_at']});
      if (!mounted) return;
      final data = r.data as Map;
      setState(() {
        _absorb(data);
        _hasMore = data['has_more'] == true;
      });
    } catch (e) {
      if (mounted) _toast(errorText(e));
    }
  }

  void _toast(String t) => ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(t)));

  Future<void> _send() async {
    final text = _text.text.trim();
    if (text.isEmpty && _attach == null) return;
    setState(() => _sending = true);
    try {
      final r = await ref.read(dioProvider).post('/chat', data: {
        if (text.isNotEmpty) 'text': text,
        if (_attach != null) ...{
          'ref_kind': _attach!['kind'],
          'ref_id': _attach!['id']?.toString(),
          'ref_title': _attach!['title'],
        },
        if (_replyTo != null) 'reply_to': _replyTo!['id'],
      });
      if (!mounted) return;
      setState(() {
        _absorb({'messages': [r.data], 'partner_read_at': _partnerReadAt?.toIso8601String()});
        _text.clear();
        _attach = null;
        _replyTo = null;
      });
    } catch (e) {
      _toast(errorText(e));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  void _reply(Map<String, dynamic> m) {
    if (m['deleted'] == true) return;
    HapticFeedback.selectionClick();
    setState(() => _replyTo = m);
    _focus.requestFocus();
  }

  Future<void> _heart(Map<String, dynamic> m) async {
    if (m['mine'] == true || m['deleted'] == true) return;
    HapticFeedback.lightImpact();
    try {
      final r = await ref.read(dioProvider)
          .post('/chat/${m['id']}/react', data: {'emoji': m['reaction'] == '❤️' ? null : '❤️'});
      if (mounted) {
        setState(() => _absorb({'messages': [r.data], 'partner_read_at': _partnerReadAt?.toIso8601String()}));
      }
    } catch (e) {
      _toast(errorText(e));
    }
  }

  void _hideHint() {
    setState(() => _showHint = false);
    secureStorage.write(key: _hintKey, value: '1');
  }

  Future<void> _actions(Map<String, dynamic> m) async {
    if (m['deleted'] == true) return;
    final mine = m['mine'] == true;
    final choice = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: VColors.card,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            if (!mine)
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
                child: Row(mainAxisAlignment: MainAxisAlignment.spaceEvenly, children: [
                  for (final e in _reactions)
                    InkWell(
                      borderRadius: BorderRadius.circular(24),
                      onTap: () => Navigator.of(ctx).pop('react:$e'),
                      child: Padding(
                        padding: const EdgeInsets.all(8),
                        child: Text(e, style: TextStyle(fontSize: m['reaction'] == e ? 32 : 26)),
                      ),
                    ),
                ]),
              ),
            ListTile(
              leading: const Icon(Icons.reply, color: VColors.lavender),
              title: const Text('Ответить'),
              onTap: () => Navigator.of(ctx).pop('reply'),
            ),
            if ((m['text'] as String?) != null)
              ListTile(
                leading: const Icon(Icons.copy, color: VColors.lavender),
                title: const Text('Скопировать'),
                onTap: () => Navigator.of(ctx).pop('copy'),
              ),
            if (mine)
              ListTile(
                leading: const Icon(Icons.delete_outline, color: VColors.coral),
                title: const Text('Удалить', style: TextStyle(color: VColors.coral)),
                onTap: () => Navigator.of(ctx).pop('delete'),
              ),
          ]),
        ),
      ),
    );
    if (choice == null || !mounted) return;
    final dio = ref.read(dioProvider);
    try {
      if (choice == 'reply') {
        _reply(m);
      } else if (choice == 'copy') {
        await Clipboard.setData(ClipboardData(text: m['text'] as String));
        _toast('Скопировано');
      } else if (choice == 'delete') {
        final r = await dio.delete('/chat/${m['id']}');
        setState(() => _absorb({'messages': [r.data], 'partner_read_at': _partnerReadAt?.toIso8601String()}));
      } else if (choice.startsWith('react:')) {
        final e = choice.substring(6);
        final r = await dio.post('/chat/${m['id']}/react', data: {'emoji': m['reaction'] == e ? null : e});
        setState(() => _absorb({'messages': [r.data], 'partner_read_at': _partnerReadAt?.toIso8601String()}));
      }
    } catch (e) {
      _toast(errorText(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final partnerName = ref.watch(pairProvider).valueOrNull?['partner_name'] as String?;
    final msgs = _messages;
    final items = <Widget>[];
    String? lastDay;
    for (final m in msgs) {
      final at = DateTime.parse(m['created_at'] as String).toLocal();
      final day = formatDay(at);
      if (day != lastDay) {
        items.add(_DayLabel(text: day == formatDay(DateTime.now()) ? 'Сегодня' : day));
        lastDay = day;
      }
      final read = _partnerReadAt != null && !_partnerReadAt!.isBefore(DateTime.parse(m['created_at'] as String));
      items.add(Dismissible(
        key: ValueKey('swipe-${m['id']}'),
        direction: m['deleted'] == true ? DismissDirection.none : DismissDirection.startToEnd,
        dismissThresholds: const {DismissDirection.startToEnd: 0.25},
        confirmDismiss: (_) async {
          _reply(m);
          return false; // сообщение остаётся на месте — свайп только начинает ответ
        },
        background: const Align(
          alignment: Alignment.centerLeft,
          child: Padding(padding: EdgeInsets.only(left: 8), child: Icon(Icons.reply, color: VColors.gold)),
        ),
        child: _Bubble(
          m: m,
          at: at,
          read: read,
          onLongPress: () => _actions(m),
          onDoubleTap: () => _heart(m),
        ),
      ));
    }

    return Scaffold(
      appBar: AppBar(
        titleSpacing: 0,
        title: Row(children: [
          Initial(name: partnerName, size: 36),
          const SizedBox(width: 10),
          Expanded(child: Text(partnerName ?? 'Партнёр', style: const TextStyle(fontSize: 17))),
        ]),
        actions: [
          IconButton(
            tooltip: 'Звонок — скоро',
            icon: const Icon(Icons.call_outlined, color: VColors.muted),
            onPressed: () => _toast('Звонки появятся вместе с сервером'),
          ),
        ],
      ),
      body: SafeArea(
        child: Column(children: [
          Expanded(
            child: _loading
                ? const Center(child: CircularProgressIndicator())
                : msgs.isEmpty
                    ? _Empty(error: _error, partnerName: partnerName)
                    : ListView(
                        reverse: true,
                        padding: const EdgeInsets.fromLTRB(14, 8, 14, 8),
                        children: [
                          ...items.reversed,
                          if (_hasMore)
                            Center(
                              child: TextButton(
                                onPressed: _loadOlder,
                                child: const Text('Показать ранние', style: TextStyle(color: VColors.muted)),
                              ),
                            ),
                        ],
                      ),
          ),
          if (_showHint && msgs.isNotEmpty)
            Container(
              margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
              padding: const EdgeInsets.fromLTRB(14, 8, 4, 8),
              decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(14)),
              child: Row(children: [
                const Icon(Icons.lightbulb_outline, size: 18, color: VColors.gold),
                const SizedBox(width: 8),
                const Expanded(
                  child: Text('Свайп вправо — ответить · двойное касание — ❤️ · удерживай — ещё',
                      style: TextStyle(color: VColors.soft, fontSize: 13)),
                ),
                IconButton(icon: const Icon(Icons.close, size: 18, color: VColors.muted), onPressed: _hideHint),
              ]),
            ),
          _Composer(
            controller: _text,
            focus: _focus,
            sending: _sending,
            attach: _attach,
            replyTo: _replyTo,
            onClearAttach: () => setState(() => _attach = null),
            onClearReply: () => setState(() => _replyTo = null),
            onSend: _send,
          ),
        ]),
      ),
    );
  }
}

class _Empty extends StatelessWidget {
  const _Empty({this.error, this.partnerName});
  final String? error;
  final String? partnerName;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          const Icon(Icons.chat_bubble_outline, size: 40, color: VColors.gold),
          const SizedBox(height: 12),
          Text(error ?? 'Здесь только вы двое. Напиши ${partnerName ?? 'партнёру'} что-нибудь тёплое.\n\n'
                  'Свайп вправо по сообщению — ответить, двойное касание — ❤️, удерживай — реакции и другое.',
              textAlign: TextAlign.center, style: const TextStyle(color: VColors.soft, height: 1.4)),
        ]),
      ),
    );
  }
}

class _DayLabel extends StatelessWidget {
  const _DayLabel({required this.text});
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 10),
        child: Center(child: Text(text, style: const TextStyle(color: VColors.muted, fontSize: 12))),
      );
}

class _Bubble extends StatelessWidget {
  const _Bubble({required this.m, required this.at, required this.read, required this.onLongPress, this.onDoubleTap});
  final Map<String, dynamic> m;
  final DateTime at;
  final bool read;
  final VoidCallback onLongPress;
  final VoidCallback? onDoubleTap;

  @override
  Widget build(BuildContext context) {
    final mine = m['mine'] == true;
    final deleted = m['deleted'] == true;
    final time = '${at.hour.toString().padLeft(2, '0')}:${at.minute.toString().padLeft(2, '0')}';
    final kind = m['ref_kind'] as String?;
    return Align(
      alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
      child: GestureDetector(
        onLongPress: onLongPress,
        onDoubleTap: onDoubleTap,
        child: Stack(clipBehavior: Clip.none, children: [
          Container(
            constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.78),
            margin: EdgeInsets.only(bottom: m['reaction'] != null ? 18 : 6),
            padding: const EdgeInsets.fromLTRB(14, 10, 12, 8),
            decoration: BoxDecoration(
              color: mine ? VColors.segment : VColors.card,
              borderRadius: BorderRadius.only(
                topLeft: const Radius.circular(18),
                topRight: const Radius.circular(18),
                bottomLeft: Radius.circular(mine ? 18 : 4),
                bottomRight: Radius.circular(mine ? 4 : 18),
              ),
              border: Border.all(color: mine ? const Color(0x33F3C98B) : const Color(0x14FFFFFF)),
            ),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              if (m['reply_text'] != null)
                Container(
                  margin: const EdgeInsets.only(bottom: 6),
                  padding: const EdgeInsets.only(left: 8),
                  decoration: const BoxDecoration(border: Border(left: BorderSide(color: VColors.gold, width: 2))),
                  child: Text(m['reply_text'] as String,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(color: VColors.muted, fontSize: 13)),
                ),
              if (kind != null && !deleted)
                Container(
                  margin: const EdgeInsets.only(bottom: 6),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(color: const Color(0x14FFFFFF), borderRadius: BorderRadius.circular(12)),
                  child: Row(mainAxisSize: MainAxisSize.min, children: [
                    Icon(_refIcons[kind] ?? Icons.link, size: 18, color: VColors.gold),
                    const SizedBox(width: 8),
                    Flexible(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(_refLabels[kind] ?? '', style: const TextStyle(color: VColors.label, fontSize: 11)),
                        if (m['ref_title'] != null)
                          Text(m['ref_title'] as String, style: const TextStyle(fontSize: 14, height: 1.3)),
                      ]),
                    ),
                  ]),
                ),
              if (deleted)
                const Text('Сообщение удалено', style: TextStyle(color: VColors.muted, fontStyle: FontStyle.italic))
              else if (m['text'] != null)
                Text(m['text'] as String, style: const TextStyle(fontSize: 15, height: 1.35)),
              const SizedBox(height: 3),
              Row(mainAxisSize: MainAxisSize.min, children: [
                Text(time, style: const TextStyle(color: VColors.muted, fontSize: 11)),
                if (mine) ...[
                  const SizedBox(width: 4),
                  Icon(read ? Icons.done_all : Icons.done, size: 14, color: read ? VColors.gold : VColors.muted),
                ],
              ]),
            ]),
          ),
          if (m['reaction'] != null)
            Positioned(
              bottom: 0,
              right: mine ? null : 8,
              left: mine ? 8 : null,
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(color: VColors.card2, borderRadius: BorderRadius.circular(12)),
                child: Text(m['reaction'] as String, style: const TextStyle(fontSize: 14)),
              ),
            ),
        ]),
      ),
    );
  }
}

class _Composer extends StatelessWidget {
  const _Composer({
    required this.controller,
    required this.focus,
    required this.sending,
    required this.attach,
    required this.replyTo,
    required this.onClearAttach,
    required this.onClearReply,
    required this.onSend,
  });

  final TextEditingController controller;
  final FocusNode focus;
  final bool sending;
  final Map? attach;
  final Map<String, dynamic>? replyTo;
  final VoidCallback onClearAttach;
  final VoidCallback onClearReply;
  final VoidCallback onSend;

  Widget _chip(IconData icon, String label, String text, VoidCallback onClear) => Container(
        margin: const EdgeInsets.fromLTRB(12, 8, 12, 0),
        padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
        decoration: BoxDecoration(color: VColors.card, borderRadius: BorderRadius.circular(14)),
        child: Row(children: [
          Icon(icon, size: 18, color: VColors.gold),
          const SizedBox(width: 8),
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(label, style: const TextStyle(color: VColors.label, fontSize: 11)),
              Text(text, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 14)),
            ]),
          ),
          IconButton(icon: const Icon(Icons.close, size: 18, color: VColors.muted), onPressed: onClear),
        ]),
      );

  @override
  Widget build(BuildContext context) {
    final a = attach;
    final r = replyTo;
    return Container(
      decoration: const BoxDecoration(
        color: VColors.nav,
        border: Border(top: BorderSide(color: Color(0x0FFFFFFF))),
      ),
      child: Column(mainAxisSize: MainAxisSize.min, children: [
        if (a != null)
          _chip(_refIcons[a['kind']] ?? Icons.link, 'Обсудить: ${_refLabels[a['kind']] ?? ''}',
              (a['title'] ?? '') as String, onClearAttach),
        if (r != null)
          _chip(Icons.reply, 'Ответ', (r['text'] ?? r['ref_title'] ?? '') as String, onClearReply),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 6, 8),
          child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
            Expanded(
              child: TextField(
                controller: controller,
                focusNode: focus,
                minLines: 1,
                maxLines: 5,
                maxLength: 2000,
                textCapitalization: TextCapitalization.sentences,
                decoration: const InputDecoration(hintText: 'Сообщение', counterText: ''),
              ),
            ),
            const SizedBox(width: 4),
            IconButton(
              tooltip: 'Голосовое — скоро',
              icon: const Icon(Icons.mic_none, color: VColors.muted),
              onPressed: () => ScaffoldMessenger.of(context)
                  .showSnackBar(const SnackBar(content: Text('Голосовые появятся вместе с сервером'))),
            ),
            IconButton(
              tooltip: 'Отправить',
              icon: Icon(Icons.send_rounded, color: sending ? VColors.muted : VColors.gold),
              onPressed: sending ? null : onSend,
            ),
          ]),
        ),
      ]),
    );
  }
}
