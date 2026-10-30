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


def call(name, **args):
    return {"id": f"c_{name}", "type": "function",
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
                  db_path=tmp_path / "t.db", log_dir=tmp_path / "logs")


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
    async def auto(cmd, reason):
        return approve
    w.agent.tools.approve = auto
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
    await run_until(lambda: w.current and w.current["id"] == 2 and w.current["step"] == 1)
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
