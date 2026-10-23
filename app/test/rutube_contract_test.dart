import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:vecher_app/features/videos/rutube_embed.dart';
import 'package:vecher_app/features/videos/videos.dart';

const _id = '55c99704eff3d904a7d0ea9d0200bb0e';

Map<String, dynamic> _video() => {
  'id': 'rutube-$_id',
  'title': 'Домашний лимонад',
  'author': 'Готовим дома',
  'source_url': 'https://rutube.ru/video/$_id/',
  'playback_kind': 'rutube_embed',
  'provider_video_id': _id,
  'media_url': null,
  'license_name': null,
  'license_url': null,
  'topics': ['cooking'],
  'duration_seconds': 35,
  'aspect_ratio': 16 / 9,
};

void main() {
  test(
    'a missing pair conflict is not misreported as a required app update',
    () {
      final options = RequestOptions(path: '/videos/feed');
      final error = DioException(
        requestOptions: options,
        response: Response(
          requestOptions: options,
          statusCode: 409,
          data: {'detail': 'Для общей подборки сначала соединись с партнёром'},
        ),
        type: DioExceptionType.badResponse,
      );
      expect(
        videoErrorText(error),
        'Для общей подборки сначала соединись с партнёром',
      );
    },
  );
  test('platform DTO needs no extracted stream or fictional license', () {
    final video = ShortVideo.fromJson(_video());
    expect(video.isRutube, isTrue);
    expect(video.mediaUrl, isNull);
    expect(video.licenseName, isNull);
    expect(video.licenseUrl, isNull);
    expect(video.attribution, 'Готовим дома · RUTUBE');
    expect(
      video.rutubeEmbedUri.toString(),
      'https://rutube.ru/play/embed/$_id?autoplay=false&q=auto',
    );
  });

  test('catalogue embed URL cannot inject navigation or script', () {
    final data = _video()..['embed_url'] = 'javascript:stealToken()';
    expect(ShortVideo.fromJson(data).rutubeEmbedUri?.host, 'rutube.ru');
    for (final id in [
      '${_id}a',
      '../$_id',
      '<script>',
      '$_id?x=1',
      _id.toUpperCase(),
    ]) {
      expect(safeRutubeEmbedUri(id), isNull, reason: id);
      expect(() => rutubeEmbedHtml(id), throwsArgumentError);
    }
    for (final override in [
      {'source_url': 'https://rutube.ru/video/${'a' * 32}/'},
      {'source_url': 'https://rutube.ru.evil.test/video/$_id/'},
      {'source_url': 'https://user@rutube.ru/video/$_id/'},
      {'source_url': 'http://rutube.ru/video/$_id/'},
      {'source_url': 'https://rutube.ru:8443/video/$_id/'},
      {'source_url': 'https://rutube.ru/video/$_id/?secret=1'},
      {'source_url': 'https://rutube.ru/video/$_id/#secret'},
      {'id': 'another-id'},
    ]) {
      expect(
        ShortVideo.fromJson({..._video(), ...override}).rutubeEmbedUri,
        isNull,
        reason: override.toString(),
      );
    }
  });

  test('source page is separate from allowlisted native media', () {
    expect(safeVideoSourceUri('https://rutube.ru/video/$_id/'), isNotNull);
    expect(safeVideoUri('https://rutube.ru/video/$_id/'), isNull);
    expect(
      safeVideoSourceUri('https://rutube.ru/video/$_id/', license: true),
      isNull,
    );
    expect(safeVideoSourceUri(null), isNull);
  });

  test(
    'wrapper filters origin and frame, starts by gesture, and has teardown',
    () {
      final html = rutubeEmbedHtml(_id);
      expect(html, contains('event.origin!==\'https://rutube.ru\''));
      expect(html, contains('event.source!==frame.contentWindow'));
      expect(html, contains('autoplay=false&amp;q=auto'));
      expect(html, contains('frame.remove()'));
      expect(html, isNot(contains('player:play\'')));
      expect(html, isNot(contains('Authorization')));
      expect(html, isNot(contains('postMessage(JSON.stringify(value),\'*\')')));
    },
  );

  test('untrusted bridge cannot supply errors, URLs or commands', () {
    expect(RutubeEvent.parse('{"type":"ready"}')?.kind, 'ready');
    expect(
      RutubeEvent.parse('{"type":"state","state":"playing"}')?.state,
      'playing',
    );
    expect(RutubeEvent.parse('{"type":"time","seconds":3.5}')?.seconds, 3.5);
    for (final value in [
      'invalid-json',
      '[]',
      'null',
      '{"type":"navigate","url":"file:///private"}',
      '{"type":"state","state":"steal"}',
      '{"type":"time","seconds":-1}',
      '{"type":"time","seconds":7201}',
      '{"type":"time","seconds":"3"}',
      '{"type":"time","seconds":1e1000}',
      '{"type":"ready","oversized":"${'x' * 160}"}',
    ]) {
      expect(RutubeEvent.parse(value), isNull, reason: value);
    }
  });

  test(
    'WebView cannot navigate its main frame into a browser or local files',
    () {
      expect(
        allowedRutubeNavigation(rutubeWrapperOrigin, mainFrame: true),
        isTrue,
      );
      expect(allowedRutubeNavigation('about:blank', mainFrame: true), isTrue);
      for (final url in [
        'https://rutube.ru/',
        'http://rutube.ru/',
        'file:///private',
        'intent://anything',
        'javascript:alert(1)',
        'https://vecher-player.invalid.evil.test/',
      ]) {
        expect(allowedRutubeNavigation(url, mainFrame: true), isFalse);
      }
      expect(
        allowedRutubeNavigation(
          'https://rutube.ru/play/embed/$_id',
          mainFrame: false,
        ),
        isTrue,
      );
      expect(
        allowedRutubeNavigation('http://rutube.ru/', mainFrame: false),
        isFalse,
      );
      expect(
        allowedRutubeNavigation('https://user@rutube.ru/', mainFrame: false),
        isFalse,
      );
    },
  );

  test(
    'unavailable interests remain selected and visible without replacement',
    () {
      final settings = VideoSettings.fromJson({
        'topics': [
          {'id': 'humor', 'title': 'Юмор', 'available': false},
          {'id': 'cooking', 'title': 'Кулинария', 'available': true},
        ],
        'personal_topics': ['humor'],
        'shared_topics': [],
        'personal_revision': 2,
        'shared_revision': 0,
        'has_pair': false,
      });
      expect(settings.personalTopics, ['humor']);
      expect(settings.topics.first.available, isFalse);
      expect(settings.topics.last.available, isTrue);
    },
  );
}
