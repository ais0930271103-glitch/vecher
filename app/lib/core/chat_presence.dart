import 'package:flutter/material.dart';

/// The root navigator reports when another page or a dialog covers the chat.
final chatRouteObserver = RouteObserver<ModalRoute<dynamic>>();

final Set<Object> _visibleScreens = <Object>{};

/// True only while at least one chat route is visible in the foreground.
/// Separate owners keep stacked chat routes from resetting each other's state.
bool get chatOnScreen => _visibleScreens.isNotEmpty;

void setChatScreenVisible(Object owner, bool visible) {
  if (visible) {
    _visibleScreens.add(owner);
  } else {
    _visibleScreens.remove(owner);
  }
}
