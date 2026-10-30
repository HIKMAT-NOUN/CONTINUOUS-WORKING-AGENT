package com.myagent.app

import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ModelsTest {
    @Test fun relayUrlRequiresTls() {
        assertEquals("wss://relay.example/ws/app", normalizeRelayUrl(" wss://Relay.example/ws/app ").url)
        assertEquals("wss://relay.example/ws/app", normalizeRelayUrl("https://relay.example").url)
        assertEquals("wss://relay.example/ws/app", normalizeRelayUrl("relay.example/").url)
        assertEquals("wss://relay.example:8443/custom/ws", normalizeRelayUrl("wss://relay.example:8443/custom/ws?token=x").url)
        assertNull(normalizeRelayUrl("ws://relay.example/ws/app").url)
        assertNull(normalizeRelayUrl("http://relay.example").url)
        assertNull(normalizeRelayUrl("ftp://relay.example").url)
        assertNull(normalizeRelayUrl("wss://user:pw@relay.example").url)
        assertNull(normalizeRelayUrl("").url)
    }

    @Test fun htmlSubsetIsStyledAndDecoded() {
        val a = htmlToAnnotated("🔧 <b>step 2</b>: <code>ls -la &amp;&amp; echo &#x27;hi&#39;</code> &lt;ok&gt;")
        assertEquals("🔧 step 2: ls -la && echo 'hi' <ok>", a.text)
        assertTrue(a.spanStyles.any { it.item.fontWeight == FontWeight.SemiBold && a.text.substring(it.start, it.end) == "step 2" })
        assertTrue(a.spanStyles.any { it.item.fontFamily == FontFamily.Monospace })
        assertEquals("a\nb", htmlToText("a<br>b"))
        assertEquals("x", htmlToText("<script>x</script>"))       // unknown tags dropped, text kept
        assertEquals("", htmlToText("   "))
    }

    @Test fun outputEventsCarryTheirText() {
        val ev = JSONObject("""{"id":7,"ts":1.0,"kind":"output","html":"<pre>x</pre>","agent":"w1",
            "data":{"step":3,"output":"total 0\nfile"}}""")
        val f = eventToFeed(ev)!!
        assertEquals("output", f.kind); assertEquals("w1", f.agent); assertEquals("e7", f.key)
        assertEquals("output of step 3", f.text.text); assertEquals("total 0\nfile", f.output)
        assertNull(eventToFeed(JSONObject("""{"id":8,"kind":"step","html":""}""")))
    }

    @Test fun approvalsAppearAndClear() {
        val ask = JSONObject("""{"id":1,"kind":"approval","agent":"w2","html":"..",
            "data":{"aid":"a1","command":"rm -rf build","reason":"<b>deletes</b> files","agent":"w2"}}""")
        var p = applyApprovalEvent(emptyList(), ask)
        assertEquals(1, p.size); assertEquals("w2", p[0].agent); assertEquals("deletes files", p[0].reason)
        p = applyApprovalEvent(p, ask)                     // duplicate (catch-up overlap)
        assertEquals(1, p.size)
        p = applyApprovalEvent(p, JSONObject("""{"id":2,"kind":"approval_done","data":{"aid":"a1"}}"""))
        assertTrue(p.isEmpty())
    }

    @Test fun statusAndTree() {
        val s = parseStatus(JSONObject("""{"state":"working","queue_count":2,"tokens_today":1500,"budget":0,
            "current":{"id":4,"description":"build it","step":2,"action":"<code>ls</code>"},
            "fleet":[{"id":"main","role":"main","parent":null,"status":"working","step":2,"max_steps":30},
                     {"id":"reviewer1","role":"reviewer","parent":"main","status":"working"},
                     {"id":"w2","role":"worker","parent":"main","status":"done","result":"ok"},
                     {"id":"w3","role":"worker","parent":"w2","status":"working"}]}"""))
        assertEquals("ls", s.current!!.action)
        assertNull(s.fleet[0].parent)
        val tree = treeOrder(s.fleet).map { it.first.id to it.second }
        assertEquals(listOf("main" to 0, "reviewer1" to 1, "w2" to 1, "w3" to 2), tree)
        assertNotNull(parseStatus(JSONObject("{}")))
    }
}
