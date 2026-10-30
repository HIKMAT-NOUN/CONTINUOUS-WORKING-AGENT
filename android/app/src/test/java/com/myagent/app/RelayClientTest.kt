package com.myagent.app

import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.tls.HandshakeCertificates
import okhttp3.tls.HeldCertificate
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/** A server-side socket that answers the client's close frame, like aiohttp does. */
private open class ServerSide : WebSocketListener() {
    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(1000, null) }
}

/** Drives RelayClient against a local TLS server that speaks the relay protocol. */
class RelayClientTest {
    private lateinit var server: MockWebServer
    private lateinit var http: OkHttpClient
    private val got = LinkedBlockingQueue<String>()

    private val listener = object : RelayClient.Listener {
        override fun onConnecting(attempt: Int) {}
        override fun onConnected() { got.add("connected") }
        override fun onRetrying(reason: String, delaySec: Long) { got.add("retrying") }
        override fun onAuthFailed(code: Int) { got.add("auth:$code") }
        override fun onHello(agentOnline: Boolean, events: JSONArray, state: JSONObject?) {
            got.add("hello:$agentOnline:${events.length()}:${state?.optString("state")}")
        }
        override fun onEvent(ev: JSONObject) { got.add("event:${ev.optLong("id")}") }
        override fun onState(state: JSONObject) { got.add("state") }
        override fun onReply(ref: String?, html: String) { got.add("reply:$ref:$html") }
        override fun onPresence(agentOnline: Boolean) { got.add("presence:$agentOnline") }
    }

    @Before fun setUp() {
        val cert = HeldCertificate.Builder().addSubjectAlternativeName("localhost").build()
        val serverCerts = HandshakeCertificates.Builder().heldCertificate(cert).build()
        val clientCerts = HandshakeCertificates.Builder().addTrustedCertificate(cert.certificate).build()
        server = MockWebServer().apply { useHttps(serverCerts.sslSocketFactory(), false); start() }
        http = OkHttpClient.Builder()
            .sslSocketFactory(clientCerts.sslSocketFactory(), clientCerts.trustManager)
            .followRedirects(false).build()
    }

    @After fun tearDown() { server.shutdown() }

    private fun url() = "wss://localhost:${server.port}/ws/app"
    private fun next() = got.poll(5, TimeUnit.SECONDS)

    @Test fun speaksTheRelayProtocol() {
        val fromApp = LinkedBlockingQueue<String>()
        server.enqueue(MockResponse().withWebSocketUpgrade(object : ServerSide() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                webSocket.send("""{"type":"hello","agent_online":true,"events":[{"id":41,"kind":"task","html":"t"}],"state":{"state":"idle"}}""")
                webSocket.send("""{"type":"event","event":{"id":42,"kind":"step","html":"s"}}""")
                webSocket.send("""{"type":"presence","agent_online":false}""")
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                fromApp.add(text)
                val ref = JSONObject(text).getString("ref")
                webSocket.send("""{"type":"reply","ref":"$ref","html":"ok"}""")
            }
        }))
        val c = RelayClient(url(), "s3cret", listener, startAfter = 40, http = http)
        c.connect()
        assertEquals("connected", next())
        assertEquals("hello:true:1:idle", next())
        assertEquals("event:42", next())
        assertEquals("presence:false", next())
        assertEquals(42, c.lastEventId)

        val req = server.takeRequest(5, TimeUnit.SECONDS)!!
        assertEquals("Bearer s3cret", req.getHeader("Authorization"))
        assertEquals("/ws/app?after=40", req.path)              // token never in the URL

        val ref = c.sendCommand("/approve a1")
        assertNotNull(ref)
        val sent = JSONObject(fromApp.poll(5, TimeUnit.SECONDS)!!)
        assertEquals("command", sent.getString("type"))
        assertEquals("/approve a1", sent.getString("text"))
        assertEquals("reply:$ref:ok", next())
        c.close()
        assertNull(c.sendCommand("/stop"))                       // closed: nothing goes out
    }

    @Test fun badTokenStopsRetrying() {
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"error":"unauthorized"}"""))
        val c = RelayClient(url(), "wrong", listener, http = http)
        c.connect()
        assertEquals("auth:401", next())
        assertNull(got.poll(1500, TimeUnit.MILLISECONDS))       // no reconnect storm
        assertEquals(1, server.requestCount)
        c.close()
    }

    @Test fun dropsAreRetried() {
        server.enqueue(MockResponse().setResponseCode(502))
        server.enqueue(MockResponse().withWebSocketUpgrade(object : ServerSide() {}))
        val c = RelayClient(url(), "t", listener, http = http)
        c.connect()
        assertEquals("retrying", next())
        c.retryNow()                                            // skip the backoff wait
        assertEquals("connected", next())
        c.close()
    }

    @Test(expected = IllegalArgumentException::class)
    fun refusesPlainWebSockets() { RelayClient("ws://localhost/ws/app", "t", listener) }

    @Test fun closeSilencesCallbacks() {
        server.enqueue(MockResponse().withWebSocketUpgrade(object : ServerSide() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                Thread.sleep(300); webSocket.send("""{"type":"presence","agent_online":true}""")
            }
        }))
        val c = RelayClient(url(), "t", listener, http = http)
        c.connect()
        assertEquals("connected", next())
        c.close()
        assertTrue(got.poll(1000, TimeUnit.MILLISECONDS) == null)
    }
}
