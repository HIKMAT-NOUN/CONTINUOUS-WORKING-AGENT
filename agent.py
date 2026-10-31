"""The agent loop for one task: LLM -> tool calls -> results -> LLM ... with live narration."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from llm import LLM, LLMError, reasoning_params
from hub import Hub
from textfmt import code, h, truncate
from tools import Tools, first_line

if TYPE_CHECKING:
    from config import Config
    from db import DB

log = logging.getLogger("agent")

BASE_RULES = """Working directory (WORKDIR): {workdir}
{access}
- Before each batch of tool calls, write ONE short sentence saying what you are about to do \
and why. It is shown to the human as your plan.
- Use send_update for notable milestones in long tasks; don't spam it.
- You have at most {max_steps} tool calls for this job. Be efficient.
- When the job is complete (or cannot be completed), reply WITHOUT any tool call. That reply \
is your final report: state the result first, concisely, then key details."""

MAIN_PROMPT = """You are the lead autonomous agent running 24/7 on a Linux server. A human \
supervises you through a live dashboard, Telegram and a terminal, and sees every step you take.

{base}

Delegation (optional — use it when a job has independent parts or benefits from parallel work):
- spawn_worker(title, instructions): start a helper agent that works in parallel on a focused \
sub-task and reports back. Give it a short title and self-contained instructions. It cannot \
spawn further helpers. Returns an agent id.
- wait_for_workers(agent_ids): wait for one or more helpers you spawned and read their results.
- request_review(subject): ask the review agent for a second opinion before you finish \
something important or risky. It inspects the work and returns a verdict and notes.
Keep the number of helpers small and purposeful; you do the final synthesis and report."""

WORKER_PROMPT = """You are a helper agent spawned by the lead agent to do one focused sub-task \
in parallel with others. Work only on what you were asked.

{base}

- You cannot spawn further helpers. Finish by reporting your result clearly to the lead agent."""

REVIEW_PROMPT = """You are the review agent. The lead agent has asked you to review some work \
before it is accepted. Inspect what was done — read files, check outputs, re-run quick checks — \
and judge whether it is correct, complete and safe.

{base}

- Be concise and specific. Finish with a verdict line starting with VERDICT: APPROVED or \
VERDICT: CHANGES NEEDED, followed by the key reasons and any concrete fixes."""


@dataclass
class Job:
    task_id: int                      # numeric id used for step logging / persistence
    description: str
    kind: str = "task"                # task | goal | worker | review
    system: str = ""                  # system prompt; filled by the factory
    max_steps: int = 40
    persist: bool = False             # save conversation to the tasks table (main queue tasks only)
    recap: bool = False               # emit periodic plain-language recaps (main only)
    messages: list | None = None      # resume from a saved conversation
    steps: int = 0
    tokens: int = 0

_THINK = re.compile(r"<think>.*?(?:</think>|$)", re.S | re.I)


def visible_text(content: str) -> str:
    """Hide reasoning blocks (<think>…</think>, e.g. MiniMax/DeepSeek) from what the human sees.
    The full content stays in the conversation history sent back to the model."""
    return _THINK.sub("", content or "").strip()


def access_rules(cfg: "Config") -> str:
    if cfg.full_access:
        rules = ("- You have FULL ACCESS to this machine: shell() starts in WORKDIR but may work anywhere, "
                 "and read_file/write_file/list_dir accept any path (relative paths are relative to WORKDIR). "
                 "Shell commands are non-interactive (no stdin): use sudo -n and flags like -y.")
    else:
        rules = ("- shell() runs bash in WORKDIR. read_file/write_file/list_dir only work inside WORKDIR.")
    if cfg.approvals:
        what = "deleting recursively, sudo, piping downloads into a shell, ..." + (
            "" if cfg.full_access else " or touching paths outside WORKDIR")
        rules += (f"\n- Risky shell commands ({what}) are sent to the human for approval. If one is "
                  "denied, do not retry it; find another way or explain why it is needed.")
    else:
        rules += "\n- Commands run without approval. Be careful with destructive or irreversible actions."
    return rules


@dataclass
class Outcome:
    status: str     # done | failed
    summary: str
    steps: int = 0
    tokens: int = 0


def describe_call(name: str, args: dict) -> str:
    """The pre-call narration line."""
    if name == "shell":
        return f"⚙️ running: {code(args.get('command', ''), 400)}"
    if name == "web_fetch":
        return f"🌐 fetching: {h(truncate(args.get('url', ''), 400))}"
    if name == "write_file":
        verb = "appending" if args.get("append") else "writing"
        return f"📄 {verb}: {code(args.get('path', ''), 200)}"
    if name == "read_file":
        return f"📖 reading: {code(args.get('path', ''), 200)}"
    if name == "list_dir":
        return f"📂 listing: {code(args.get('path') or '.', 200)}"
    return f"🔧 {h(name)}: {code(json.dumps(args, ensure_ascii=False), 300)}"


class Agent:
    def __init__(self, cfg: "Config", db: "DB", llm: LLM, hub: Hub, tools: Tools,
                 wait_if_paused: Callable[[], Awaitable[None]],
                 agent_id: str = "main", role: str = "main",
                 on_update: Callable[..., None] | None = None):
        self.cfg = cfg
        self.db = db
        self.llm = llm
        self.hub = hub
        self.tools = tools
        self.wait_if_paused = wait_if_paused
        self.agent_id = agent_id
        self.role = role
        self.on_update = on_update or (lambda **k: None)
        tools.update = self._model_update   # route the model's send_update through this agent

    # narration helpers ----------------------------------------------------------
    def say(self, text_html: str, level: str = "steps", kind: str = "info", **data) -> None:
        """level: 'always' (finals, alerts), 'steps' (narration) or 'full' (tool output)."""
        self.hub.emit(text_html, kind=kind, level=level, agent=self.agent_id, **data)

    def _model_update(self, text: str) -> None:
        self.say(f"💬 {h(text)}", kind="update", text=text)

    def _reasoning(self) -> dict:
        return reasoning_params(self.db.get("reasoning") or self.cfg.reasoning)

    def account(self, prompt: int, completion: int, provider_cost: float | None) -> int:
        cost = provider_cost if provider_cost is not None else (
            prompt * self.cfg.price_input_per_m + completion * self.cfg.price_output_per_m) / 1e6
        self.db.add_usage(prompt, completion, cost)
        return prompt + completion

    # main loop --------------------------------------------------------------------
    async def run(self, job: Job) -> Outcome:
        task_id = job.task_id
        messages = job.messages
        steps, tokens = job.steps, job.tokens
        actions: list[str] = []       # short lines fed to the periodic plain-language recap
        last_recap = steps
        if messages:
            messages.append({"role": "user", "content": "(The agent process was restarted. "
                             "Continue the job from where you left off; verify the state of "
                             "anything that might have been interrupted.)"})
        else:
            messages = [{"role": "system", "content": job.system},
                        {"role": "user", "content": job.description}]
        tools_specs = self.tools.specs()

        while True:
            await self.wait_if_paused()  # also enforces the daily token budget
            self.on_update(step=steps, thinking="")
            try:
                resp = await self.llm.chat(messages, tools_specs, self._reasoning())
            except LLMError as e:
                return Outcome("failed", f"LLM error: {e}", steps, tokens)
            tokens += self.account(resp.prompt_tokens, resp.completion_tokens, resp.cost)
            messages.append(resp.message)

            if not resp.tool_calls:
                if job.persist:
                    self.db.save_progress(task_id, messages, steps, tokens)
                return Outcome("done", visible_text(resp.content) or "(no final message)", steps, tokens)

            plan = visible_text(resp.content)
            if plan:
                self.on_update(thinking=first_line(plan, 300))
                self.say(f"🧠 thinking: {h(truncate(first_line(plan, 300), 300))}",
                         kind="thinking", task_id=task_id, text=plan)

            for tc in resp.tool_calls:
                name = tc["function"]["name"]
                if steps >= job.max_steps:
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": "error: step limit reached, call not executed"})
                    continue
                steps += 1
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                except ValueError as e:
                    result_text = f"error: invalid JSON arguments: {e}"
                    self.say(f"❌ {h(name)}: invalid arguments from model", kind="result",
                             task_id=task_id, step=steps, ok=False)
                    self.db.log_step(task_id, steps, name, tc["function"]["arguments"], "error",
                                     result_text, 0, self.agent_id)
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result_text})
                    continue

                narration = describe_call(name, args)
                self.on_update(step=steps, action=narration)
                if name != "send_update":
                    self.say(narration, kind="step", task_id=task_id, step=steps, tool=name, args=args)
                t0 = time.monotonic()
                res = await self.tools.call(name, args)
                dt = time.monotonic() - t0
                status = "denied" if res.denied else ("ok" if res.ok else "error")
                self.db.log_step(task_id, steps, name, args, status, res.output, dt, self.agent_id)
                log.info("[%s] step %s %s %s -> %s (%s)", self.agent_id, steps, name,
                         truncate(json.dumps(args, ensure_ascii=False), 300), status, res.summary)
                if name != "send_update":
                    icon = "✅ ok" if res.ok else ("🚫 denied" if res.denied else "❌ error")
                    self.say(f"{icon}" + (f" — {h(truncate(res.summary, 300))}" if res.summary else ""),
                             kind="result", task_id=task_id, step=steps, ok=res.ok, denied=res.denied,
                             duration=round(dt, 2))
                    if res.output:
                        self.say(f"<pre>{h(truncate(res.output, 500))}</pre>", level="full", kind="output",
                                 task_id=task_id, step=steps, output=truncate(res.output, 6000))
                actions.append(f"{name}({truncate(json.dumps(args, ensure_ascii=False), 120)}) "
                               f"-> {status}: {truncate(res.summary, 120)}")
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": res.output})

            if job.persist:
                self.db.save_progress(task_id, messages, steps, tokens)

            if job.recap and self.cfg.progress_every and steps - last_recap >= self.cfg.progress_every \
                    and steps < job.max_steps:
                tokens += await self._recap(job, actions)
                last_recap = steps

            if steps >= job.max_steps:
                summary, extra = await self._final_summary(messages)
                return Outcome("failed", f"Step limit ({job.max_steps}) reached.\n\n{summary}",
                               steps, tokens + extra)

    async def _recap(self, job: Job, actions: list[str]) -> int:
        """One friendly sentence about the recent steps, for a non-technical watcher.
        A small, separate LLM call (does not carry the full tool output)."""
        recent = actions[-(self.cfg.progress_every * 2):]
        ask = [
            {"role": "system", "content": "You narrate an AI agent's work for a non-technical person "
             "watching live. Given the task and the agent's recent actions, reply with ONE short, "
             "plain-English sentence (max 25 words) about what it has been doing and where it is now. "
             "No tool names, no markdown, no preamble."},
            {"role": "user", "content": "Task: " + truncate(job.description, 300) + "\n\nRecent actions:\n- "
             + "\n- ".join(recent)},
        ]
        try:
            resp = await self.llm.chat(ask, None)
        except LLMError:
            return 0
        line = visible_text(resp.content)
        if line:
            self.say(f"🧭 {h(truncate(line, 300))}", kind="progress", level="steps",
                     task_id=job.task_id, text=line)
        return self.account(resp.prompt_tokens, resp.completion_tokens, resp.cost)

    async def _final_summary(self, messages: list[dict]) -> tuple[str, int]:
        ask = messages + [{"role": "user", "content": "You have reached the step limit and cannot "
                           "call more tools. Summarize what you accomplished and what remains."}]
        try:
            resp = await self.llm.chat(ask, None)
            used = self.account(resp.prompt_tokens, resp.completion_tokens, resp.cost)
            return visible_text(resp.content), used
        except LLMError as e:
            return f"(could not get a summary: {e})", 0
