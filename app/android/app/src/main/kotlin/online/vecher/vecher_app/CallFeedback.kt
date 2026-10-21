package online.vecher.vecher_app

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.Ringtone
import android.media.RingtoneManager
import android.media.ToneGenerator
import android.os.Build
import android.os.Handler
import android.os.Looper

/** Foreground-only unanswered-call sounds; never owns a microphone or an audio route. */
class CallFeedback(context: Context) {
    private val context = context.applicationContext
    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val handler = Handler(Looper.getMainLooper())
    private var ringtone: Ringtone? = null
    private var tone: ToneGenerator? = null
    private var focusRequest: AudioFocusRequest? = null
    private var legacyFocusListener: AudioManager.OnAudioFocusChangeListener? = null
    private var foreground = false
    private var disposed = false
    private var latestOwner = -1L
    private var latestGeneration = -1L

    fun setForeground(value: Boolean) {
        foreground = value
        if (!value) stopPlayback()
        // Resuming never replays a stale call. Dart first refreshes server state.
    }

    fun play(owner: Long, generation: Long, incoming: Boolean, durationMs: Long): Boolean {
        if (!admit(owner, generation)) return false
        stopPlayback()
        if (!foreground || durationMs <= 0) return false
        val duration = durationMs.coerceAtMost(60_000L)
        try {
            // A deliberately silent default ringtone is not replaced by a fallback beep.
            if (incoming && (!mayRing() ||
                RingtoneManager.getActualDefaultRingtoneUri(context, RingtoneManager.TYPE_RINGTONE) == null)) {
                return false
            }
            val attributes = AudioAttributes.Builder()
                .setUsage(if (incoming) AudioAttributes.USAGE_NOTIFICATION_RINGTONE else AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build()
            val listener = AudioManager.OnAudioFocusChangeListener { change ->
                if (change < 0 && owner == latestOwner && generation == latestGeneration) {
                    stopPlayback()
                }
            }
            val granted = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                    .setAudioAttributes(attributes)
                    .setAcceptsDelayedFocusGain(false)
                    .setWillPauseWhenDucked(true)
                    .setOnAudioFocusChangeListener(listener, handler)
                    .build()
                focusRequest = request
                audio.requestAudioFocus(request)
            } else {
                legacyFocusListener = listener
                @Suppress("DEPRECATION")
                audio.requestAudioFocus(listener,
                    if (incoming) AudioManager.STREAM_RING else AudioManager.STREAM_MUSIC,
                    AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
            }
            if (granted != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
                stopPlayback()
                return false
            }
            if (incoming) {
                val player = RingtoneManager.getRingtone(context,
                    RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE))
                if (player == null) {
                    stopPlayback()
                    return false
                }
                ringtone = player
                player.audioAttributes = attributes
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) player.isLooping = true
                player.play()
                monitorRingtone(owner, generation)
            } else {
                val player = ToneGenerator(AudioManager.STREAM_MUSIC, 55)
                tone = player
                if (!player.startTone(ToneGenerator.TONE_SUP_RINGTONE, duration.toInt())) {
                    stopPlayback()
                    return false
                }
            }
            handler.postDelayed({
                if (owner == latestOwner && generation == latestGeneration) stopPlayback()
            }, duration)
            return true
        } catch (_: RuntimeException) {
            // Sound is optional. A missing/busy native player must not break accepting a call.
            stopPlayback()
            return false
        }
    }

    fun stop(owner: Long, generation: Long) {
        if (admit(owner, generation)) stopPlayback()
    }

    private fun admit(owner: Long, generation: Long): Boolean {
        if (disposed || owner < latestOwner ||
            (owner == latestOwner && generation <= latestGeneration)) return false
        latestOwner = owner
        latestGeneration = generation
        return true
    }

    private fun mayRing(): Boolean = audio.ringerMode == AudioManager.RINGER_MODE_NORMAL &&
        audio.getStreamVolume(AudioManager.STREAM_RING) > 0

    private fun monitorRingtone(owner: Long, generation: Long) {
        handler.postDelayed(object : Runnable {
            override fun run() {
                if (owner != latestOwner || generation != latestGeneration || ringtone == null) return
                try {
                    if (!foreground || !mayRing()) {
                        stopPlayback()
                        return
                    }
                    val player = ringtone ?: return
                    // setLooping is only public from API 28. Never restart an already playing sound.
                    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P && !player.isPlaying) player.play()
                    handler.postDelayed(this, 1000)
                } catch (_: RuntimeException) {
                    stopPlayback()
                }
            }
        }, 1000)
    }

    private fun stopPlayback() {
        handler.removeCallbacksAndMessages(null)
        val oldRingtone = ringtone
        ringtone = null
        try { oldRingtone?.stop() } catch (_: RuntimeException) {}
        val oldTone = tone
        tone = null
        try { oldTone?.stopTone() } catch (_: RuntimeException) {}
        try { oldTone?.release() } catch (_: RuntimeException) {}
        val oldRequest = focusRequest
        focusRequest = null
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && oldRequest != null) {
            try { audio.abandonAudioFocusRequest(oldRequest) } catch (_: RuntimeException) {}
        }
        val oldListener = legacyFocusListener
        legacyFocusListener = null
        if (oldListener != null) {
            @Suppress("DEPRECATION")
            try { audio.abandonAudioFocus(oldListener) } catch (_: RuntimeException) {}
        }
        // Do not change audio mode, speaker/Bluetooth routing or system volume.
        // Flutter WebRTC owns those once this method has acknowledged stopping.
    }

    fun dispose() {
        disposed = true
        foreground = false
        stopPlayback()
    }
}
