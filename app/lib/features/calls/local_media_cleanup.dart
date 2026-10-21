import 'package:flutter_webrtc/flutter_webrtc.dart';

/// Detach shared tracks before stopping them and dispose only empty streams.
/// Android's native registry forgets a stopped track, so removing it afterwards
/// cannot empty its streams. This also avoids the plugin's streamDispose loop
/// mutating the track list that it is iterating.
Future<void> disposeLocalMediaStreams(Iterable<MediaStream> values) async {
  final streams = {for (final stream in values) stream.id: stream};
  final snapshots = {
    for (final stream in streams.values)
      stream.id: List<MediaStreamTrack>.of(stream.getTracks()),
  };
  final tracks = {
    for (final list in snapshots.values)
      for (final track in list) track.id: track,
  };
  for (final track in tracks.values) {
    try {
      track.enabled = false;
    } catch (_) {}
  }
  for (final stream in streams.values) {
    for (final track in snapshots[stream.id]!) {
      await _quiet(() => stream.removeTrack(track));
    }
  }
  for (final track in tracks.values) {
    await _quiet(track.stop);
  }
  for (final stream in streams.values) {
    await _quiet(stream.dispose);
  }
}

Future<void> _quiet(Future<void> Function() action) async {
  try {
    await action();
  } catch (_) {}
}
