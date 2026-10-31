package com.myagent.app

import android.app.Application
import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.ConcurrentHashMap

class AgentViewModel(app: Application) : AndroidViewModel(app), RelayClient.Listener {
    // App-private storage (MODE_PRIVATE). allowBackup is off in the manifest so it isn't copied off the phone.
    private val prefs = app.getSharedPreferences("myagent", Context.MODE_PRIVATE)
    val url = MutableStateFlow(prefs.getString("url", "") ?: "")
    val token = MutableStateFlow(prefs.getString("token", "") ?: "")
    val configured = MutableStateFlow(url.value.isNotBlank() && token.value.isNotBlank())

    val feed = MutableStateFlow<List<FeedItem>>(emptyList())
    val status = MutableStateFlow<Status?>(null)
    val connected = MutableStateFlow(false)
    val agentOnline = MutableStateFlow(false)
    val banner = MutableStateFlow("")
    val approvals = MutableStateFlow<List<Approval>>(emptyList())
    val attachments = MutableStateFlow<List<Attachment>>(emptyList())
    val quick = MutableStateFlow(quickFromJson(prefs.getString("quick", null)) ?: DEFAULT_QUICK)
    val sharedText = MutableStateFlow<String?>(null)

    private var client: RelayClient? = null
    private val resolvedAids = ConcurrentHashMap.newKeySet<String>()
    private val answeredAids = ConcurrentHashMap.newKeySet<String>()      // tapped Approve/Deny, waiting
    private val pendingUploads = ConcurrentHashMap<String, Pair<String, String>>()   // ref -> (name, base64)
    val busyAids = MutableStateFlow<Set<String>>(emptySet())

    // ---- connection ---------------------------------------------------------------------
    fun saveAndConnect(u: String, t: String) {
        prefs.edit().putString("url", u.trim()).putString("token", t.trim()).apply()
        url.value = u.trim(); token.value = t.trim(); configured.value = true
        connect()
    }

    fun connect() {
        client?.close()
        feed.value = emptyList(); status.value = null; approvals.value = emptyList()
        if (url.value.isBlank() || token.value.isBlank()) return
        client = RelayClient(url.value, token.value, this).also { it.connect() }
    }

    fun logout() {
        client?.close(); client = null
        prefs.edit().remove("url").remove("token").apply()     // keep quick commands
        url.value = ""; token.value = ""; configured.value = false
        feed.value = emptyList(); status.value = null; connected.value = false
        approvals.value = emptyList(); attachments.value = emptyList(); pendingUploads.clear()
    }

    override fun onCleared() { client?.close() }

    // ---- commands -------------------------------------------------------------------------
    /** Send a task or /command and echo it in the feed. */
    fun send(text: String): Boolean {
        val t = text.trim()
        if (t.isEmpty()) return false
        appendLocal(t)
        val ok = client?.sendCommand(t) != null
        if (!ok) addNote("⚠️ Not connected — not sent.")
        return ok
    }

    /** Send the composer text plus the paths of any uploaded attachments. */
    fun sendTask(text: String): Boolean {
        val atts = attachments.value
        if (atts.any { it.uploading }) return false
        val paths = atts.mapNotNull { it.path }
        var t = text.trim()
        if (paths.isNotEmpty()) t = (if (t.isEmpty()) "" else "$t\n\n") + "[attached files in WORKDIR: ${paths.joinToString(", ")}]"
        if (t.isEmpty()) return false
        send(t)
        attachments.value = emptyList()
        return true
    }

    fun approve(a: Approval, yes: Boolean) {
        if (!send(if (yes) "/approve ${a.aid}" else "/deny ${a.aid}")) return   // offline: keep the buttons live
        answeredAids.add(a.aid); busyAids.value = answeredAids.toSet()
    }

    fun newSession() { feed.value = emptyList(); send("/newproject") }

    fun stopAll() = send("/stopall")
    fun clearFeed() = send("/clear")
    fun setReview(on: Boolean) = send("/review ${if (on) "on" else "off"}")
    fun setReasoning(level: String) = send("/reasoning $level")
    fun setMaxSteps(n: Int) = send("/maxsteps $n")
    fun setInterval(min: String) = send("/interval $min")
    fun setWorkers(n: Int) = send("/workers $n")

    // ---- quick commands (saved on the device) -------------------------------------------------
    fun saveQuick(list: List<QuickCmd>) {
        val clean = list.map { QuickCmd(it.label.trim(), it.command.trim()) }
            .filter { it.label.isNotEmpty() && it.command.isNotEmpty() }
        quick.value = clean
        prefs.edit().putString("quick", quickToJson(clean)).apply()
    }

    // ---- attachments ------------------------------------------------------------------------
    fun attach(uris: List<Uri>) {
        val cr = getApplication<Application>().contentResolver
        for (uri in uris) {
            val ref = "u" + System.nanoTime()
            var name = "file"; var size = -1L
            try {
                cr.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
                    if (c.moveToFirst()) {
                        c.getColumnIndex(OpenableColumns.DISPLAY_NAME).takeIf { it >= 0 }?.let { c.getString(it) }?.let { name = it }
                        c.getColumnIndex(OpenableColumns.SIZE).takeIf { it >= 0 && !c.isNull(it) }?.let { size = c.getLong(it) }
                    }
                }
            } catch (_: Exception) {}
            if (size > MAX_UPLOAD_BYTES) {
                attachments.update { it + Attachment(ref, name, size, error = "too large (max ${fmtSize(MAX_UPLOAD_BYTES)})") }
                continue
            }
            attachments.update { it + Attachment(ref, name, maxOf(size, 0)) }
            viewModelScope.launch {
                val result = withContext(Dispatchers.IO) { readCapped(uri) }
                if (result == null) { failAttachment(ref, "couldn't read the file"); return@launch }
                if (result.size > MAX_UPLOAD_BYTES) { failAttachment(ref, "too large (max ${fmtSize(MAX_UPLOAD_BYTES)})"); return@launch }
                val b64 = withContext(Dispatchers.Default) { Base64.encodeToString(result, Base64.NO_WRAP) }
                attachments.update { l -> l.map { if (it.ref == ref) it.copy(size = result.size.toLong()) else it } }
                pendingUploads[ref] = name to b64
                trySendUpload(ref)           // if offline it stays queued and goes out on reconnect
            }
        }
    }

    fun removeAttachment(ref: String) {
        pendingUploads.remove(ref)
        attachments.update { l -> l.filterNot { it.ref == ref } }
    }

    private fun readCapped(uri: Uri): ByteArray? = try {
        getApplication<Application>().contentResolver.openInputStream(uri)?.use { input ->
            val out = ByteArrayOutputStream()
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buf); if (n < 0) break
                out.write(buf, 0, n)
                if (out.size() > MAX_UPLOAD_BYTES) break     // stop early; caller reports "too large"
            }
            out.toByteArray()
        }
    } catch (e: Exception) { null }

    private fun trySendUpload(ref: String) {
        val (name, b64) = pendingUploads[ref] ?: return
        client?.sendUpload(name, b64, ref)
    }

    private fun failAttachment(ref: String, msg: String) {
        pendingUploads.remove(ref)
        attachments.update { l -> l.map { if (it.ref == ref) it.copy(error = msg) else it } }
    }

    // ---- feed helpers -----------------------------------------------------------------------
    private fun appendLocal(text: String) = add(FeedItem(-System.nanoTime(), now(), "mine", "you", text))
    private fun addNote(text: String) = add(FeedItem(-System.nanoTime(), now(), "reply", "main", text))
    private fun now() = System.currentTimeMillis() / 1000.0
    private fun add(item: FeedItem?) {
        if (item == null) return
        feed.update { l -> if (item.id > 0 && l.any { it.id == item.id }) l else (l + item).takeLast(500) }
    }

    /** Route one event: approvals, the 'clear' wipe, then the feed. */
    private fun handleEvent(ev: JSONObject, into: MutableList<FeedItem>?) {
        val data = ev.optJSONObject("data") ?: JSONObject()
        when (ev.optString("kind")) {
            "clear" -> {
                if (into != null) into.clear() else feed.value = emptyList()
            }
            "approval" -> {
                val a = parseApproval(data).let { if (it.agent.isBlank() || it.agent == "main") it.copy(agent = ev.optString("agent", "main")) else it }
                if (a.aid.isNotEmpty() && a.aid !in resolvedAids)
                    approvals.update { l -> if (l.any { it.aid == a.aid }) l else l + a }
            }
            "approval_done" -> {
                val aid = data.optString("aid")
                resolvedAids.add(aid); answeredAids.remove(aid); busyAids.value = answeredAids.toSet()
                approvals.update { l -> l.filterNot { it.aid == aid } }
            }
        }
        val item = eventToFeed(ev) ?: return
        if (into != null) into.add(item) else add(item)
    }

    private fun applyState(st: JSONObject) {
        val s = parseStatus(st)
        status.value = s
        approvals.value = s.pendingApprovals.filterNot { it.aid in resolvedAids }   // the agent is the source of truth
    }

    // ---- RelayClient.Listener (called off the main thread; StateFlow is thread-safe) ---------
    override fun onConnected() {
        connected.value = true; banner.value = ""
        pendingUploads.keys.forEach(::trySendUpload)
    }
    override fun onDisconnected(reason: String) {
        connected.value = false
        banner.value = if (reason.contains("wss://")) "Relay URL must start with wss:// — open ⚙ to fix it." else "reconnecting…"
    }
    override fun onHello(agentOnlineFlag: Boolean, events: JSONArray, state: JSONObject?) {
        agentOnline.value = agentOnlineFlag
        // On a reconnect the relay only sends what we missed, so merge rather than replace.
        // Event ids are never reused, so de-duplicating by id is safe (and keeps list keys unique).
        val list = ArrayList(feed.value)
        for (i in 0 until events.length()) events.optJSONObject(i)?.let { handleEvent(it, list) }
        feed.value = list.distinctBy { it.id }.takeLast(500)
        state?.let(::applyState)
    }
    override fun onEvent(ev: JSONObject) = handleEvent(ev, null)
    override fun onState(state: JSONObject) = applyState(state)
    override fun onReply(ref: String?, html: String) {
        val t = htmlToText(html)
        if (t.isNotEmpty()) addNote(t)
    }
    override fun onPresence(online: Boolean) { agentOnline.value = online }
    override fun onUploadAck(ref: String?, files: JSONArray?, error: String?) {
        if (ref == null || attachments.value.none { it.ref == ref }) return   // another device's upload
        pendingUploads.remove(ref)
        val f = files?.optJSONObject(0)
        if (error != null || f == null) { failAttachment(ref, error ?: "upload failed"); return }
        attachments.update { l -> l.map {
            if (it.ref == ref) it.copy(name = f.optString("name", it.name), path = f.optString("path"), size = f.optLong("size", it.size)) else it
        } }
    }
}
