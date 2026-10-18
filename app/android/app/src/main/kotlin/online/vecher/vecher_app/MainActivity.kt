package online.vecher.vecher_app

import android.os.Bundle
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.WindowManager
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Запрет скриншотов включён сразу, ещё до старта Flutter; дальше приложение
        // применяет настройку человека («Пара» → «Настройки» → «Запрет скриншотов»).
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "vecher/call").setMethodCallHandler { call, result ->
            when (call.method) {
                "start" -> {
                    if (CallService.running) {
                        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                        result.success(null)
                        return@setMethodCallHandler
                    }
                    val handler = Handler(Looper.getMainLooper())
                    val timeout = Runnable {
                        CallService.completeStart(false)
                        stopService(Intent(this, CallService::class.java))
                    }
                    val waiting = CallService.awaitStart { success ->
                        handler.removeCallbacks(timeout)
                        if (success) {
                            window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                            result.success(null)
                        } else {
                            result.error("call_service_unavailable", "Не удалось запустить звонок", null)
                        }
                    }
                    if (!waiting) {
                        result.error("call_service_busy", "Звонок уже запускается", null)
                        return@setMethodCallHandler
                    }
                    try {
                        // Permission is granted; success waits for startForeground() inside the service.
                        val service = Intent(this, CallService::class.java)
                        handler.postDelayed(timeout, 7000)
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(service)
                        } else {
                            startService(service)
                        }
                    } catch (error: RuntimeException) {
                        CallService.completeStart(false)
                        stopService(Intent(this, CallService::class.java))
                    }
                }
                "stop" -> {
                    stopService(Intent(this, CallService::class.java))
                    window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
                    result.success(null)
                }
                else -> result.notImplemented()
            }
        }
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "vecher/secure").setMethodCallHandler { call, result ->
            if (call.method == "set") {
                val on = call.arguments as? Boolean ?: true
                runOnUiThread {
                    if (on) {
                        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
                    } else {
                        window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
                    }
                }
                result.success(null)
            } else {
                result.notImplemented()
            }
        }
    }
}
