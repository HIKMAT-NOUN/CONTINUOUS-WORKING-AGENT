package com.myagent.app

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import app.cash.paparazzi.DeviceConfig
import app.cash.paparazzi.Paparazzi
import org.json.JSONObject
import org.junit.Rule
import org.junit.Test

/** Renders the real screens with the agent's real state snapshot (src/test/resources/state.json).
 *  `./gradlew recordPaparazziDebug` writes the PNGs to app/src/test/snapshots/images/. */
class ScreensTest {
    @get:Rule val paparazzi = Paparazzi(deviceConfig = DeviceConfig.PIXEL_5.copy(softButtons = false),
        theme = "android:Theme.Material.NoActionBar", maxPercentDifference = 0.1)

    private val state = parseStatus(JSONObject(javaClass.classLoader!!.getResource("state.json")!!.readText())
        .getJSONObject("state")).copy(
            recent = listOf(RecentTask(12, "task", "summarise the repo layout into notes.md", "done", 9, 14200),
                            RecentTask(11, "task", "migrate the db to postgres", "failed", 40, 88000)),
            tokens = 10_683_500, budget = 20_000_000, cost = 1.37, promptTokens = 10_560_000, completionTokens = 123_500)

    @Composable private fun Themed(content: @Composable () -> Unit) =
        MaterialTheme(colorScheme = darkColorScheme(primary = Accent, background = Bg, surface = Panel,
            onBackground = TextMain, onSurface = TextMain)) { Surface(Modifier.fillMaxSize(), color = Bg) { content() } }

    @Test fun settingsTab() = paparazzi.snapshot {
        Themed {
            Column {
                TopBar(connected = true, agentOnline = true, status = state, tab = 1) {}
                SettingsContent(state, "wss://relay.example/ws/app", showOutput = false, tint = true, act = SettingsActions())
            }
        }
    }

    @Test fun settingsTabBeforeStateArrives() = paparazzi.snapshot {
        Themed {
            Column {
                TopBar(connected = false, agentOnline = false, status = null, tab = 1) {}
                SettingsContent(null, "wss://relay.example/ws/app", showOutput = true, tint = true, act = SettingsActions())
            }
        }
    }

    @Test fun activityTab() = paparazzi.snapshot {
        Themed {
            Column {
                TopBar(connected = true, agentOnline = true, status = state, tab = 0) {}
                NowCard(state, {}, {})
                QuickBar(DEFAULT_QUICK, {}, {})
                listOf(
                    FeedItem(1, 0.0, "mine", "you", "build a todo app"),
                    FeedItem(2, 0.0, "task", "main", "▶️ Task #13: build a todo app"),
                    FeedItem(3, 0.0, "thinking", "main", "🧠 I'll scaffold Flask first."),
                    FeedItem(4, 0.0, "step", "main", "📂 listing: ."),
                    FeedItem(5, 0.0, "result", "main", "✅ ok — 3 files", ok = true),
                    FeedItem(6, 0.0, "output", "main", "README.md\nnotes.txt\nrequirements.txt"),
                    FeedItem(7, 0.0, "step", "w1", "💻 running: pytest -q"),
                    FeedItem(8, 0.0, "result", "w1", "❌ error — 1 failed", ok = false),
                ).forEach { FeedRow(it, tint = true) }
                state.pendingApprovals.forEach { ApprovalCard(it, busy = false) {} }
            }
        }
    }
}
