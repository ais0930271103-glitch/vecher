# Native WebRTC invokes Java callbacks via JNI in release builds.
-keep class org.webrtc.** { *; }
-dontwarn org.webrtc.**
