import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/features/calls/call_api.dart';
import 'package:vecher_app/features/calls/call_media_exception.dart';

void main() {
  test(
    'HTTP configuration removes STUN null auth and preserves TURN auth',
    () async {
      final response = <String, dynamic>{
        'enabled': true,
        'relay_configured': true,
        'ice_servers': [
          {
            'urls': ['stun:stun.example.test:3478'],
            'username': null,
            'credential': null,
          },
          {
            'urls': [
              'turn:relay.example.test:3478?transport=udp',
              'turns:relay.example.test:5349?transport=tcp',
            ],
            'username': '1234567890:user',
            'credential': 'SyntheticCaseSensitive+/Auth=',
          },
        ],
      };
      final dio = Dio(BaseOptions(baseUrl: 'https://api.example.test'));
      addTearDown(() => dio.close(force: true));
      dio.interceptors.add(
        InterceptorsWrapper(
          onRequest: (request, handler) {
            expect(request.path, '/calls/config');
            handler.resolve(Response(requestOptions: request, data: response));
          },
        ),
      );

      final configuration = await DioCallApi(dio).configuration();

      expect(configuration.enabled, isTrue);
      expect(configuration.relayConfigured, isTrue);
      expect(configuration.iceServers.first, {
        'urls': ['stun:stun.example.test:3478'],
      });
      expect(
        configuration.iceServers.last,
        (response['ice_servers'] as List).last,
      );
      // Normalization must not mutate the HTTP response or opaque credentials.
      expect(
        (response['ice_servers'] as List).first,
        containsPair('username', null),
      );
    },
  );

  test('missing TURN credentials are a safe configuration error', () {
    for (final auth in <Map<String, dynamic>>[
      {},
      {'username': null, 'credential': null},
      {'username': 'user', 'credential': ''},
      {'username': ' ', 'credential': 'synthetic-secret'},
      {'username': 123, 'credential': 'synthetic-secret'},
      {
        'username': 'user',
        'credential': {'token': 'synthetic-secret'},
      },
    ]) {
      expect(
        () => CallConfiguration.fromMap({
          'ice_servers': [
            {
              'urls': [
                'stun:stun.example.test:3478',
                'turns:relay.example.test:5349',
              ],
              ...auth,
            },
          ],
        }),
        throwsA(
          isA<CallMediaException>()
              .having((e) => e.stage, 'stage', 'ice_configuration')
              .having(
                (e) => e.message,
                'safe message',
                isNot(contains('synthetic-secret')),
              ),
        ),
      );
    }
  });

  test('ICE URLs are validated and detached from mutable response lists', () {
    for (final invalid in <Object?>[
      'not-a-server-list',
      [null],
      [
        {'urls': []},
      ],
      [
        {
          'urls': [null],
        },
      ],
      [
        {
          'urls': ['https://relay.example.test'],
        },
      ],
      [
        {
          'urls': ['turn:'],
        },
      ],
      [
        {
          'urls': ['stun:host with spaces'],
        },
      ],
    ]) {
      expect(
        () => CallConfiguration.fromMap({'ice_servers': invalid}),
        throwsA(
          isA<CallMediaException>().having(
            (e) => e.stage,
            'stage',
            'ice_configuration',
          ),
        ),
      );
    }

    final urls = ['stun:stun.example.test:3478'];
    final configuration = CallConfiguration.fromMap({
      'ice_servers': [
        {'urls': urls},
      ],
    });
    urls[0] = 'turn:unexpected.example.test';
    expect(configuration.iceServers.single['urls'], [
      'stun:stun.example.test:3478',
    ]);
  });

  test('disabled configuration remains usable without ICE servers', () {
    final configuration = CallConfiguration.fromMap({
      'enabled': false,
      'relay_configured': false,
    });
    expect(configuration.enabled, isFalse);
    expect(configuration.iceServers, isEmpty);
  });
}
