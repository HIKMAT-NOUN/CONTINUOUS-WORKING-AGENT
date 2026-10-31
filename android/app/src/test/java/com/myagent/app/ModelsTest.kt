package com.myagent.app

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

/** Parses real messages captured from the Python agent (src/test/resources). */
class ModelsTest {
    private fun res(name: String) = javaClass.classLoader!!.getResource(name)!!.readText()

    @Test fun parsesNewStateFields() {
        val s = parseStatus(JSONObject(res("state.json")).getJSONObject("state"))
        assertEquals("high", s.reasoning)
        assertTrue(s.review)
        assertEquals(0, s.maxSteps)                 // 0 = unlimited
        assertEquals(2, s.maxWorkers)
        assertEquals("45", fmtNum(s.goalIntervalMin))
        assertEquals("keep tests green", s.goal)
        assertEquals(1, s.queue)
        assertNull(s.currentId)
        assertEquals(listOf(Approval("30e0d7", "rm -rf build/", "deletes files outside the workdir", "w1")), s.pendingApprovals)
        assertTrue(s.recent.isEmpty())
    }

    @Test fun parsesRecentTasks() {
        val st = JSONObject().put("recent", JSONArray().put(JSONObject()
            .put("id", 7).put("kind", "task").put("description", "fix it").put("status", "done").put("steps", 3).put("tokens", 1200)))
        assertEquals(listOf(RecentTask(7, "task", "fix it", "done", 3, 1200)), parseStatus(st).recent)
        assertEquals("auto", parseStatus(JSONObject()).reasoning)   // missing -> default
    }

    @Test fun approvalAndClearEvents() {
        val evs = JSONArray(res("events.json"))
        val kinds = (0 until evs.length()).map { evs.getJSONObject(it).getString("kind") }
        assertEquals(listOf("approval", "approval_done", "clear"), kinds)
        val approval = evs.getJSONObject(0)
        assertNull("approvals get a card, not a feed line", eventToFeed(approval))
        val a = parseApproval(approval.getJSONObject("data"))
        assertEquals("30e0d7", a.aid); assertEquals("rm -rf build/", a.command)
        assertEquals("w1", approval.getString("agent"))
        assertEquals("30e0d7", evs.getJSONObject(1).getJSONObject("data").getString("aid"))
        assertEquals("clear", eventToFeed(evs.getJSONObject(2))!!.kind)
    }

    @Test fun failedResultIsMarked() {
        val ev = JSONObject().put("id", 1).put("kind", "result").put("html", "❌ error")
            .put("data", JSONObject().put("ok", false))
        assertEquals(false, eventToFeed(ev)!!.ok)
    }

    @Test fun quickCommandsRoundTrip() {
        val list = listOf(QuickCmd("Deploy", "/task deploy \"it\""), QuickCmd("Stop", "/stop"))
        assertEquals(list, quickFromJson(quickToJson(list)))
        assertNull(quickFromJson("not json"))
        assertEquals(listOf("Pause", "Resume", "Status", "Stop"), DEFAULT_QUICK.map { it.label })
    }

    @Test fun htmlToTextDecodesOnce() {
        assertEquals("a <b> & &lt;", htmlToText("<i>a</i> &lt;b&gt; &amp; &amp;lt;"))
    }

    @Test fun onlyTlsUrls() {
        assertTrue(RelayClient.isSecureUrl("wss://relay.example/ws/app"))
        assertFalse(RelayClient.isSecureUrl("ws://relay.example/ws/app"))
        assertFalse(RelayClient.isSecureUrl("https://relay.example"))
    }

    @Test fun uploadCapFitsOkHttpMessageLimit() {
        val base64 = (MAX_UPLOAD_BYTES + 2) / 3 * 4
        assertTrue(base64 + 4096 < 16L * 1024 * 1024)
    }
}
