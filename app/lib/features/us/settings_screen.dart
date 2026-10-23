import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api.dart';
import '../../core/network_diagnostics_screen.dart';
import '../../core/privacy.dart';
import '../../core/updater.dart';
import '../../core/theme.dart';
import '../../core/ui.dart';
import '../auth/city_screen.dart';
import '../today/today_screen.dart' show pairProvider;
import 'us_screen.dart';

final _settingsProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/settings');
  return Map<String, dynamic>.from(r.data as Map);
});

final _cityProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/me/city');
  return Map<String, dynamic>.from(r.data as Map);
});

final _meProvider = FutureProvider.autoDispose<Map<String, dynamic>>((ref) async {
  final r = await ref.read(dioProvider).get('/auth/me');
  return Map<String, dynamic>.from(r.data as Map);
});

/// Настройки: уведомления, защита, пара, аккаунт.
class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key});

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  bool? _pin;
  bool? _secure;

  @override
  void initState() {
    super.initState();
    hasPin().then((v) {
      if (mounted) setState(() => _pin = v);
    });
    readSecureScreen().then((v) {
      if (mounted) setState(() => _secure = v);
    });
  }

  void _toast(String text) =>
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(text)));

  Future<void> _setPush(Map<String, dynamic> body) async {
    try {
      await ref.read(dioProvider).put('/settings', data: body);
      ref.invalidate(_settingsProvider);
    } catch (e) {
      _toast(errorText(e));
    }
  }

  Future<void> _togglePin(bool on) async {
    if (!on) {
      await clearPin();
      setState(() => _pin = false);
      _toast('Код-пароль выключен');
      return;
    }
    final ok = await Navigator.of(context).push<bool>(
      MaterialPageRoute(fullscreenDialog: true, builder: (_) => const _SetPinScreen()),
    );
    if (ok == true) {
      setState(() => _pin = true);
      _toast('Код-пароль включён');
    }
  }

  Future<void> _toggleSecure(bool on) async {
    await setSecureScreen(on);
    setState(() => _secure = on);
  }

  Future<bool> _confirm(String title, String text, String action, {bool danger = false}) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: VColors.card,
        title: Text(title),
        content: Text(text),
        actions: [
          TextButton(onPressed: () => Navigator.of(ctx).pop(false), child: const Text('Отмена')),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(true),
            child: Text(action, style: TextStyle(color: danger ? VColors.coral : VColors.gold)),
          ),
        ],
      ),
    );
    return ok == true;
  }

  Future<void> _pause(bool on) async {
    if (on &&
        !await _confirm(
          'Поставить паузу?',
          'Уведомления друг другу приходить не будут. Всё сохранится, партнёр узнает о паузе. '
              'Снять её сможешь только ты.',
          'Поставить',
        )) {
      return;
    }
    try {
      await ref.read(dioProvider).post('/pairs/pause', data: {'on': on});
      ref.invalidate(pairProvider);
    } catch (e) {
      _toast(errorText(e));
    }
  }

  Future<void> _leave(String? partnerName) async {
    if (!await _confirm(
      'Завершить пару?',
      'Удалятся у вас обоих: ответы, совпадения, планы, даты, напоминания, письма, сюрпризы и заметки. '
          '${partnerName ?? 'Партнёр'} получит уведомление. Анкета «о себе» и тихие желания останутся. '
          'Отменить нельзя.',
      'Завершить',
      danger: true,
    )) {
      return;
    }
    try {
      await ref.read(dioProvider).post('/pairs/leave');
      ref.invalidate(pairProvider);
      invalidateUs(ref);
      if (mounted) context.go('/today');
    } catch (e) {
      _toast(errorText(e));
    }
  }

  Future<void> _delete() async {
    if (!await _confirm(
      'Удалить аккаунт?',
      'Удалятся все твои данные и всё общее с партнёром, пара завершится. Это и есть отзыв согласия '
          'на обработку данных. Отменить нельзя.',
      'Удалить навсегда',
      danger: true,
    )) {
      return;
    }
    final dio = ref.read(dioProvider);
    try {
      await dio.delete('/auth/me');
    } catch (e) {
      _toast(errorText(e));
      return;
    }
    await signOut(dio);
    if (mounted) context.go('/welcome');
  }

  Future<void> _logout() async {
    await signOut(ref.read(dioProvider));
    if (mounted) context.go('/welcome');
  }

  Future<void> _setCity(Map<String, dynamic> body) async {
    try {
      await ref.read(dioProvider).put('/me/city', data: body);
      ref.invalidate(_cityProvider);
    } catch (e) {
      _toast(errorText(e));
    }
  }

  Future<void> _changeHome() async {
    final name = await pickCity(context, title: 'Где ты живёшь?');
    if (name != null) await _setCity({'home_city': name});
  }

  Future<void> _travel() async {
    final name = await pickCity(context,
        title: 'Где ты сейчас?', subtitle: 'Покажем места и события там, пока ты в поездке.');
    if (name == null || !mounted) return;
    final now = DateTime.now();
    final until = await showDatePicker(
      context: context,
      initialDate: now.add(const Duration(days: 3)),
      firstDate: now,
      lastDate: now.add(const Duration(days: 365)),
      helpText: 'До какого дня? (можно пропустить)',
      cancelText: 'Не знаю',
    );
    await _setCity({
      'travel_city': name,
      if (until != null)
        'travel_until': '${until.year}-${until.month.toString().padLeft(2, '0')}-${until.day.toString().padLeft(2, '0')}',
    });
  }

  Future<void> _rename(String? current) async {
    final name = await askText(context, title: 'Как тебя зовут?', initial: current ?? '', maxLength: 60);
    if (name == null || name.trim().isEmpty) return;
    try {
      await ref.read(dioProvider).put('/auth/me', data: {'display_name': name.trim()});
      ref.invalidate(_meProvider);
      ref.invalidate(pairProvider);
      invalidateUs(ref);
    } catch (e) {
      _toast(errorText(e));
    }
  }

  @override
  Widget build(BuildContext context) {
    final settings = ref.watch(_settingsProvider).valueOrNull;
    final me = ref.watch(_meProvider).valueOrNull;
    final city = ref.watch(_cityProvider).valueOrNull;
    final travelUntil = city?['travel_until'] as String?;
    final pair = ref.watch(pairProvider).valueOrNull;
    final active = pair?['status'] == 'active';
    final paused = pair?['paused'] == true;
    final pausedByMe = pair?['paused_by_me'] == true;
    final pushOn = settings?['push_enabled'] != false;

    return Scaffold(
      appBar: AppBar(title: Text('Настройки', style: display(24))),
      body: SafeArea(
        child: ListView(padding: const EdgeInsets.fromLTRB(20, 8, 20, 32), children: [
          const SectionLabel('Уведомления', color: VColors.label),
          const SizedBox(height: 10),
          _Group(children: [
            _SwitchRow(
              title: 'Присылать уведомления',
              subtitle: 'Ответы, совпадения, письма, напоминания, даты',
              value: settings == null ? null : pushOn,
              onChanged: (v) => _setPush({'push_enabled': v}),
            ),
            _SwitchRow(
              title: 'Без текста',
              subtitle: 'Только «Вечер: есть что-то новое для тебя» — без имён и подробностей',
              value: settings == null ? null : settings['push_private'] == true,
              onChanged: pushOn ? (v) => _setPush({'push_private': v}) : null,
            ),
            _LinkRow(title: 'Проверить уведомления', onTap: () => testPush(context, ref)),
          ]),
          const SizedBox(height: 24),
          const SectionLabel('Защита', color: VColors.label),
          const SizedBox(height: 10),
          _Group(children: [
            _SwitchRow(
              title: 'Код-пароль на вход',
              subtitle: 'Спросим при запуске и после 30 секунд в фоне',
              value: _pin,
              onChanged: kIsWeb ? null : _togglePin,
            ),
            _SwitchRow(
              title: 'Запрет скриншотов',
              subtitle: 'Экран нельзя снять или записать, в недавних приложениях он скрыт',
              value: _secure,
              onChanged: kIsWeb ? null : _toggleSecure,
            ),
          ]),
          if (active) ...[
            const SizedBox(height: 24),
            const SectionLabel('Пара', color: VColors.label),
            const SizedBox(height: 10),
            _Group(children: [
              if (!paused)
                _LinkRow(
                  title: 'Поставить паузу',
                  subtitle: 'Взять время для себя: без уведомлений, всё сохранится',
                  onTap: () => _pause(true),
                )
              else if (pausedByMe)
                _LinkRow(title: 'Снять паузу', subtitle: 'Сейчас «Вечер» на паузе', onTap: () => _pause(false))
              else
                _LinkRow(
                  title: 'Пауза',
                  subtitle: '${pair?['partner_name'] ?? 'Партнёр'} поставил(а) паузу — снять её может только он(а)',
                ),
              _LinkRow(
                title: 'Завершить пару',
                danger: true,
                onTap: () => _leave(pair?['partner_name'] as String?),
              ),
            ]),
          ],
          const SizedBox(height: 24),
          const SectionLabel('Город', color: VColors.label),
          const SizedBox(height: 10),
          _Group(children: [
            _LinkRow(
              title: 'Живу в',
              subtitle: city?['home_city'] as String? ?? 'Не указан',
              onTap: _changeHome,
            ),
            if (city?['travel_city'] == null)
              _LinkRow(
                title: 'Сейчас в другом городе',
                subtitle: 'Уехал(а)? Подборки будут для того города',
                onTap: _travel,
              )
            else
              _LinkRow(
                title: 'Сейчас в: ${city!['travel_city']}',
                subtitle: travelUntil == null
                    ? 'Нажми, когда вернёшься'
                    : 'До ${travelUntil.substring(8, 10)}.${travelUntil.substring(5, 7)} · нажми, если вернулся(лась) раньше',
                onTap: () => _setCity({'travel_city': ''}),
              ),
          ]),
          const SizedBox(height: 24),
          const SectionLabel('Аккаунт', color: VColors.label),
          const SizedBox(height: 10),
          _Group(children: [
            _LinkRow(
              title: 'Имя',
              subtitle: me?['display_name'] as String? ?? '—',
              onTap: () => _rename(me?['display_name'] as String?),
            ),
            _LinkRow(title: 'Почта', subtitle: me?['email'] as String? ?? '—'),
            _LinkRow(title: 'Выйти', onTap: _logout),
            _LinkRow(title: 'Удалить аккаунт', danger: true, onTap: _delete),
          ]),
          const SizedBox(height: 24),
          const SectionLabel('Приложение', color: VColors.label),
          const SizedBox(height: 10),
          _Group(children: [
            FutureBuilder<String>(
              future: currentVersionLabel(),
              builder: (context, snap) => _LinkRow(
                title: 'Проверить обновления',
                subtitle: 'Версия ${snap.data ?? '…'}',
                onTap: () => checkForUpdate(context, manual: true),
              ),
            ),
            _LinkRow(
              title: 'Проверить соединение',
              subtitle: 'Если Вечер не открывается с VPN или без него',
              onTap: () => Navigator.of(context).push(MaterialPageRoute<void>(
                builder: (_) => const NetworkDiagnosticsScreen(),
              )),
            ),
          ]),
          const SizedBox(height: 20),
          const Text(
            'Данные хранятся на серверах в России. Вопросы о данных — privacy@вечер.online',
            textAlign: TextAlign.center,
            style: TextStyle(color: VColors.muted, fontSize: 12),
          ),
        ]),
      ),
    );
  }
}

class _Group extends StatelessWidget {
  const _Group({required this.children});
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: cardBox(),
      child: Column(children: [
        for (var i = 0; i < children.length; i++) ...[
          if (i > 0) const Divider(height: 1, indent: 18, endIndent: 18, color: Color(0x14FFFFFF)),
          children[i],
        ],
      ]),
    );
  }
}

class _SwitchRow extends StatelessWidget {
  const _SwitchRow({required this.title, required this.subtitle, required this.value, required this.onChanged});
  final String title, subtitle;
  final bool? value;
  final ValueChanged<bool>? onChanged;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(18, 12, 10, 12),
      child: Row(children: [
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600)),
            const SizedBox(height: 3),
            Text(subtitle, style: const TextStyle(color: VColors.muted, fontSize: 13)),
          ]),
        ),
        Switch(
          value: value ?? false,
          onChanged: value == null ? null : onChanged,
          activeColor: VColors.ink,
          activeTrackColor: VColors.gold,
        ),
      ]),
    );
  }
}

class _LinkRow extends StatelessWidget {
  const _LinkRow({required this.title, this.subtitle, this.onTap, this.danger = false});
  final String title;
  final String? subtitle;
  final VoidCallback? onTap;
  final bool danger;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(18, 14, 14, 14),
        child: Row(children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title,
                  style: TextStyle(
                      fontSize: 16, fontWeight: FontWeight.w600, color: danger ? VColors.coral : VColors.text)),
              if (subtitle != null) ...[
                const SizedBox(height: 3),
                Text(subtitle!, style: const TextStyle(color: VColors.muted, fontSize: 13)),
              ],
            ]),
          ),
          if (onTap != null) const Icon(Icons.chevron_right, color: VColors.muted),
        ]),
      ),
    );
  }
}

/// Установка кода-пароля: ввести дважды.
class _SetPinScreen extends StatefulWidget {
  const _SetPinScreen();

  @override
  State<_SetPinScreen> createState() => _SetPinScreenState();
}

class _SetPinScreenState extends State<_SetPinScreen> {
  String? _first;
  String? _error;
  int _key = 0;

  Future<void> _onPin(String pin) async {
    if (_first == null) {
      setState(() {
        _first = pin;
        _error = null;
        _key++;
      });
      return;
    }
    if (pin != _first) {
      setState(() {
        _first = null;
        _error = 'Коды не совпали, попробуй ещё раз';
        _key++;
      });
      return;
    }
    await savePin(pin);
    PinGate.unlock();
    if (mounted) Navigator.of(context).pop(true);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(),
      body: SafeArea(
        child: PinPad(
          key: ValueKey(_key),
          title: 'Код-пароль',
          subtitle: _first == null ? 'Придумай код из 4 цифр' : 'Повтори код',
          error: _error,
          onCompleted: _onPin,
        ),
      ),
    );
  }
}
