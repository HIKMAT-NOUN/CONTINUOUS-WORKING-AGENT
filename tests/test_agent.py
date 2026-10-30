"""End-to-end worker test with a scripted LLM and a fake Telegram bot: the README's
'list the files in WORKDIR and tell me how many there are' scenario."""
import asyncio
import json
from dataclasses import replace

import pytest

from approvals import Approvals
from config import Config
from db import DB
from llm import LLMResponse
from hub import Hub
from worker import Worker


_call_seq = [0]
def call(name, **args):
    _call_seq[0] += 1
    return {"id": f"c{_call_seq[0]}_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.seen = []

    async def chat(self, messages, tools=None):
        self.seen.append(list(messages))
        if not self.script:  # behave like a hung request
            await asyncio.Event().wait()
        content, calls = self.script.pop(0)
        msg = {"role": "assistant", "content": content or None}
        if calls:
            msg["tool_calls"] = calls
        return LLMResponse(msg, content or "", calls or [], 100, 20)


@pytest.fixture
def cfg(tmp_path):
    wd = tmp_path / "work"
    wd.mkdir()
    for n in ("a.txt", "b.txt", "c.py"):
        (wd / n).write_text("x")
    return Config(api_base_url="http://x", api_key="sk-supersecret", model="m",
                  telegram_bot_token="123:tokensecret", allowed_user_id=1, workdir=wd,
                  db_path=tmp_path / "t.db", log_dir=tmp_path / "logs", review=False)


class Collector:
    """Hub sink that records every event, like an always-full-verbosity interface."""
    def __init__(self):
        self.sent = []

    def deliver(self, ev):
        self.sent.append({"text": ev.html, "kind": ev.kind, "data": ev.data})

    def start(self):
        pass

    async def stop(self):
        pass


def build(cfg, llm, approve=True):
    db = DB(cfg.db_path)
    hub = Hub(db, cfg.secrets)
    bot = n = Collector()
    hub.subscribe(bot)
    ap = Approvals(hub, 5)
    w = Worker(cfg, db, llm, hub, ap)
    async def auto(cmd, reason, agent_id="main"):
        return approve
    w.agent.tools.approve = auto
    w.approvals.request = auto
    return db, bot, n, w


async def run_until(pred, timeout=5):
    async def loop():
        while not pred():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(loop(), timeout)


async def test_list_files_task(cfg):
    llm = ScriptedLLM([
        ("I'll list WORKDIR.", [call("list_dir", path=".")]),
        ("Double-checking with ls.", [call("shell", command="ls -1 | wc -l")]),
        ("There are 3 files in WORKDIR: a.txt, b.txt, c.py.", None),
    ])
    db, bot, n, w = build(cfg, llm)
    db.add_task("list the files in WORKDIR and tell me how many there are")
    n.start()
    wt = asyncio.create_task(w.run_forever())
    await run_until(lambda: db.get_task(1)["status"] == "done")
    await run_until(lambda: any("There are 3 files" in m["text"] for m in bot.sent))
    wt.cancel()
    await n.stop()
    text = "\n".join(m["text"] for m in bot.sent)
    for expected in ("Task #1", "🧠 thinking: I'll list WORKDIR.",
                     "📂 listing:", "✅ ok — 3 files, 0 dirs", "⚙️ running: <code>ls -1 | wc -l</code>",
                     "✅ ok — exit 0", "Task #1 done", "There are 3 files"):
        assert expected in text, expected
    assert "3 files, 0 directories" in llm.seen[1][-1]["content"]
    steps = db.last_steps()
    assert [s["tool"] for s in steps] == ["list_dir", "shell"]
    assert db.usage_today()["prompt_tokens"] == 300


async def test_denied_and_secret_never_sent(cfg):
    llm = ScriptedLLM([
        ("Cleaning up", [call("shell", command="rm -rf /"), call("shell", command="echo sk-supersecret")]),
        ("Done", None),
    ])
    db, bot, n, w = build(cfg, llm, approve=False)
    db.add_task("t")
    n.start()
    wt = asyncio.create_task(w.run_forever())
    await run_until(lambda: db.get_task(1)["status"] == "done")
    await run_until(lambda: any("Task #1 done" in m["text"] for m in bot.sent))
    wt.cancel()
    await n.stop()
    text = "\n".join(m["text"] for m in bot.sent)
    assert "🚫 denied" in text
    assert "supersecret" not in text
    assert [s["status"] for s in db.last_steps()] == ["denied", "ok"]


async def test_step_limit_and_stop(cfg):
    cfg = replace(cfg, max_steps_per_task=2)
    llm = ScriptedLLM([("", [call("list_dir")])] * 2 + [("summary here", None)])
    db, bot, n, w = build(cfg, llm)
    db.add_task("loop")
    n.start()
    wt = asyncio.create_task(w.run_forever())
    await run_until(lambda: db.get_task(1)["status"] == "failed")
    assert "Step limit (2)" in db.get_task(1)["result"]

    # /stop aborts a long-running shell command
    llm.script = [("", [call("shell", command="sleep 30")])]
    db.add_task("slow")
    w.poke()
    await run_until(lambda: w.current and w.current["id"] == 2 and w.fleet.agents["main"]["step"] == 1)
    await asyncio.sleep(0.2)
    assert w.stop_current() == 2
    await run_until(lambda: db.get_task(2)["status"] == "aborted")
    wt.cancel()
    await n.stop()


async def test_budget_pauses(cfg):
    cfg = replace(cfg, daily_token_budget=100)
    llm = ScriptedLLM([("", [call("list_dir")]), ("done", None)])
    db, bot, n, w = build(cfg, llm)
    db.add_task("t")
    n.start()
    wt = asyncio.create_task(w.run_forever())
    await run_until(lambda: w.paused and w.pause_reason == "budget")
    assert db.get_task(1)["status"] == "running"          # saved, waiting, not lost
    assert w.resume() is not None                          # refused: still over budget
    assert w.resume(force=True) is None
    await run_until(lambda: db.get_task(1)["status"] == "done")
    wt.cancel()
    await n.stop()


async def test_resume_after_restart(cfg):
    llm = ScriptedLLM([("", [call("list_dir")])])
    db, bot, n, w = build(cfg, llm)
    db.add_task("t")
    n.start()
    wt = asyncio.create_task(w.run_forever())
    await run_until(lambda: db.get_task(1)["steps"] == 1 and db.get_task(1)["messages"])
    # the next chat() hangs; cancelling now simulates a crash mid-task
    wt.cancel()
    await asyncio.gather(wt, return_exceptions=True)
    await n.stop()

    llm2 = ScriptedLLM([("finished after restart", None)])
    db2, bot2, n2, w2 = build(cfg, llm2)
    n2.start()
    wt2 = asyncio.create_task(w2.run_forever())
    await run_until(lambda: db2.get_task(1)["status"] == "done")
    wt2.cancel()
    await n2.stop()
    msgs = llm2.seen[0]
    assert msgs[0]["role"] == "system" and any(m["role"] == "tool" for m in msgs)
    assert "restarted" in msgs[-1]["content"]


def test_think_blocks_hidden():
    from agent import visible_text
    assert visible_text("<think>secret plan\nmore</think>\nListing files now.") == "Listing files now."
    assert visible_text("<think>unterminated") == ""
    assert visible_text("plain") == "plain"


async def test_periodic_recap(cfg):
    from dataclasses import replace
    cfg = replace(cfg, progress_every=2, max_steps_per_task=10)
    # 5 tool steps then a final message; recap fires after step 2 and step 4.
    llm = ScriptedLLM([("", [call("list_dir")])] * 5 + [("all checked", None)])
    # intersperse recap replies: the scripted LLM pops in order, so recaps must be scripted too.
    db, bot, n, w = build(cfg, llm)
    # Replace with an interleaved script: tool, tool, RECAP, tool, tool, RECAP, tool, final
    llm.script = [
        ("", [call("list_dir")]), ("", [call("list_dir")]), ("Looked around twice.", None),
        ("", [call("list_dir")]), ("", [call("list_dir")]), ("Halfway through.", None),
        ("", [call("list_dir")]), ("done now", None),
    ]
    db.add_task("explore")
    n.start()
    wt = asyncio.create_task(w.run_forever())
    await run_until(lambda: db.get_task(1)["status"] == "done")
    wt.cancel()
    recaps = [e for e in bot.sent if e["kind"] == "progress"]
    assert [r["data"]["text"] for r in recaps] == ["Looked around twice.", "Halfway through."]


async def test_spawn_workers_and_review(cfg):
    """Lead spawns two helpers, waits for them, then a reviewer — all visible as agents."""
    from dataclasses import replace
    cfg = replace(cfg, review=False, max_workers=3)
    # Lead script: spawn two, wait, then finish. Helpers each do one list_dir then report.
    # ScriptedLLM is shared, so interleaving is nondeterministic; drive each agent by routing
    # on the system prompt instead.
    class RoleLLM:
        def __init__(self):
            self.seen = []
            self.lead_step = 0
        async def chat(self, messages, tools=None):
            self.seen.append(list(messages))
            role_sys = messages[0]["content"]
            if "You are a helper agent spawned" in role_sys:
                # a helper: do nothing, just report
                return LLMResponse({"role": "assistant", "content": "helper done: found files"},
                                   "helper done: found files", [], 50, 10)
            if "You are the review agent." in role_sys:
                return LLMResponse({"role": "assistant", "content": "VERDICT: APPROVED looks fine"},
                                   "VERDICT: APPROVED looks fine", [], 50, 10)
            # the lead
            self.lead_step += 1
            if self.lead_step == 1:
                calls = [call("spawn_worker", title="A", instructions="scan A"),
                         call("spawn_worker", title="B", instructions="scan B")]
                return LLMResponse({"role": "assistant", "content": "Delegating two scans.",
                                    "tool_calls": calls}, "Delegating two scans.", calls, 100, 20)
            if self.lead_step == 2:
                c = [call("wait_for_workers", agent_ids=[])]
                return LLMResponse({"role": "assistant", "content": "Waiting.", "tool_calls": c},
                                   "Waiting.", c, 100, 20)
            if self.lead_step == 3:
                c = [call("request_review", subject="the scans")]
                return LLMResponse({"role": "assistant", "content": "Reviewing.", "tool_calls": c},
                                   "Reviewing.", c, 100, 20)
            return LLMResponse({"role": "assistant", "content": "Both scans done and approved."},
                               "Both scans done and approved.", [], 100, 20)

    llm = RoleLLM()
    db, bot, n, w = build(cfg, llm)
    # spawn tool ids must be unique per call; patch call() ids
    db.add_task("investigate with helpers")
    wt = asyncio.create_task(w.run_forever())
    await run_until(lambda: db.get_task(1)["status"] == "done", timeout=10)
    wt.cancel()
    await asyncio.gather(wt, return_exceptions=True)

    snap = {a["id"]: a for a in w.fleet.snapshot()}
    roles = sorted(a["role"] for a in snap.values())
    assert roles.count("worker") == 2 and roles.count("reviewer") == 1 and "main" in snap
    assert all(a["status"] in ("done", "idle") for a in snap.values())
    # events are tagged by agent
    agents_in_events = {e["data"].get("agent") for e in bot.sent if e["kind"] == "agent"}
    assert any(aid.startswith("w") for aid in snap) and any(aid.startswith("r") for aid in snap)
    # steps were logged per-agent
    assert {s["agent"] for s in db.last_steps(50)} >= {"main"}
