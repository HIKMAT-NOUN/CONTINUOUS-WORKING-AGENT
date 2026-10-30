package com.myagent.app

import android.util.Log
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit

/** Talks to the relay from the phone. Both the agent and this app dial out to the relay,
 *  so the agent machine needs no open port. Reconnects on its own with backoff.
 *
 *  Callbacks arrive on OkHttp's threads. After [close] no further callbacks are made, and a
 *  socket that has been replaced never reports anything, so a stale connection can't flip
 *  the UI back to "disconnected". */
class RelayClient(
    baseUrl: String,               // wss://host/ws/app
    private val token: String,
    private val listener: Listener,
    startAfter: Long = 0,          // resume from this event id (0 = full catch-up)
    private val http: OkHttpClient = sharedHttp,   // tests inject one that trusts a local cert
) {
    interface Listener {
        fun onConnecting(attempt: Int)
        fun onConnected()
        fun onRetrying(reason: String, delaySec: Long)
        fun onAuthFailed(code: Int)
        fun onHello(agentOnline: Boolean, events: JSONArray, state: JSONObject?)
        fun onEvent(ev: JSONObject)
        fun onState(state: JSONObject)
        fun onReply(ref: String?, html: String)
        fun onPresence(agentOnline: Boolean)
    }

    private val httpBase = run {
        require(baseUrl.startsWith("wss://")) { "relay URL must use wss://" }
        ("https://" + baseUrl.removePrefix("wss://")).toHttpUrl()   // OkHttp upgrades https -> wss
    }
    private val scheduler = Executors.newSingleThreadScheduledExecutor { r ->
        Thread(r, "relay-reconnect").apply { isDaemon = true }
    }
    private val lock = Any()
    private var ws: WebSocket? = null
    private var open = false
    private var pending: ScheduledFuture<*>? = null
    private var backoffSec = 1L
    private var attempt = 0
    @Volatile private var lastId = startAfter
    @Volatile private var closed = false

    val lastEventId: Long get() = lastId
    val isOpen: Boolean get() = synchronized(lock) { open }

    fun connect() = synchronized(lock) { if (!closed && ws == null) openLocked() }

    /** Skip the backoff wait and try again right away (e.g. the network just came back). */
    fun retryNow() = synchronized(lock) {
        if (closed || open) return@synchronized
        pending?.cancel(false); pending = null
        backoffSec = 1
        ws?.cancel(); ws = null
        openLocked()
    }

    private fun openLocked() {
        attempt++
        listener.onConnecting(attempt)
        val url = httpBase.newBuilder().setQueryParameter("after", lastId.toString()).build()
        val req = Request.Builder().url(url).header("Authorization", "Bearer $token").build()
        ws = http.newWebSocket(req, Callbacks())
    }

    private inner class Callbacks : WebSocketListener() {
        private fun live(w: WebSocket) = !closed && synchronized(lock) { ws === w }

        override fun onOpen(webSocket: WebSocket, response: Response) {
            synchronized(lock) {
                if (closed || ws !== webSocket) return
                open = true; backoffSec = 1; attempt = 0
            }
            listener.onConnected()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            if (!live(webSocket)) return
            val m = try { JSONObject(text) } catch (e: Exception) {
                Log.w(TAG, "ignored a malformed relay message"); return     // never log content
            }
            when (m.optString("type")) {
                "hello" -> {
                    val events = m.optJSONArray("events") ?: JSONArray()
                    for (i in 0 until events.length()) bump(events.optJSONObject(i))
                    listener.onHello(m.optBoolean("agent_online"), events, m.optJSONObject("state"))
                }
                "event" -> m.optJSONObject("event")?.let { bump(it); listener.onEvent(it) }
                "state" -> m.optJSONObject("state")?.let(listener::onState)
                "reply" -> listener.onReply(if (m.isNull("ref")) null else m.optString("ref"), m.optString("html"))
                "presence" -> listener.onPresence(m.optBoolean("agent_online"))
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
            lost(webSocket, "relay closed the connection")
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) =
            lost(webSocket, "connection closed")

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            val code = response?.code ?: 0
            response?.close()
            if (code == 401 || code == 403) {
                synchronized(lock) {
                    if (closed || ws !== webSocket) return
                    ws = null; open = false
                }
                listener.onAuthFailed(code)          // don't hammer the relay with a bad token
                return
            }
            lost(webSocket, if (code > 0) "relay answered HTTP $code" else friendly(t))
        }
    }

    private fun lost(w: WebSocket, reason: String) {
        val delay = synchronized(lock) {
            if (closed || ws !== w) return
            ws = null; open = false
            val d = backoffSec
            backoffSec = minOf(backoffSec * 2, 30)
            pending = scheduler.schedule({ synchronized(lock) { if (!closed && ws == null) openLocked() } },
                d, TimeUnit.SECONDS)
            d
        }
        listener.onRetrying(reason, delay)
    }

    private fun bump(ev: JSONObject?) {
        val id = ev?.optLong("id") ?: return
        if (id > lastId) lastId = id
    }

    /** Send a command (plain text or /command). Returns the ref used, or null if not connected. */
    fun sendCommand(text: String): String? {
        val ref = "a" + System.nanoTime().toString(36)
        val payload = JSONObject().put("type", "command").put("text", text).put("ref", ref)
        val sent = synchronized(lock) { open && ws?.send(payload.toString()) == true }
        return if (sent) ref else null
    }

    fun close() {
        val w = synchronized(lock) {
            closed = true
            pending?.cancel(false); pending = null
            open = false
            ws.also { ws = null }
        }
        w?.close(1000, "bye")
        scheduler.shutdownNow()
    }

    companion object {
        private const val TAG = "relay"

        // One shared client: connection pool + threads are reused across reconnects.
        private val sharedHttp: OkHttpClient = OkHttpClient.Builder()
            .pingInterval(20, TimeUnit.SECONDS)      // detects dead mobile connections
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .followRedirects(false)                  // never re-send the token somewhere else
            .followSslRedirects(false)
            .build()

        private fun friendly(t: Throwable): String = when (t) {
            is java.net.UnknownHostException -> "can't find the relay host (no internet or wrong address)"
            is java.net.SocketTimeoutException -> "the relay didn't answer in time"
            is java.net.ConnectException -> "can't reach the relay"
            is javax.net.ssl.SSLException -> "secure connection failed (TLS certificate problem?)"
            is java.io.EOFException -> "connection dropped"
            else -> t.javaClass.simpleName.ifEmpty { "connection failed" }
        }
    }
}
