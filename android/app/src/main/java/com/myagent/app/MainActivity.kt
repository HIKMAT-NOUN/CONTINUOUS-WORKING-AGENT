package com.myagent.app

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.compose.viewModel

// ---------- colours (match the web console's dark theme) ----------
val Bg = Color(0xFF171716); val Panel = Color(0xFF1F1F1D); val Panel2 = Color(0xFF262624)
val Panel3 = Color(0xFF30302D); val Border = Color(0xFF3B3B37); val TextMain = Color(0xFFECEBE8)
val Text2 = Color(0xFFC9C7C1); val Muted = Color(0xFF96938B); val Accent = Color(0xFFEF8A54)
val AccentInk = Color(0xFF1A1009); val AccentSoft = Color(0x22EF8A54)
val OkC = Color(0xFF5CC98A); val ErrC = Color(0xFFF07167); val WarnC = Color(0xFFF2B84B); val Violet = Color(0xFFB49CF0)
val Palette = listOf(Color(0xFF22A06B), Color(0xFF8B5CF6), Color(0xFF0EA5A4), Color(0xFFE0508C),
    Color(0xFFD99A0B), Color(0xFF2A9FD6), Color(0xFFE5534B), Color(0xFF7AA31C))
private val agentColors = HashMap<String, Color>()
private var colorCursor = 0
fun colorFor(agent: String): Color {
    if (agent == "main" || agent == "you") return Accent
    return agentColors.getOrPut(agent) { Palette[colorCursor++ % Palette.size] }
}
fun roleIcon(role: String) = when (role) { "main" -> "🧠"; "reviewer" -> "🔎"; else -> "🤖" }

// ---------- activity ----------
class MainActivity : ComponentActivity() {
    private val vm by lazy { ViewModelProvider(this)[AgentViewModel::class.java] }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (savedInstanceState == null) handleShare(intent)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme(
                primary = Accent, onPrimary = AccentInk, background = Bg, surface = Panel,
                surfaceVariant = Panel2, onBackground = TextMain, onSurface = TextMain,
                onSurfaceVariant = Muted, outline = Border, error = ErrC)) {
                val model: AgentViewModel = viewModel()
                val configured by model.configured.collectAsState()
                LaunchedEffect(configured) { if (configured) model.connect() }
                Surface(Modifier.fillMaxSize(), color = Bg) {
                    if (configured) AgentScreen(model) else SetupScreen(model)
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    /** "Share to My Agent" from the gallery / files / other apps. */
    @Suppress("DEPRECATION")
    private fun handleShare(i: Intent?) {
        i ?: return
        when (i.action) {
            Intent.ACTION_SEND -> {
                val uri: Uri? = if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
                                else i.getParcelableExtra(Intent.EXTRA_STREAM)
                if (uri != null) vm.attach(listOf(uri))
                i.getStringExtra(Intent.EXTRA_TEXT)?.takeIf { it.isNotBlank() }?.let { vm.sharedText.value = it }
            }
            Intent.ACTION_SEND_MULTIPLE -> {
                val uris: List<Uri>? = if (Build.VERSION.SDK_INT >= 33) i.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                                       else i.getParcelableArrayListExtra(Intent.EXTRA_STREAM)
                if (!uris.isNullOrEmpty()) vm.attach(uris)
            }
        }
    }
}

// ---------- setup ----------
@Composable
fun SetupScreen(vm: AgentViewModel) {
    var u by remember { mutableStateOf(vm.url.value) }
    var t by remember { mutableStateOf(vm.token.value) }
    val secure = RelayClient.isSecureUrl(u)
    Column(Modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.Center) {
        Text("Connect to your agent", color = TextMain, fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(6.dp))
        Text("Enter your relay address and token. The agent must be running with the same relay configured.",
            color = Muted, fontSize = 13.sp)
        Spacer(Modifier.height(20.dp))
        OutlinedTextField(u, { u = it }, label = { Text("Relay URL (wss://…/ws/app)") },
            singleLine = true, modifier = Modifier.fillMaxWidth(), colors = fieldColors(),
            isError = u.isNotBlank() && !secure,
            supportingText = { if (u.isNotBlank() && !secure) Text("Must start with wss:// (TLS)", color = ErrC) },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri))
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(t, { t = it }, label = { Text("User token") },
            singleLine = true, modifier = Modifier.fillMaxWidth(), colors = fieldColors(),
            visualTransformation = PasswordVisualTransformation(),
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password))
        Spacer(Modifier.height(20.dp))
        Button(onClick = { vm.saveAndConnect(u, t) }, enabled = secure && t.isNotBlank(),
            modifier = Modifier.fillMaxWidth()) { Text("Connect") }
    }
}

@Composable
fun fieldColors() = OutlinedTextFieldDefaults.colors(
    focusedTextColor = TextMain, unfocusedTextColor = TextMain,
    focusedBorderColor = Accent, unfocusedBorderColor = Border,
    focusedLabelColor = Accent, unfocusedLabelColor = Muted, cursorColor = Accent,
    focusedContainerColor = Panel, unfocusedContainerColor = Panel)

// ---------- main screen ----------
@Composable
fun AgentScreen(vm: AgentViewModel) {
    val feed by vm.feed.collectAsState()
    val status by vm.status.collectAsState()
    val connected by vm.connected.collectAsState()
    val agentOnline by vm.agentOnline.collectAsState()
    val banner by vm.banner.collectAsState()
    val approvals by vm.approvals.collectAsState()
    val busyAids by vm.busyAids.collectAsState()
    val quick by vm.quick.collectAsState()
    val showOutput by vm.showOutput.collectAsState()
    val tint by vm.tintAgents.collectAsState()
    var tab by rememberSaveable { mutableStateOf(0) }            // 0 = Activity, 1 = Settings
    var editQuick by remember { mutableStateOf(false) }
    var confirm by remember { mutableStateOf<String?>(null) }   // "new" | "stopall" | "clear"
    val listState = rememberLazyListState()
    val shown = if (showOutput) feed else feed.filter { it.kind != "output" }
    LaunchedEffect(shown.size, tab) { if (tab == 0 && shown.isNotEmpty()) listState.animateScrollToItem(shown.size - 1) }
    if (tab == 1) BackHandler { tab = 0 }

    Column(Modifier.fillMaxSize().imePadding()) {
        TopBar(connected, agentOnline, status, tab) { tab = it }
        if (banner.isNotEmpty())
            Text(banner, color = WarnC, fontSize = 12.sp,
                modifier = Modifier.fillMaxWidth().background(Panel2).padding(14.dp, 5.dp))

        if (tab == 1) {
            val url by vm.url.collectAsState()
            val focus = LocalFocusManager.current
            SettingsContent(status, url, showOutput, tint, SettingsActions(
                onMaxSteps = vm::setMaxSteps, onInterval = vm::setInterval, onWorkers = vm::setWorkers,
                onReasoning = { focus.clearFocus(); vm.setReasoning(it) },
                onReview = { focus.clearFocus(); vm.setReview(it) },
                onVerbose = { vm.setVerbose(it) },
                onLog = { vm.postLog(); tab = 0 },
                onShowOutput = vm::setShowOutput, onTint = vm::setTintAgents,
                onConfirm = { confirm = it }, onLogout = vm::logout),
                modifier = Modifier.weight(1f))
        } else {
        NowCard(status, onNew = { confirm = "new" }, onStopAll = { confirm = "stopall" })
        QuickBar(quick, onTap = { vm.send(it.command) }, onEdit = { editQuick = true })

        status?.fleet?.takeIf { it.size > 1 || it.any { a -> a.status == "working" } }?.let { fleet ->
            LazyRow(Modifier.fillMaxWidth().padding(vertical = 6.dp), contentPadding = PaddingValues(horizontal = 12.dp)) {
                items(fleet, key = { it.id }) { a -> AgentChip(a) }
            }
        }

        // feed
        LazyColumn(state = listState, modifier = Modifier.weight(1f).fillMaxWidth(),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp)) {
            if (shown.isEmpty()) item {
                Text("Nothing here yet. Send a task below and watch every step live.",
                    color = Muted, fontSize = 13.sp, modifier = Modifier.padding(top = 40.dp).fillMaxWidth())
            }
            items(shown, key = { it.id }) { item -> FeedRow(item, tint) }
        }

        // approvals (pinned so they can't be missed)
        if (approvals.isNotEmpty())
            Column(Modifier.fillMaxWidth().heightIn(max = 300.dp).verticalScroll(rememberScrollState())
                .padding(horizontal = 10.dp)) {
                approvals.forEach { a -> ApprovalCard(a, busy = a.aid in busyAids, onAnswer = { yes -> vm.approve(a, yes) }) }
            }

        Composer(vm)
        }
    }

    if (editQuick) QuickEditor(quick, onDismiss = { editQuick = false }, onSave = { vm.saveQuick(it); editQuick = false })

    confirm?.let { what ->
        val (title, body, action) = when (what) {
            "new" -> Triple("Start a new session?", "Stops all tasks, clears the queue, the standing goal and the history.", "New session")
            "stopall" -> Triple("Stop everything?", "Aborts the current task and clears the whole queue.", "Stop all")
            else -> Triple("Clear the activity feed?", "Removes the activity history for every device. Tasks keep running.", "Clear")
        }
        AlertDialog(onDismissRequest = { confirm = null }, containerColor = Panel,
            title = { Text(title, color = TextMain) }, text = { Text(body, color = Text2) },
            confirmButton = { TextButton(onClick = {
                when (what) { "new" -> vm.newSession(); "stopall" -> vm.stopAll(); else -> vm.clearFeed() }
                confirm = null; tab = 0
            }) { Text(action, color = ErrC) } },
            dismissButton = { TextButton(onClick = { confirm = null }) { Text("Cancel", color = Text2) } })
    }
}

/** Header (status) + the Activity | Settings tab bar. */
@Composable
fun TopBar(connected: Boolean, agentOnline: Boolean, status: Status?, tab: Int, onTab: (Int) -> Unit) {
    Column {
        // header
        Row(Modifier.fillMaxWidth().background(Panel).statusBarsPadding().padding(14.dp, 10.dp, 6.dp, 6.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(28.dp).clip(RoundedCornerShape(9.dp)).background(Accent), contentAlignment = Alignment.Center) {
                Text("✳", color = AccentInk, fontSize = 15.sp, fontWeight = FontWeight.Bold)
            }
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text("My Agent", color = TextMain, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                Row(verticalAlignment = Alignment.CenterVertically) {
                    val dot = when { !connected -> ErrC; !agentOnline -> Muted; status?.state == "paused" -> WarnC; else -> OkC }
                    Box(Modifier.size(7.dp).clip(RoundedCornerShape(50)).background(dot))
                    Spacer(Modifier.width(5.dp))
                    val st = status
                    val label = when {
                        !connected -> "offline"; !agentOnline -> "agent offline"
                        st == null -> "live"
                        st.state == "paused" && st.pauseReason == "budget" -> "paused · budget"
                        else -> st.state
                    }
                    Text(label, color = Muted, fontSize = 12.sp)
                    status?.let { Text("  ·  ${fmtTok(it.tokens)}/${fmtTok(it.budget)}", color = Muted, fontSize = 12.sp) }
                }
            }
            Box(Modifier.size(40.dp).clip(RoundedCornerShape(10.dp)).background(if (tab == 1) AccentSoft else Color.Transparent)
                .clickable { onTab(if (tab == 1) 0 else 1) }, contentAlignment = Alignment.Center) {
                Text("⚙", color = if (tab == 1) Accent else Text2, fontSize = 20.sp)
            }
        }
        // Activity | Settings tabs
        Row(Modifier.fillMaxWidth().background(Panel)) {
            listOf("💬  Activity", "⚙  Settings").forEachIndexed { i, label ->
                Column(Modifier.weight(1f).clickable { onTab(i) }, horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(label, color = if (tab == i) TextMain else Muted, fontSize = 14.sp,
                        fontWeight = if (tab == i) FontWeight.SemiBold else FontWeight.Normal,
                        modifier = Modifier.padding(vertical = 10.dp))
                    Box(Modifier.fillMaxWidth(.5f).height(2.dp).background(if (tab == i) Accent else Color.Transparent))
                }
            }
        }
        Box(Modifier.fillMaxWidth().height(1.dp).background(Border))
    }
}

@Composable
fun NowCard(s: Status?, onNew: () -> Unit, onStopAll: () -> Unit) {
    Column(Modifier.padding(12.dp, 10.dp, 12.dp, 4.dp).fillMaxWidth().clip(RoundedCornerShape(14.dp))
        .background(Panel).border(1.dp, if (s?.currentId != null) Accent.copy(alpha = .5f) else Border, RoundedCornerShape(14.dp))
        .padding(12.dp, 10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            if (s?.currentId != null) {
                CircularProgressIndicator(Modifier.size(14.dp), color = Accent, strokeWidth = 2.dp)
                Spacer(Modifier.width(8.dp))
            }
            Column(Modifier.weight(1f)) {
                val title = when {
                    s == null -> "Connecting…"
                    s.currentId != null -> "Task #${s.currentId} · ${s.desc.lineSequence().firstOrNull() ?: ""}"
                    s.state == "paused" -> "Paused"
                    else -> "Idle"
                }
                Text(title, color = TextMain, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                val sub = when {
                    s == null -> ""
                    s.currentId != null -> "step ${s.step}${if (s.maxSteps > 0) "/${s.maxSteps}" else ""} · ${s.action.ifEmpty { "thinking…" }}"
                    s.queue > 0 -> "${s.queue} task(s) queued"
                    s.goal.isNotEmpty() -> "Waiting for the next goal cycle"
                    else -> "Waiting for work"
                }
                if (sub.isNotEmpty()) Text(sub, color = Muted, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
        }
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            SmallBtn("＋ New session", Modifier.weight(1f), onClick = onNew)
            SmallBtn("■ Stop all", Modifier.weight(1f), color = ErrC, onClick = onStopAll)
        }
    }
}

@Composable
fun SmallBtn(label: String, modifier: Modifier = Modifier, color: Color = TextMain, enabled: Boolean = true, onClick: () -> Unit) {
    Box(modifier.clip(RoundedCornerShape(10.dp)).border(1.dp, if (color == TextMain) Border else color.copy(alpha = .5f), RoundedCornerShape(10.dp))
        .clickable(enabled = enabled, onClick = onClick).padding(horizontal = 10.dp, vertical = 8.dp),
        contentAlignment = Alignment.Center) {
        Text(label, color = if (enabled) color else Muted, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun QuickBar(list: List<QuickCmd>, onTap: (QuickCmd) -> Unit, onEdit: () -> Unit) {
    Column(Modifier.padding(horizontal = 12.dp, vertical = 4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("QUICK COMMANDS", color = Muted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, modifier = Modifier.weight(1f))
            Text("✎ Edit", color = Muted, fontSize = 12.sp, modifier = Modifier.clip(RoundedCornerShape(6.dp)).clickable(onClick = onEdit).padding(6.dp, 2.dp))
        }
        Spacer(Modifier.height(4.dp))
        if (list.isEmpty()) Text("No quick commands — tap Edit to add some.", color = Muted, fontSize = 12.sp)
        list.chunked(4).forEach { row ->
            Row(Modifier.fillMaxWidth().padding(bottom = 6.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                row.forEach { q ->
                    Box(Modifier.weight(1f).clip(RoundedCornerShape(10.dp)).background(Panel2)
                        .clickable { onTap(q) }.padding(vertical = 9.dp, horizontal = 4.dp), contentAlignment = Alignment.Center) {
                        Text(q.label, color = TextMain, fontSize = 13.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                }
                repeat(4 - row.size) { Spacer(Modifier.weight(1f)) }
            }
        }
    }
}

@Composable
fun QuickEditor(initial: List<QuickCmd>, onDismiss: () -> Unit, onSave: (List<QuickCmd>) -> Unit) {
    val rows = remember { mutableStateListOf<QuickCmd>().apply { addAll(initial) } }
    AlertDialog(onDismissRequest = onDismiss, containerColor = Panel,
        title = { Text("Quick commands", color = TextMain) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Text("Each button sends its command (a /command or a plain-text task). Saved on this phone.",
                    color = Muted, fontSize = 12.sp)
                Spacer(Modifier.height(10.dp))
                rows.forEachIndexed { i, q ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        OutlinedTextField(q.label, { rows[i] = q.copy(label = it) }, label = { Text("Label") },
                            singleLine = true, colors = fieldColors(), modifier = Modifier.weight(.8f))
                        Spacer(Modifier.width(6.dp))
                        OutlinedTextField(q.command, { rows[i] = q.copy(command = it) }, label = { Text("Command") },
                            singleLine = true, colors = fieldColors(), modifier = Modifier.weight(1.2f))
                        Text("✕", color = ErrC, fontSize = 16.sp, modifier = Modifier.clickable { rows.removeAt(i) }.padding(8.dp))
                    }
                    Spacer(Modifier.height(6.dp))
                }
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    SmallBtn("＋ Add") { rows.add(QuickCmd("", "")) }
                    SmallBtn("Reset to defaults") { rows.clear(); rows.addAll(DEFAULT_QUICK) }
                }
            }
        },
        confirmButton = { TextButton(onClick = { onSave(rows.toList()) }) { Text("Save", color = Accent) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel", color = Text2) } })
}

@Composable
fun ApprovalCard(a: Approval, busy: Boolean, onAnswer: (Boolean) -> Unit) {
    Column(Modifier.padding(bottom = 8.dp).fillMaxWidth().clip(RoundedCornerShape(12.dp))
        .background(Color(0x1AF2B84B)).border(1.dp, WarnC.copy(alpha = .6f), RoundedCornerShape(12.dp)).padding(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("⚠️ Needs your approval", color = WarnC, fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
            if (a.agent.isNotEmpty() && a.agent != "main") {
                Spacer(Modifier.width(6.dp))
                Text(a.agent, color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Bold,
                    modifier = Modifier.clip(RoundedCornerShape(50)).background(colorFor(a.agent)).padding(6.dp, 1.dp))
            }
            Spacer(Modifier.weight(1f))
            Text(a.aid, color = Muted, fontSize = 11.sp)
        }
        Text(a.command, color = TextMain, fontFamily = FontFamily.Monospace, fontSize = 12.5.sp,
            modifier = Modifier.padding(top = 6.dp).fillMaxWidth().clip(RoundedCornerShape(8.dp)).background(Bg).padding(8.dp))
        if (a.reason.isNotEmpty()) Text(a.reason, color = Text2, fontSize = 13.sp, modifier = Modifier.padding(top = 6.dp))
        Row(Modifier.padding(top = 10.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            SmallBtn(if (busy) "…" else "✓ Approve", Modifier.weight(1f), color = OkC, enabled = !busy) { onAnswer(true) }
            SmallBtn(if (busy) "…" else "✕ Deny", Modifier.weight(1f), color = ErrC, enabled = !busy) { onAnswer(false) }
        }
    }
}

@Composable
fun Composer(vm: AgentViewModel) {
    val atts by vm.attachments.collectAsState()
    val shared by vm.sharedText.collectAsState()
    var input by rememberSaveable { mutableStateOf("") }
    var menu by remember { mutableStateOf(false) }
    LaunchedEffect(shared) {
        shared?.let { input = if (input.isBlank()) it else input + "\n" + it; vm.sharedText.value = null }
    }
    val pickPhotos = rememberLauncherForActivityResult(ActivityResultContracts.PickMultipleVisualMedia()) { uris ->
        if (uris.isNotEmpty()) vm.attach(uris)
    }
    val pickFiles = rememberLauncherForActivityResult(ActivityResultContracts.GetMultipleContents()) { uris ->
        if (uris.isNotEmpty()) vm.attach(uris)
    }
    val uploading = atts.any { it.uploading }
    val canSend = !uploading && (input.isNotBlank() || atts.any { it.path != null })
    val doSend = { if (canSend && vm.sendTask(input)) input = "" }

    Column(Modifier.fillMaxWidth().background(Panel).navigationBarsPadding().padding(10.dp, 8.dp)) {
        if (atts.isNotEmpty()) {
            LazyRow(Modifier.fillMaxWidth().padding(bottom = 6.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                items(atts, key = { it.ref }) { a -> AttachmentChip(a) { vm.removeAttachment(a.ref) } }
            }
        }
        Row(verticalAlignment = Alignment.Bottom) {
            Box {
                Box(Modifier.size(48.dp).clip(RoundedCornerShape(12.dp)).background(Panel2).clickable { menu = true },
                    contentAlignment = Alignment.Center) { Text("📎", fontSize = 18.sp) }
                DropdownMenu(expanded = menu, onDismissRequest = { menu = false }, containerColor = Panel2) {
                    DropdownMenuItem(text = { Text("Photo", color = TextMain) }, onClick = {
                        menu = false
                        pickPhotos.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly))
                    })
                    DropdownMenuItem(text = { Text("File", color = TextMain) }, onClick = {
                        menu = false; pickFiles.launch("*/*")
                    })
                }
            }
            Spacer(Modifier.width(8.dp))
            OutlinedTextField(input, { input = it },
                placeholder = { Text("Give the agent a task…", color = Muted) },
                modifier = Modifier.weight(1f), maxLines = 5, colors = fieldColors(), shape = RoundedCornerShape(14.dp),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                keyboardActions = KeyboardActions(onSend = { doSend() }))
            Spacer(Modifier.width(8.dp))
            Button(onClick = { doSend() }, enabled = canSend, modifier = Modifier.height(52.dp),
                shape = RoundedCornerShape(14.dp), contentPadding = PaddingValues(horizontal = 14.dp)) {
                Text(if (uploading) "…" else "Send", fontWeight = FontWeight.SemiBold)
            }
        }
    }
}

@Composable
fun AttachmentChip(a: Attachment, onRemove: () -> Unit) {
    val border = when { a.error != null -> ErrC; a.path != null -> OkC.copy(alpha = .6f); else -> Border }
    Row(Modifier.clip(RoundedCornerShape(50)).background(Panel2).border(1.dp, border, RoundedCornerShape(50))
        .padding(start = 10.dp, end = 4.dp, top = 4.dp, bottom = 4.dp), verticalAlignment = Alignment.CenterVertically) {
        when {
            a.uploading -> CircularProgressIndicator(Modifier.size(12.dp), color = Accent, strokeWidth = 2.dp)
            a.error != null -> Text("!", color = ErrC, fontWeight = FontWeight.Bold, fontSize = 12.sp)
            else -> Text("✓", color = OkC, fontSize = 12.sp)
        }
        Spacer(Modifier.width(6.dp))
        Column(Modifier.widthIn(max = 180.dp)) {
            Text(a.name, color = TextMain, fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            Text(a.error ?: (if (a.uploading) "uploading ${fmtSize(a.size)}…" else fmtSize(a.size)),
                color = if (a.error != null) ErrC else Muted, fontSize = 10.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
        Text("✕", color = Muted, fontSize = 13.sp, modifier = Modifier.clip(RoundedCornerShape(50)).clickable(onClick = onRemove).padding(8.dp, 4.dp))
    }
}

@Composable
fun AgentChip(a: AgentNode) {
    val c = colorFor(a.id)
    Column(Modifier.padding(end = 8.dp).width(160.dp).clip(RoundedCornerShape(12.dp))
        .background(Panel).border(1.dp, Border, RoundedCornerShape(12.dp)).padding(9.dp, 7.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            val dot = when (a.status) { "working" -> c; "done" -> OkC; "idle" -> Muted; else -> ErrC }
            Box(Modifier.size(8.dp).clip(RoundedCornerShape(50)).background(dot))
            Spacer(Modifier.width(6.dp))
            Text("${roleIcon(a.role)} ${a.id}", color = TextMain, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.weight(1f))
            Text(if (a.status == "working") "${a.step}${if (a.maxSteps > 0) "/${a.maxSteps}" else ""}" else a.status,
                color = Muted, fontSize = 10.sp)
        }
        if (a.title.isNotEmpty())
            Text(a.title, color = Muted, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
        val sub = if (a.status == "working") a.action.ifEmpty { "working…" } else ""
        if (sub.isNotEmpty())
            Text(sub, color = Text2, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
    }
}

@Composable
fun FeedRow(item: FeedItem, tint: Boolean = true) {
    val agentTag: @Composable () -> Unit = {
        if (tint && item.agent != "main" && item.agent != "you")
            Text(item.agent, color = Color.White, fontSize = 10.sp, fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(bottom = 2.dp).clip(RoundedCornerShape(50)).background(colorFor(item.agent)).padding(6.dp, 1.dp))
    }
    when (item.kind) {
        "mine" -> Row(Modifier.fillMaxWidth().padding(top = 10.dp, bottom = 4.dp), horizontalArrangement = Arrangement.End) {
            Text(item.text, color = TextMain, fontSize = 14.sp,
                modifier = Modifier.widthIn(max = 300.dp).clip(RoundedCornerShape(16.dp, 16.dp, 4.dp, 16.dp))
                    .background(Panel3).padding(12.dp, 8.dp))
        }
        "task" -> Column(Modifier.fillMaxWidth().padding(top = 14.dp, bottom = 4.dp).clip(RoundedCornerShape(12.dp))
            .background(Panel).border(1.dp, Border, RoundedCornerShape(12.dp)).padding(12.dp, 9.dp)) {
            Text("TASK", color = Accent, fontSize = 10.sp, fontWeight = FontWeight.Bold)
            Text(item.text, color = TextMain, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
        }
        "final", "review" -> Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min).padding(vertical = 6.dp).clip(RoundedCornerShape(12.dp)).background(Panel)) {
            Box(Modifier.width(3.dp).fillMaxHeight().background(if (item.kind == "review") Violet
                else if (item.text.contains("done")) OkC else ErrC))
            Text(item.text, color = TextMain, fontSize = 14.sp, modifier = Modifier.padding(12.dp, 10.dp))
        }
        "info", "clear" -> Text(item.text, color = Muted, fontSize = 12.sp,
            modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp))
        "reply" -> Text(item.text, color = Text2, fontSize = 13.sp,
            modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp).clip(RoundedCornerShape(10.dp)).background(Panel2).padding(10.dp, 7.dp))
        "alert", "approval_done" -> Text(item.text, color = WarnC, fontSize = 13.sp, modifier = Modifier.padding(vertical = 3.dp))
        "progress", "update" -> Column(Modifier.padding(start = 10.dp, top = 3.dp, bottom = 3.dp)) {
            agentTag()
            Text(item.text, color = TextMain, fontSize = 13.5.sp,
                modifier = Modifier.clip(RoundedCornerShape(10.dp)).background(AccentSoft).padding(10.dp, 7.dp))
        }
        "output" -> {   // raw tool output (Settings → Show raw tool output); tap to expand
            var open by remember { mutableStateOf(false) }
            Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min).padding(start = 10.dp)) {
                Box(Modifier.width(2.dp).heightIn(min = 22.dp).fillMaxHeight().background(Border))
                Text(item.text, color = Text2, fontSize = 11.5.sp, fontFamily = FontFamily.Monospace,
                    maxLines = if (open) Int.MAX_VALUE else 4, overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.padding(start = 10.dp, top = 2.dp, bottom = 4.dp).fillMaxWidth()
                        .clip(RoundedCornerShape(8.dp)).background(Bg).border(1.dp, Border, RoundedCornerShape(8.dp))
                        .clickable { open = !open }.padding(8.dp))
            }
        }
        else -> Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min).padding(start = 10.dp)) {   // the step-by-step timeline
            val rail = if (item.agent == "main" || !tint) Border else colorFor(item.agent).copy(alpha = .6f)
            Box(Modifier.width(2.dp).heightIn(min = 22.dp).fillMaxHeight().background(rail))
            Column(Modifier.padding(start = 10.dp, top = 2.dp, bottom = 2.dp)) {
                agentTag()
                Text(item.text,
                    color = when { item.kind == "thinking" -> Muted; item.ok == false -> ErrC; item.kind == "result" -> Muted; else -> Text2 },
                    fontSize = if (item.kind == "result") 12.5.sp else 13.5.sp,
                    fontStyle = if (item.kind == "thinking") FontStyle.Italic else FontStyle.Normal,
                    fontWeight = if (item.kind == "agent") FontWeight.Medium else FontWeight.Normal)
            }
        }
    }
}

// ---------- settings ----------
/** Everything the Settings tab can do; keeps [SettingsContent] free of the view model so it can be previewed/tested. */
data class SettingsActions(
    val onMaxSteps: (Int) -> Unit = {}, val onInterval: (String) -> Unit = {}, val onWorkers: (Int) -> Unit = {},
    val onReasoning: (String) -> Unit = {}, val onReview: (Boolean) -> Unit = {}, val onVerbose: (String) -> Unit = {},
    val onLog: () -> Unit = {}, val onShowOutput: (Boolean) -> Unit = {}, val onTint: (Boolean) -> Unit = {},
    val onConfirm: (String) -> Unit = {}, val onLogout: () -> Unit = {},
)

/** The Settings tab — the same panel as the web console's Settings dialog. */
@Composable
fun SettingsContent(st: Status?, url: String, showOutput: Boolean, tint: Boolean, act: SettingsActions,
                    modifier: Modifier = Modifier) {
    Column(modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(16.dp, 0.dp, 16.dp, 24.dp).navigationBarsPadding()) {
        if (st == null) Text("Not connected to the agent yet — values show once it reports in. You can still change them.",
            color = WarnC, fontSize = 12.sp, modifier = Modifier.padding(top = 12.dp))

        Section("Agent settings")
        SetCard {
            SettingField("Max steps", st?.maxSteps?.toString() ?: "", "Tool calls per task · 0 = unlimited (∞)", decimal = false,
                valid = { it.toIntOrNull()?.let { n -> n >= 0 } == true }) { act.onMaxSteps(it.toInt()) }
            SettingField("Goal every (minutes)", st?.let { fmtNum(it.goalIntervalMin) } ?: "", "Minutes between standing-goal cycles", decimal = true,
                valid = { it.toDoubleOrNull()?.let { d -> d > 0 } == true }) { act.onInterval(it) }
            SettingField("Max helpers", st?.maxWorkers?.toString() ?: "", "Parallel helper agents · 0 = none", decimal = false,
                valid = { it.toIntOrNull()?.let { n -> n >= 0 } == true }) { act.onWorkers(it.toInt()) }
            ReasoningPicker(st?.reasoning ?: "—", act.onReasoning)
            SetDivider()
            ToggleRow("Review", "A reviewer agent checks each finished task", st?.review ?: false, act.onReview)
        }

        Section("Conversation")
        SetCard {
            ToggleRow("Show raw tool output", "Output under each step (tap it to expand)", showOutput, act.onShowOutput)
            SetDivider()
            ToggleRow("Colour by agent", "Tag lines from helper agents with their colour", tint, act.onTint)
        }

        Section("Telegram / CLI verbosity")
        Segmented(VERBOSITY_LEVELS, st?.verbose, act.onVerbose)

        Section("Tools")
        SmallBtn("Post the step log to the chat", Modifier.fillMaxWidth(), onClick = act.onLog)

        Section("Session")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            SmallBtn("＋ New session", Modifier.weight(1f)) { act.onConfirm("new") }
            SmallBtn("■ Stop all", Modifier.weight(1f), color = ErrC) { act.onConfirm("stopall") }
        }
        Spacer(Modifier.height(8.dp))
        SmallBtn("Clear activity feed", Modifier.fillMaxWidth()) { act.onConfirm("clear") }

        Section("Agent")
        SetCard {
            InfoRow("Workdir", st?.workdir?.ifEmpty { "—" } ?: "—", mono = true)
            InfoRow("Access", st?.let { "${it.access} · approvals ${if (it.approvalsOn) "on" else "off"}" } ?: "—")
            InfoRow("Model", st?.model?.ifEmpty { "—" } ?: "—")
            InfoRow("Tokens in / out", st?.let { "${fmtTok(it.promptTokens)} / ${fmtTok(it.completionTokens)}" } ?: "—")
            InfoRow("Today", st?.let { "${fmtTok(it.tokens)} of ${fmtTok(it.budget)} · $${"%.2f".format(it.cost)}" } ?: "—")
        }

        if (!st?.recent.isNullOrEmpty()) {
            Section("Recent tasks")
            st!!.recent.forEach { t ->
                Row(Modifier.fillMaxWidth().padding(vertical = 5.dp)) {
                    val (ic, col) = when (t.status) { "done" -> "✓" to OkC; "failed", "aborted" -> "✕" to ErrC; "running" -> "●" to Accent; else -> "•" to Muted }
                    Text(ic, color = col, fontSize = 13.sp, modifier = Modifier.width(18.dp))
                    Column {
                        Text(if (t.kind == "goal") "🎯 Goal cycle" else t.description.lineSequence().firstOrNull() ?: "",
                            color = TextMain, fontSize = 13.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
                        Text("#${t.id} · ${t.status} · ${t.steps} steps · ${fmtTok(t.tokens)} tok", color = Muted, fontSize = 11.sp)
                    }
                }
            }
        }

        Section("Connection")
        Text(url, color = Text2, fontSize = 12.sp, fontFamily = FontFamily.Monospace)
        Spacer(Modifier.height(8.dp))
        SmallBtn("Disconnect & forget token", Modifier.fillMaxWidth(), color = ErrC, onClick = act.onLogout)
    }
}

@Composable
fun SetCard(content: @Composable ColumnScope.() -> Unit) {
    Column(Modifier.fillMaxWidth().clip(RoundedCornerShape(14.dp)).background(Panel)
        .border(1.dp, Border, RoundedCornerShape(14.dp)).padding(14.dp, 10.dp), content = content)
}

@Composable
fun SetDivider() { Box(Modifier.fillMaxWidth().padding(vertical = 4.dp).height(1.dp).background(Border)) }

@Composable
fun ToggleRow(title: String, sub: String, checked: Boolean, onChange: (Boolean) -> Unit) {
    Row(Modifier.fillMaxWidth().clickable { onChange(!checked) }.padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text(title, color = TextMain, fontSize = 14.sp)
            Text(sub, color = Muted, fontSize = 12.sp)
        }
        Switch(checked = checked, onCheckedChange = onChange,
            colors = SwitchDefaults.colors(checkedTrackColor = Accent, checkedThumbColor = AccentInk,
                uncheckedTrackColor = Panel3, uncheckedThumbColor = Muted, uncheckedBorderColor = Border))
    }
}

@Composable
fun InfoRow(k: String, v: String, mono: Boolean = false) {
    Row(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Text(k, color = Muted, fontSize = 13.sp, modifier = Modifier.width(118.dp))
        Text(v, color = TextMain, fontSize = if (mono) 12.sp else 13.sp, fontFamily = if (mono) FontFamily.Monospace else FontFamily.Default)
    }
}

@Composable
fun Segmented(options: List<String>, current: String?, onPick: (String) -> Unit) {
    Row(Modifier.clip(RoundedCornerShape(12.dp)).background(Panel2).padding(3.dp)) {
        options.forEach { o ->
            val on = o == current
            Box(Modifier.clip(RoundedCornerShape(9.dp)).background(if (on) Panel3 else Color.Transparent)
                .clickable { if (!on) onPick(o) }.padding(horizontal = 18.dp, vertical = 8.dp)) {
                Text(o.replaceFirstChar { it.uppercase() }, color = if (on) TextMain else Muted, fontSize = 13.sp,
                    fontWeight = if (on) FontWeight.SemiBold else FontWeight.Normal)
            }
        }
    }
}

@Composable
fun Section(title: String) {
    Text(title.uppercase(), color = Muted, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
        modifier = Modifier.padding(top = 18.dp, bottom = 8.dp))
}

/** A number field that follows the agent's value, but never while you're editing it.
 *  The command is sent when you press Done or leave the field. */
@Composable
fun SettingField(label: String, serverValue: String, hint: String, decimal: Boolean,
                 valid: (String) -> Boolean, onCommit: (String) -> Unit) {
    var text by remember { mutableStateOf(serverValue) }
    var focused by remember { mutableStateOf(false) }
    LaunchedEffect(serverValue, focused) { if (!focused) text = serverValue }
    val focus = LocalFocusManager.current
    val commit = { val v = text.trim(); if (v != serverValue && valid(v)) onCommit(v) }
    OutlinedTextField(text, { v -> text = v.filter { it.isDigit() || (decimal && it == '.') } },
        label = { Text(label) }, singleLine = true, colors = fieldColors(),
        isError = text.isNotEmpty() && !valid(text.trim()),
        supportingText = { Text(hint, color = Muted, fontSize = 11.sp) },
        keyboardOptions = KeyboardOptions(keyboardType = if (decimal) KeyboardType.Decimal else KeyboardType.Number, imeAction = ImeAction.Done),
        keyboardActions = KeyboardActions(onDone = { focus.clearFocus() }),
        modifier = Modifier.fillMaxWidth().padding(bottom = 4.dp).onFocusChanged { fs ->
            if (focused && !fs.isFocused) commit()
            focused = fs.isFocused
        })
}

@Composable
fun ReasoningPicker(current: String, onPick: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    Row(Modifier.fillMaxWidth().padding(vertical = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f)) {
            Text("Reasoning", color = TextMain, fontSize = 14.sp)
            Text("How hard the model thinks before acting", color = Muted, fontSize = 12.sp)
        }
        Box {
            SmallBtn("$current  ▾") { open = true }
            DropdownMenu(expanded = open, onDismissRequest = { open = false }, containerColor = Panel2) {
                REASONING_LEVELS.forEach { lvl ->
                    DropdownMenuItem(text = { Text((if (lvl == current) "✓ " else "    ") + lvl, color = TextMain) },
                        onClick = { open = false; if (lvl != current) onPick(lvl) })
                }
            }
        }
    }
}
