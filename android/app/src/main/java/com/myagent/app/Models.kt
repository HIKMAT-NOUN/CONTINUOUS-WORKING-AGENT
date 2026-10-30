package com.myagent.app

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import org.json.JSONObject
import java.net.URI

// ---------- models ----------
/** One row in the feed. [key] is unique and stable (used as the LazyColumn key). */
data class FeedItem(
    val key: String,
    val id: Long,                 // relay event id, 0 for rows made on the phone
    val ts: Double,
    val kind: String,
    val agent: String,
    val text: AnnotatedString,
    val output: String? = null,   // only for kind == "output": the full tool output
)

data class AgentNode(
    val id: String, val role: String, val parent: String?, val status: String, val step: Int,
    val maxSteps: Int, val title: String, val action: String, val thinking: String, val result: String,
)

data class CurrentTask(val id: Long, val description: String, val step: Int, val action: String)

data class Status(
    val state: String, val current: CurrentTask?, val model: String, val tokens: Long,
    val budget: Long, val cost: Double, val queue: Int, val fleet: List<AgentNode>,
)

data class Approval(
    val aid: String, val command: String, val reason: String, val agent: String,
    val busy: Boolean = false,    // a decision was sent and we're waiting for approval_done
)

/** Kinds hidden by the "Summary" filter — same set as the web dashboard's Summary chip. */
val DETAIL_KINDS = setOf("step", "result", "output")

// ---------- the agent's small HTML subset (b, i, code, pre, strong, em) ----------
private val ENTITY = Regex("&(#[xX][0-9a-fA-F]+|#[0-9]+|[a-zA-Z]+);")
private val NAMED = mapOf("lt" to "<", "gt" to ">", "amp" to "&", "quot" to "\"", "apos" to "'", "nbsp" to " ")

fun decodeEntities(s: String): String = ENTITY.replace(s) { m ->
    val e = m.groupValues[1]
    val cp = when {
        e.startsWith("#x") || e.startsWith("#X") -> e.substring(2).toIntOrNull(16)
        e.startsWith("#") -> e.substring(1).toIntOrNull()
        else -> null
    }
    when {
        cp != null && Character.isValidCodePoint(cp) -> String(Character.toChars(cp))
        cp != null -> m.value
        else -> NAMED[e] ?: m.value
    }
}

private val TAG = Regex("<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")

/** Render the agent's HTML subset as styled text. Unknown tags are dropped, their text kept. */
fun htmlToAnnotated(html: String, codeBg: Color = Color.Unspecified): AnnotatedString = buildAnnotatedString {
    var bold = 0; var italic = 0; var mono = 0
    var pos = 0
    fun text(raw: String) {
        if (raw.isEmpty()) return
        val t = decodeEntities(raw)
        val style = SpanStyle(
            fontWeight = if (bold > 0) FontWeight.SemiBold else null,
            fontStyle = if (italic > 0) FontStyle.Italic else null,
            fontFamily = if (mono > 0) FontFamily.Monospace else null,
            background = if (mono > 0) codeBg else Color.Unspecified,
        )
        if (bold == 0 && italic == 0 && mono == 0) append(t)
        else { pushStyle(style); append(t); pop() }
    }
    for (m in TAG.findAll(html)) {
        text(html.substring(pos, m.range.first))
        pos = m.range.last + 1
        val closing = m.groupValues[1] == "/"
        val d = if (closing) -1 else 1
        when (m.groupValues[2].lowercase()) {
            "b", "strong" -> bold = maxOf(0, bold + d)
            "i", "em" -> italic = maxOf(0, italic + d)
            "code" -> mono = maxOf(0, mono + d)
            "pre" -> { mono = maxOf(0, mono + d); if (length > 0 && !closing) append("\n") }
            "br" -> append("\n")
        }
    }
    text(html.substring(pos))
}.trimmed()

private fun AnnotatedString.trimmed(): AnnotatedString {
    val start = text.indexOfFirst { !it.isWhitespace() }
    if (start < 0) return AnnotatedString("")
    val end = text.indexOfLast { !it.isWhitespace() } + 1
    return if (start == 0 && end == length) this else subSequence(start, end)
}

fun htmlToText(html: String): String = htmlToAnnotated(html).text

// ---------- relay messages ----------
private fun JSONObject.str(name: String): String = if (isNull(name)) "" else optString(name)

fun parseAgent(o: JSONObject) = AgentNode(
    id = o.str("id"), role = o.str("role"), parent = o.str("parent").ifEmpty { null },
    status = o.str("status"), step = o.optInt("step"), maxSteps = o.optInt("max_steps"),
    title = o.str("title"), action = htmlToText(o.str("action")), thinking = o.str("thinking"),
    result = o.str("result"))

fun parseStatus(s: JSONObject): Status {
    val fleet = ArrayList<AgentNode>()
    s.optJSONArray("fleet")?.let { arr ->
        for (i in 0 until arr.length()) arr.optJSONObject(i)?.let { fleet.add(parseAgent(it)) }
    }
    val cur = s.optJSONObject("current")?.let {
        CurrentTask(it.optLong("id"), it.str("description"), it.optInt("step"), htmlToText(it.str("action")))
    }
    return Status(
        state = s.str("state"), current = cur, model = s.str("model"),
        tokens = s.optLong("tokens_today"), budget = s.optLong("budget"),
        cost = s.optDouble("cost_today", 0.0), queue = s.optInt("queue_count"), fleet = fleet)
}

fun eventToFeed(ev: JSONObject, codeBg: Color = Color.Unspecified): FeedItem? {
    val id = ev.optLong("id")
    val kind = ev.str("kind")
    val agent = ev.str("agent").ifEmpty { "main" }
    val data = ev.optJSONObject("data")
    if (kind == "output") {
        val out = data?.str("output")?.ifEmpty { null } ?: htmlToText(ev.str("html"))
        if (out.isBlank()) return null
        val step = data?.opt("step")?.takeIf { it != JSONObject.NULL }?.toString() ?: ""
        return FeedItem("e$id", id, ev.optDouble("ts"), kind, agent,
            AnnotatedString("output of step $step".trim()), output = out)
    }
    val text = htmlToAnnotated(ev.str("html"), codeBg)
    if (text.isEmpty()) return null
    return FeedItem("e$id", id, ev.optDouble("ts"), kind, agent, text)
}

/** Apply an approval / approval_done event to the list of pending approvals. */
fun applyApprovalEvent(pending: List<Approval>, ev: JSONObject): List<Approval> {
    val data = ev.optJSONObject("data") ?: return pending
    val aid = data.str("aid").ifEmpty { return pending }
    return when (ev.str("kind")) {
        "approval" -> if (pending.any { it.aid == aid }) pending else pending + Approval(
            aid, data.str("command"), htmlToText(data.str("reason")),
            data.str("agent").ifEmpty { ev.str("agent").ifEmpty { "main" } })
        "approval_done" -> pending.filterNot { it.aid == aid }
        else -> pending
    }
}

/** Lead first, then each agent's children indented beneath it. Returns (agent, depth). */
fun treeOrder(fleet: List<AgentNode>): List<Pair<AgentNode, Int>> {
    val ids = fleet.map { it.id }.toSet()
    val kids = fleet.groupBy { it.parent?.takeIf { p -> p in ids && p != it.id } }
    val out = ArrayList<Pair<AgentNode, Int>>()
    val seen = HashSet<String>()
    fun walk(a: AgentNode, depth: Int) {
        if (!seen.add(a.id)) return
        out.add(a to depth)
        kids[a.id]?.forEach { walk(it, depth + 1) }
    }
    kids[null]?.forEach { walk(it, 0) }
    fleet.forEach { walk(it, 0) }       // anything left over (cycles) goes at the top level
    return out
}

// ---------- relay URL ----------
data class UrlCheck(val url: String?, val error: String?)

/** Accepts wss://host[/path] (or https://, or a bare host) and returns a clean wss:// URL.
 *  Plain ws:// / http:// is refused: the token must never travel unencrypted. */
fun normalizeRelayUrl(input: String): UrlCheck {
    var s = input.trim()
    if (s.isEmpty()) return UrlCheck(null, "Enter the relay address")
    val lower = s.lowercase()
    when {
        lower.startsWith("ws://") || lower.startsWith("http://") ->
            return UrlCheck(null, "Use a secure wss:// address — the token must not be sent unencrypted")
        lower.startsWith("https://") -> s = "wss://" + s.substring(8)
        lower.startsWith("wss://") -> s = "wss://" + s.substring(6)
        "://" in s -> return UrlCheck(null, "The address must start with wss://")
        else -> s = "wss://$s"
    }
    val uri = try { URI(s) } catch (e: Exception) { return UrlCheck(null, "That doesn't look like a valid address") }
    val host = uri.host ?: return UrlCheck(null, "The address is missing a host name")
    if (uri.rawUserInfo != null) return UrlCheck(null, "Don't put credentials in the address; use the token field")
    val path = uri.rawPath.orEmpty().trimEnd('/').ifEmpty { "/ws/app" }
    val port = if (uri.port > 0) ":${uri.port}" else ""
    val h = if (host.contains(':') && !host.startsWith("[")) "[$host]" else host
    return UrlCheck("wss://${h.lowercase()}$port$path", null)
}

fun fmtTok(n: Long) = when {
    n >= 1_000_000 -> "%.2fM".format(n / 1e6)
    n >= 1000 -> "%.1fk".format(n / 1e3)
    else -> n.toString()
}
