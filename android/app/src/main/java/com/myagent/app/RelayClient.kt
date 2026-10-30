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
 *  so the agent machine needs no open port. Reconnects on its own with backoff. */
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
    }

    private val http = OkHttpClient.Builder()
        .pingInterval(25, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()
    private var ws: WebSocket? = null
    private val scheduler: ScheduledExecutorService = Executors.newSingleThreadScheduledExecutor()
    private var backoffSec = 2L
    private var lastId = 0L
    private var closed = false

    fun connect() {
        closed = false
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
                    }
                } catch (e: Exception) {
                    Log.w("relay", "bad message", e)
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
        scheduler.schedule({ open() }, delay, TimeUnit.SECONDS)
    }

    /** Send a command (plain text or /command). Returns the ref used, for matching the reply. */
    fun sendCommand(text: String): String {
        val ref = System.nanoTime().toString()
        val payload = JSONObject()
            .put("type", "command").put("text", text).put("ref", ref)
        ws?.send(payload.toString())
        return ref
    }

    fun close() {
        closed = true
        ws?.close(1000, "bye")
        scheduler.shutdownNow()
    }
}
