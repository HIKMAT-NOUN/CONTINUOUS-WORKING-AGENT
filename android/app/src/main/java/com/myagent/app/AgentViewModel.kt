package com.myagent.app

import android.app.Application
import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.compose.ui.text.AnnotatedString
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import kotlinx.coroutines.channels.BufferOverflow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import org.json.JSONArray
import org.json.JSONObject

enum class Phase { IDLE, CONNECTING, LIVE, RETRYING, AUTH_FAILED }

/** Connection status shown in the header/banner. [retryAt] is an elapsedRealtime() deadline. */
data class Conn(val phase: Phase, val detail: String = "", val retryAt: Long = 0, val attempt: Int = 0)

class AgentViewModel(app: Application) : AndroidViewModel(app) {
    private val store = SecureStore(app)
    private val ui = app.getSharedPreferences("ui", Context.MODE_PRIVATE)   // view toggles only
    private val main = Handler(Looper.getMainLooper())

    val url = MutableStateFlow(store.url)
    val configured = MutableStateFlow(url.value.isNotBlank() && store.token.isNotBlank())
    val editing = MutableStateFlow(false)          // showing the setup screen while configured

    val conn = MutableStateFlow(Conn(Phase.IDLE))
    val agentOnline = MutableStateFlow(false)
    val feed = MutableStateFlow<List<FeedItem>>(emptyList())
    val status = MutableStateFlow<Status?>(null)
    val approvals = MutableStateFlow<List<Approval>>(emptyList())
    val agentFilter = MutableStateFlow<String?>(null)
    val showOutput = MutableStateFlow(ui.getBoolean("show_output", false))
    val summaryOnly = MutableStateFlow(ui.getBoolean("summary_only", false))
    /** One-off messages for the snackbar (task finished, stop sent, send failed…). */
    val notices = MutableSharedFlow<String>(extraBufferCapacity = 8, onBufferOverflow = BufferOverflow.DROP_OLDEST)

    private var client: RelayClient? = null
    private var generation = 0
    private var lastEventId = 0L                   // survives pauses so we resume, not replay
    private var localSeq = 0L
    private var dropped = false                    // lost the link since the last LIVE
    private val approvalRefs = HashMap<String, String>()   // command ref -> approval id

    // ---- lifecycle: hold the socket only while the app is on screen ----------------------
    private val appLifecycle = object : DefaultLifecycleObserver {
        override fun onStart(owner: LifecycleOwner) { if (configured.value) connect(resume = true) }
        override fun onStop(owner: LifecycleOwner) { pause() }
    }
    private val net = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) { main.post { client?.retryNow() } }
    }

    init {
        ProcessLifecycleOwner.get().lifecycle.addObserver(appLifecycle)
        try {
            app.getSystemService(ConnectivityManager::class.java)?.registerDefaultNetworkCallback(net)
        } catch (_: Exception) {}
    }

    override fun onCleared() {
        ProcessLifecycleOwner.get().lifecycle.removeObserver(appLifecycle)
        try { getApplication<Application>().getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(net) } catch (_: Exception) {}
        client?.close(); client = null
    }

    // ---- settings -------------------------------------------------------------------------
    /** Returns an error message, or null on success. A blank token keeps the saved one. */
    fun saveAndConnect(rawUrl: String, rawToken: String): String? {
        val check = normalizeRelayUrl(rawUrl)
        val u = check.url ?: return check.error
        val t = rawToken.trim()
        val keepToken = t.isEmpty() && configured.value
        if (!keepToken && t.isEmpty()) return "Enter the user token"
        if (t.any { it.isWhitespace() || it.code < 0x21 }) return "The token can't contain spaces or line breaks"
        try {
            store.url = u
            if (!keepToken) store.token = t
        } catch (e: Exception) {
            return "Couldn't store the token securely on this device"
        }
        url.value = u
        configured.value = true
        editing.value = false
        disconnect()
        resetSession()
        connect(resume = false)
        return null
    }

    fun forget() {
        disconnect()
        store.clear()
        url.value = ""; configured.value = false; editing.value = false
        resetSession()
    }

    fun setShowOutput(v: Boolean) { showOutput.value = v; ui.edit().putBoolean("show_output", v).apply() }
    fun setSummaryOnly(v: Boolean) { summaryOnly.value = v; ui.edit().putBoolean("summary_only", v).apply() }
    fun toggleAgentFilter(id: String) { agentFilter.value = if (agentFilter.value == id) null else id }
    fun clearAgentFilter() { agentFilter.value = null }

    // ---- connection -----------------------------------------------------------------------
    private fun resetSession() {
        feed.value = emptyList(); status.value = null; approvals.value = emptyList()
        agentFilter.value = null; agentOnline.value = false; lastEventId = 0
        approvalRefs.clear(); AgentColors.reset()
    }

    private fun connect(resume: Boolean) {
        if (client != null) return
        val u = url.value
        val t = store.token
        if (u.isBlank() || t.isBlank()) { configured.value = false; return }
        if (!resume) lastEventId = 0
        val gen = ++generation
        conn.value = Conn(Phase.CONNECTING)
        client = try {
            RelayClient(u, t, Callbacks(gen), startAfter = lastEventId).also { it.connect() }
        } catch (e: IllegalArgumentException) {
            conn.value = Conn(Phase.AUTH_FAILED, "The saved relay address isn't a secure wss:// URL")
            null
        }
    }

    private fun pause() {
        disconnect()
        conn.value = Conn(Phase.IDLE, "paused while the app is in the background")
    }

    private fun disconnect() {
        client?.let { lastEventId = maxOf(lastEventId, it.lastEventId); it.close() }
        client = null
        generation++
    }

    /** Manual "Reconnect": skip the wait, or start over after the token was rejected. */
    fun reconnect() {
        val c = client
        if (c != null && conn.value.phase != Phase.AUTH_FAILED) { c.retryNow(); return }
        disconnect()
        connect(resume = true)
    }

    // ---- sending --------------------------------------------------------------------------
    /** Returns true if it went out. On failure the text stays in the input box. */
    fun send(text: String): Boolean {
        val t = text.trim()
        if (t.isEmpty()) return false
        val ref = client?.sendCommand(t)
        if (ref == null) { notices.tryEmit("Not connected — your message wasn't sent"); return false }
        addLocal("mine", "main", AnnotatedString("› $t"))
        return true
    }

    fun stop() {
        if (client?.sendCommand("/stop") != null) notices.tryEmit("Stop sent")
        else notices.tryEmit("Not connected — couldn't send /stop")
    }

    fun decide(aid: String, approve: Boolean) {
        val ref = client?.sendCommand(if (approve) "/approve $aid" else "/deny $aid")
        if (ref == null) { notices.tryEmit("Not connected — try again when the relay is back"); return }
        approvalRefs[ref] = aid
        approvals.value = approvals.value.map { if (it.aid == aid) it.copy(busy = true) else it }
    }

    // ---- incoming -------------------------------------------------------------------------
    private fun addLocal(kind: String, agent: String, text: AnnotatedString) {
        val now = System.currentTimeMillis() / 1000.0
        append(listOf(FeedItem("l${localSeq++}", 0, now, kind, agent, text)))
    }

    private fun append(items: List<FeedItem>) {
        if (items.isNotEmpty()) feed.value = (feed.value + items).takeLast(FEED_MAX)
    }

    /** Handles one relay event. Returns its feed row (if any) without adding it. */
    private fun ingest(ev: JSONObject, live: Boolean): FeedItem? {
        val id = ev.optLong("id")
        if (id in 1..lastEventId) return null           // already have it (overlapping catch-up)
        if (id > lastEventId) lastEventId = id
        val kind = ev.optString("kind")
        val agent = ev.optString("agent").ifEmpty { "main" }
        AgentColors.register(agent)
        if (kind == "approval" || kind == "approval_done") approvals.value = applyApprovalEvent(approvals.value, ev)
        if (live) {
            val d = ev.optJSONObject("data")
            when (kind) {
                "final" -> {
                    val head = when (d?.optString("status")) {
                        "done" -> "🏁 Task done"; "failed" -> "⚠️ Task stopped"; "aborted" -> "🛑 Task aborted"
                        else -> "Task update"
                    }
                    val first = d?.optString("summary").orEmpty().lineSequence().firstOrNull { it.isNotBlank() }
                    notices.tryEmit(if (first != null) "$head — ${htmlToText(first)}" else head)
                }
                "review" -> notices.tryEmit("🔎 Review ready")
                "approval" -> notices.tryEmit("⚠️ Approval needed")
            }
        }
        return eventToFeed(ev, CodeBg)
    }

    private inner class Callbacks(private val gen: Int) : RelayClient.Listener {
        private fun ui(block: () -> Unit) { main.post { if (gen == generation) block() } }

        override fun onConnecting(attempt: Int) = ui {
            if (conn.value.phase != Phase.RETRYING) conn.value = Conn(Phase.CONNECTING, attempt = attempt)
            else conn.value = conn.value.copy(phase = Phase.CONNECTING, attempt = attempt)
        }
        override fun onConnected() = ui {
            if (dropped) notices.tryEmit("Reconnected")
            dropped = false
            conn.value = Conn(Phase.LIVE)
        }
        override fun onRetrying(reason: String, delaySec: Long) = ui {
            dropped = true
            conn.value = Conn(Phase.RETRYING, reason, SystemClock.elapsedRealtime() + delaySec * 1000,
                conn.value.attempt)
        }
        override fun onAuthFailed(code: Int) = ui {
            conn.value = Conn(Phase.AUTH_FAILED, "The relay rejected the token (HTTP $code). Check your settings.")
            client?.close(); client = null
        }
        override fun onHello(agentOnline: Boolean, events: JSONArray, state: JSONObject?) = ui {
            this@AgentViewModel.agentOnline.value = agentOnline
            val rows = ArrayList<FeedItem>()
            for (i in 0 until events.length()) events.optJSONObject(i)?.let { ev -> ingest(ev, live = false)?.let(rows::add) }
            append(rows)
            state?.let(::applyState)
        }
        override fun onEvent(ev: JSONObject) = ui { ingest(ev, live = true)?.let { append(listOf(it)) } }
        override fun onState(state: JSONObject) = ui { applyState(state) }
        override fun onReply(ref: String?, html: String) = ui {
            val aid = ref?.let { approvalRefs.remove(it) }
            val text = htmlToAnnotated(html, CodeBg)
            if (aid != null && text.isNotEmpty()) {
                // A non-empty answer to /approve or /deny means it didn't apply.
                approvals.value = if (text.text.startsWith("No pending approval"))
                    approvals.value.filterNot { it.aid == aid }
                else approvals.value.map { if (it.aid == aid) it.copy(busy = false) else it }
            }
            if (text.isNotEmpty()) addLocal("reply", "main", text)
        }
        override fun onPresence(online: Boolean) = ui {
            agentOnline.value = online
            notices.tryEmit(if (online) "Agent is back online" else "Agent went offline")
        }
    }

    private fun applyState(s: JSONObject) {
        val st = parseStatus(s)
        st.fleet.forEach { AgentColors.register(it.id) }
        status.value = st
    }

    companion object { const val FEED_MAX = 800 }
}
