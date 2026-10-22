import 'dart:convert';

import 'package:flutter/foundation.dart';

enum ApiConnectionState { unknown, reachable, unavailable }

/// Compile-time API origins are a trust boundary, not a list supplied by a feed.
/// Every fallback must serve the same API, account data and realtime service.
class ApiEndpoints extends ChangeNotifier {
  ApiEndpoints({
    required String primary,
    String alternativesJson = '[]',
    this.allowLocalHttp = false,
  }) {
    final values = jsonDecode(alternativesJson);
    if (values is! List || values.any((value) => value is! String)) {
      throw const FormatException('API_URLS must be a JSON array of origins');
    }
    final parsed = <Uri>[];
    for (final value in <String>[primary, ...values.cast<String>()]) {
      final uri = _parseOrigin(value, allowLocalHttp: allowLocalHttp);
      if (!parsed.contains(uri)) parsed.add(uri);
    }
    if (parsed.length > 3) {
      throw const FormatException('At most three API origins are supported');
    }
    origins = List.unmodifiable(parsed);
    _selected = origins.first;
  }

  final bool allowLocalHttp;
  late final List<Uri> origins;
  late Uri _selected;
  int _revision = 0;
  final connection = ValueNotifier(ApiConnectionState.unknown);

  Uri get primary => origins.first;
  Uri get selected => _selected;
  int get revision => _revision;

  List<Uri> get candidates => [
    _selected,
    ...origins.where((origin) => origin != _selected),
  ];

  bool trusts(Uri uri) =>
      uri.userInfo.isEmpty &&
      !uri.hasFragment &&
      origins.any((origin) => sameOrigin(uri, origin));

  /// A late response must not undo another request's successful switch.
  void succeeded(Uri origin, {required int startingRevision}) {
    connection.value = ApiConnectionState.reachable;
    if (startingRevision != _revision || !origins.contains(origin)) return;
    if (_selected == origin) return;
    _selected = origin;
    _revision++;
    notifyListeners();
  }

  void unavailable({required int startingRevision}) {
    if (startingRevision == _revision) {
      connection.value = ApiConnectionState.unavailable;
    }
  }

  /// Only the main API's websocket URL follows its trusted API mirror.
  /// A separately configured realtime host retains its own address.
  String realtimeUrl(String url, Uri respondingApi) {
    final original = Uri.tryParse(url);
    if (original == null || !trusts(respondingApi)) return url;
    final apiScheme = switch (original.scheme) {
      'wss' => 'https',
      'ws' => 'http',
      _ => '',
    };
    if (apiScheme.isEmpty) return url;
    final equivalent = original.replace(scheme: apiScheme);
    if (!sameOrigin(equivalent, primary)) return url;
    return Uri(
      scheme: respondingApi.scheme == 'https' ? 'wss' : 'ws',
      host: respondingApi.host,
      port: respondingApi.hasPort ? respondingApi.port : null,
      path: original.path,
    ).toString();
  }

  static bool sameOrigin(Uri first, Uri second) =>
      first.scheme == second.scheme &&
      first.host == second.host &&
      first.port == second.port;

  static Uri _parseOrigin(String value, {required bool allowLocalHttp}) {
    if (value.trim() != value || RegExp(r'[\\\s]').hasMatch(value)) {
      throw const FormatException('Invalid API origin');
    }
    final uri = Uri.tryParse(value);
    if (uri == null ||
        !uri.hasAuthority ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        uri.hasQuery ||
        uri.hasFragment ||
        (uri.path.isNotEmpty && uri.path != '/') ||
        uri.port < 1 ||
        uri.port > 65535 ||
        !(uri.scheme == 'https' ||
            (allowLocalHttp &&
                uri.scheme == 'http' &&
                {
                  'localhost',
                  '127.0.0.1',
                  '::1',
                  '10.0.2.2',
                }.contains(uri.host)))) {
      throw const FormatException('API origins require HTTPS');
    }
    return Uri(
      scheme: uri.scheme,
      host: uri.host,
      port: uri.hasPort ? uri.port : null,
    );
  }

  @override
  void dispose() {
    connection.dispose();
    super.dispose();
  }
}
