"""One command processor shared by every interface (Telegram, web, CLI).

handle("/task do x") -> HTML reply for the person who typed it. Plain text without a
leading slash is treated as a new task.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from textfmt import h, truncate

if TYPE_CHECKING:
    from approvals import Approvals
    from db import DB
    from worker import Worker

HELP = """<b>Commands</b> (plain text without a / is added as a task)
/task &lt;text&gt; — add a task to the queue
/goal &lt;text&gt; — set standing goal (/goal clear to remove, /goal to show)
/status — current task, step, queue, tokens and cost today
/queue — list pending tasks
/pause, /resume — pause or resume the worker (/resume force ignores the daily budget)
/stop — abort the current task
/stopall — abort the current task AND clear the whole queue
/review on|off — turn the automatic review agent on or off
/reasoning auto|low|medium|high|max|off — set the model's thinking depth
/approve &lt;id&gt;, /deny &lt;id&gt; — answer a pending approval (id optional if only one)
/verbose off|steps|full — narration level for Telegram and the CLI
/log — last 20 steps"""

NAMES = ("task", "goal", "status", "queue", "pause", "resume", "stop", "stopall", "cancelall",
         "review", "reasoning", "approve", "deny", "verbose", "log", "help", "start")


class Commands:
    def __init__(self, db: "DB", worker: "Worker", approvals: "Approvals"):
        self.db = db
        self.worker = worker
        self.approvals = approvals

    async def handle(self, text: str, source: str = "user") -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if not text.startswith("/"):
            return self.task(text)
        name, _, arg = text[1:].partition(" ")
        name = name.split("@", 1)[0].lower()   # Telegram adds /cmd@botname in groups
        arg = arg.strip()
        fn = getattr(self, f"c_{name}", None)
        if name not in NAMES or fn is None:
            return f"Unknown command /{h(name)}.\n\n{HELP}"
        return fn(arg, source)

    # individual commands -------------------------------------------------------------------
    def task(self, text: str) -> str:
        tid = self.db.add_task(text)
        self.worker.poke()
        ahead = self.db.count_pending() - 1 + (1 if self.worker.current else 0)
        extra = " (worker is paused)" if self.worker.paused else (f" · {ahead} ahead of it" if ahead else "")
        return f"📥 Task #{tid} queued{extra}."

    def c_task(self, arg, source):
        return self.task(arg) if arg else "Usage: /task &lt;description&gt;"

    def c_help(self, arg, source):
        return HELP

    c_start = c_help

    def c_goal(self, arg, source):
        if not arg:
            g = self.db.get("goal")
            return f"🎯 Goal: {h(g)}" if g else "No standing goal. Set one with /goal &lt;text&gt;."
        if arg.lower() in ("clear", "none", "off"):
            self.worker.set_goal(None)
            return "🎯 Standing goal cleared."
        self.worker.set_goal(arg)
        return (f"🎯 Standing goal set. When the queue is empty I'll work on it every "
                f"{self.worker.cfg.goal_interval_min:g} min.")

    def c_status(self, arg, source):
        return self.worker.status_html()

    def c_queue(self, arg, source):
        rows = self.db.pending(30)
        if not rows:
            return "📭 Queue is empty."
        lines = [f"<b>Pending ({self.db.count_pending()})</b>"]
        for r in rows:
            tag = "🎯 " if r["kind"] == "goal" else ""
            lines.append(f"#{r['id']} {tag}{h(truncate(r['description'].splitlines()[0], 120))}")
        return "\n".join(lines)

    def c_pause(self, arg, source):
        self.worker.pause("manual")
        cur = f" Task #{self.worker.current['id']} will continue after /resume." if self.worker.current else ""
        return f"⏸ Worker paused (takes effect before the next LLM call).{cur}"

    def c_resume(self, arg, source):
        err = self.worker.resume(force=arg.lower() == "force")
        return f"⛔ {h(err)}" if err else "▶️ Worker resumed."

    def c_stop(self, arg, source):
        tid = self.worker.stop_current()
        return f"🛑 Stopping task #{tid}…" if tid else "Nothing is running."

    def c_stopall(self, arg, source):
        cleared, tid = self.worker.stop_all()
        parts = []
        if tid:
            parts.append(f"aborting current task #{tid}")
        if cleared:
            parts.append(f"cleared {cleared} queued task(s)")
        return "🧹 " + ("; ".join(parts) + ".") if parts else "Nothing to stop — no task running and the queue is empty."

    c_cancelall = c_stopall

    REASONING_LEVELS = ("auto", "low", "medium", "high", "max", "off")

    def c_reasoning(self, arg, source):
        a = arg.strip().lower()
        if a in self.REASONING_LEVELS:
            self.worker.set_reasoning(a)
            note = " (MiniMax-M3 only; M3.1-Flash rejects it)" if a == "off" else ""
            return f"🧠 Reasoning depth set to <b>{a}</b>.{note}"
        return (f"🧠 Reasoning depth is <b>{h(self.worker.reasoning)}</b>. "
                "Usage: /reasoning auto|low|medium|high|max|off")

    def c_review(self, arg, source):
        a = arg.strip().lower()
        if a in ("on", "off"):
            self.worker.set_review(a == "on")
            return f"🔎 Automatic review turned <b>{a}</b>."
        return (f"🔎 Automatic review is <b>{'on' if self.worker.review_enabled else 'off'}</b>. "
                "Usage: /review on|off")

    def _answer(self, arg: str, approved: bool, source: str) -> str:
        aid = arg or self.approvals.only_pending_id()
        if not aid:
            if not self.approvals.pending:
                return "No approval is pending."
            return "Several approvals are pending; specify the id: " + ", ".join(self.approvals.pending)
        if not self.approvals.resolve(aid, approved, by=source):
            return f"No pending approval with id {h(aid)}."
        return ""   # the hub broadcasts the verdict to everyone

    def c_approve(self, arg, source):
        return self._answer(arg, True, source)

    def c_deny(self, arg, source):
        return self._answer(arg, False, source)

    def c_verbose(self, arg, source):
        level = arg.lower()
        if level not in ("off", "steps", "full"):
            return f"Verbose is <b>{h(self.worker.hub.verbosity)}</b>. Usage: /verbose off|steps|full"
        self.db.set("verbose", level)
        return f"🔊 Verbose set to <b>{level}</b>."

    def c_log(self, arg, source):
        rows = self.db.last_steps(20)
        if not rows:
            return "No steps logged yet."
        icons = {"ok": "✅", "error": "❌", "denied": "🚫"}
        lines = ["<b>Last steps</b>"]
        for r in rows:
            ts = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
            lines.append(f"{icons.get(r['status'], '•')} {ts} #{r['task_id']}.{r['step_no']} "
                         f"<b>{h(r['tool'])}</b> <code>{h(truncate(r['args'] or '', 90))}</code>")
        return "\n".join(lines)
