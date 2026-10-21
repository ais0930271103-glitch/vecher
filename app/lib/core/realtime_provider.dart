import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'api.dart';
import 'realtime_controller.dart';
import 'realtime_transport.dart';

final realtimeControllerProvider = Provider<RealtimeController>((ref) {
  final dio = ref.read(dioProvider);
  final controller = RealtimeController(
    loadSession: () async {
      try {
        final response = await dio.get('/realtime/session');
        return RealtimeSession.parse(
          response.data,
          allowLocalWs: Uri.parse(dio.options.baseUrl).scheme == 'http',
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
  ref.onDispose(controller.dispose);
  return controller;
});
