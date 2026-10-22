import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api.dart';
import 'network_client.dart';
import 'realtime_controller.dart';
import 'realtime_transport.dart';

final realtimeControllerProvider = Provider<RealtimeController>((ref) {
  final dio = ref.read(dioProvider);
  final endpoints = endpointsOf(dio);
  final controller = RealtimeController(
    loadSession: () async {
      try {
        final response = await dio.get('/realtime/session');
        final session = RealtimeSession.parse(
          response.data,
          allowLocalWs: Uri.parse(dio.options.baseUrl).scheme == 'http',
        );
        if (!session.enabled || endpoints == null) return session;
        return RealtimeSession(
          url: endpoints.realtimeUrl(session.url, response.requestOptions.uri),
          token: session.token,
          channel: session.channel,
        );
      } on DioException catch (error) {
        // Compatibility with the currently deployed API and disabled feature.
        if ({404, 405, 501}.contains(error.response?.statusCode)) {
          return const RealtimeSession.disabled();
        }
        rethrow;
      }
    },
    createTransport: CentrifugoTransport.new,
  );
  void endpointChanged() {
    // If session loading itself selected a mirror, that result already carries
    // its URL. A subscribed transport needs a fresh session after a later switch.
    if (controller.connected) controller.reconnect();
  }

  endpoints?.addListener(endpointChanged);
  ref.onDispose(() => endpoints?.removeListener(endpointChanged));
  ref.onDispose(controller.dispose);
  return controller;
});
