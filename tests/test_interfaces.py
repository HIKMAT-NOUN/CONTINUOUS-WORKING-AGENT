"""Commands, hub verbosity routing, the web API/WebSocket and the console view."""
import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from approvals import Approvals
from commands import Commands
from console import ConsoleView
from db import DB
from hub import Hub, html_to_terminal, visible
from web import make_app
from tests.test_agent import ScriptedLLM, call, cfg, run_until  # noqa: F401  (fixture)
from worker import Worker

TOKEN = "web-token-for-tests"


def build(cfg, llm=None):
    db = DB(cfg.db_path)
    hub = Hub(db, cfg.secrets + [TOKEN])
    ap = Approvals(hub, 5)
    w = Worker(cfg, db, llm or ScriptedLLM([]), hub, ap)
    return db, hub, ap, w, Commands(db, w, ap)


async def test_commands(cfg):
    db, hub, ap, w, c = build(cfg)
    assert "Task #1 queued" in await c.handle("/task do a thing")
    assert "Task #2 queued" in await c.handle("plain text is a task")
    assert "#1" in await c.handle("/queue") and "#2" in await c.handle("/queue")
    assert "paused" in await c.handle("/pause")
    assert w.paused
    assert "resumed" in await c.handle("/resume")
    assert "Standing goal set" in await c.handle("/goal keep docs tidy")
    assert db.get("goal") == "keep docs tidy"
    assert "cleared" in await c.handle("/goal clear")
    assert "full" in await c.handle("/verbose full")
    assert hub.verbosity == "full"
    assert "Unknown command" in await c.handle("/nope")
    assert "No approval is pending" in await c.handle("/approve")
    assert "Nothing is running" in await c.handle("/stop")
    assert "Tokens today" in await c.handle("/status")


def test_visibility():
    assert visible("always", "off") and not visible("steps", "off")
    assert visible("steps", "steps") and not visible("full", "steps")
    assert visible("full", "full")
    assert html_to_terminal("<b>x</b> &lt;y&gt; <code>z</code>", color=False) == "x <y> z"


def test_console_translate():
    v = ConsoleView(lambda: "steps")
    assert v.translate("y") == "y"  # no approval pending: it's just text (a task)
    v.show({"id": 1, "ts": 0, "kind": "approval", "level": "always", "html": "a", "data": {"aid": "ab12"}})
    assert v.translate("y") == "/approve ab12"
    assert v.translate("no") == "/deny ab12"
    v.show({"id": 2, "ts": 0, "kind": "approval_done", "level": "always", "html": "d", "data": {"aid": "ab12"}})
    assert v.translate("y") == "y"


async def test_web_auth_api_and_ws(cfg):
    llm = ScriptedLLM([("Listing.", [call("list_dir")]), ("3 files.", None)])
    db, hub, ap, w, c = build(cfg, llm)
    app = make_app(TOKEN, db, hub, w, c)
    async with TestClient(TestServer(app)) as client:
        # unauthenticated
        r = await client.get("/")
        assert r.status == 401 and "token" in await r.text()
        assert (await client.get("/api/state")).status == 401
        assert (await client.post("/login", data={"token": "wrong"})).status == 401
        # token in URL sets the cookie
        r = await client.get(f"/?token={TOKEN}")
        assert r.status == 200 and "Agent Console" in await r.text()
        # cross-origin POST is refused even with the cookie
        r = await client.post("/api/command", json={"text": "/status"}, headers={"Origin": "http://evil.example"})
        assert r.status == 403

        ws = await client.ws_connect("/ws")
        hist = json.loads((await ws.receive()).data)
        assert hist["type"] == "history"
        await ws.send_str(json.dumps({"type": "command", "text": "list the files"}))
        wt = asyncio.create_task(w.run_forever())
        seen = []
        while not any(e["kind"] == "final" for e in seen):
            msg = json.loads((await asyncio.wait_for(ws.receive(), 5)).data)
            if msg["type"] == "event":
                seen.append(msg["event"])
            elif msg["type"] == "reply":
                assert "Task #1 queued" in msg["html"]
        kinds = [e["kind"] for e in seen]
        for k in ("task", "thinking", "step", "result", "output", "final"):
            assert k in kinds, k
        wt.cancel()
        await ws.close()

        st = await (await client.get("/api/state")).json()
        assert st["recent"][0]["status"] == "done"
        detail = await (await client.get("/api/tasks/1")).json()
        assert detail["steps"][0]["tool"] == "list_dir" and "messages" not in detail["task"]
        # bearer auth for API clients (cli.py)
        r = await client.post("/api/command", json={"text": "/queue"}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert "Queue is empty" in (await r.json())["reply"]
        assert TOKEN not in json.dumps(db.events_after(0, 1000))


async def test_stopall_and_review_toggle(cfg):
    db, hub, ap, w, c = build(cfg)
    await c.handle("/task one"); await c.handle("/task two")
    assert db.count_pending() == 2
    msg = await c.handle("/stopall")
    assert "cleared 2" in msg and db.count_pending() == 0
    assert "Nothing to stop" in await c.handle("/stopall")
    # auto-review toggle (default on from cfg fixture? fixture sets review=False)
    assert "off" in (await c.handle("/review")).lower() or "on" in (await c.handle("/review")).lower()
    await c.handle("/review on"); assert w.review_enabled is True
    await c.handle("/review off"); assert w.review_enabled is False
    # the review tool is hidden from the lead when review is off and no workers
    assert "request_review" not in [t["function"]["name"] for t in w.agent.tools.specs()]


async def test_runtime_settings_and_unlimited_steps(cfg):
    db, hub, ap, w, c = build(cfg)
    # max steps editable + unlimited
    assert "40" in await c.handle("/maxsteps 7") or w.max_steps == 7
    assert w.max_steps == 7 and w.effective_max_steps == 7
    await c.handle("/maxsteps off")
    assert w.max_steps == 0 and w.effective_max_steps == 1_000_000
    # interval + workers
    await c.handle("/interval 10"); assert w.goal_interval == 10
    await c.handle("/workers 5"); assert w.max_workers == 5 and w.spawner.can_spawn is True
    await c.handle("/workers 0"); assert w.max_workers == 0 and w.spawner.can_spawn is False
    # status reflects runtime values
    st = w.status_dict()
    assert st["max_steps"] == 0 and st["goal_interval_min"] == 10 and st["max_workers"] == 0
    # new project clears goal + queue + events
    await c.handle("/goal keep tidy"); await c.handle("/task a")
    assert db.get("goal") and db.count_pending() == 1
    await c.handle("/newproject")
    assert db.get("goal") is None and db.count_pending() == 0
