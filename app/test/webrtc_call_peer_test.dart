import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/features/calls/webrtc_call_peer.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'closing an unanswered native call is safe and does not touch media channels',
    () async {
      final calls = <String>[];
      final messenger =
          TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
      for (final name in ['FlutterWebRTC.Method', 'vecher/call']) {
        final channel = MethodChannel(name);
        messenger.setMockMethodCallHandler(channel, (call) async {
          calls.add('$name:${call.method}');
          return null;
        });
        addTearDown(() => messenger.setMockMethodCallHandler(channel, null));
      }
      final peer = WebRtcCallPeer();
      await peer.close();
      await peer.close();
      expect(peer.mediaReady, isFalse);
      expect(peer.videoVisible, isFalse);
      expect(calls, isEmpty);
      peer.dispose();
    },
  );
}
