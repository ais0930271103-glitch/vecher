// Browsers do not expose the native socket/TLS exception to Dart.
bool isTlsFailure(Object? error) => false;
bool isSocketFailure(Object? error) => false;
