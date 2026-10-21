/// A user-facing media failure and an optional safe diagnostic stage.
/// Never put SDP, credentials, device addresses or raw native errors in [stage].
class CallMediaException implements Exception {
  const CallMediaException(this.message, {this.stage});

  final String message;
  final String? stage;
}
