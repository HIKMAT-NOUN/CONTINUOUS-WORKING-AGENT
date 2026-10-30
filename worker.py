"""Background worker: runs queued tasks forever, works on the standing goal when idle,
enforces pause / daily token budget, and supports aborting the current task."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from agent import Agent
from db import today
from textfmt import h, truncate

if TYPE_CHECKING:
    from approvals import Approvals
    from config import Config
    from db import DB
    from llm import LLM
    from hub import Hub

log = logging.getLogger("worker")

GOAL_PROMPT = """Standing goal: {goal}

This is goal cycle #{cycle}. You are working on this goal in recurring cycles while the task \
queue is empty. Make concrete, incremental progress in this cycle (keep it well under your step \
limit), then stop and report: what you did, the current state, and the next step for the \
following cycle. Keep notes for future cycles in a file such as GOAL_NOTES.md in WORKDIR.
If the goal is fully achieved, include the exact phrase GOAL COMPLETE in your final report.

Reports from recent cycles (oldest first):
{history}"""


def fmt_tokens(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


class Worker:
    def __init__(self, cfg: "Config", db: "DB", llm: "LLM", hub: "Hub", approvals: "Approvals"):
        self.cfg = cfg
        self.db = db
        self.hub = hub
        self.approvals = approvals
        self.changed = asyncio.Event()       # new task / resume / goal change
        self.current: dict | None = None
        self.inner: asyncio.Task | None = None
        self.agent = Agent(cfg, db, llm, hub, approvals.request, self.wait_if_paused, self._on_step)

    # state ------------------------------------------------------------------------
    def poke(self) -> None:
        self.changed.set()

    @property
    def paused(self) -> bool:
        return self.db.get("paused") == "1"

    @property
    def pause_reason(self) -> str:
        return self.db.get("pause_reason", "") or ""

    def tokens_today(self) -> int:
        u = self.db.usage_today()
        return u["prompt_tokens"] + u["completion_tokens"]

    def budget_exceeded(self) -> bool:
        if self.db.get("budget_override_day") == today():
            return False
        return self.tokens_today() >= self.cfg.daily_token_budget

    def pause(self, reason: str = "manual") -> None:
        self.db.set("paused", "1")
        self.db.set("pause_reason", reason)
        self.poke()

    def resume(self, force: bool = False) -> str | None:
        """Returns an error message if resuming is not allowed."""
        if force:
            self.db.set("budget_override_day", today())
        elif self.budget_exceeded():
            return (f"Daily token budget is used up ({fmt_tokens(self.tokens_today())} / "
                    f"{fmt_tokens(self.cfg.daily_token_budget)}). It resets at 00:00 UTC. "
                    "Use /resume force to ignore the budget for the rest of today.")
        self.db.set("paused", "0")
        self.db.set("pause_reason", "")
        self.poke()
        return None

    async def wait_if_paused(self) -> None:
        """Blocks while paused. Also the single place that enforces the token budget."""
        announced = False
        while True:
            self.changed.clear()
            if not self.paused:
                if not self.budget_exceeded():
                    return
                self.pause("budget")
                self.hub.emit(
                    f"💸 <b>Daily token budget reached</b> ({fmt_tokens(self.tokens_today())} / "
                    f"{fmt_tokens(self.cfg.daily_token_budget)}). Worker paused; the current task "
                    "is saved and resumes automatically after 00:00 UTC. /resume force overrides.",
                    kind="alert")
                log.warning("daily token budget reached; pausing")
                announced = True
            elif self.pause_reason == "budget" and not self.budget_exceeded():
                self.resume()
                self.hub.emit("▶️ New day — token budget reset, worker resumed.", kind="alert")
                return
            if not announced and self.current:
                log.info("worker paused during task %s", self.current["id"])
                announced = True
            try:
                await asyncio.wait_for(self.changed.wait(), 60)
            except asyncio.TimeoutError:
                pass

    def _on_step(self, step: int, narration: str) -> None:
        if self.current:
            self.current["step"] = step
            self.current["action"] = narration

    # control ----------------------------------------------------------------------
    def stop_current(self) -> int | None:
        if self.inner and not self.inner.done() and self.current:
            self.inner.cancel()
            return self.current["id"]
        return None

    def set_goal(self, goal: str | None) -> None:
        self.db.set("goal", goal)
        self.db.set("goal_last_end", None)   # first cycle runs as soon as the queue is empty
        if goal:
            self.db.set("goal_cycle", "0")
        self.poke()

    # main loop ----------------------------------------------------------------------
    async def run_forever(self) -> None:
        for t in self.db.recover_running():
            self.hub.emit(f"♻️ Restarted — resuming task #{t['id']}: {h(truncate(t['description'], 200))}",
                          kind="task", task_id=t["id"])
        state = "paused" if self.paused else "running"
        self.hub.emit(f"🟢 Agent online ({h(self.cfg.model)}). Worker {state}, "
                           f"{self.db.count_pending()} task(s) queued.", kind="info")
        failures = 0
        while True:
            try:
                await self.wait_if_paused()
                self.changed.clear()
                task = self.db.next_pending()
                if task:
                    await self._run(task)
                    failures = 0
                    continue
                timeout = 300.0
                goal = self.db.get("goal")
                if goal:
                    last = float(self.db.get("goal_last_end") or 0)
                    due = last + self.cfg.goal_interval_min * 60
                    if time.time() >= due:
                        self._queue_goal_cycle(goal)
                        continue
                    timeout = min(timeout, max(1.0, due - time.time()))
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the worker alive no matter what
                failures += 1
                log.exception("worker loop error")
                self.hub.emit(f"⚠️ worker error: {h(truncate(str(e), 300))} (retrying)", kind="alert")
                await asyncio.sleep(min(5 * failures, 120))

    def _queue_goal_cycle(self, goal: str) -> None:
        cycle = int(self.db.get("goal_cycle") or 0) + 1
        self.db.set("goal_cycle", str(cycle))
        history = "\n\n".join(f"- cycle task #{r['id']} ({r['status']}): {truncate(r['result'] or '', 1500)}"
                              for r in self.db.recent_goal_results(5)) or "(none yet)"
        self.db.add_task(GOAL_PROMPT.format(goal=goal, cycle=cycle, history=history), kind="goal")

    async def _run(self, task: dict) -> None:
        tid = task["id"]
        self.db.start_task(tid)
        self.current = {"id": tid, "kind": task["kind"], "description": task["description"],
                        "step": task.get("steps") or 0, "action": "", "started": time.time()}
        if task["kind"] == "goal":
            title = f"🎯 Goal cycle (task #{tid}): {h(truncate(self.db.get('goal') or '', 200))}"
        else:
            title = f"▶️ Task #{tid}: {h(truncate(task['description'], 300))}"
        self.hub.emit(title, kind="task", level="always", task_id=tid, state="started",
                      description=task["description"], task_kind=task["kind"])
        log.info("task %s started", tid)
        self.inner = asyncio.create_task(self.agent.run(task), name=f"task-{tid}")
        try:
            await asyncio.wait({self.inner})
        except asyncio.CancelledError:   # service shutting down: leave it 'running' to resume later
            self.inner.cancel()
            await asyncio.gather(self.inner, return_exceptions=True)
            raise
        finally:
            self.current = None

        if self.inner.cancelled():
            self.db.finish_task(tid, "aborted", "aborted by user")
            self.hub.emit(f"🛑 Task #{tid} aborted.", kind="final", task_id=tid, status="aborted")
            log.info("task %s aborted", tid)
        elif self.inner.exception() is not None:
            err = self.inner.exception()
            log.error("task %s crashed", tid, exc_info=err)
            self.db.finish_task(tid, "failed", f"internal error: {err!r}")
            self.hub.emit(f"💥 Task #{tid} crashed: {h(truncate(repr(err), 400))}", kind="final",
                          task_id=tid, status="failed")
        else:
            out = self.inner.result()
            self.db.finish_task(tid, out.status, out.summary)
            icon = "🏁" if out.status == "done" else "⚠️"
            head = (f"{icon} <b>Task #{tid} {'done' if out.status == 'done' else 'stopped'}</b> · "
                    f"{out.steps} steps · {fmt_tokens(out.tokens)} tokens\n\n")
            self.hub.emit(head + h(out.summary), kind="final", task_id=tid, status=out.status,
                          summary=out.summary, steps=out.steps, tokens=out.tokens)
            log.info("task %s finished: %s", tid, out.status)
        self.inner = None

        if task["kind"] == "goal":
            self.db.set("goal_last_end", str(time.time()))
            res = (self.db.get_task(tid) or {}).get("result") or ""
            if "GOAL COMPLETE" in res and self.db.get("goal"):
                self.db.set("goal", None)
                self.hub.emit("🎉 Standing goal reported complete — goal cleared.", kind="alert")

    # reporting ------------------------------------------------------------------------
    def status_dict(self) -> dict:
        u = self.db.usage_today()
        cur = None
        if self.current:
            c = self.current
            cur = {"id": c["id"], "kind": c["kind"], "description": c["description"],
                   "step": c["step"], "action": c["action"], "started": c["started"]}
        return {
            "state": "paused" if self.paused else ("working" if self.current else "idle"),
            "pause_reason": self.pause_reason,
            "current": cur,
            "max_steps": self.cfg.max_steps_per_task,
            "queue": self.db.pending(50),
            "queue_count": self.db.count_pending(),
            "goal": self.db.get("goal"),
            "goal_interval_min": self.cfg.goal_interval_min,
            "tokens_today": u["prompt_tokens"] + u["completion_tokens"],
            "prompt_tokens": u["prompt_tokens"], "completion_tokens": u["completion_tokens"],
            "budget": self.cfg.daily_token_budget,
            "cost_today": u["cost"],
            "verbose": self.hub.verbosity,
            "model": self.cfg.model,
            "workdir": str(self.cfg.workdir),
        }

    def status_html(self) -> str:
        u = self.db.usage_today()
        used = u["prompt_tokens"] + u["completion_tokens"]
        if self.paused:
            state = "⏸ paused" + (" (daily budget)" if self.pause_reason == "budget" else "")
        elif self.current:
            state = "⚙️ working"
        else:
            state = "💤 idle"
        lines = [f"<b>Status:</b> {state}"]
        if self.current:
            c = self.current
            name = "goal cycle" if c["kind"] == "goal" else "task"
            mins = (time.time() - c["started"]) / 60
            lines.append(f"<b>Current:</b> {name} #{c['id']} — {h(truncate(c['description'], 200))}")
            lines.append(f"<b>Step:</b> {c['step']}/{self.cfg.max_steps_per_task} · {mins:.0f} min")
            if c["action"]:
                lines.append(f"<b>Last action:</b> {c['action']}")
        lines.append(f"<b>Queue:</b> {self.db.count_pending()} pending")
        goal = self.db.get("goal")
        lines.append(f"<b>Goal:</b> {h(truncate(goal, 200)) if goal else '—'}")
        lines.append(f"<b>Tokens today:</b> {fmt_tokens(used)} / {fmt_tokens(self.cfg.daily_token_budget)} "
                     f"(in {fmt_tokens(u['prompt_tokens'])}, out {fmt_tokens(u['completion_tokens'])})")
        lines.append(f"<b>Cost today:</b> ~${u['cost']:.4f}")
        lines.append(f"<b>Verbose:</b> {self.hub.verbosity} · <b>Model:</b> {h(self.cfg.model)}")
        return "\n".join(lines)
