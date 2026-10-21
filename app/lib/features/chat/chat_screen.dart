import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/chat_presence.dart';
import '../../core/format.dart';
import '../../core/privacy.dart' show PinGate;
import '../../core/theme.dart';
import '../../core/top_banner.dart';
import '../../core/ui.dart';
import '../calls/call_screen.dart';
import '../today/today_screen.dart' show pairProvider;
import 'attachment_download.dart';
import 'attachment_models.dart';
import 'chat_attachments.dart';

/// Непрочитанные сообщения партнёра — для значка в шапке «Сегодня».
final chatUnreadProvider = FutureProvider.autoDispose<int>((ref) async {
  final r = await ref.read(dioProvider).get('/chat/unread');
  return (r.data as Map)['count'] as int;
});

/// Открыть чат; с вложением — «Обсудить» совпадение, план, приглашение или письмо.
void openChat(BuildContext context, {String? kind, String? id, String? title}) {
  context.push(
    '/chat',
    extra: kind == null ? null : {'kind': kind, 'id': id, 'title': title},
  );
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

class _ChatScreenState extends ConsumerState<ChatScreen>
    with WidgetsBindingObserver, RouteAware {
  final _text = TextEditingController();
  final _focus = FocusNode();
  final Map<String, Map<String, dynamic>> _byId = {};
  final Map<String, CancelToken> _downloads = {};
  final Map<String, double?> _downloadProgress = {};
  late final Dio _dio;
  ModalRoute<dynamic>? _route;
  AppLifecycleState _lifecycle = AppLifecycleState.resumed;
  bool _routeVisible = false;
  bool _refreshing = false;
  bool _loadingOlder = false;
  bool _pickingFile = false;
  bool _uploading = false;
  bool _posting = false;
  double? _uploadProgress;
  CancelToken? _uploadCancel;
  SelectedChatFile? _file;
  _OutgoingDraft? _draft;
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
    _dio = ref.read(dioProvider);
    _lifecycle =
        WidgetsBinding.instance.lifecycleState ?? AppLifecycleState.resumed;
    WidgetsBinding.instance.addObserver(this);
    PinGate.lockedListenable.addListener(_updatePresence);
    _attach = widget.attach;
    if (_attach != null) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) _focus.requestFocus();
      });
    }
    secureStorage.read(key: _hintKey).then((v) {
      if (mounted && v == null) setState(() => _showHint = true);
    });
    // Poll only a visible foreground chat. Covered/background screens must not
    // mark a partner's messages read or suppress their incoming notification.
    _poll = Timer.periodic(const Duration(seconds: 4), (_) => _refresh());
  }

  bool get _visible =>
      _routeVisible &&
      (_route?.isCurrent ?? false) &&
      _lifecycle == AppLifecycleState.resumed &&
      !PinGate.isLocked;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final route = ModalRoute.of(context);
    if (identical(_route, route)) return;
    chatRouteObserver.unsubscribe(this);
    _route = route;
    if (route != null) chatRouteObserver.subscribe(this, route);
    _routeVisible = route?.isCurrent ?? false;
    _updatePresence();
  }

  void _updatePresence() {
    setChatScreenVisible(this, _visible);
    if (_visible) {
      // Remove any snackbar queued by the preceding screen before opening the
      // keyboard. All feedback produced in this chat uses the top overlay.
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted || !_visible) return;
        final messenger = ScaffoldMessenger.maybeOf(context);
        messenger?.clearSnackBars();
        messenger?.removeCurrentSnackBar();
      });
      unawaited(_refresh(markRead: true));
    }
  }

  @override
  void didPush() {
    _routeVisible = true;
    _updatePresence();
  }

  @override
  void didPopNext() {
    _routeVisible = true;
    _updatePresence();
  }

  @override
  void didPushNext() {
    _routeVisible = false;
    _updatePresence();
  }

  @override
  void didPop() {
    _routeVisible = false;
    _updatePresence();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _lifecycle = state;
    _updatePresence();
  }

  @override
  void dispose() {
    chatRouteObserver.unsubscribe(this);
    WidgetsBinding.instance.removeObserver(this);
    PinGate.lockedListenable.removeListener(_updatePresence);
    setChatScreenVisible(this, false);
    _poll?.cancel();
    _uploadCancel?.cancel();
    for (final token in _downloads.values) {
      token.cancel();
    }
    if (!_posting) unawaited(_discardUploaded(_file));
    _text.dispose();
    _focus.dispose();
    super.dispose();
  }

  List<Map<String, dynamic>> get _messages {
    final list = _byId.values.toList()
      ..sort(
        (a, b) =>
            (a['created_at'] as String).compareTo(b['created_at'] as String),
      );
    return list;
  }

  void _absorb(Map data) {
    for (final m in (data['messages'] as List)) {
      final msg = Map<String, dynamic>.from(m as Map);
      // Время сравниваем как текст: приводим к UTC, чтобы порядок был верным.
      msg['created_at'] = DateTime.parse(
        msg['created_at'] as String,
      ).toUtc().toIso8601String();
      _byId[msg['id'] as String] = msg;
      // The POST response can be lost after the server commits. History carries
      // our idempotency key too, so polling can acknowledge that exact draft.
      if (msg['mine'] == true &&
          msg['client_message_id'] == _draft?.id &&
          _draft != null) {
        _completeDraft();
      }
    }
    final read = data['partner_read_at'] as String?;
    _partnerReadAt = read == null ? null : DateTime.parse(read).toUtc();
  }

  Future<void> _refresh({bool markRead = false}) async {
    if (!_visible || _refreshing) return;
    _refreshing = true;
    try {
      final before = _byId.length;
      final lastPartner = _messages
          .where((m) => m['mine'] != true)
          .lastOrNull?['id'];
      final r = await ref.read(dioProvider).get('/chat');
      if (!mounted) return;
      final data = r.data as Map;
      setState(() {
        if (before == 0) _hasMore = data['has_more'] == true;
        _absorb(data);
        _loading = false;
        _error = null;
      });
      final newLastPartner = _messages
          .where((m) => m['mine'] != true)
          .lastOrNull?['id'];
      if (_visible && (markRead || newLastPartner != lastPartner)) {
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
    } finally {
      _refreshing = false;
    }
  }

  Future<void> _loadOlder() async {
    final first = _messages.firstOrNull;
    if (first == null || _loadingOlder) return;
    setState(() => _loadingOlder = true);
    try {
      final r = await ref
          .read(dioProvider)
          .get('/chat', queryParameters: {'before': first['created_at']});
      if (!mounted) return;
      final data = r.data as Map;
      setState(() {
        _absorb(data);
        _hasMore = data['has_more'] == true;
      });
    } catch (e) {
      if (mounted) _toast(errorText(e));
    } finally {
      if (mounted) setState(() => _loadingOlder = false);
    }
  }

  void _toast(String text) {
    if (!mounted) return;
    showTopBanner(Overlay.maybeOf(context, rootOverlay: true), text: text);
  }

  Future<void> _pickFile() async {
    if (_sending || _pickingFile || _draft != null) return;
    setState(() => _pickingFile = true);
    try {
      final chosen = await pickChatFile();
      if (chosen == null || !mounted) return;
      final previous = _file;
      setState(() => _file = chosen);
      unawaited(_discardUploaded(previous));
    } catch (e) {
      _toast(chatFileErrorText(e));
    } finally {
      if (mounted) setState(() => _pickingFile = false);
    }
  }

  Future<void> _discardUploaded(SelectedChatFile? file) async {
    final uploaded = file?.uploaded;
    if (uploaded == null) return;
    try {
      // The server only permits deleting a still-unattached upload owned by us.
      // An already-delivered message must be deleted via the message action.
      await _dio.delete(chatAttachmentPath(uploaded.id));
    } catch (_) {
      // Failed cleanup is retried by the server's 24-hour orphan expiry.
    }
  }

  void _removeFile() {
    if (_sending || _draft != null) return;
    final file = _file;
    setState(() => _file = null);
    unawaited(_discardUploaded(file));
  }

  void _discardDraft() {
    if (_sending) return;
    final file = _file;
    setState(_completeDraft);
    unawaited(_discardUploaded(file));
  }

  void _completeDraft() {
    _draft = null;
    _text.clear();
    _attach = null;
    _replyTo = null;
    _file = null;
  }

  Future<void> _openFile(ChatAttachment attachment) async {
    if (_downloads.containsKey(attachment.id)) return;
    final token = CancelToken();
    setState(() {
      _downloads[attachment.id] = token;
      _downloadProgress[attachment.id] = null;
    });
    try {
      final notice = await downloadChatAttachment(
        _dio,
        attachment,
        cancelToken: token,
        onProgress: (got, total) {
          if (mounted) {
            setState(
              () => _downloadProgress[attachment.id] = total > 0
                  ? (got / total).clamp(0.0, 1.0)
                  : null,
            );
          }
        },
      );
      if (notice != null) _toast(notice);
    } catch (e) {
      if (!(e is DioException &&
          CancelToken.isCancel(e) &&
          e.error is! ChatFileException)) {
        _toast(chatFileErrorText(e));
      }
    } finally {
      if (mounted) {
        setState(() {
          _downloads.remove(attachment.id);
          _downloadProgress.remove(attachment.id);
        });
      }
    }
  }

  Future<void> _send() async {
    if (_sending || _pickingFile) return;
    final text = _text.text.trim();
    if (_draft == null && text.isEmpty && _attach == null && _file == null) {
      return;
    }
    final draft = _draft ??= _OutgoingDraft(
      id: newChatMessageId(),
      text: text,
      reference: _attach == null ? null : Map<String, dynamic>.from(_attach!),
      replyId: _replyTo?['id'] as String?,
      file: _file,
    );
    setState(() => _sending = true);
    var postStarted = false;
    try {
      final file = draft.file;
      if (file != null && file.uploaded == null) {
        final cancel = CancelToken();
        _uploadCancel = cancel;
        setState(() {
          _uploading = true;
          _uploadProgress = null;
        });
        await uploadChatFile(
          _dio,
          file,
          cancelToken: cancel,
          onProgress: (sent, total) {
            if (mounted) {
              setState(
                () => _uploadProgress = total > 0
                    ? (sent / total).clamp(0.0, 1.0)
                    : null,
              );
            }
          },
        );
        if (cancel.isCancelled) throw cancel.cancelError!;
      }
      if (!mounted) {
        await _discardUploaded(file);
        return;
      }
      setState(() => _uploading = false);
      _uploadCancel = null;
      _posting = postStarted = true;
      final r = await _dio.post('/chat', data: draft.toRequest());
      if (!mounted) return;
      setState(() {
        _absorb({
          'messages': [r.data],
          'partner_read_at': _partnerReadAt?.toIso8601String(),
        });
        if (_draft?.id == draft.id) _completeDraft();
      });
    } catch (e) {
      if (!mounted) {
        await _discardUploaded(draft.file);
        return;
      }
      final status = e is DioException ? e.response?.statusCode : null;
      final rejected =
          status != null &&
          status >= 400 &&
          status < 500 &&
          status != 408 &&
          status != 429;
      if (!postStarted || rejected) setState(() => _draft = null);
      if (e is DioException &&
          CancelToken.isCancel(e) &&
          e.error is! ChatFileException) {
        _toast('Загрузка остановлена. Файл остался в черновике');
      } else {
        _toast(chatFileErrorText(e));
      }
    } finally {
      _posting = false;
      _uploadCancel = null;
      if (mounted) {
        setState(() {
          _sending = false;
          _uploading = false;
          _uploadProgress = null;
        });
      }
    }
  }

  void _reply(Map<String, dynamic> m) {
    if (m['deleted'] == true || _sending || _draft != null) return;
    HapticFeedback.selectionClick();
    setState(() => _replyTo = m);
    _focus.requestFocus();
  }

  Future<void> _heart(Map<String, dynamic> m) async {
    if (m['mine'] == true || m['deleted'] == true) return;
    HapticFeedback.lightImpact();
    try {
      final r = await ref
          .read(dioProvider)
          .post(
            '/chat/${m['id']}/react',
            data: {'emoji': m['reaction'] == '❤️' ? null : '❤️'},
          );
      if (mounted) {
        setState(
          () => _absorb({
            'messages': [r.data],
            'partner_read_at': _partnerReadAt?.toIso8601String(),
          }),
        );
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
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(24)),
      ),
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (!mine)
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
                  child: Row(
                    mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                    children: [
                      for (final e in _reactions)
                        InkWell(
                          borderRadius: BorderRadius.circular(24),
                          onTap: () => Navigator.of(ctx).pop('react:$e'),
                          child: Padding(
                            padding: const EdgeInsets.all(8),
                            child: Text(
                              e,
                              style: TextStyle(
                                fontSize: m['reaction'] == e ? 32 : 26,
                              ),
                            ),
                          ),
                        ),
                    ],
                  ),
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
                  leading: const Icon(
                    Icons.delete_outline,
                    color: VColors.coral,
                  ),
                  title: const Text(
                    'Удалить',
                    style: TextStyle(color: VColors.coral),
                  ),
                  onTap: () => Navigator.of(ctx).pop('delete'),
                ),
            ],
          ),
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
        if (!mounted) return;
        setState(
          () => _absorb({
            'messages': [r.data],
            'partner_read_at': _partnerReadAt?.toIso8601String(),
          }),
        );
      } else if (choice.startsWith('react:')) {
        final e = choice.substring(6);
        final r = await dio.post(
          '/chat/${m['id']}/react',
          data: {'emoji': m['reaction'] == e ? null : e},
        );
        if (!mounted) return;
        setState(
          () => _absorb({
            'messages': [r.data],
            'partner_read_at': _partnerReadAt?.toIso8601String(),
          }),
        );
      }
    } catch (e) {
      _toast(errorText(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final partnerName =
        ref.watch(pairProvider).valueOrNull?['partner_name'] as String?;
    final msgs = _messages;
    final items = <Widget>[];
    String? lastDay;
    for (final m in msgs) {
      final at = DateTime.parse(m['created_at'] as String).toLocal();
      final day = formatDay(at);
      if (day != lastDay) {
        items.add(
          _DayLabel(text: day == formatDay(DateTime.now()) ? 'Сегодня' : day),
        );
        lastDay = day;
      }
      final read =
          _partnerReadAt != null &&
          !_partnerReadAt!.isBefore(DateTime.parse(m['created_at'] as String));
      items.add(
        Dismissible(
          key: ValueKey('swipe-${m['id']}'),
          direction: m['deleted'] == true
              ? DismissDirection.none
              : DismissDirection.startToEnd,
          dismissThresholds: const {DismissDirection.startToEnd: 0.25},
          confirmDismiss: (_) async {
            _reply(m);
            return false; // сообщение остаётся на месте — свайп только начинает ответ
          },
          background: const Align(
            alignment: Alignment.centerLeft,
            child: Padding(
              padding: EdgeInsets.only(left: 8),
              child: Icon(Icons.reply, color: VColors.gold),
            ),
          ),
          child: _Bubble(
            m: m,
            at: at,
            read: read,
            downloads: _downloads,
            downloadProgress: _downloadProgress,
            onOpenFile: _openFile,
            onLongPress: () => _actions(m),
            onDoubleTap: () => _heart(m),
          ),
        ),
      );
    }

    return Scaffold(
      appBar: AppBar(
        titleSpacing: 0,
        title: Row(
          children: [
            Initial(name: partnerName, size: 36),
            const SizedBox(width: 10),
            Expanded(
              child: Text(
                partnerName ?? 'Партнёр',
                style: const TextStyle(fontSize: 17),
              ),
            ),
          ],
        ),
        actions: [
          IconButton(
            tooltip: 'Видеозвонок',
            icon: const Icon(Icons.videocam_outlined, color: VColors.gold),
            onPressed: () => openVideoCall(context),
          ),
        ],
      ),
      body: SafeArea(
        child: Column(
          children: [
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
                              onPressed: _loadingOlder ? null : _loadOlder,
                              child: Text(
                                _loadingOlder
                                    ? 'Загружаем…'
                                    : 'Показать ранние',
                                style: const TextStyle(color: VColors.muted),
                              ),
                            ),
                          ),
                      ],
                    ),
            ),
            if (_showHint && msgs.isNotEmpty)
              Container(
                margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
                padding: const EdgeInsets.fromLTRB(14, 8, 4, 8),
                decoration: BoxDecoration(
                  color: VColors.card,
                  borderRadius: BorderRadius.circular(14),
                ),
                child: Row(
                  children: [
                    const Icon(
                      Icons.lightbulb_outline,
                      size: 18,
                      color: VColors.gold,
                    ),
                    const SizedBox(width: 8),
                    const Expanded(
                      child: Text(
                        'Свайп вправо — ответить · двойное касание — ❤️ · удерживай — ещё',
                        style: TextStyle(color: VColors.soft, fontSize: 13),
                      ),
                    ),
                    IconButton(
                      icon: const Icon(
                        Icons.close,
                        size: 18,
                        color: VColors.muted,
                      ),
                      onPressed: _hideHint,
                    ),
                  ],
                ),
              ),
            _Composer(
              controller: _text,
              focus: _focus,
              sending: _sending,
              retryPending: _draft != null && !_sending,
              pickingFile: _pickingFile,
              file: _file,
              uploading: _uploading,
              uploadProgress: _uploadProgress,
              attach: _attach,
              replyTo: _replyTo,
              onClearAttach: () {
                if (!_sending && _draft == null) setState(() => _attach = null);
              },
              onClearReply: () {
                if (!_sending && _draft == null) {
                  setState(() => _replyTo = null);
                }
              },
              onPickFile: _pickFile,
              onRemoveFile: _removeFile,
              onCancelUpload: () => _uploadCancel?.cancel(),
              onDiscardDraft: _discardDraft,
              onSend: _send,
            ),
          ],
        ),
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
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(
              Icons.chat_bubble_outline,
              size: 40,
              color: VColors.gold,
            ),
            const SizedBox(height: 12),
            Text(
              error ??
                  'Здесь только вы двое. Напиши ${partnerName ?? 'партнёру'} что-нибудь тёплое.\n\n'
                      'Свайп вправо по сообщению — ответить, двойное касание — ❤️, удерживай — реакции и другое.',
              textAlign: TextAlign.center,
              style: const TextStyle(color: VColors.soft, height: 1.4),
            ),
          ],
        ),
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
    child: Center(
      child: Text(
        text,
        style: const TextStyle(color: VColors.muted, fontSize: 12),
      ),
    ),
  );
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.m,
    required this.at,
    required this.read,
    required this.onLongPress,
    required this.downloads,
    required this.downloadProgress,
    required this.onOpenFile,
    this.onDoubleTap,
  });
  final Map<String, dynamic> m;
  final DateTime at;
  final bool read;
  final VoidCallback onLongPress;
  final VoidCallback? onDoubleTap;
  final Map<String, CancelToken> downloads;
  final Map<String, double?> downloadProgress;
  final ValueChanged<ChatAttachment> onOpenFile;

  @override
  Widget build(BuildContext context) {
    final mine = m['mine'] == true;
    final deleted = m['deleted'] == true;
    final time =
        '${at.hour.toString().padLeft(2, '0')}:${at.minute.toString().padLeft(2, '0')}';
    final kind = m['ref_kind'] as String?;
    return Align(
      alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
      child: GestureDetector(
        onLongPress: onLongPress,
        onDoubleTap: onDoubleTap,
        child: Stack(
          clipBehavior: Clip.none,
          children: [
            Container(
              constraints: BoxConstraints(
                maxWidth: MediaQuery.of(context).size.width * 0.78,
              ),
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
                border: Border.all(
                  color: mine
                      ? const Color(0x33F3C98B)
                      : const Color(0x14FFFFFF),
                ),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (m['reply_text'] != null)
                    Container(
                      margin: const EdgeInsets.only(bottom: 6),
                      padding: const EdgeInsets.only(left: 8),
                      decoration: const BoxDecoration(
                        border: Border(
                          left: BorderSide(color: VColors.gold, width: 2),
                        ),
                      ),
                      child: Text(
                        m['reply_text'] as String,
                        maxLines: 2,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(
                          color: VColors.muted,
                          fontSize: 13,
                        ),
                      ),
                    ),
                  if (kind != null && !deleted)
                    Container(
                      margin: const EdgeInsets.only(bottom: 6),
                      padding: const EdgeInsets.all(10),
                      decoration: BoxDecoration(
                        color: const Color(0x14FFFFFF),
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Icon(
                            _refIcons[kind] ?? Icons.link,
                            size: 18,
                            color: VColors.gold,
                          ),
                          const SizedBox(width: 8),
                          Flexible(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(
                                  _refLabels[kind] ?? '',
                                  style: const TextStyle(
                                    color: VColors.label,
                                    fontSize: 11,
                                  ),
                                ),
                                if (m['ref_title'] != null)
                                  Text(
                                    m['ref_title'] as String,
                                    style: const TextStyle(
                                      fontSize: 14,
                                      height: 1.3,
                                    ),
                                  ),
                              ],
                            ),
                          ),
                        ],
                      ),
                    ),
                  if (deleted)
                    const Text(
                      'Сообщение удалено',
                      style: TextStyle(
                        color: VColors.muted,
                        fontStyle: FontStyle.italic,
                      ),
                    )
                  else if (m['text'] != null)
                    Text(
                      m['text'] as String,
                      style: const TextStyle(fontSize: 15, height: 1.35),
                    ),
                  for (final attachment in messageAttachments(m))
                    Padding(
                      padding: EdgeInsets.only(
                        top: m['text'] != null ? 8 : 0,
                        bottom: 4,
                      ),
                      child: ChatAttachmentTile(
                        attachment: attachment,
                        downloading: downloads.containsKey(attachment.id),
                        progress: downloadProgress[attachment.id],
                        onOpen: () => onOpenFile(attachment),
                        onCancel: () => downloads[attachment.id]?.cancel(),
                      ),
                    ),
                  const SizedBox(height: 3),
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(
                        time,
                        style: const TextStyle(
                          color: VColors.muted,
                          fontSize: 11,
                        ),
                      ),
                      if (mine) ...[
                        const SizedBox(width: 4),
                        Icon(
                          read ? Icons.done_all : Icons.done,
                          size: 14,
                          color: read ? VColors.gold : VColors.muted,
                        ),
                      ],
                    ],
                  ),
                ],
              ),
            ),
            if (m['reaction'] != null)
              Positioned(
                bottom: 0,
                right: mine ? null : 8,
                left: mine ? 8 : null,
                child: Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 6,
                    vertical: 2,
                  ),
                  decoration: BoxDecoration(
                    color: VColors.card2,
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Text(
                    m['reaction'] as String,
                    style: const TextStyle(fontSize: 14),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _Composer extends StatelessWidget {
  const _Composer({
    required this.controller,
    required this.focus,
    required this.sending,
    required this.retryPending,
    required this.pickingFile,
    required this.file,
    required this.uploading,
    required this.uploadProgress,
    required this.attach,
    required this.replyTo,
    required this.onClearAttach,
    required this.onClearReply,
    required this.onPickFile,
    required this.onRemoveFile,
    required this.onCancelUpload,
    required this.onDiscardDraft,
    required this.onSend,
  });

  final TextEditingController controller;
  final FocusNode focus;
  final bool sending;
  final bool retryPending;
  final bool pickingFile;
  final SelectedChatFile? file;
  final bool uploading;
  final double? uploadProgress;
  final Map? attach;
  final Map<String, dynamic>? replyTo;
  final VoidCallback onClearAttach;
  final VoidCallback onClearReply;
  final VoidCallback onPickFile;
  final VoidCallback onRemoveFile;
  final VoidCallback onCancelUpload;
  final VoidCallback onDiscardDraft;
  final VoidCallback onSend;

  Widget _chip(
    IconData icon,
    String label,
    String text,
    VoidCallback onClear,
  ) => Container(
    margin: const EdgeInsets.fromLTRB(12, 8, 12, 0),
    padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
    decoration: BoxDecoration(
      color: VColors.card,
      borderRadius: BorderRadius.circular(14),
    ),
    child: Row(
      children: [
        Icon(icon, size: 18, color: VColors.gold),
        const SizedBox(width: 8),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                label,
                style: const TextStyle(color: VColors.label, fontSize: 11),
              ),
              Text(
                text,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 14),
              ),
            ],
          ),
        ),
        IconButton(
          icon: const Icon(Icons.close, size: 18, color: VColors.muted),
          onPressed: onClear,
        ),
      ],
    ),
  );

  @override
  Widget build(BuildContext context) {
    final a = attach;
    final r = replyTo;
    final editingLocked = sending || retryPending;
    final replyFiles = r == null
        ? const <ChatAttachment>[]
        : messageAttachments(r);
    return Container(
      decoration: const BoxDecoration(
        color: VColors.nav,
        border: Border(top: BorderSide(color: Color(0x0FFFFFFF))),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (a != null)
            _chip(
              _refIcons[a['kind']] ?? Icons.link,
              'Обсудить: ${_refLabels[a['kind']] ?? ''}',
              (a['title'] ?? '') as String,
              onClearAttach,
            ),
          if (r != null)
            _chip(
              Icons.reply,
              'Ответ',
              (r['text'] ??
                      r['ref_title'] ??
                      (replyFiles.isEmpty
                          ? 'Сообщение'
                          : replyFiles.first.name))
                  as String,
              onClearReply,
            ),
          if (file != null)
            PendingChatFileTile(
              file: file!,
              sending: editingLocked,
              uploading: uploading,
              progress: uploadProgress,
              onRemove: onRemoveFile,
              onCancel: onCancelUpload,
            ),
          if (retryPending)
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 8, 8, 0),
              child: Row(
                children: [
                  const Expanded(
                    child: Text(
                      'Отправка не подтверждена. Повтори — сообщение не продублируется.',
                      style: TextStyle(color: VColors.muted, fontSize: 12),
                    ),
                  ),
                  TextButton(
                    onPressed: onDiscardDraft,
                    child: const Text('Убрать черновик'),
                  ),
                ],
              ),
            ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 6, 8),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                Expanded(
                  child: TextField(
                    controller: controller,
                    focusNode: focus,
                    readOnly: editingLocked,
                    minLines: 1,
                    maxLines: 5,
                    maxLength: 2000,
                    textCapitalization: TextCapitalization.sentences,
                    decoration: InputDecoration(
                      hintText: file == null ? 'Сообщение' : 'Подпись к файлу',
                      counterText: '',
                    ),
                  ),
                ),
                const SizedBox(width: 4),
                IconButton(
                  tooltip: 'Прикрепить файл до 25 МБ',
                  icon: pickingFile
                      ? const SizedBox(
                          width: 20,
                          height: 20,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.attach_file, color: VColors.muted),
                  onPressed: editingLocked || pickingFile ? null : onPickFile,
                ),
                IconButton(
                  tooltip: retryPending ? 'Повторить отправку' : 'Отправить',
                  icon: Icon(
                    retryPending ? Icons.refresh : Icons.send_rounded,
                    color: sending ? VColors.muted : VColors.gold,
                  ),
                  onPressed: sending || pickingFile ? null : onSend,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// A failed POST keeps the exact same payload and idempotency key. The composer
/// remains a retryable draft until POST or a subsequent history poll confirms it.
class _OutgoingDraft {
  const _OutgoingDraft({
    required this.id,
    required this.text,
    this.reference,
    this.replyId,
    this.file,
  });
  final String id;
  final String text;
  final Map<String, dynamic>? reference;
  final String? replyId;
  final SelectedChatFile? file;

  Map<String, dynamic> toRequest() => {
    'client_message_id': id,
    if (text.isNotEmpty) 'text': text,
    if (reference != null) ...{
      'ref_kind': reference!['kind'],
      'ref_id': reference!['id']?.toString(),
      'ref_title': reference!['title'],
    },
    if (replyId != null) 'reply_to': replyId,
    if (file?.uploaded != null) 'attachment_ids': [file!.uploaded!.id],
  };
}
