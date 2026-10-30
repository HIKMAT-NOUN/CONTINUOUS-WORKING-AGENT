package com.myagent.app

import android.app.Application
import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONArray
import org.json.JSONObject

// ---------- models ----------
data class FeedItem(val id: Long, val ts: Double, val kind: String, val agent: String, val text: String)
data class AgentNode(val id: String, val role: String, val status: String, val step: Int,
                     val maxSteps: Int, val title: String, val action: String, val thinking: String)
data class Status(val state: String, val desc: String, val model: String, val tokens: Long,
                  val budget: Long, val cost: Double, val queue: Int, val fleet: List<AgentNode>)

fun htmlToText(html: String): String =
    html.replace(Regex("<[^>]+>"), "")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        .replace("&quot;", "\"").replace("&#x27;", "'").replace("&#39;", "'").trim()

fun parseAgent(o: JSONObject) = AgentNode(
    o.optString("id"), o.optString("role"), o.optString("status"),
    o.optInt("step"), o.optInt("max_steps"), o.optString("title"),
    htmlToText(o.optString("action")), o.optString("thinking"))

fun parseStatus(s: JSONObject): Status {
    val fleet = ArrayList<AgentNode>()
    s.optJSONArray("fleet")?.let { for (i in 0 until it.length()) fleet.add(parseAgent(it.getJSONObject(i))) }
    val cur = s.optJSONObject("current")
    return Status(
        state = s.optString("state"),
        desc = cur?.optString("description") ?: "",
        model = s.optString("model"),
        tokens = s.optLong("tokens_today"),
        budget = s.optLong("budget"),
        cost = s.optDouble("cost_today", 0.0),
        queue = s.optInt("queue_count"),
        fleet = fleet)
}

fun eventToFeed(ev: JSONObject): FeedItem? {
    val kind = ev.optString("kind")
    if (kind == "output") return null              // keep the phone feed light
    val text = htmlToText(ev.optString("html"))
    if (text.isEmpty()) return null
    return FeedItem(ev.optLong("id"), ev.optDouble("ts"), kind, ev.optString("agent", "main"), text)
}

// ---------- colors ----------
val Bg = Color(0xFF0E1015); val Panel = Color(0xFF161920); val Panel2 = Color(0xFF1C2029)
val Border = Color(0xFF2A2F3B); val TextMain = Color(0xFFE6E8EE); val Muted = Color(0xFF8B93A5)
val Accent = Color(0xFF3B82F6); val OkC = Color(0xFF4ADE80); val ErrC = Color(0xFFF87171)
val Palette = listOf(Color(0xFF22C55E), Color(0xFFA855F7), Color(0xFF14B8A6), Color(0xFFEC4899),
    Color(0xFFF59E0B), Color(0xFF06B6D4), Color(0xFFEF4444), Color(0xFF84CC16))
private val agentColors = HashMap<String, Color>()
private var colorCursor = 0
fun colorFor(agent: String): Color {
    if (agent == "main") return Accent
    return agentColors.getOrPut(agent) { Palette[colorCursor++ % Palette.size] }
}
fun roleIcon(role: String) = when (role) { "main" -> "🧠"; "reviewer" -> "🔎"; else -> "🤖" }
fun fmtTok(n: Long) = when { n >= 1_000_000 -> "%.2fM".format(n / 1e6); n >= 1000 -> "%.1fk".format(n / 1e3); else -> n.toString() }

// ---------- view model ----------
class AgentViewModel(app: Application) : AndroidViewModel(app), RelayClient.Listener {
    private val prefs = app.getSharedPreferences("myagent", Context.MODE_PRIVATE)
    val url = MutableStateFlow(prefs.getString("url", "") ?: "")
    val token = MutableStateFlow(prefs.getString("token", "") ?: "")
    val configured = MutableStateFlow(!url.value.isNullOrBlank() && !token.value.isNullOrBlank())

    val feed = MutableStateFlow<List<FeedItem>>(emptyList())
    val status = MutableStateFlow<Status?>(null)
    val connected = MutableStateFlow(false)
    val agentOnline = MutableStateFlow(false)
    val banner = MutableStateFlow("")

    private var client: RelayClient? = null

    fun saveAndConnect(u: String, t: String) {
        prefs.edit().putString("url", u.trim()).putString("token", t.trim()).apply()
        url.value = u.trim(); token.value = t.trim(); configured.value = true
        connect()
    }

    fun connect() {
        client?.close()
        feed.value = emptyList(); status.value = null
        if (url.value.isBlank() || token.value.isBlank()) return
        client = RelayClient(url.value, token.value, this).also { it.connect() }
    }

    fun send(text: String) {
        if (text.isBlank()) return
        appendLocal(text)
        client?.sendCommand(text)
    }

    fun logout() {
        client?.close(); client = null
        prefs.edit().clear().apply()
        url.value = ""; token.value = ""; configured.value = false
        feed.value = emptyList(); status.value = null; connected.value = false
    }

    private fun appendLocal(text: String) {
        val item = FeedItem(-System.nanoTime(), System.currentTimeMillis() / 1000.0, "mine", "you", "› $text")
        feed.value = (feed.value + item).takeLast(500)
    }
    private fun add(item: FeedItem?) { if (item != null) feed.value = (feed.value + item).takeLast(500) }

    // RelayClient.Listener (called off the main thread; StateFlow is thread-safe) ----------
    override fun onConnected() { connected.value = true; banner.value = "" }
    override fun onDisconnected(reason: String) { connected.value = false; banner.value = "reconnecting…" }
    override fun onHello(agentOnlineFlag: Boolean, events: JSONArray, state: JSONObject?) {
        agentOnline.value = agentOnlineFlag
        val list = ArrayList<FeedItem>()
        for (i in 0 until events.length()) eventToFeed(events.getJSONObject(i))?.let { list.add(it) }
        feed.value = list.takeLast(500)
        state?.let { status.value = parseStatus(it) }
    }
    override fun onEvent(ev: JSONObject) { add(eventToFeed(ev)) }
    override fun onState(state: JSONObject) { status.value = parseStatus(state) }
    override fun onReply(ref: String?, html: String) {
        val t = htmlToText(html); if (t.isNotEmpty())
            add(FeedItem(-System.nanoTime(), System.currentTimeMillis() / 1000.0, "reply", "main", t))
    }
    override fun onPresence(online: Boolean) { agentOnline.value = online }
}

// ---------- activity ----------
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                primary = Accent, background = Bg, surface = Panel, onBackground = TextMain, onSurface = TextMain)) {
                val vm: AgentViewModel = viewModel()
                val configured by vm.configured.collectAsState()
                LaunchedEffect(configured) { if (configured) vm.connect() }
                Surface(Modifier.fillMaxSize(), color = Bg) {
                    if (configured) AgentScreen(vm) else SetupScreen(vm)
                }
            }
        }
    }
}

@Composable
fun SetupScreen(vm: AgentViewModel) {
    var u by remember { mutableStateOf(vm.url.value) }
    var t by remember { mutableStateOf(vm.token.value) }
    Column(Modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.Center) {
        Text("Connect to your agent", color = TextMain, fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(6.dp))
        Text("Enter your relay address and token. The agent must be running with the same relay configured.",
            color = Muted, fontSize = 13.sp)
        Spacer(Modifier.height(20.dp))
        OutlinedTextField(u, { u = it }, label = { Text("Relay URL (wss://…/ws/app)") },
            singleLine = true, modifier = Modifier.fillMaxWidth(), colors = fieldColors())
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(t, { t = it }, label = { Text("User token") },
            singleLine = true, modifier = Modifier.fillMaxWidth(), colors = fieldColors())
        Spacer(Modifier.height(20.dp))
        Button(onClick = { vm.saveAndConnect(u, t) }, enabled = u.isNotBlank() && t.isNotBlank(),
            modifier = Modifier.fillMaxWidth()) { Text("Connect") }
    }
}

@Composable
fun fieldColors() = OutlinedTextFieldDefaults.colors(
    focusedTextColor = TextMain, unfocusedTextColor = TextMain,
    focusedBorderColor = Accent, unfocusedBorderColor = Border,
    focusedLabelColor = Accent, unfocusedLabelColor = Muted, cursorColor = Accent)

@Composable
fun AgentScreen(vm: AgentViewModel) {
    val feed by vm.feed.collectAsState()
    val status by vm.status.collectAsState()
    val connected by vm.connected.collectAsState()
    val agentOnline by vm.agentOnline.collectAsState()
    val banner by vm.banner.collectAsState()
    var input by remember { mutableStateOf("") }
    val listState = rememberLazyListState()
    LaunchedEffect(feed.size) { if (feed.isNotEmpty()) listState.animateScrollToItem(feed.size - 1) }

    Column(Modifier.fillMaxSize()) {
        // header
        Row(Modifier.fillMaxWidth().background(Panel).padding(14.dp, 12.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Text("🤖 My Agent", color = TextMain, fontWeight = FontWeight.Bold, fontSize = 17.sp)
            Spacer(Modifier.width(10.dp))
            val dot = when { !connected -> ErrC; agentOnline -> OkC; else -> Muted }
            Box(Modifier.size(9.dp).clip(RoundedCornerShape(50)).background(dot))
            Spacer(Modifier.width(6.dp))
            Text(if (!connected) "offline" else if (agentOnline) (status?.state ?: "live") else "agent off",
                color = Muted, fontSize = 12.sp)
            Spacer(Modifier.weight(1f))
            status?.let { Text("${fmtTok(it.tokens)}/${fmtTok(it.budget)}", color = Muted, fontSize = 11.sp) }
            Spacer(Modifier.width(10.dp))
            Text("⚙", color = Muted, fontSize = 18.sp, modifier = Modifier.clickable { vm.logout() })
        }
        if (banner.isNotEmpty())
            Text(banner, color = Color(0xFFFBBF24), fontSize = 12.sp,
                modifier = Modifier.fillMaxWidth().background(Panel2).padding(14.dp, 4.dp))

        // agents row
        status?.fleet?.takeIf { it.isNotEmpty() }?.let { fleet ->
            LazyRow(Modifier.fillMaxWidth().background(Panel).padding(vertical = 8.dp),
                contentPadding = PaddingValues(horizontal = 12.dp)) {
                items(fleet) { a -> AgentChip(a) }
            }
        }

        // feed
        LazyColumn(state = listState, modifier = Modifier.weight(1f).fillMaxWidth().padding(horizontal = 12.dp)) {
            items(feed) { item -> FeedRow(item) }
        }

        // input
        Row(Modifier.fillMaxWidth().background(Panel).padding(10.dp),
            verticalAlignment = Alignment.Bottom) {
            OutlinedTextField(input, { input = it },
                placeholder = { Text("Describe a task… or /command", color = Muted) },
                modifier = Modifier.weight(1f), maxLines = 4, colors = fieldColors(),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { vm.send(input); input = "" }))
            Spacer(Modifier.width(8.dp))
            Button(onClick = { vm.send(input); input = "" }, enabled = input.isNotBlank(),
                modifier = Modifier.height(52.dp)) { Text("Send") }
        }
    }
}

@Composable
fun AgentChip(a: AgentNode) {
    val c = colorFor(a.id)
    Column(Modifier.padding(end = 8.dp).width(150.dp).clip(RoundedCornerShape(10.dp))
        .background(Panel2).padding(8.dp, 7.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            val dot = when (a.status) { "working" -> c; "done" -> OkC; "idle" -> Muted; else -> ErrC }
            Box(Modifier.size(8.dp).clip(RoundedCornerShape(50)).background(dot))
            Spacer(Modifier.width(6.dp))
            Text("${roleIcon(a.role)} ${a.id}", color = TextMain, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.weight(1f))
            Text(if (a.status == "working") "${a.step}/${a.maxSteps}" else a.status,
                color = Muted, fontSize = 10.sp)
        }
        if (a.title.isNotEmpty())
            Text(a.title, color = Muted, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
        val sub = if (a.status == "working") (a.action.ifEmpty { "working…" }) else ""
        if (sub.isNotEmpty())
            Text(sub, color = TextMain, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun FeedRow(item: FeedItem) {
    val isMine = item.kind == "mine"
    val tagged = item.agent != "main" && !isMine
    val c = colorFor(item.agent)
    Column(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
        if (tagged)
            Text(item.agent.uppercase(), color = c, fontSize = 10.sp, fontWeight = FontWeight.Bold)
        Text(item.text,
            color = if (isMine) Accent else TextMain,
            fontSize = 14.sp,
            fontWeight = if (item.kind == "final" || item.kind == "task") FontWeight.Medium else FontWeight.Normal,
            fontFamily = if (item.kind == "step") FontFamily.Monospace else FontFamily.Default)
    }
}
