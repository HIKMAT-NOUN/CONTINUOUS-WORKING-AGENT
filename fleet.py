"""Multi-agent fleet: a lead agent that can spawn helper agents and ask a review agent.

- Fleet: in-memory registry of every agent session (lead, helpers, reviewers) with live
  status, used by /api/fleet and the dashboard's agent tree.
- AgentBuilder: builds an Agent bound to the shared LLM / hub / approvals / budget and a
  given identity.
- Spawner: the delegation tools the lead agent calls (spawn_worker / wait_for_workers /
  request_review). Helpers and reviewers get no spawner, so the tree is at most 2 deep.

Everything shares one token budget, one pause gate and one approval channel, so limits and
/stop apply to the whole tree.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable

from agent import (Agent, Job, MAIN_PROMPT, WORKER_PROMPT, REVIEW_PROMPT, BASE_RULES,
                   Outcome, access_rules)
from textfmt import h, truncate
from tools import Tools, ToolResult

if TYPE_CHECKING:
    from approvals import Approvals
    from config import Config
    from db import DB
    from hub import Hub
    from llm import LLM

log = logging.getLogger("fleet")

ROLE_ICON = {"main": "🧠", "worker": "🤖", "reviewer": "🔎"}


class Fleet:
    """Live registry of agent sessions. Mutated in place; the dashboard reads snapshots and
    also reacts to the 'agent' lifecycle events emitted here."""

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.agents: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}   # background tasks for helpers / reviewers
        self._counter = 0

    def new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def register(self, agent_id: str, role: str, parent: str | None, title: str,
                 max_steps: int) -> dict:
        ctx = {"id": agent_id, "role": role, "parent": parent, "title": title,
               "status": "working", "step": 0, "max_steps": max_steps, "action": "",
               "thinking": "", "started": time.time(), "finished": None, "result": ""}
        self.agents[agent_id] = ctx
        self._emit(agent_id, f"{ROLE_ICON.get(role, '•')} <b>{h(role)} agent</b> "
                   f"[{h(agent_id)}] started: {h(truncate(title, 160))}", state="spawned")
        return ctx

    def update(self, agent_id: str, **fields) -> None:
        ctx = self.agents.get(agent_id)
        if not ctx:
            return
        ctx.update({k: v for k, v in fields.items() if v is not None or k in ("action", "thinking")})

    def finish(self, agent_id: str, status: str, result: str) -> None:
        ctx = self.agents.get(agent_id)
        if not ctx:
            return
        ctx.update(status=status, finished=time.time(), result=result, action="", thinking="")
        if ctx["role"] != "main":   # 'main' finishing a job just goes idle; don't announce
            icon = "✅" if status == "done" else ("🛑" if status == "aborted" else "⚠️")
            self._emit(agent_id, f"{icon} <b>{h(ctx['role'])} agent</b> [{h(agent_id)}] "
                       f"{h(status)}: {h(truncate(ctx['title'], 120))}", state="finished", status=status)

    def set_main_status(self, status: str, title: str = "") -> None:
        ctx = self.agents.get("main")
        if ctx:
            ctx.update(status=status, title=title or ctx["title"])
            if status != "working":
                ctx.update(action="", thinking="", step=0)

    def _emit(self, agent_id: str, html: str, **data) -> None:
        self.hub.emit(html, kind="agent", level="always", agent=agent_id, **data)

    def snapshot(self) -> list[dict]:
        order = {"main": 0, "reviewer": 1, "worker": 2}
        return sorted((dict(c) for c in self.agents.values()),
                      key=lambda c: (order.get(c["role"], 3), c["started"]))

    def active_workers(self, parent: str) -> list[str]:
        return [a["id"] for a in self.agents.values()
                if a["role"] == "worker" and a["parent"] == parent and a["status"] == "working"]

    def cancel_all_children(self) -> None:
        for aid, task in list(self.tasks.items()):
            if not task.done():
                task.cancel()


class AgentBuilder:
    """Creates Agent instances that share the lead agent's resources and budget."""

    def __init__(self, cfg: "Config", db: "DB", llm: "LLM", hub: "Hub", approvals: "Approvals",
                 wait_if_paused: Callable, fleet: Fleet):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.hub = hub
        self.approvals = approvals
        self.wait_if_paused = wait_if_paused
        self.fleet = fleet

    def build(self, agent_id: str, role: str, spawner=None) -> Agent:
        tools = Tools(self.cfg.workdir, self.cfg.secrets, self.approvals.request, lambda t: None,
                      self.cfg.shell_timeout, self.cfg.full_access, self.cfg.approvals,
                      agent_id=agent_id, spawner=spawner)
        return Agent(self.cfg, self.db, self.llm, self.hub, tools, self.wait_if_paused,
                     agent_id=agent_id, role=role,
                     on_update=lambda **kw: self.fleet.update(agent_id, **kw))

    def prompt(self, template: str, max_steps: int) -> str:
        base = BASE_RULES.format(workdir=self.cfg.workdir, max_steps=max_steps,
                                 access=access_rules(self.cfg))
        return template.format(base=base)


class Spawner:
    """Delegation tools for the lead agent. Returns ToolResult objects."""

    def __init__(self, fleet: Fleet, builder: AgentBuilder, cfg: "Config",
                 parent_task_id_getter: Callable[[], int], review_enabled: Callable[[], bool] | None = None,
                 max_workers: Callable[[], int] | None = None):
        self.fleet = fleet
        self.builder = builder
        self.cfg = cfg
        self._parent_task_id = parent_task_id_getter
        self.results: dict[str, Outcome] = {}
        self._review_enabled = review_enabled
        self._max_workers = max_workers

    @property
    def max_workers(self) -> int:
        return self._max_workers() if self._max_workers else self.cfg.max_workers

    @property
    def can_spawn(self) -> bool:
        return self.max_workers > 0

    @property
    def can_review(self) -> bool:
        return self._review_enabled() if self._review_enabled else self.cfg.review

    async def _run_child(self, aid: str, role: str, job: Job) -> Outcome:
        agent = self.builder.build(aid, role, spawner=None)   # helpers/reviewers cannot delegate
        try:
            out = await agent.run(job)
        except asyncio.CancelledError:
            self.fleet.finish(aid, "aborted", "cancelled")
            raise
        except Exception as e:
            log.exception("agent %s crashed", aid)
            out = Outcome("failed", f"internal error: {e!r}")
        self.results[aid] = out
        self.fleet.finish(aid, out.status, out.summary)
        return out

    async def spawn(self, title: str, instructions: str, parent_id: str) -> ToolResult:
        running = self.fleet.active_workers(parent_id)
        if len(running) >= self.max_workers:
            return ToolResult(False, f"error: you already have {len(running)} helper(s) running "
                              f"(max {self.max_workers}). Call wait_for_workers first.",
                              "worker limit reached")
        aid = self.fleet.new_id("w")
        self.fleet.register(aid, "worker", parent_id, title, self.cfg.worker_max_steps)
        job = Job(task_id=self._parent_task_id(), description=instructions, kind="worker",
                  system=self.builder.prompt(WORKER_PROMPT, self.cfg.worker_max_steps),
                  max_steps=self.cfg.worker_max_steps)
        self.fleet.tasks[aid] = asyncio.create_task(self._run_child(aid, "worker", job), name=f"agent-{aid}")
        return ToolResult(True, f"started helper {aid} titled '{truncate(title, 80)}'. It is working "
                          f"in parallel; call wait_for_workers(['{aid}']) to read its report.",
                          f"spawned {aid}")

    async def wait(self, agent_ids: list[str], parent_id: str) -> ToolResult:
        ids = [a for a in agent_ids if a in self.fleet.tasks] or self.fleet.active_workers(parent_id) \
            or [a for a in self.fleet.agents if self.fleet.agents[a]["role"] == "worker"
                and self.fleet.agents[a]["parent"] == parent_id]
        ids = list(dict.fromkeys(ids))
        if not ids:
            return ToolResult(False, "error: no helpers to wait for. Spawn one first.", "no helpers")
        tasks = [self.fleet.tasks[a] for a in ids if a in self.fleet.tasks]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        parts = []
        for a in ids:
            out = self.results.get(a)
            ctx = self.fleet.agents.get(a, {})
            title = ctx.get("title", a)
            if out:
                parts.append(f"=== helper {a} ('{title}') — {out.status} ===\n{out.summary}")
            else:
                parts.append(f"=== helper {a} ('{title}') — no result ===")
        return ToolResult(True, "\n\n".join(parts), f"{len(ids)} helper(s) finished")

    async def review(self, subject: str, parent_id: str) -> ToolResult:
        aid = self.fleet.new_id("r")
        self.fleet.register(aid, "reviewer", parent_id, truncate(subject, 120), self.cfg.review_max_steps)
        job = Job(task_id=self._parent_task_id(), description="Review this work:\n" + subject,
                  kind="review", system=self.builder.prompt(REVIEW_PROMPT, self.cfg.review_max_steps),
                  max_steps=self.cfg.review_max_steps)
        out = await self._run_child(aid, "reviewer", job)
        return ToolResult(out.status == "done", f"Review verdict:\n{out.summary}",
                          "review done")
