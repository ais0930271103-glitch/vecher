import 'package:dio/dio.dart';

import 'ice_configuration.dart';

class CallSession {
  const CallSession({
    required this.id,
    required this.status,
    required this.mine,
    required this.partnerName,
    required this.expiresAt,
    this.acceptedAt,
    this.endReason,
  });

  factory CallSession.fromMap(Map data) => CallSession(
    id: data['id'] as String,
    status: data['status'] as String,
    mine: data['mine'] == true,
    partnerName: (data['partner_name'] as String?)?.trim().isNotEmpty == true
        ? data['partner_name'] as String
        : 'Партнёр',
    expiresAt: DateTime.parse(data['expires_at'] as String).toUtc(),
    acceptedAt: data['accepted_at'] == null
        ? null
        : DateTime.parse(data['accepted_at'] as String).toUtc(),
    endReason: data['end_reason'] as String?,
  );

  final String id;
  final String status;
  final bool mine;
  final String partnerName;
  final DateTime expiresAt;
  final DateTime? acceptedAt;
  final String? endReason;

  bool get ringing => status == 'ringing';
  bool get active => status == 'active';
  bool get ended => status == 'ended';

  String get endText => switch (endReason) {
    'declined' => 'Звонок отклонён',
    'timeout' || 'missed' || 'ring_timeout' => 'Нет ответа',
    'disconnected' || 'heartbeat_timeout' => 'Связь прервалась',
    'duration_limit' || 'expired' => 'Время звонка истекло',
    _ => 'Звонок завершён',
  };
}

class CallConfiguration {
  const CallConfiguration({
    required this.iceServers,
    required this.enabled,
    required this.relayConfigured,
  });

  factory CallConfiguration.fromMap(Map data) => CallConfiguration(
    iceServers: normalizeIceServers(data['ice_servers'] ?? const []),
    enabled: data['enabled'] != false,
    relayConfigured: data['relay_configured'] == true,
  );

  final List<Map<String, dynamic>> iceServers;
  final bool enabled;
  final bool relayConfigured;
}

class CallSignal {
  const CallSignal({required this.seq, required this.kind, required this.data});
  factory CallSignal.fromMap(Map data) => CallSignal(
    seq: (data['seq'] as num).toInt(),
    kind: data['kind'] as String,
    data: Map<String, dynamic>.from(data['data'] as Map),
  );
  final int seq;
  final String kind;
  final Map<String, dynamic> data;
}

class CallSignalBatch {
  const CallSignalBatch({
    required this.call,
    required this.signals,
    required this.cursor,
  });
  final CallSession call;
  final List<CallSignal> signals;
  final int cursor;
}

/// A small boundary around signaling; media never goes through these HTTP requests.
abstract interface class CallApi {
  Future<CallConfiguration> configuration();
  Future<CallSession> start();
  Future<CallSession> get(String id);
  Future<CallSession?> active();
  Future<CallSession> accept(String id);
  Future<void> decline(String id);
  Future<void> end(String id);
  Future<CallSignalBatch> signals(String id, int after);
  Future<void> sendSignal(
    String id,
    String kind,
    Map<String, dynamic> data,
    String clientId,
  );
}

class DioCallApi implements CallApi {
  DioCallApi(this.dio);
  final Dio dio;

  @override
  Future<CallConfiguration> configuration() async =>
      CallConfiguration.fromMap((await dio.get('/calls/config')).data as Map);

  @override
  Future<CallSession> start() async =>
      CallSession.fromMap((await dio.post('/calls', data: {})).data as Map);

  @override
  Future<CallSession> get(String id) async =>
      CallSession.fromMap((await dio.get('/calls/$id')).data as Map);

  @override
  Future<CallSession?> active() async {
    final data = (await dio.get('/calls/active')).data;
    return data == null ? null : CallSession.fromMap(data as Map);
  }

  @override
  Future<CallSession> accept(String id) async => CallSession.fromMap(
    (await dio.post('/calls/$id/accept', data: {})).data as Map,
  );

  @override
  Future<void> decline(String id) async {
    await dio.post('/calls/$id/decline', data: {});
  }

  @override
  Future<void> end(String id) async {
    await dio.post('/calls/$id/end', data: {});
  }

  @override
  Future<CallSignalBatch> signals(String id, int after) async {
    final data =
        (await dio.get(
              '/calls/$id/signals',
              queryParameters: {'after': after},
            )).data
            as Map;
    return CallSignalBatch(
      call: CallSession.fromMap(data['call'] as Map),
      signals: (data['signals'] as List)
          .map((s) => CallSignal.fromMap(s as Map))
          .toList(growable: false),
      cursor: (data['cursor'] as num).toInt(),
    );
  }

  @override
  Future<void> sendSignal(
    String id,
    String kind,
    Map<String, dynamic> data,
    String clientId,
  ) async {
    await dio.post(
      '/calls/$id/signals',
      data: {'kind': kind, 'data': data, 'client_id': clientId},
    );
  }
}
