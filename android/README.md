# My Agent — Android app

A native Android client (Kotlin + Jetpack Compose) for your agent. It talks to the agent
through the **relay** (see `../relay/`), so it works from anywhere — mobile data, other
Wi‑Fi — and your agent's machine never needs an open port.

It shows the live feed, the agent tree (lead + helpers + reviewer) with each one's status,
and an input box to send tasks and `/commands`. Replies and events stream back in real time.

## What you need first
1. A **relay** running on a public host with TLS, from `../relay/` (its README has the
   steps). You'll have a `wss://your-relay.example/ws/app` URL and a `RELAY_USER_TOKEN`.
2. Your **agent** running with the relay configured in its `.env`:
   ```
   RELAY_URL=wss://your-relay.example/ws/agent
   RELAY_AGENT_KEY=...        # matches the relay's RELAY_AGENT_KEY
   ```
   Restart the agent; the relay's `/healthz` should show `"agent_online": true`.

## Build the APK
This project builds with Android Studio — no server, no Play Store needed.

1. Install **Android Studio** (any recent version).
2. **Open** this `android/` folder in Android Studio. Let it sync Gradle (it downloads the
   Gradle wrapper and the Android SDK/build tools automatically the first time).
3. Plug in your phone with **USB debugging** on (Settings → Developer options), or use an
   emulator. Press **Run ▶**. The app installs and opens.
   - To get a shareable file instead: **Build → Build Bundle(s)/APK(s) → Build APK(s)**.
     The APK lands in `app/build/outputs/apk/debug/app-debug.apk`; copy it to your phone and
     open it to install (allow "install unknown apps" once).

From the command line instead (Android SDK installed, `ANDROID_HOME` set):
```bash
cd android && ./gradlew assembleDebug
```
(If `./gradlew` is missing, run `gradle wrapper` once in this folder, or just open the
project in Android Studio which creates it.)

## Use it
On first launch, enter:
- **Relay URL:** `wss://your-relay.example/ws/app`
- **User token:** your `RELAY_USER_TOKEN`

Tap **Connect**. The settings are saved on the device (tap ⚙ in the header to change them
or disconnect). Type a task and hit **Send**; watch the feed and the agent row update live.

## Notes
- The token is stored in the app's private storage on your phone. Anyone with it can
  command your agent, so treat it like a password and only install the app on your own
  phone. Use a `wss://` relay (TLS) so it's never sent in the clear.
- This is a thin client: all the safety limits (approvals, budget, `/stop`) live in the
  agent. Approvals still happen — approve risky commands from the web dashboard, the CLI,
  or Telegram. (A future version can add Approve/Deny buttons here too.)
- Build target: minSdkVersion 26 (Android 8.0) and up.
