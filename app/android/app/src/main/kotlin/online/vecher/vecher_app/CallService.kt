package online.vecher.vecher_app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder

/** Keeps an explicitly accepted call alive; never starts or accepts an incoming call. */
class CallService : Service() {
    companion object {
        var running = false
            private set
        private var pendingStart: ((Boolean) -> Unit)? = null

        fun awaitStart(callback: (Boolean) -> Unit): Boolean {
            if (pendingStart != null) return false
            pendingStart = callback
            return true
        }

        fun completeStart(success: Boolean) {
            val callback = pendingStart
            pendingStart = null
            callback?.invoke(success)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        try {
            val channelId = "vecher_active_call"
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val manager = getSystemService(NotificationManager::class.java)
                manager.createNotificationChannel(
                    NotificationChannel(channelId, "Текущий звонок", NotificationManager.IMPORTANCE_LOW).apply {
                        description = "Показывается, пока продолжается звонок в «Вечере»"
                        setSound(null, null)
                    }
                )
            }
            val open = Intent(this, MainActivity::class.java).apply {
                addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            }
            val pending = PendingIntent.getActivity(
                this, 0, open, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                Notification.Builder(this, channelId)
            } else {
                @Suppress("DEPRECATION")
                Notification.Builder(this)
            }
            val notification = builder
                .setContentTitle("Звонок в «Вечере»")
                .setContentText("Нажми, чтобы вернуться к разговору")
                .setSmallIcon(android.R.drawable.sym_call_incoming)
                .setContentIntent(pending)
                .setCategory(Notification.CATEGORY_CALL)
                .setVisibility(Notification.VISIBILITY_PRIVATE)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .build()
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                startForeground(4201, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
            } else {
                startForeground(4201, notification)
            }
            running = true
            completeStart(true)
        } catch (error: RuntimeException) {
            // Android 14+ checks camera/microphone access again at promotion time.
            // Return a failure to Dart instead of crashing on a background/permission race.
            running = false
            completeStart(false)
            stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        running = false
        completeStart(false)
        stopForeground(STOP_FOREGROUND_REMOVE)
        super.onDestroy()
    }
}
