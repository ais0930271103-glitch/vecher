import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/auth/consent_screen.dart';
import '../features/chat/chat_screen.dart';
import '../features/solo/solo_screen.dart';
import '../features/today/invites.dart';
import '../features/today/paired_today.dart';
import '../features/today/today_screen.dart';
import '../features/us/us_screen.dart';
import '../features/wishes/matches_screen.dart';
import '../features/wishes/places_screen.dart';
import '../features/wishes/plan_screen.dart';
import '../features/wishes/wishes_screen.dart';
import '../features/wishes/evenings.dart';
import 'api.dart';
import 'privacy.dart';
import 'realtime_controller.dart';
import 'realtime_provider.dart';
import 'session_changes.dart';

/// Lives above routes, below the PIN gate. There is one socket for the whole app.
class RealtimeBridge extends ConsumerStatefulWidget {
  const RealtimeBridge({super.key, required this.child});
  final Widget child;

  @override
  ConsumerState<RealtimeBridge> createState() => _RealtimeBridgeState();
}

class _RealtimeBridgeState extends ConsumerState<RealtimeBridge>
    with WidgetsBindingObserver {
  late final RealtimeController _controller;
  StreamSubscription<Set<String>>? _updates;
  AppLifecycleState _lifecycle = AppLifecycleState.resumed;
  int _gateRevision = 0;

  bool get _allowed =>
      mounted &&
      _lifecycle == AppLifecycleState.resumed &&
      PinGate.isReady &&
      !PinGate.isLocked &&
      !sessionChanges.isChanging;

  @override
  void initState() {
    super.initState();
    _controller = ref.read(realtimeControllerProvider);
    _lifecycle =
        WidgetsBinding.instance.lifecycleState ?? AppLifecycleState.resumed;
    WidgetsBinding.instance.addObserver(this);
    sessionChanges.addListener(_sync);
    PinGate.lockedListenable.addListener(_sync);
    PinGate.readyListenable.addListener(_sync);
    _updates = _controller.invalidations.listen((scopes) {
      if (_allowed) _invalidate(scopes);
    });
    _sync();
  }

  void _sync() {
    final gateRevision = ++_gateRevision;
    final sessionRevision = sessionChanges.revision;
    _controller.deactivate();
    if (!_allowed) return;
    unawaited(() async {
      try {
        final token = await readToken();
        if (!_allowed ||
            gateRevision != _gateRevision ||
            sessionRevision != sessionChanges.revision) {
          return;
        }
        if (token != null && token.isNotEmpty) _controller.activate(token);
      } catch (_) {
        // Secure storage being unavailable must not start anonymous/account-stale work.
      }
    }());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _lifecycle = state;
    _sync();
  }

  void _invalidate(Set<String> scopes) {
    final all = scopes.contains('all') || scopes.contains('pair');
    if (all) {
      ref.invalidate(pairProvider);
      ref.invalidate(prepProvider);
      ref.invalidate(meProvider);
    }
    if (all || scopes.contains('today')) {
      ref.invalidate(todayProvider);
      ref.invalidate(prepTodayProvider);
      ref.invalidate(invitesProvider);
      ref.invalidate(inboxProvider);
      ref.invalidate(incomingSurprisesProvider);
    }
    if (all || scopes.contains('wishes')) {
      ref.invalidate(decksProvider);
      ref.invalidate(matchesProvider);
      ref.invalidate(prepProvider);
    }
    if (all || scopes.contains('plans')) {
      ref.invalidate(planProvider);
      ref.invalidate(placesProvider);
      ref.invalidate(upcomingProvider);
      invalidateEvenings(ref);
    }
    if (all || scopes.contains('chat')) ref.invalidate(chatUnreadProvider);
    if (all || scopes.contains('us')) {
      ref.invalidate(personProvider);
      ref.invalidate(datesProvider);
      ref.invalidate(remindersProvider);
    }
    if (all || scopes.contains('solo')) {
      ref.invalidate(soloProvider);
      ref.invalidate(inboxProvider);
      ref.invalidate(incomingSurprisesProvider);
    }
  }

  @override
  void dispose() {
    ++_gateRevision;
    sessionChanges.removeListener(_sync);
    PinGate.lockedListenable.removeListener(_sync);
    PinGate.readyListenable.removeListener(_sync);
    WidgetsBinding.instance.removeObserver(this);
    unawaited(_updates?.cancel());
    _controller.deactivate();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
