package com.myagent.app

import org.json.JSONArray
import org.json.JSONObject

// ---------- models ----------
data class FeedItem(val id: Long, val ts: Double, val kind: String, val agent: String, val text: String,
                    val ok: Boolean? = null)

data class AgentNode(val id: String, val role: String, val status: String, val step: Int,
                     val maxSteps: Int, val title: String, val action: String, val thinking: String)

data class RecentTask(val id: Long, val kind: String, val description: String, val status: String,
                      val steps: Int, val tokens: Long)

data class Approval(val aid: String, val command: String, val reason: String, val agent: String)

data class Status(
    val state: String,
    val pauseReason: String,
    val currentId: Long?,
    val desc: String,
    val step: Int,
    val action: String,
    val model: String,
    val tokens: Long,
    val budget: Long,
    val cost: Double,
    val queue: Int,
    val goal: String,
    val fleet: List<AgentNode>,
    // editable settings
    val reasoning: String,
    val review: Boolean,
    val maxSteps: Int,          // 0 = unlimited
    val maxWorkers: Int,
    val goalIntervalMin: Double,
    val recent: List<RecentTask>,
    val pendingApprovals: List<Approval>,
    // read-only info (same as the web console's Settings → Agent)
    val verbose: String = "steps",
    val workdir: String = "",
    val access: String = "",
    val approvalsOn: Boolean = true,
    val promptTokens: Long = 0,
    val completionTokens: Long = 0,
)

/** A user-defined button: tapping it sends [command]. */
data class QuickCmd(val label: String, val command: String)

val DEFAULT_QUICK = listOf(
    QuickCmd("Pause", "/pause"), QuickCmd("Resume", "/resume"),
    QuickCmd("Status", "/status"), QuickCmd("Stop", "/stop"))

/** A file picked to send with the next task. Uploaded right away; [path] is filled from the ack. */
data class Attachment(val ref: String, val name: String, val size: Long,
                      val path: String? = null, val error: String? = null) {
    val uploading get() = path == null && error == null
}

val REASONING_LEVELS = listOf("auto", "low", "medium", "high", "max", "off")
val VERBOSITY_LEVELS = listOf("off", "steps", "full")

/** Largest file the app will send. The relay accepts more, but OkHttp refuses (and drops the
 *  connection on) any single WebSocket message over 16 MiB, and base64 adds a third. */
const val MAX_UPLOAD_BYTES = 11L * 1024 * 1024

// ---------- parsing ----------
fun htmlToText(html: String): String =
    html.replace(Regex("<[^>]+>"), "")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"")
        .replace("&#x27;", "'").replace("&#39;", "'").replace("&amp;", "&").trim()

fun parseAgent(o: JSONObject) = AgentNode(
    o.optString("id"), o.optString("role"), o.optString("status"),
    o.optInt("step"), o.optInt("max_steps"), o.optString("title"),
    htmlToText(o.optString("action")), o.optString("thinking"))

private inline fun <T> JSONArray?.mapObjects(f: (JSONObject) -> T): List<T> {
    if (this == null) return emptyList()
    val out = ArrayList<T>(length())
    for (i in 0 until length()) optJSONObject(i)?.let { out.add(f(it)) }
    return out
}

fun parseApproval(o: JSONObject) = Approval(
    o.optString("aid"), o.optString("command"), o.optString("reason"), o.optString("agent", "main"))

private fun JSONObject.str(key: String) = if (isNull(key)) "" else optString(key)

fun parseStatus(s: JSONObject): Status {
    val cur = s.optJSONObject("current")
    return Status(
        state = s.optString("state"),
        pauseReason = s.str("pause_reason"),
        currentId = cur?.optLong("id"),
        desc = cur?.optString("description") ?: "",
        step = cur?.optInt("step") ?: 0,
        action = htmlToText(cur?.optString("action") ?: ""),
        model = s.optString("model"),
        tokens = s.optLong("tokens_today"),
        budget = s.optLong("budget"),
        cost = s.optDouble("cost_today", 0.0),
        queue = s.optInt("queue_count"),
        goal = s.str("goal"),
        fleet = s.optJSONArray("fleet").mapObjects(::parseAgent),
        reasoning = s.str("reasoning").ifEmpty { "auto" },
        review = s.optBoolean("review"),
        maxSteps = s.optInt("max_steps"),
        maxWorkers = s.optInt("max_workers"),
        goalIntervalMin = s.optDouble("goal_interval_min", 0.0),
        recent = s.optJSONArray("recent").mapObjects {
            RecentTask(it.optLong("id"), it.optString("kind"), it.optString("description"),
                it.optString("status"), it.optInt("steps"), it.optLong("tokens"))
        },
        pendingApprovals = s.optJSONArray("pending_approvals").mapObjects(::parseApproval),
        verbose = s.str("verbose").ifEmpty { "steps" },
        workdir = s.str("workdir"),
        access = s.str("access"),
        approvalsOn = s.optBoolean("approvals", true),
        promptTokens = s.optLong("prompt_tokens"),
        completionTokens = s.optLong("completion_tokens"),
    )
}

fun eventToFeed(ev: JSONObject): FeedItem? {
    val kind = ev.optString("kind")
    if (kind == "approval") return null                       // approvals get a card instead
    val data = ev.optJSONObject("data")
    if (kind == "output") {                                   // raw tool output: shown only if enabled in Settings
        val out = data?.optString("output").orEmpty().ifEmpty { htmlToText(ev.optString("html")) }
        if (out.isBlank()) return null
        return FeedItem(ev.optLong("id"), ev.optDouble("ts"), kind, ev.optString("agent", "main"), out.take(4000))
    }
    val text = htmlToText(ev.optString("html"))
    if (text.isEmpty()) return null
    val ok = if (kind == "result" && data != null && data.has("ok")) data.optBoolean("ok") && !data.optBoolean("denied") else null
    return FeedItem(ev.optLong("id"), ev.optDouble("ts"), kind, ev.optString("agent", "main"), text, ok)
}

fun quickToJson(list: List<QuickCmd>): String =
    JSONArray().apply { list.forEach { put(JSONObject().put("label", it.label).put("command", it.command)) } }.toString()

fun quickFromJson(s: String?): List<QuickCmd>? = try {
    if (s.isNullOrBlank()) null
    else JSONArray(s).mapObjects { QuickCmd(it.optString("label"), it.optString("command")) }
        .filter { it.label.isNotBlank() && it.command.isNotBlank() }
} catch (e: Exception) { null }

fun fmtTok(n: Long) = when {
    n >= 1_000_000 -> "%.2fM".format(n / 1e6); n >= 1000 -> "%.1fk".format(n / 1e3); else -> n.toString()
}

fun fmtSize(n: Long) = when {
    n >= 1024 * 1024 -> "%.1f MB".format(n / 1048576.0); n >= 1024 -> "${n / 1024} KB"; else -> "$n B"
}

fun fmtNum(d: Double) = if (d == Math.floor(d)) d.toLong().toString() else d.toString()
