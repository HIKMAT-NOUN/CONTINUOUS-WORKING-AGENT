package com.myagent.app

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.ui.Modifier
import app.cash.paparazzi.DeviceConfig
import app.cash.paparazzi.Paparazzi
import org.json.JSONObject
import org.junit.Rule
import org.junit.Test

/** The whole Settings tab on one tall canvas, so every section can be checked at once. */
class SettingsFullTest {
    @get:Rule val paparazzi = Paparazzi(deviceConfig = DeviceConfig.PIXEL_5.copy(screenHeight = 5600, softButtons = false),
        theme = "android:Theme.Material.NoActionBar", maxPercentDifference = 0.1)

    @Test fun wholeSettingsTab() {
        val st = parseStatus(JSONObject(javaClass.classLoader!!.getResource("state.json")!!.readText()).getJSONObject("state"))
            .copy(recent = listOf(RecentTask(12, "task", "summarise the repo layout into notes.md", "done", 9, 14200)),
                  promptTokens = 10_560_000, completionTokens = 123_500)
        paparazzi.snapshot {
            MaterialTheme(colorScheme = darkColorScheme(primary = Accent, background = Bg, surface = Panel)) {
                Surface(Modifier.fillMaxSize(), color = Bg) {
                    Column {
                        TopBar(connected = true, agentOnline = true, status = st, tab = 1) {}
                        SettingsContent(st, "wss://relay.example/ws/app", showOutput = false, tint = true, act = SettingsActions())
                    }
                }
            }
        }
    }
}
