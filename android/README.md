# My Agent — Android app

A native Android client (Kotlin + Jetpack Compose) for your agent. It talks to the agent
through the **relay** (see `../relay/`), so it works from anywhere — mobile data, other
Wi‑Fi — and your agent's machine never needs an open port.

What it does:
- **Live feed** of everything the agent does (tasks, thinking, each tool step and result,
  helper agents in their own colour, recaps, answers, reviews), plus the agent tree.
- **Now card** with the current task and step, and **＋ New session** (`/newproject`) and
  **■ Stop all** (`/stopall`) buttons. Both ask for confirmation.
- **Quick commands**: a grid of buttons you can edit (label + command), saved on the phone.
  Defaults: Pause, Resume, Status, Stop.
- **Approvals**: risky commands show up as cards with **Approve** / **Deny**.
- **Attach photos and files** with 📎, or share them to *My Agent* from the gallery or any other app.
  Each file is uploaded right away into the agent's `WORKDIR/uploads/`. When you send the
  task, its path is added to the text as `[attached files in WORKDIR: uploads/x.png]`.
  Limit: **11 MB per file**. The relay protocol sends a file as one base64 message, and the
  app's WebSocket library (OkHttp) can't send a single message over 16 MB.
- **⚙ Settings**, filled in from the agent's live state: max steps (0 = unlimited), goal
  interval, max helpers, reasoning depth, auto review, plus recent tasks, *Clear activity
  feed* (`/clear`), and disconnect. A number field is sent when you press Done or leave it,
  and is never overwritten while you're typing in it.

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

From the command line instead (Android SDK installed, `ANDROID_HOME` set, or `sdk.dir` in
`local.properties`):
```bash
cd android && ./gradlew assembleDebug
```
The APK is written to `app/build/outputs/apk/debug/app-debug.apk`. `./gradlew testDebugUnitTest`
runs the unit tests (they parse real state and event messages captured from the agent).

## Use it
On first launch, enter:
- **Relay URL:** `wss://your-relay.example/ws/app`
- **User token:** your `RELAY_USER_TOKEN`

Tap **Connect**. The URL and token are saved on the device. To disconnect, open
**⚙ Settings → Disconnect & forget token**. Type a task and tap **Send**, then watch the feed
and the agent row update live.

## Notes
- The token is stored in the app's private storage on your phone, and it is excluded from
  backups and device-to-device transfer. Anyone with it can command your agent, so treat it
  like a password and only install the app on your own phone.
- Only `wss://` (TLS) relay URLs are accepted, and plain-HTTP traffic is disabled, so the
  token is never sent in the clear. The app never logs message contents.
- This is a thin client: all the safety limits (approvals, budget, `/stop`) live in the
  agent. You can answer an approval here or from the web dashboard, the CLI or Telegram.
  Whichever answer arrives first wins, and the card disappears everywhere.
- Build target: minSdkVersion 26 (Android 8.0) and up.
