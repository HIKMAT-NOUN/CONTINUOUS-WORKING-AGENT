package com.myagent.app

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/** Talks to the relay from the phone. Both the agent and this app dial out to the relay,
 *  so the agent machine needs no open port. Reconnects on its own with backoff.
 *  Only wss:// (TLS) URLs are accepted, so the token is never sent in the clear. */
class RelayClient(
    private val baseUrl: String,   // wss://host/ws/app
    private val token: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onConnected()
        fun onDisconnected(reason: String)
        fun onHello(agentOnline: Boolean, events: JSONArray, state: JSONObject?)
        fun onEvent(ev: JSONObject)
        fun onState(state: JSONObject)
        fun onReply(ref: String?, html: String)
        fun onPresence(agentOnline: Boolean)
        fun onUploadAck(ref: String?, files: JSONArray?, error: String?)
    }

    companion object {
        fun isSecureUrl(url: String) = url.trim().lowercase().startsWith("wss://")
    }

    private val http = OkHttpClient.Builder()
        .pingInterval(25, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()
    @Volatile private var ws: WebSocket? = null
    private val scheduler: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor()
    private var backoffSec = 2L
    private var lastId = 0L
    @Volatile private var closed = false

    fun connect() {
        closed = false
        if (!isSecureUrl(baseUrl)) {          // never send the token over plain ws://
            listener.onDisconnected("relay URL must start with wss://")
            return
        }
        open()
    }

    private fun open() {
        if (closed) return
        val sep = if (baseUrl.contains("?")) "&" else "?"
        val url = "$baseUrl${sep}after=$lastId"
        val req = Request.Builder().url(url).addHeader("Authorization", "Bearer $token").build()
        ws = http.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                backoffSec = 2
                listener.onConnected()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val m = JSONObject(text)
                    when (m.optString("type")) {
                        "hello" -> {
                            val events = m.optJSONArray("events") ?: JSONArray()
                            bumpLast(events)
                            listener.onHello(m.optBoolean("agent_online"), events,
                                m.optJSONObject("state"))
                        }
                        "event" -> {
                            val ev = m.optJSONObject("event") ?: return
                            lastId = maxOf(lastId, ev.optLong("id"))
                            listener.onEvent(ev)
                        }
                        "state" -> m.optJSONObject("state")?.let(listener::onState)
                        "reply" -> listener.onReply(
                            if (m.isNull("ref")) null else m.optString("ref"),
                            m.optString("html"))
                        "presence" -> listener.onPresence(m.optBoolean("agent_online"))
                        "upload_ack" -> listener.onUploadAck(
                            if (m.isNull("ref")) null else m.optString("ref"),
                            m.optJSONArray("files"),
                            if (m.has("error") && !m.isNull("error")) m.optString("error") else null)
                    }
                } catch (e: Exception) {
                    Log.w("relay", "bad message: ${e.javaClass.simpleName}")   // never log payloads
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = scheduleReconnect("closed")
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                scheduleReconnect(t.message ?: "connection failed")
        })
    }

    private fun bumpLast(events: JSONArray) {
        for (i in 0 until events.length()) {
            lastId = maxOf(lastId, events.optJSONObject(i)?.optLong("id") ?: 0)
        }
    }

    private fun scheduleReconnect(reason: String) {
        listener.onDisconnected(reason)
        if (closed) return
        val delay = backoffSec
        backoffSec = minOf(backoffSec * 2, 30)
        try { scheduler.schedule({ open() }, delay, TimeUnit.SECONDS) } catch (_: Exception) {}
    }

    private fun newRef() = System.nanoTime().toString()

    /** Send a command (plain text or /command). Returns the ref used, or null if not connected. */
    fun sendCommand(text: String): String? {
        val ref = newRef()
        val payload = JSONObject().put("type", "command").put("text", text).put("ref", ref)
        return if (ws?.send(payload.toString()) == true) ref else null
    }

    /** Send a file as base64. The agent saves it under WORKDIR/uploads and answers with an
     *  upload_ack carrying the same ref. Returns false if it couldn't be queued. */
    fun sendUpload(name: String, base64: String, ref: String): Boolean {
        val payload = JSONObject().put("type", "upload").put("name", name).put("data", base64).put("ref", ref)
        return ws?.send(payload.toString()) == true
    }

    fun close() {
        closed = true
        ws?.close(1000, "bye")
        scheduler.shutdownNow()
    }
}
