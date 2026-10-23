import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/theme.dart';
import '../content/content.dart';
import 'blogs.dart';

class BlogProfileScreen extends StatelessWidget {
  const BlogProfileScreen({super.key, this.blogId, this.kind = 'individual'});
  final String? blogId;
  final String kind;
  @override
  Widget build(BuildContext context) => BlogGate(
    child: _Profile(blogId: blogId, kind: kind),
  );
}

class _Profile extends ConsumerStatefulWidget {
  const _Profile({this.blogId, required this.kind});
  final String? blogId;
  final String kind;
  @override
  ConsumerState<_Profile> createState() => _ProfileState();
}

class _ProfileState extends ConsumerState<_Profile>
    with ContentRequests<_Profile> {
  final _name = TextEditingController(), _bio = TextEditingController();
  final _clientId = newBlogRequestId();
  PublicBlog? _blog;
  bool _loading = false, _busy = false, _consent = false;
  Object? _error;
  @override
  void initState() {
    super.initState();
    if (widget.blogId != null) Future.microtask(_load);
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final data = await contentRequest(
        'GET',
        '/blogs/${Uri.encodeComponent(widget.blogId!)}',
      );
      if (!mounted || data == null) return;
      final blog = PublicBlog.fromJson(data as Map);
      setState(() {
        _blog = blog;
        _name.text = blog.name;
        _bio.text = blog.bio;
        _consent = false;
      });
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _save() async {
    if (widget.blogId != null && _blog == null) return;
    if (_busy || !_consent || _name.text.trim().isEmpty) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final blog = _blog;
      final data = await contentRequest(
        blog == null ? 'POST' : 'PATCH',
        blog == null ? '/blogs' : '/blogs/${Uri.encodeComponent(blog.id)}',
        data: {
          if (blog == null) ...{
            'client_id': _clientId,
            'kind': widget.kind,
            'public_consent': true,
            'consent_version': publicBlogConsentVersion,
          },
          if (blog != null) 'revision': blog.revision,
          'name': _name.text.trim(),
          'bio': _bio.text.trim(),
        },
      );
      if (!mounted || data == null) return;
      var updated = PublicBlog.fromJson(data as Map);
      if (blog != null) {
        final confirmed = await contentRequest(
          'POST',
          '/blogs/${Uri.encodeComponent(updated.id)}/consent',
          data: {
            'revision': updated.revision,
            'accepted': true,
            'consent_version': publicBlogConsentVersion,
          },
        );
        if (!mounted || confirmed == null) return;
        updated = PublicBlog.fromJson(confirmed as Map);
      }
      invalidateBlogs(ref);
      if (blog == null) {
        context.replace('/blogs/${Uri.encodeComponent(updated.id)}');
      } else {
        setState(() {
          _blog = updated;
          _consent = false;
        });
      }
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _agree() async {
    final blog = _blog;
    if (blog == null || !blog.canEdit || !_consent || _busy) return;
    await _action('consent', {
      'revision': blog.revision,
      'accepted': true,
      'consent_version': publicBlogConsentVersion,
    });
  }

  Future<void> _withdraw() async {
    final yes = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Снять блог с публикации?'),
        content: const Text(
          'Профиль и его видео перестанут быть доступны другим пользователям. Черновики останутся в кабинете. Для блога пары достаточно решения одного участника.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Оставить'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Снять блог'),
          ),
        ],
      ),
    );
    if (yes == true && mounted && _blog != null) {
      await _action('withdraw', {'revision': _blog!.revision});
    }
  }

  Future<void> _action(String action, Map<String, dynamic> data) async {
    if (_busy || _blog == null) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final response = await contentRequest(
        'POST',
        '/blogs/${Uri.encodeComponent(_blog!.id)}/$action',
        data: data,
      );
      if (!mounted || response == null) return;
      setState(() {
        _blog = PublicBlog.fromJson(response as Map);
        _consent = false;
      });
      invalidateBlogs(ref);
    } catch (e) {
      if (mounted) setState(() => _error = e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _name.dispose();
    _bio.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final blog = _blog;
    final couple = blog?.couple ?? widget.kind == 'couple';
    final editable = !_busy && (blog?.canEdit ?? widget.blogId == null);
    final changed =
        blog == null ||
        _name.text.trim() != blog.name ||
        _bio.text.trim() != blog.bio;
    return Scaffold(
      appBar: AppBar(
        title: Text(couple ? 'Профиль блога пары' : 'Публичный профиль'),
      ),
      body: SafeArea(
        child: _loading
            ? const Center(child: CircularProgressIndicator())
            : ListView(
                padding: const EdgeInsets.fromLTRB(20, 8, 20, 32),
                children: [
                  const Text(
                    'Так тебя увидят другие',
                    style: TextStyle(fontSize: 25, fontWeight: FontWeight.w700),
                  ),
                  const SizedBox(height: 10),
                  const Text(
                    'Укажи отдельное публичное имя. Имя из личного профиля, адрес почты и сведения о паре не добавляются автоматически.',
                    style: TextStyle(color: VColors.soft, height: 1.5),
                  ),
                  const SizedBox(height: 20),
                  TextField(
                    key: const ValueKey('blog-public-name'),
                    controller: _name,
                    enabled: editable,
                    maxLength: 120,
                    onChanged: (_) => setState(() => _consent = false),
                    decoration: const InputDecoration(
                      labelText: 'Публичное имя',
                    ),
                  ),
                  const SizedBox(height: 12),
                  TextField(
                    key: const ValueKey('blog-public-bio'),
                    controller: _bio,
                    enabled: editable,
                    minLines: 3,
                    maxLines: 6,
                    maxLength: 1600,
                    onChanged: (_) => setState(() => _consent = false),
                    decoration: const InputDecoration(
                      labelText: 'О чём этот блог',
                    ),
                  ),
                  if (blog != null) ...[
                    const SizedBox(height: 12),
                    Text(
                      blog.statusLabel,
                      style: const TextStyle(color: VColors.gold),
                    ),
                  ],
                  const SizedBox(height: 16),
                  Text(
                    couple
                        ? 'Для публичного профиля нужны согласия обоих участников. Каждое видео вы также подтверждаете отдельно — до проверки редакцией.'
                        : 'Создание профиля не публикует видео. Каждую публикацию нужно отправить на проверку отдельно.',
                    style: const TextStyle(color: VColors.soft, height: 1.5),
                  ),
                  if (blog != null && changed) ...[
                    const SizedBox(height: 12),
                    const Text(
                      'Изменение имени или описания снимет текущие публикации. Понадобятся новые согласия на профиль и повторная отправка видео.',
                      style: TextStyle(color: VColors.gold),
                    ),
                  ],
                  if (editable ||
                      (blog?.status == 'pending_consent' &&
                          blog?.myConsent == false)) ...[
                    const SizedBox(height: 12),
                    CheckboxListTile(
                      key: const ValueKey('blog-profile-consent'),
                      value: _consent,
                      onChanged: !editable
                          ? null
                          : (value) => setState(() => _consent = value == true),
                      contentPadding: EdgeInsets.zero,
                      title: Text(
                        couple
                            ? 'Согласен(на) сделать именно этот профиль пары публичным'
                            : 'Согласен(на) сделать это имя и описание публичными',
                      ),
                    ),
                    const SizedBox(height: 12),
                    if (changed)
                      FilledButton(
                        onPressed:
                            editable && _consent && _name.text.trim().isNotEmpty
                            ? _save
                            : null,
                        child: Text(
                          blog == null
                              ? (couple
                                    ? 'Предложить профиль партнёру'
                                    : 'Создать публичный профиль')
                              : 'Сохранить и подтвердить изменения',
                        ),
                      )
                    else if (blog.myConsent == false ||
                        blog.status == 'withdrawn')
                      FilledButton(
                        onPressed: _consent && editable ? _agree : null,
                        child: const Text('Подтвердить публичный профиль'),
                      ),
                  ],
                  if (_busy)
                    const Padding(
                      padding: EdgeInsets.all(14),
                      child: LinearProgressIndicator(),
                    ),
                  if (_error != null)
                    ContentError(
                      error: blogErrorText(_error!),
                      onRetry: _busy
                          ? () {}
                          : blog == null
                          ? () => setState(() => _error = null)
                          : _load,
                      retryLabel: blog == null
                          ? 'Вернуться к форме'
                          : 'Обновить профиль',
                    ),
                  if (blog != null && blog.status != 'withdrawn') ...[
                    const SizedBox(height: 28),
                    OutlinedButton(
                      onPressed: _busy ? null : _withdraw,
                      child: const Text('Снять блог с публикации'),
                    ),
                  ],
                ],
              ),
      ),
    );
  }
}
