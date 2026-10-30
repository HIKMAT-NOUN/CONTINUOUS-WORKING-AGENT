# My Agent — Android app

A native Android client (Kotlin + Jetpack Compose) for your agent. It talks to the agent
through the **relay** (see `../relay/`), so it works from anywhere — mobile data, other
Wi‑Fi — and your agent's machine never needs an open port.

What it does (matches the web dashboard):
- **Live feed**, colour-coded per agent, with `<b>`/`<code>` formatting and timestamps.
  Long-press any row to copy it. Tool **output** is hidden by default (⋮ → *Show tool output*);
  when shown, each output is a collapsed row you tap to expand. ⋮ → *Summary only* hides
  steps/results/output, like the web *Summary* chip.
- **Agent tree** — lead + helpers + reviewer with status, step `n/max`, current action and
  thinking. Tap *AGENTS* to switch between a compact row and an indented tree. **Tap an agent**
  to filter the feed to just that agent (tap again, *Show all*, or Back to clear).
- **Approve / Deny** cards when the agent asks for approval (sends `/approve <id>` or
  `/deny <id>`); the card disappears when `approval_done` arrives from any interface.
- **■ Stop** in the header while a task runs (sends `/stop` after a confirm).
- **Connection feedback** — header dot (live / agent offline / reconnecting / token
  rejected), a banner with the retry countdown and *Retry now*, and a "Reconnected" notice.
  It reconnects with backoff, retries immediately when the network comes back, and resumes
  from the last event it saw (no duplicate replay). The socket is closed while the app is in
  the background and resumes when you return.
- Snackbars for *task done/failed*, *review ready*, *approval needed*.

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

From the command line instead (JDK 17+ and the Android SDK installed; set `ANDROID_HOME`
or put `sdk.dir=/path/to/sdk` in `local.properties`):
```bash
cd android && ./gradlew assembleDebug
```
The APK is `app/build/outputs/apk/debug/app-debug.apk`. Unit tests: `./gradlew testDebugUnitTest`.

Without Android Studio, the SDK pieces this needs are
`sdkmanager "platforms;android-34" "build-tools;34.0.0" "platform-tools"` (from Google's
*command line tools* download).

## Install on your phone
- **USB:** enable *Developer options → USB debugging*, plug in, then
  `adb install -r app/build/outputs/apk/debug/app-debug.apk`.
- **No cable:** copy `app-debug.apk` to the phone (Drive, email to yourself, USB file copy),
  tap it in *Files*, and allow *Install unknown apps* for that app when asked.
- Updating keeps your saved settings as long as it's signed with the same key. A debug APK
  is signed with the debug key of the machine that built it, so keep building on one machine
  (or uninstall first if Android says the signature doesn't match).

## Use it
On first launch, enter:
- **Relay URL:** `wss://your-relay.example/ws/app`
- **User token:** your `RELAY_USER_TOKEN`

(`https://…` or a bare host is accepted and turned into `wss://…/ws/app`; plain `ws://` /
`http://` is refused.) Tap **Connect**. Change it later with ⋮ → *Connection settings* (leave the
token empty to keep the saved one), or ⋮ → *Forget token & disconnect*. Type a task and hit
**Send**; watch the feed and the agents update live.

## Security notes
- The token is a password to your machine. It's kept in the app's private storage,
  encrypted with an AES key held in the Android Keystore (it never leaves the phone), and the
  app opts out of cloud backup and device-to-device transfer. The setup screen blocks
  screenshots and the recent-apps preview.
- Only `wss://` (TLS) is allowed, cleartext traffic is disabled for the whole app, only the
  system's trusted CAs are accepted, and redirects are not followed (so the token is never
  re-sent to another host). The token goes in the `Authorization` header, never in the URL.
- The app never logs the token, URL or message contents.
- If the relay rejects the token (401/403), the app stops retrying and asks you to check
  your settings instead of hammering the relay.
- This is a thin client: all the safety limits (approvals, budget, `/stop`) live in the agent.
- Build target: minSdk 26 (Android 8.0) and up.
