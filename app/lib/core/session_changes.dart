import 'package:flutter/foundation.dart';

/// A change signal, never a container for a token or other account data.
/// Suspending synchronously closes account-scoped work before storage/network awaits.
class SessionChanges extends ChangeNotifier {
  int _revision = 0;
  bool _changing = false;

  int get revision => _revision;
  bool get isChanging => _changing;

  int begin() {
    _changing = true;
    _revision++;
    notifyListeners();
    return _revision;
  }

  void finish(int revision) {
    if (revision != _revision) return;
    _changing = false;
    _revision++;
    notifyListeners();
  }
}

final sessionChanges = SessionChanges();
