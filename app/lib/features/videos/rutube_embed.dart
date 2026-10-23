import 'dart:convert';

import 'videos.dart';

const rutubeWrapperOrigin = 'https://vecher-player.invalid/';

/// Only the provider's public ID enters this local wrapper. It has no access
/// to API credentials, account data, files, or a caller-supplied HTML string.
String rutubeEmbedHtml(String providerId) {
  final embed = safeRutubeEmbedUri(providerId);
  if (embed == null) throw ArgumentError('Invalid public RUTUBE ID');
  final src = const HtmlEscape().convert(embed.toString());
  return '''<!doctype html>
<html lang="ru"><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="referrer" content="strict-origin-when-cross-origin">
<style>html,body{margin:0;width:100%;height:100%;background:#000;overflow:hidden}
iframe{border:0;width:100%;height:100%;display:block}</style>
</head><body>
<iframe id="player" title="Плеер RUTUBE" src="$src"
 allow="autoplay; fullscreen; encrypted-media; picture-in-picture" allowfullscreen></iframe>
<script>
(function(){
  'use strict';
  var frame=document.getElementById('player');
  var active=true;
  function emit(value){
    if(active && window.VecherVideo) VecherVideo.postMessage(JSON.stringify(value));
  }
  window.addEventListener('message', function(event){
    if(!active || event.origin!=='https://rutube.ru' || event.source!==frame.contentWindow) return;
    var message;
    try { message=typeof event.data==='string' ? JSON.parse(event.data) : event.data; }
    catch (_) { return; }
    if(!message || typeof message!=='object') return;
    var data=message.data || {};
    switch(message.type){
      case 'player:ready': emit({type:'ready'}); break;
      case 'player:changeState':
        if(['playing','paused','stopped'].indexOf(data.state)!==-1)
          emit({type:'state',state:data.state});
        break;
      case 'player:currentTime':
        if(typeof data.time==='number' && Number.isFinite(data.time) && data.time>=0 && data.time<=7200)
          emit({type:'time',seconds:data.time});
        break;
      case 'player:error': emit({type:'error'}); break;
    }
  });
  window.vecherPause=function(){
    if(active && frame.contentWindow)
      frame.contentWindow.postMessage(JSON.stringify({type:'player:pause',data:{}}),'https://rutube.ru');
  };
  window.vecherClose=function(){
    if(!active) return;
    if(frame.contentWindow)
      frame.contentWindow.postMessage(JSON.stringify({type:'player:stop',data:{}}),'https://rutube.ru');
    active=false;
    frame.remove();
  };
  document.addEventListener('visibilitychange',function(){if(document.hidden) window.vecherPause();});
})();
</script></body></html>''';
}

/// The bridge accepts a tiny, non-authoritative status vocabulary. Provider
/// messages never carry raw URLs, errors, identifiers or navigation commands
/// into the app. Platform channels may also be visible inside iframes.
class RutubeEvent {
  const RutubeEvent(this.kind, {this.state, this.seconds});
  final String kind;
  final String? state;
  final double? seconds;

  static RutubeEvent? parse(String value) {
    if (value.length > 160) return null;
    try {
      final data = jsonDecode(value);
      if (data is! Map<String, dynamic>) return null;
      switch (data['type']) {
        case 'ready':
        case 'error':
          return RutubeEvent(data['type'] as String);
        case 'state':
          final state = data['state'];
          if (state is String &&
              {'playing', 'paused', 'stopped'}.contains(state)) {
            return RutubeEvent('state', state: state);
          }
        case 'time':
          final seconds = data['seconds'];
          if (seconds is num &&
              seconds.isFinite &&
              seconds >= 0 &&
              seconds <= 7200) {
            return RutubeEvent('time', seconds: seconds.toDouble());
          }
      }
    } catch (_) {
      // An untrusted, malformed provider message is ignored.
    }
    return null;
  }
}

bool allowedRutubeNavigation(String value, {required bool mainFrame}) {
  if (value == 'about:blank') return true;
  if (mainFrame) return value == rutubeWrapperOrigin;
  final uri = Uri.tryParse(value);
  // Subframes belong to the official player (including its advertising).
  // Resource loads remain under the WebView's normal HTTPS/SOP restrictions.
  return uri != null &&
      uri.scheme == 'https' &&
      uri.host.isNotEmpty &&
      uri.userInfo.isEmpty &&
      (!uri.hasPort || uri.port == 443);
}
