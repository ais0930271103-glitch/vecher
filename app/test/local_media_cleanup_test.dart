import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:vecher_app/features/calls/local_media_cleanup.dart';

void main() {
  test(
    'shared camera tracks detach from every stream before unique teardown',
    () async {
      final events = <String>[];
      final violations = <String>[];
      final microphone = _NativeTrack(
        'microphone',
        'audio',
        events,
        violations,
      );
      final camera = _NativeTrack('camera', 'video', events, violations);
      final main = _FakeStream(
        'main',
        [_FakeTrack(microphone), _FakeTrack(camera)],
        events,
        violations,
      );
      // Two Dart wrappers can refer to one native video track after a camera
      // restart. Both streams own that same native track until it is detached.
      final capture = _FakeStream(
        'capture',
        [_FakeTrack(camera)],
        events,
        violations,
      );

      await disposeLocalMediaStreams([main, capture, main, capture]);

      expect(violations, isEmpty);
      expect(main.getTracks(), isEmpty);
      expect(capture.getTracks(), isEmpty);
      expect(main.disposeCalls, 1);
      expect(capture.disposeCalls, 1);
      for (final track in [microphone, camera]) {
        expect(track.attachments, isEmpty);
        expect(track.stopCalls, 1);
        expect(track.stopped, isTrue);
      }
      final lastDetach = events.lastIndexWhere(
        (event) => event.startsWith('detach:'),
      );
      final firstStop = events.indexWhere((event) => event.startsWith('stop:'));
      final lastStop = events.lastIndexWhere(
        (event) => event.startsWith('stop:'),
      );
      final firstDispose = events.indexWhere(
        (event) => event.startsWith('dispose:'),
      );
      expect(lastDetach, greaterThanOrEqualTo(0));
      expect(firstStop, greaterThan(lastDetach));
      expect(firstDispose, greaterThan(lastStop));
    },
  );

  test(
    'the last audio or video track is removed before native stream disposal',
    () async {
      for (final kind in ['audio', 'video']) {
        final events = <String>[];
        final violations = <String>[];
        final track = _NativeTrack('last-$kind', kind, events, violations);
        final stream = _FakeStream(
          'only-$kind',
          [_FakeTrack(track)],
          events,
          violations,
        );

        await disposeLocalMediaStreams([stream]);

        expect(violations, isEmpty, reason: kind);
        expect(events, [
          'detach:only-$kind:last-$kind',
          'stop:last-$kind',
          'dispose:only-$kind',
        ]);
        expect(track.stopped, isTrue, reason: kind);
        expect(track.stopCalls, 1, reason: kind);
        expect(stream.disposeCalls, 1, reason: kind);
        expect(stream.getTracks(), isEmpty, reason: kind);
      }
    },
  );
}

/// Models the native track table shared by MediaStreamTrack Dart wrappers.
/// Android can no longer remove a track from a stream once trackDispose has
/// removed it from this table; callers must detach it before stopping it.
class _NativeTrack {
  _NativeTrack(this.id, this.kind, this.events, this.violations);
  final String id;
  final String kind;
  final List<String> events;
  final List<String> violations;
  final Set<String> attachments = {};
  bool enabled = true;
  bool stopped = false;
  int stopCalls = 0;

  Future<void> stop() async {
    stopCalls++;
    if (attachments.isNotEmpty || stopped) {
      violations.add(
        'stop:$id while attached to $attachments or stopped=$stopped',
      );
      throw StateError(
        'Cannot stop an attached or already stopped native track',
      );
    }
    events.add('stop:$id');
    enabled = false;
    stopped = true;
  }
}

class _FakeTrack extends MediaStreamTrack {
  _FakeTrack(this.native);
  final _NativeTrack native;

  @override
  String get id => native.id;

  @override
  String get kind => native.kind;

  @override
  String get label => native.kind;

  @override
  bool get enabled => native.enabled;

  @override
  set enabled(bool value) => native.enabled = value;

  @override
  bool get muted => !native.enabled;

  @override
  Future<void> stop() => native.stop();

  @override
  Future<void> dispose() => stop();
}

class _FakeStream extends MediaStream {
  _FakeStream(String id, List<_FakeTrack> tracks, this.events, this.violations)
    : _tracks = List<MediaStreamTrack>.of(tracks),
      super(id, 'local') {
    for (final track in tracks) {
      track.native.attachments.add(id);
    }
  }
  final List<MediaStreamTrack> _tracks;
  final List<String> events;
  final List<String> violations;
  int disposeCalls = 0;

  @override
  bool get active =>
      _tracks.any((track) => !(track as _FakeTrack).native.stopped);

  @override
  Future<void> getMediaTracks() async {}

  @override
  Future<void> addTrack(
    MediaStreamTrack track, {
    bool addToNative = true,
  }) async {
    _tracks.add(track);
    if (addToNative) (track as _FakeTrack).native.attachments.add(id);
  }

  @override
  Future<void> removeTrack(
    MediaStreamTrack track, {
    bool removeFromNative = true,
  }) async {
    final native = (track as _FakeTrack).native;
    if (native.stopped || !removeFromNative) {
      violations.add(
        'detach:$id:${native.id} after stop or without native removal',
      );
      throw StateError('Native track can no longer be detached');
    }
    _tracks.removeWhere((item) => item.id == native.id);
    native.attachments.remove(id);
    events.add('detach:$id:${native.id}');
  }

  // Return the live list so the cleanup must take its own snapshot before
  // removeTrack mutates it, including when only one audio/video track remains.
  @override
  List<MediaStreamTrack> getTracks() => _tracks;

  @override
  List<MediaStreamTrack> getAudioTracks() =>
      _tracks.where((track) => track.kind == 'audio').toList();

  @override
  List<MediaStreamTrack> getVideoTracks() =>
      _tracks.where((track) => track.kind == 'video').toList();

  @override
  Future<void> dispose() async {
    disposeCalls++;
    if (_tracks.isNotEmpty || disposeCalls > 1) {
      violations.add('dispose:$id while nonempty or disposed twice');
      throw StateError(
        'Native streamDispose must receive an empty stream once',
      );
    }
    events.add('dispose:$id');
  }
}
