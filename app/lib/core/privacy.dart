import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart' show kIsWeb, ValueListenable;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api.dart';
import 'network_client.dart';
import 'push.dart';
import 'router.dart';
import 'session_changes.dart';
import 'theme.dart';

// ---------- Запрет скриншотов (Android FLAG_SECURE) ----------

const _secureKey = 'vecher_secure_screen';
const _secureChannel = MethodChannel('vecher/secure');

/// Включён ли запрет скриншотов. По умолчанию — да: в «Вечере» личное.
Future<bool> readSecureScreen() async => (await secureStorage.read(key: _secureKey)) != '0';

/// Сохранить и сразу применить. Заодно скрывает содержимое в списке недавних приложений.
Future<void> setSecureScreen(bool on) async {
  await secureStorage.write(key: _secureKey, value: on ? '1' : '0');
  await applySecureScreen(on);
}

Future<void> applySecureScreen([bool? on]) async {
  if (kIsWeb) return;
  try {
    await _secureChannel.invokeMethod('set', on ?? await readSecureScreen());
  } catch (_) {
    // Не Android (или старая сборка без канала) — просто пропускаем.
  }
}

// ---------- Код-пароль на вход ----------

const _pinKey = 'vecher_pin';
const pinLength = 4;

/// Через сколько секунд в фоне снова спрашивать код.
const _relockAfter = Duration(seconds: 30);

Future<bool> hasPin() async => (await secureStorage.read(key: _pinKey)) != null;
Future<void> savePin(String pin) => secureStorage.write(key: _pinKey, value: pin);
Future<void> clearPin() => secureStorage.delete(key: _pinKey);
Future<bool> checkPin(String pin) async => (await secureStorage.read(key: _pinKey)) == pin;

/// Выйти из аккаунта на этом телефоне: отвязать push, забыть сессию и код-пароль.
Future<void> signOut(Dio dio) async {
  final revision = sessionChanges.begin();
  final cleanup = CancelToken();
  bool current() =>
      sessionChanges.isChanging && sessionChanges.revision == revision;
  void changed() {
    if (!current() && !cleanup.isCancelled) {
      cleanup.cancel('Logout session ended');
    }
  }

  sessionChanges.addListener(changed);
  try {
    // Retire the old push listeners before the first await, including when a
    // new login starts while native secure storage is still reading the token.
    final oldToken = readToken().timeout(const Duration(seconds: 3))
        .onError((_, __) => null);
    await stopPush(
      dio,
      unregister: (token, platform) async {
        final accessToken = await oldToken;
        if (accessToken == null || !current()) return;
        await unregisterPushForLogout(
          dio,
          accessToken: accessToken,
          revision: revision,
          pushToken: token,
          platform: platform,
          cancelToken: cleanup,
        );
      },
    );
    if (!current()) return;
    await clearToken();
    // clearToken owns a nested begin/finish pair. Do not clear a newer login's
    // PIN if another session replaced this logout while storage was awaited.
    if (sessionChanges.isChanging || sessionChanges.revision != revision + 2) {
      return;
    }
    await clearPin();
    if (sessionChanges.isChanging || sessionChanges.revision != revision + 2) {
      return;
    }
    PinGate.unlock();
  } finally {
    sessionChanges.removeListener(changed);
    if (!cleanup.isCancelled) cleanup.cancel('Logout cleanup finished');
    // A failed push cleanup must not leave foreground refresh permanently suspended.
    sessionChanges.finish(revision);
  }
}

/// Закрывает всё приложение экраном кода, если код-пароль включён:
/// при запуске и при возвращении после 30 секунд в фоне.
class PinGate extends StatefulWidget {
  const PinGate({super.key, required this.child});
  final Widget child;

  static final _locked = ValueNotifier<bool>(false);
  static final _ready = ValueNotifier<bool>(false);
  static bool get isLocked => _locked.value;
  static ValueListenable<bool> get lockedListenable => _locked;
  static bool get isReady => _ready.value;
  static ValueListenable<bool> get readyListenable => _ready;

  /// Код только что задан — не просить его сразу же.
  static void unlock() => _locked.value = false;

  @override
  State<PinGate> createState() => _PinGateState();
}

class _PinGateState extends State<PinGate> with WidgetsBindingObserver {
  DateTime? _backgroundSince;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    applySecureScreen();
    PinGate._ready.value = false;
    hasPin().then((v) {
      if (!mounted) return;
      PinGate._locked.value = v;
      PinGate._ready.value = true;
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused || state == AppLifecycleState.hidden) {
      _backgroundSince ??= DateTime.now();
    } else if (state == AppLifecycleState.resumed) {
      final since = _backgroundSince;
      _backgroundSince = null;
      if (since != null && DateTime.now().difference(since) >= _relockAfter) {
        // Lock synchronously, before the call screen can resume its camera.
        PinGate._locked.value = true;
        hasPin().then((v) {
          PinGate._locked.value = v;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return ValueListenableBuilder<bool>(
      valueListenable: PinGate._locked,
      builder: (context, locked, child) => Stack(children: [
        child!,
        if (locked) const Positioned.fill(child: _LockScreen()),
      ]),
      child: widget.child,
    );
  }
}

class _LockScreen extends StatefulWidget {
  const _LockScreen();

  @override
  State<_LockScreen> createState() => _LockScreenState();
}

class _LockScreenState extends State<_LockScreen> {
  int _fails = 0;
  DateTime? _waitUntil;
  String? _error;
  int _resetKey = 0;
  bool _confirmForgot = false;
  Timer? _tick;

  @override
  void dispose() {
    _tick?.cancel();
    super.dispose();
  }

  int get _waitLeft {
    final w = _waitUntil;
    if (w == null) return 0;
    final s = w.difference(DateTime.now()).inSeconds;
    return s > 0 ? s + 1 : 0;
  }

  Future<void> _onPin(String pin) async {
    if (_waitLeft > 0) return;
    if (await checkPin(pin)) {
      PinGate.unlock();
      return;
    }
    _fails++;
    setState(() {
      _resetKey++;
      if (_fails % 5 == 0) {
        // После 5 ошибок — пауза 30 секунд, чтобы код нельзя было подобрать перебором.
        _waitUntil = DateTime.now().add(const Duration(seconds: 30));
        _tick?.cancel();
        _tick = Timer.periodic(const Duration(seconds: 1), (t) {
          if (!mounted) return;
          setState(() {});
          if (_waitLeft == 0) {
            t.cancel();
            setState(() => _error = null);
          }
        });
        _error = 'Слишком много попыток. Подожди немного';
      } else {
        _error = 'Неверный код';
      }
    });
    HapticFeedback.mediumImpact();
  }

  /// Экран кода лежит поверх навигации, поэтому подтверждение — прямо здесь, без диалога.
  Future<void> _forgot() async {
    await signOut(ProviderScope.containerOf(context, listen: false).read(dioProvider));
    router.go('/welcome');
  }

  @override
  Widget build(BuildContext context) {
    final wait = _waitLeft;
    return Material(
      color: VColors.bg,
      child: SafeArea(
        child: PinPad(
          key: ValueKey(_resetKey),
          title: 'Вечер',
          subtitle: wait > 0 ? 'Попробуй через $wait с' : 'Введи код-пароль',
          error: wait > 0 ? null : _error,
          enabled: wait == 0,
          onCompleted: _onPin,
          footer: _confirmForgot
              ? Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 28),
                  child: Column(children: [
                    const Text(
                      'Выйди из аккаунта на этом телефоне и войди снова по коду из письма. '
                      'Код-пароль сбросится, данные останутся.',
                      textAlign: TextAlign.center,
                      style: TextStyle(color: VColors.soft, fontSize: 14),
                    ),
                    Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                      TextButton(
                        onPressed: () => setState(() => _confirmForgot = false),
                        child: const Text('Отмена', style: TextStyle(color: VColors.muted)),
                      ),
                      TextButton(
                        onPressed: _forgot,
                        child: const Text('Выйти', style: TextStyle(color: VColors.gold)),
                      ),
                    ]),
                  ]),
                )
              : TextButton(
                  onPressed: () => setState(() => _confirmForgot = true),
                  child: const Text('Забыли код?', style: TextStyle(color: VColors.muted)),
                ),
        ),
      ),
    );
  }
}

/// Цифровая клавиатура для кода-пароля (вход и установка).
class PinPad extends StatefulWidget {
  const PinPad({
    super.key,
    required this.title,
    required this.subtitle,
    required this.onCompleted,
    this.error,
    this.enabled = true,
    this.footer,
  });

  final String title;
  final String subtitle;
  final String? error;
  final bool enabled;
  final Widget? footer;
  final ValueChanged<String> onCompleted;

  @override
  State<PinPad> createState() => _PinPadState();
}

class _PinPadState extends State<PinPad> {
  String _pin = '';

  void _press(String d) {
    if (!widget.enabled || _pin.length >= pinLength) return;
    HapticFeedback.selectionClick();
    setState(() => _pin += d);
    if (_pin.length == pinLength) {
      final pin = _pin;
      Future<void>.delayed(const Duration(milliseconds: 120), () => widget.onCompleted(pin));
    }
  }

  void _back() {
    if (_pin.isEmpty) return;
    setState(() => _pin = _pin.substring(0, _pin.length - 1));
  }

  Widget _key(String label, {VoidCallback? onTap, Widget? child}) => SizedBox(
        width: 76,
        height: 76,
        child: Material(
          color: child == null ? VColors.card : Colors.transparent,
          shape: const CircleBorder(),
          child: InkWell(
            customBorder: const CircleBorder(),
            onTap: onTap,
            child: Center(child: child ?? Text(label, style: const TextStyle(fontSize: 28))),
          ),
        ),
      );

  @override
  Widget build(BuildContext context) {
    return Column(children: [
      const Spacer(flex: 2),
      const Icon(Icons.nightlight_round, color: VColors.gold, size: 40),
      const SizedBox(height: 12),
      Text(widget.title, style: display(30)),
      const SizedBox(height: 8),
      Text(widget.subtitle, style: const TextStyle(color: VColors.soft, fontSize: 16)),
      const SizedBox(height: 24),
      Row(mainAxisAlignment: MainAxisAlignment.center, children: [
        for (var i = 0; i < pinLength; i++)
          Container(
            width: 16,
            height: 16,
            margin: const EdgeInsets.symmetric(horizontal: 10),
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: i < _pin.length ? VColors.gold : Colors.transparent,
              border: Border.all(color: VColors.gold, width: 1.5),
            ),
          ),
      ]),
      SizedBox(
        height: 36,
        child: Center(
          child: Text(widget.error ?? '', style: const TextStyle(color: VColors.coral)),
        ),
      ),
      const Spacer(),
      for (final row in const [
        ['1', '2', '3'],
        ['4', '5', '6'],
        ['7', '8', '9'],
      ])
        Padding(
          padding: const EdgeInsets.only(bottom: 14),
          child: Row(mainAxisAlignment: MainAxisAlignment.spaceEvenly, children: [
            for (final d in row) _key(d, onTap: () => _press(d)),
          ]),
        ),
      Row(mainAxisAlignment: MainAxisAlignment.spaceEvenly, children: [
        const SizedBox(width: 76, height: 76),
        _key('0', onTap: () => _press('0')),
        _key('', onTap: _back, child: const Icon(Icons.backspace_outlined, color: VColors.muted)),
      ]),
      const SizedBox(height: 8),
      widget.footer ?? const SizedBox(height: 48),
      const Spacer(),
    ]);
  }
}
