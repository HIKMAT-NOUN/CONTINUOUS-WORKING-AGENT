package com.myagent.app

import android.app.Activity
import android.os.Bundle
import android.os.SystemClock
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// ---------- colors (same palette as the web dashboard) ----------
val Bg = Color(0xFF0E1015); val Panel = Color(0xFF161920); val Panel2 = Color(0xFF1C2029)
val Border = Color(0xFF2A2F3B); val TextMain = Color(0xFFE6E8EE); val Muted = Color(0xFF8B93A5)
val Accent = Color(0xFF3B82F6); val OkC = Color(0xFF4ADE80); val ErrC = Color(0xFFF87171)
val Warn = Color(0xFFFBBF24); val CodeBg = Color(0xFF232836)
val Palette = listOf(Color(0xFF22C55E), Color(0xFFA855F7), Color(0xFF14B8A6), Color(0xFFEC4899),
    Color(0xFFF59E0B), Color(0xFF06B6D4), Color(0xFFEF4444), Color(0xFF84CC16))

/** A stable colour per agent, assigned in the order agents first appear (main thread only). */
object AgentColors {
    private val map = HashMap<String, Color>()
    private var cursor = 0
    fun register(id: String) { if (id != "main") map.getOrPut(id) { Palette[cursor++ % Palette.size] } }
    fun of(id: String): Color = if (id == "main") Accent else { register(id); map.getValue(id) }
    fun reset() { map.clear(); cursor = 0 }
}

fun roleIcon(role: String) = when (role) { "main" -> "🧠"; "reviewer" -> "🔎"; else -> "🤖" }

// ---------- activity ----------
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                primary = Accent, onPrimary = Color.White, background = Bg, surface = Panel,
                surfaceVariant = Panel2, onBackground = TextMain, onSurface = TextMain,
                onSurfaceVariant = Muted, outline = Border, error = ErrC,
                surfaceContainer = Panel2, surfaceContainerHigh = Panel2)) {
                val vm: AgentViewModel = viewModel()
                val configured by vm.configured.collectAsState()
                val editing by vm.editing.collectAsState()
                Surface(Modifier.fillMaxSize(), color = Bg) {
                    if (!configured || editing) SetupScreen(vm, canCancel = configured) else AgentScreen(vm)
                }
            }
        }
    }
}

// ---------- setup ----------
@Composable
fun SetupScreen(vm: AgentViewModel, canCancel: Boolean) {
    // Keep the token out of screenshots and the recent-apps thumbnail while this screen is up.
    val activity = LocalContext.current as? Activity
    DisposableEffect(activity) {
        activity?.window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        onDispose { activity?.window?.clearFlags(WindowManager.LayoutParams.FLAG_SECURE) }
    }
    if (canCancel) BackHandler { vm.editing.value = false }

    var u by rememberSaveable { mutableStateOf(vm.url.value) }
    var t by remember { mutableStateOf("") }       // never saved into instance state
    var reveal by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    val submit = { error = vm.saveAndConnect(u, t); if (error == null) t = "" }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).imePadding().padding(24.dp),
        verticalArrangement = Arrangement.Center) {
        Text("🤖", fontSize = 40.sp)
        Spacer(Modifier.height(8.dp))
        Text(if (canCancel) "Connection settings" else "Connect to your agent",
            color = TextMain, fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(6.dp))
        Text("Enter your relay address and the RELAY_USER_TOKEN. Your agent must be running with the same relay in its .env.",
            color = Muted, fontSize = 13.sp)
        Spacer(Modifier.height(20.dp))
        OutlinedTextField(u, { u = it; error = null }, label = { Text("Relay URL") },
            placeholder = { Text("wss://your-relay.example/ws/app", color = Muted) },
            singleLine = true, modifier = Modifier.fillMaxWidth(), colors = fieldColors(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, autoCorrectEnabled = false,
                imeAction = ImeAction.Next))
        Spacer(Modifier.height(12.dp))
        OutlinedTextField(t, { t = it; error = null },
            label = { Text("User token") },
            placeholder = { if (canCancel) Text("Leave empty to keep the saved token", color = Muted) },
            singleLine = true, modifier = Modifier.fillMaxWidth(), colors = fieldColors(),
            visualTransformation = if (reveal) VisualTransformation.None else PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, autoCorrectEnabled = false,
                imeAction = ImeAction.Done),
            keyboardActions = KeyboardActions(onDone = { submit() }),
            trailingIcon = {
                TextButton(onClick = { reveal = !reveal }) { Text(if (reveal) "Hide" else "Show", fontSize = 12.sp) }
            })
        error?.let {
            Spacer(Modifier.height(8.dp))
            Text(it, color = ErrC, fontSize = 13.sp)
        }
        Spacer(Modifier.height(20.dp))
        Button(onClick = submit, enabled = u.isNotBlank() && (t.isNotBlank() || canCancel),
            modifier = Modifier.fillMaxWidth().height(48.dp)) { Text("Connect") }
        if (canCancel) {
            Spacer(Modifier.height(8.dp))
            OutlinedButton(onClick = { vm.editing.value = false }, modifier = Modifier.fillMaxWidth()) { Text("Cancel") }
        }
        Spacer(Modifier.height(16.dp))
        Text("🔒 Only secure wss:// relays are accepted. The token is encrypted with a key kept in this phone's " +
            "hardware keystore and is never included in backups. Anyone with the token can command your agent.",
            color = Muted, fontSize = 12.sp)
    }
}

@Composable
fun fieldColors() = OutlinedTextFieldDefaults.colors(
    focusedTextColor = TextMain, unfocusedTextColor = TextMain,
    focusedBorderColor = Accent, unfocusedBorderColor = Border,
    focusedLabelColor = Accent, unfocusedLabelColor = Muted, cursorColor = Accent)

// ---------- main screen ----------
@Composable
fun AgentScreen(vm: AgentViewModel) {
    val feed by vm.feed.collectAsState()
    val status by vm.status.collectAsState()
    val conn by vm.conn.collectAsState()
    val agentOnline by vm.agentOnline.collectAsState()
    val approvals by vm.approvals.collectAsState()
    val filter by vm.agentFilter.collectAsState()
    val showOutput by vm.showOutput.collectAsState()
    val summaryOnly by vm.summaryOnly.collectAsState()
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    var confirmStop by remember { mutableStateOf(false) }
    var confirmForget by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        vm.notices.collect { msg ->
            snackbar.currentSnackbarData?.dismiss()
            scope.launch { snackbar.showSnackbar(msg, duration = SnackbarDuration.Short) }
        }
    }
    BackHandler(enabled = filter != null) { vm.clearAgentFilter() }

    val live = conn.phase == Phase.LIVE
    Scaffold(containerColor = Bg, snackbarHost = { SnackbarHost(snackbar) }) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            Header(vm, status, conn, agentOnline, showOutput, summaryOnly,
                onStop = { confirmStop = true }, onForget = { confirmForget = true })
            ConnBanner(vm, conn, agentOnline)
            status?.current?.let { cur ->
                Text("⚙️ #${cur.id} · ${cur.description}", color = Muted, fontSize = 12.sp, maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.fillMaxWidth().background(Panel).padding(horizontal = 14.dp, vertical = 4.dp))
            }
            status?.fleet?.takeIf { it.isNotEmpty() }?.let { AgentsPanel(it, filter, vm::toggleAgentFilter) }
            filter?.let { id ->
                val role = status?.fleet?.firstOrNull { it.id == id }?.role ?: if (id == "main") "main" else ""
                FilterBar(id, role, vm::clearAgentFilter)
            }
            if (approvals.isNotEmpty()) ApprovalsPanel(approvals, live, vm::decide)
            Feed(feed, filter, showOutput, summaryOnly, modifier = Modifier.weight(1f),
                onCopied = { scope.launch { snackbar.currentSnackbarData?.dismiss(); snackbar.showSnackbar("Copied") } })
            InputBar(vm, live)
        }
    }

    if (confirmStop) AlertDialog(
        onDismissRequest = { confirmStop = false },
        title = { Text("Stop the current task?") },
        text = { Text(status?.current?.let { "Aborts task #${it.id} and any helpers it started." }
            ?: "Sends /stop to the agent.") },
        confirmButton = { TextButton(onClick = { confirmStop = false; vm.stop() }) { Text("Stop", color = ErrC) } },
        dismissButton = { TextButton(onClick = { confirmStop = false }) { Text("Cancel") } })
    if (confirmForget) AlertDialog(
        onDismissRequest = { confirmForget = false },
        title = { Text("Forget this relay?") },
        text = { Text("Disconnects and erases the saved address and token from this phone.") },
        confirmButton = { TextButton(onClick = { confirmForget = false; vm.forget() }) { Text("Forget", color = ErrC) } },
        dismissButton = { TextButton(onClick = { confirmForget = false }) { Text("Cancel") } })
}

@Composable
fun Header(vm: AgentViewModel, status: Status?, conn: Conn, agentOnline: Boolean, showOutput: Boolean,
           summaryOnly: Boolean, onStop: () -> Unit, onForget: () -> Unit) {
    var menu by remember { mutableStateOf(false) }
    val live = conn.phase == Phase.LIVE
    Row(Modifier.fillMaxWidth().background(Panel).padding(start = 14.dp, end = 4.dp, top = 6.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Text("My Agent", color = TextMain, fontWeight = FontWeight.Bold, fontSize = 17.sp)
        Spacer(Modifier.width(10.dp))
        val (dot, label) = when (conn.phase) {
            Phase.LIVE -> if (agentOnline) (if (status?.state == "working") OkC else OkC.copy(alpha = .7f)) to (status?.state ?: "live")
                          else Warn to "agent offline"
            Phase.CONNECTING -> Warn to "connecting…"
            Phase.RETRYING -> ErrC to "offline"
            Phase.AUTH_FAILED -> ErrC to "token rejected"
            Phase.IDLE -> Muted to "paused"
        }
        StatusDot(dot, pulse = live && agentOnline && status?.state == "working")
        Spacer(Modifier.width(6.dp))
        Text(label, color = Muted, fontSize = 12.sp)
        Spacer(Modifier.weight(1f))
        status?.takeIf { it.budget > 0 }?.let {
            Text("${fmtTok(it.tokens)}/${fmtTok(it.budget)}", color = Muted, fontSize = 11.sp)
            Spacer(Modifier.width(6.dp))
        }
        if (live && agentOnline && status?.state == "working") {
            OutlinedButton(onClick = onStop, contentPadding = PaddingValues(horizontal = 10.dp, vertical = 0.dp),
                modifier = Modifier.height(30.dp),
                border = androidx.compose.foundation.BorderStroke(1.dp, ErrC.copy(alpha = .7f))) {
                Text("■ Stop", color = ErrC, fontSize = 12.sp)
            }
        }
        Box {
            IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, "Menu", tint = Muted) }
            DropdownMenu(menu, onDismissRequest = { menu = false }) {
                CheckItem("Show tool output", showOutput) { vm.setShowOutput(!showOutput) }
                CheckItem("Summary only", summaryOnly) { vm.setSummaryOnly(!summaryOnly) }
                HorizontalDivider(color = Border)
                DropdownMenuItem(text = { Text("Reconnect now") }, onClick = { menu = false; vm.reconnect() })
                DropdownMenuItem(text = { Text("Connection settings") }, onClick = { menu = false; vm.editing.value = true })
                DropdownMenuItem(text = { Text("Forget token & disconnect", color = ErrC) },
                    onClick = { menu = false; onForget() })
                status?.model?.takeIf { it.isNotEmpty() }?.let {
                    HorizontalDivider(color = Border)
                    Text("model: $it", color = Muted, fontSize = 11.sp, modifier = Modifier.padding(16.dp, 8.dp))
                }
            }
        }
    }
}

@Composable
fun CheckItem(label: String, checked: Boolean, onClick: () -> Unit) = DropdownMenuItem(
    text = { Text(label) }, onClick = onClick,
    trailingIcon = { if (checked) Icon(Icons.Filled.Check, null, tint = Accent) else Spacer(Modifier.size(24.dp)) })

@Composable
fun StatusDot(color: Color, pulse: Boolean = false, size: Int = 9) {
    val a = if (pulse) {
        val tr = rememberInfiniteTransition(label = "pulse")
        tr.animateFloat(0.35f, 1f, infiniteRepeatable(tween(800), RepeatMode.Reverse), label = "a").value
    } else 1f
    Box(Modifier.size(size.dp).graphicsLayer { alpha = a }.clip(RoundedCornerShape(50)).background(color))
}

@Composable
fun ConnBanner(vm: AgentViewModel, conn: Conn, agentOnline: Boolean) {
    var now by remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }
    LaunchedEffect(conn) {
        while (conn.phase == Phase.RETRYING) { now = SystemClock.elapsedRealtime(); delay(500) }
    }
    val (text, color, action) = when (conn.phase) {
        Phase.RETRYING -> {
            val secs = ((conn.retryAt - now + 999) / 1000).coerceAtLeast(0)
            Triple("Connection lost — ${conn.detail}. " + (if (secs > 0) "Retrying in ${secs}s…" else "Retrying…"),
                Warn, "Retry now" to vm::reconnect)
        }
        Phase.CONNECTING -> if (conn.detail.isNotEmpty() || conn.attempt > 1)
            Triple("Reconnecting…", Warn, null) else return
        Phase.AUTH_FAILED -> Triple(conn.detail, ErrC, "Settings" to { vm.editing.value = true })
        Phase.LIVE -> if (!agentOnline) Triple("Connected to the relay, but your agent isn't. Is it running with RELAY_URL set?",
            Warn, null) else return
        Phase.IDLE -> return
    }
    Row(Modifier.fillMaxWidth().background(Panel2).padding(start = 14.dp, end = 4.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Text(text, color = color, fontSize = 12.sp, modifier = Modifier.weight(1f).padding(vertical = 6.dp))
        action?.let { (label, fn) -> TextButton(onClick = fn) { Text(label, fontSize = 12.sp) } }
    }
}

// ---------- agents ----------
@Composable
fun AgentsPanel(fleet: List<AgentNode>, selected: String?, onTap: (String) -> Unit) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    val active = fleet.count { it.status == "working" && it.role != "main" }
    Column(Modifier.fillMaxWidth().background(Panel)) {
        Row(Modifier.fillMaxWidth().clickable { expanded = !expanded }.padding(horizontal = 14.dp, vertical = 2.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Text("AGENTS", color = Muted, fontSize = 10.sp, fontWeight = FontWeight.Bold, letterSpacing = 1.sp)
            Text("  ${fleet.size}" + (if (active > 0) " · $active helping" else ""), color = Muted, fontSize = 10.sp)
            Spacer(Modifier.weight(1f))
            Text(if (expanded) "tree" else "row", color = Muted, fontSize = 10.sp)
            Icon(Icons.Filled.KeyboardArrowDown, if (expanded) "Collapse" else "Expand", tint = Muted,
                modifier = Modifier.size(18.dp).graphicsLayer { rotationZ = if (expanded) 180f else 0f })
        }
        if (expanded) {
            Column(Modifier.fillMaxWidth().heightIn(max = 300.dp).verticalScroll(rememberScrollState())
                .padding(start = 12.dp, end = 12.dp, bottom = 8.dp)) {
                for ((a, depth) in treeOrder(fleet)) {
                    AgentCard(a, a.id == selected, Modifier.padding(start = (depth * 16).dp, top = 6.dp).fillMaxWidth(),
                        lines = 2) { onTap(a.id) }
                }
                Text("Tap an agent to show only its activity.", color = Muted, fontSize = 11.sp,
                    modifier = Modifier.padding(top = 6.dp))
            }
        } else {
            LazyRow(Modifier.fillMaxWidth().padding(bottom = 8.dp), contentPadding = PaddingValues(horizontal = 12.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                items(treeOrder(fleet), key = { it.first.id }) { (a, _) ->
                    AgentCard(a, a.id == selected, Modifier.width(176.dp), lines = 1) { onTap(a.id) }
                }
            }
        }
    }
}

@Composable
fun AgentCard(a: AgentNode, selected: Boolean, modifier: Modifier, lines: Int, onTap: () -> Unit) {
    val c = AgentColors.of(a.id)
    val working = a.status == "working"
    val shape = RoundedCornerShape(10.dp)
    Column(modifier.clip(shape).background(if (selected) Panel else Panel2)
        .border(if (selected) 2.dp else 1.dp, if (selected) c else Border, shape)
        .clickable(onClick = onTap).padding(9.dp, 7.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            val dot = when (a.status) { "working" -> c; "done" -> OkC; "idle" -> Muted; else -> ErrC }
            StatusDot(dot, pulse = working, size = 8)
            Spacer(Modifier.width(6.dp))
            Text("${roleIcon(a.role)} ${a.id}", color = TextMain, fontSize = 12.sp, fontWeight = FontWeight.SemiBold,
                maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f, fill = false))
            Spacer(Modifier.width(6.dp))
            Text(a.role.uppercase(), color = c, fontSize = 9.sp, letterSpacing = .5.sp, maxLines = 1)
            Spacer(Modifier.weight(1f))
            Text(if (working) "${a.step}/${a.maxSteps}" else a.status, color = Muted, fontSize = 10.sp, maxLines = 1)
        }
        if (a.title.isNotEmpty())
            Text(a.title, color = Muted, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
        val act = when {
            working -> a.action.ifEmpty { "working…" }
            a.role == "main" -> "idle"
            else -> a.result.lineSequence().firstOrNull { it.isNotBlank() }?.take(140) ?: a.status
        }
        Text(act, color = TextMain, fontSize = 11.sp, maxLines = lines, overflow = TextOverflow.Ellipsis)
        if (working && a.thinking.isNotBlank())
            Text("🧠 ${a.thinking}", color = Muted, fontSize = 11.sp, fontStyle = FontStyle.Italic,
                maxLines = lines, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun FilterBar(id: String, role: String, onClear: () -> Unit) {
    val c = AgentColors.of(id)
    Row(Modifier.fillMaxWidth().background(Panel2).padding(start = 14.dp, end = 4.dp),
        verticalAlignment = Alignment.CenterVertically) {
        StatusDot(c, size = 8)
        Spacer(Modifier.width(8.dp))
        Text(buildString { append("Showing only "); append(id); if (role.isNotEmpty()) append(" ($role)") },
            color = TextMain, fontSize = 12.sp, modifier = Modifier.weight(1f))
        TextButton(onClick = onClear) { Text("Show all", fontSize = 12.sp) }
    }
}

// ---------- approvals ----------
@Composable
fun ApprovalsPanel(list: List<Approval>, live: Boolean, onDecide: (String, Boolean) -> Unit) {
    Column(Modifier.fillMaxWidth().heightIn(max = 340.dp).verticalScroll(rememberScrollState())
        .padding(horizontal = 10.dp, vertical = 4.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        list.forEach { ApprovalCard(it, live, onDecide) }
    }
}

@Composable
fun ApprovalCard(a: Approval, live: Boolean, onDecide: (String, Boolean) -> Unit) {
    val c = AgentColors.of(a.agent)
    var full by remember(a.aid) { mutableStateOf(false) }
    val shape = RoundedCornerShape(10.dp)
    Row(Modifier.fillMaxWidth().clip(shape).border(1.dp, Warn.copy(alpha = .8f), shape).background(Panel)
        .height(IntrinsicSize.Min)) {
        Box(Modifier.width(4.dp).fillMaxHeight().background(c))
        Column(Modifier.weight(1f).padding(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("⚠️ Approval needed", color = TextMain, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                Spacer(Modifier.width(6.dp))
                Text("[${a.aid}]", color = Muted, fontSize = 11.sp)
                if (a.agent != "main") { Spacer(Modifier.width(6.dp)); AgentTag(a.agent) }
            }
            Text(a.command, color = TextMain, fontFamily = FontFamily.Monospace, fontSize = 12.sp,
                maxLines = if (full) Int.MAX_VALUE else 6, overflow = TextOverflow.Ellipsis,
                modifier = Modifier.padding(top = 6.dp).fillMaxWidth().clip(RoundedCornerShape(6.dp))
                    .background(CodeBg).clickable { full = !full }.padding(8.dp))
            if (a.reason.isNotEmpty())
                Text(a.reason, color = Muted, fontSize = 12.sp, modifier = Modifier.padding(top = 6.dp))
            Row(Modifier.padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                Button(onClick = { onDecide(a.aid, true) }, enabled = live && !a.busy,
                    colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF16A34A), contentColor = Color.White)) {
                    Text("✅ Approve")
                }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(onClick = { onDecide(a.aid, false) }, enabled = live && !a.busy,
                    border = androidx.compose.foundation.BorderStroke(1.dp, ErrC.copy(alpha = if (live && !a.busy) 1f else .4f))) {
                    Text("❌ Deny", color = ErrC)
                }
                if (a.busy) {
                    Spacer(Modifier.width(10.dp))
                    CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp, color = Muted)
                }
            }
        }
    }
}

@Composable
fun AgentTag(agent: String) {
    Text(agent, color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Bold,
        modifier = Modifier.clip(RoundedCornerShape(4.dp)).background(AgentColors.of(agent))
            .padding(horizontal = 5.dp, vertical = 1.dp))
}

// ---------- feed ----------
@Composable
fun Feed(all: List<FeedItem>, filter: String?, showOutput: Boolean, summaryOnly: Boolean,
         modifier: Modifier, onCopied: () -> Unit) {
    val items = remember(all, filter, showOutput, summaryOnly) {
        all.filter { (filter == null || it.agent == filter) &&
            (showOutput || it.kind != "output") && (!summaryOnly || it.kind !in DETAIL_KINDS) }
    }
    val listState = rememberLazyListState()
    val expanded = remember { mutableStateMapOf<String, Boolean>() }
    var follow by remember { mutableStateOf(true) }
    var unread by remember { mutableIntStateOf(0) }
    var seen by remember { mutableIntStateOf(0) }
    val scope = rememberCoroutineScope()

    // Follow the newest row unless the user has scrolled up to read.
    LaunchedEffect(listState) {
        snapshotFlow { listState.canScrollForward }.collect { more ->
            if (listState.isScrollInProgress) { follow = !more; if (!more) unread = 0 }
        }
    }
    LaunchedEffect(filter, showOutput, summaryOnly) { follow = true }
    val lastKey = items.lastOrNull()?.key
    LaunchedEffect(lastKey, items.size) {
        if (items.isEmpty()) { seen = 0; return@LaunchedEffect }
        if (follow) {
            listState.scrollToItem(items.size - 1)
            listState.scrollBy(100_000f)                // to the very end of a tall last row
            unread = 0
        } else if (items.size > seen) unread += items.size - seen
        seen = items.size
    }

    val clipboard = LocalClipboardManager.current
    Box(modifier.fillMaxWidth()) {
        if (items.isEmpty()) {
            Text(if (all.isEmpty()) "No activity yet. Send your agent a task below." else "Nothing to show with the current filter.",
                color = Muted, fontSize = 13.sp, modifier = Modifier.align(Alignment.Center).padding(24.dp))
        }
        LazyColumn(state = listState, modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp)) {
            items(items, key = { it.key }, contentType = { it.kind }) { item ->
                FeedRow(item, expanded[item.key] == true,
                    onToggle = { expanded[item.key] = expanded[item.key] != true },
                    onLongPress = { clipboard.setText(AnnotatedString(item.output ?: item.text.text)); onCopied() })
            }
        }
        if (!follow && items.isNotEmpty()) {
            SmallFloatingActionButton(
                onClick = { follow = true; unread = 0; scope.launch { listState.scrollToItem(items.size - 1); listState.scrollBy(100_000f) } },
                containerColor = Panel2, contentColor = TextMain,
                modifier = Modifier.align(Alignment.BottomEnd).padding(12.dp)) {
                Row(Modifier.padding(horizontal = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Filled.KeyboardArrowDown, null, modifier = Modifier.size(18.dp))
                    Text(if (unread > 0) " ${if (unread > 99) "99+" else unread} new" else " Latest", fontSize = 12.sp)
                }
            }
        }
    }
}

private val timeFmt = object : ThreadLocal<SimpleDateFormat>() {
    override fun initialValue() = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
fun FeedRow(item: FeedItem, expanded: Boolean, onToggle: () -> Unit, onLongPress: () -> Unit) {
    val c = AgentColors.of(item.agent)
    val tagged = item.agent != "main"
    val time = remember(item.ts) { timeFmt.get()!!.format(Date((item.ts * 1000).toLong())) }
    val isOutput = item.kind == "output"
    Column(Modifier.fillMaxWidth().padding(top = if (item.kind == "task") 12.dp else 3.dp, bottom = 3.dp)) {
        if (item.kind == "task") HorizontalDivider(color = Border, modifier = Modifier.padding(bottom = 10.dp))
        Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min)
            .combinedClickable(onClick = { if (isOutput) onToggle() }, onLongClick = onLongPress)) {
            Box(Modifier.width(3.dp).fillMaxHeight().clip(RoundedCornerShape(2.dp))
                .background(if (tagged) c else c.copy(alpha = .35f)))
            Spacer(Modifier.width(8.dp))
            Column(Modifier.weight(1f)) {
                if (tagged) { AgentTag(item.agent); Spacer(Modifier.height(2.dp)) }
                when {
                    isOutput -> {
                        Text((if (expanded) "▾ " else "▸ ") + item.text.text, color = Muted, fontSize = 12.sp)
                        if (expanded) SelectionContainer {
                            Text(item.output.orEmpty(), color = TextMain, fontFamily = FontFamily.Monospace, fontSize = 11.sp,
                                modifier = Modifier.padding(top = 4.dp).fillMaxWidth().clip(RoundedCornerShape(6.dp))
                                    .background(CodeBg).padding(8.dp))
                        }
                    }
                    item.kind == "final" -> Text(item.text, color = TextMain, fontSize = 14.sp,
                        modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp)).background(Panel)
                            .border(1.dp, Border, RoundedCornerShape(10.dp)).padding(10.dp))
                    item.kind == "reply" -> Text(item.text, color = TextMain, fontSize = 14.sp,
                        modifier = Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(Panel2).padding(8.dp))
                    else -> Text(item.text,
                        color = when (item.kind) {
                            "mine" -> Accent; "thinking" -> Muted; "alert", "approval" -> Warn
                            "step", "result" -> TextMain.copy(alpha = .85f); else -> TextMain
                        },
                        fontSize = if (item.kind == "step" || item.kind == "result") 13.sp else 14.sp,
                        fontStyle = if (item.kind == "thinking") FontStyle.Italic else FontStyle.Normal,
                        fontWeight = if (item.kind == "task" || item.kind == "mine") FontWeight.Medium else FontWeight.Normal)
                }
            }
            Spacer(Modifier.width(6.dp))
            Text(time, color = Muted.copy(alpha = .7f), fontSize = 9.sp, modifier = Modifier.padding(top = 2.dp))
        }
    }
}

// ---------- input ----------
@Composable
fun InputBar(vm: AgentViewModel, live: Boolean) {
    var input by rememberSaveable { mutableStateOf("") }
    val send = { if (vm.send(input)) input = "" }
    Row(Modifier.fillMaxWidth().background(Panel).padding(10.dp), verticalAlignment = Alignment.Bottom) {
        OutlinedTextField(input, { input = it },
            placeholder = { Text(if (live) "Describe a task… or /command" else "Waiting for connection…", color = Muted) },
            modifier = Modifier.weight(1f), maxLines = 5, colors = fieldColors(),
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
            keyboardActions = KeyboardActions(onSend = { send() }))
        Spacer(Modifier.width(8.dp))
        FilledIconButton(onClick = send, enabled = live && input.isNotBlank(), modifier = Modifier.size(52.dp)
            .alpha(if (live) 1f else .6f)) {
            Icon(Icons.AutoMirrored.Filled.Send, "Send")
        }
    }
}
