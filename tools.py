"""Tools the model can call. Each returns a ToolResult; none of them raise."""
from __future__ import annotations

import asyncio
import os
import signal
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from safety import PathError, danger_reason, resolve_in_workdir

LLM_OUTPUT_LIMIT = 12000   # chars of tool output fed back to the model
MAX_CAPTURE = 2_000_000    # bytes of shell output captured


@dataclass
class ToolResult:
    ok: bool
    output: str            # full text returned to the model (already truncated to a sane size)
    summary: str = ""      # one line for the Telegram result message
    denied: bool = False


BASE_TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "shell",
        "description": "Run a bash command inside WORKDIR (the current directory). Returns exit "
                       "code and combined stdout/stderr. Risky commands require human approval.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "bash command line"},
            "timeout": {"type": "integer", "description": "seconds, default 120, max 1800"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file inside WORKDIR.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "character offset to start at"},
            "max_chars": {"type": "integer", "description": "default 12000"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite (or append to) a text file inside WORKDIR. "
                       "Parent directories are created.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "append": {"type": "boolean", "description": "append instead of overwrite"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir",
        "description": "List entries of a directory inside WORKDIR (default: WORKDIR itself).",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "default '.'"}}}}},
    {"type": "function", "function": {
        "name": "web_fetch",
        "description": "HTTP GET a URL and return the page as plain text (truncated).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer", "description": "default 8000"}},
            "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "send_update",
        "description": "Send the human a short free-form progress note on Telegram.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
]

# Advertised only to the lead agent, and only when delegation is enabled.
WORKER_TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "spawn_worker",
        "description": "Start a helper agent that works in parallel on a focused sub-task and "
                       "reports back. It cannot spawn further helpers. Returns its agent id so you "
                       "can wait_for_workers on it. Keep helpers few and purposeful.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "short label, e.g. 'scan logs'"},
            "instructions": {"type": "string", "description": "self-contained task for the helper"}},
            "required": ["title", "instructions"]}}},
    {"type": "function", "function": {
        "name": "wait_for_workers",
        "description": "Wait for helper agents you spawned to finish and return their reports.",
        "parameters": {"type": "object", "properties": {
            "agent_ids": {"type": "array", "items": {"type": "string"},
                          "description": "ids returned by spawn_worker; empty = all your helpers"}}}}},
]

REVIEW_TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "request_review",
        "description": "Ask the review agent to inspect some work and return a verdict (APPROVED / "
                       "CHANGES NEEDED) with reasons. Use before finishing something important.",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string", "description": "what to review and where to look"}},
            "required": ["subject"]}}},
]


def clip(text: str, limit: int = LLM_OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} chars truncated] ...\n{text[-tail:]}"


def first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
             "article", "header", "footer", "pre", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(l.split()) for l in "".join(self.parts).splitlines())
        out, blank = [], False
        for l in lines:
            if l:
                out.append(l)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        pass
    return p.text()


ApproveFn = Callable[[str, str], Awaitable[bool]]
UpdateFn = Callable[[str], None]


class Tools:
    def __init__(self, workdir: Path, secrets: list[str], approve: ApproveFn, update: UpdateFn,
                 default_shell_timeout: int = 120, full_access: bool = False, approvals: bool = True,
                 agent_id: str = "main", spawner=None):
        self.workdir = workdir
        self.secrets = secrets
        self.approve = approve
        self.update = update
        self.default_shell_timeout = default_shell_timeout
        self.full_access = full_access
        self.approvals = approvals
        self.agent_id = agent_id
        self.spawner = spawner            # set only for agents allowed to delegate (the lead)

    def specs(self) -> list[dict]:
        specs = list(BASE_TOOL_SPECS)
        if self.spawner:
            if getattr(self.spawner, "can_spawn", True):
                specs += WORKER_TOOL_SPECS
            if getattr(self.spawner, "can_review", True):
                specs += REVIEW_TOOL_SPECS
        return specs

    def _path(self, path: str) -> Path:
        return resolve_in_workdir(self.workdir, path, self.full_access)

    async def call(self, name: str, args: dict) -> ToolResult:
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            return ToolResult(False, f"error: unknown tool {name!r}", f"unknown tool {name}")
        try:
            return await fn(**args)
        except TypeError as e:
            return ToolResult(False, f"error: bad arguments for {name}: {e}", f"bad arguments: {e}")
        except PathError as e:
            return ToolResult(False, f"error: {e}", str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            return ToolResult(False, f"error: {e.__class__.__name__}: {e}", f"{e.__class__.__name__}: {e}")

    # shell ------------------------------------------------------------------
    def _child_env(self) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("API_KEY", "TELEGRAM_BOT_TOKEN") and v not in self.secrets}
        env["PWD"] = str(self.workdir)
        return env

    async def t_shell(self, command: str, timeout: int | None = None) -> ToolResult:
        timeout = max(1, min(int(timeout or self.default_shell_timeout), 1800))
        reason = danger_reason(command, self.workdir, self.full_access) if self.approvals else None
        if reason:
            if not await self.approve(command, reason, self.agent_id):
                return ToolResult(False, "error: the human DENIED this command. Do not retry it; "
                                  "find a safer alternative or explain why it is needed.",
                                  "denied by you", denied=True)
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", command, cwd=str(self.workdir), env=self._child_env(),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        buf = bytearray()

        async def pump():
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                if len(buf) < MAX_CAPTURE:
                    buf.extend(chunk[: MAX_CAPTURE - len(buf)])

        timed_out = False
        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            if proc.returncode is None:  # timeout or task cancelled (/stop): kill the whole group
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
        out = buf.decode("utf-8", "replace")
        if timed_out:
            return ToolResult(False, f"error: timed out after {timeout}s\n{clip(out)}",
                              f"timed out after {timeout}s")
        code = proc.returncode
        text = f"exit code {code}\n{clip(out)}"
        if code == 0:
            n = len(out.splitlines())
            return ToolResult(True, text, f"exit 0, {n} line{'s' if n != 1 else ''} of output")
        return ToolResult(False, text, f"exit {code}: {first_line(out) or '(no output)'}")

    # files ------------------------------------------------------------------
    async def t_read_file(self, path: str, offset: int = 0, max_chars: int = LLM_OUTPUT_LIMIT) -> ToolResult:
        p = self._path(path)
        if not p.is_file():
            return ToolResult(False, f"error: not a file: {path}", f"not a file: {path}")
        data = await asyncio.to_thread(p.read_bytes)
        text = data.decode("utf-8", "replace")
        offset = max(0, int(offset or 0))
        max_chars = max(1, min(int(max_chars or LLM_OUTPUT_LIMIT), 50000))
        chunk = text[offset: offset + max_chars]
        more = len(text) - offset - len(chunk)
        note = f"\n... [{more} more chars; use offset={offset + len(chunk)}]" if more > 0 else ""
        return ToolResult(True, chunk + note, f"{len(text)} chars")

    async def t_write_file(self, path: str, content: str, append: bool = False) -> ToolResult:
        p = self._path(path)
        if p.is_dir():
            return ToolResult(False, f"error: is a directory: {path}", f"is a directory: {path}")

        def write():
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a" if append else "w", encoding="utf-8") as f:
                f.write(content)

        await asyncio.to_thread(write)
        return ToolResult(True, f"wrote {len(content)} chars to {p}", f"{len(content)} chars written")

    async def t_list_dir(self, path: str = ".") -> ToolResult:
        p = self._path(path)
        if not p.is_dir():
            return ToolResult(False, f"error: not a directory: {path}", f"not a directory: {path}")
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = []
        files = dirs = 0
        for e in entries:
            try:
                if e.is_dir():
                    dirs += 1
                    lines.append(f"{e.name}/")
                else:
                    files += 1
                    lines.append(f"{e.name}  ({e.stat().st_size} bytes)")
            except OSError:
                lines.append(f"{e.name}  (unreadable)")
        shown = lines[:500]
        extra = f"\n... and {len(lines) - 500} more" if len(lines) > 500 else ""
        header = f"{p}: {files} files, {dirs} directories"
        return ToolResult(True, header + "\n" + "\n".join(shown) + extra, f"{files} files, {dirs} dirs")

    # web --------------------------------------------------------------------
    async def t_web_fetch(self, url: str, max_chars: int = 8000) -> ToolResult:
        if not url.lower().startswith(("http://", "https://")):
            return ToolResult(False, "error: only http(s) URLs are allowed", "only http(s) URLs allowed")
        max_chars = max(100, min(int(max_chars or 8000), 50000))
        t0 = time.monotonic()
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                     headers={"User-Agent": "myagent/1.0"}) as client:
            async with client.stream("GET", url) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 3_000_000:
                        break
                ctype = r.headers.get("content-type", "")
                status = r.status_code
                enc = r.encoding or "utf-8"
        raw = body.decode(enc, "replace")
        text = html_to_text(raw) if "html" in ctype or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html")) else raw
        out = text[:max_chars] + (f"\n... [truncated, {len(text)} chars total]" if len(text) > max_chars else "")
        ok = status < 400
        summary = f"HTTP {status}, {len(text)} chars in {time.monotonic() - t0:.1f}s"
        return ToolResult(ok, f"HTTP {status} {ctype}\n{out}", summary)

    # messaging --------------------------------------------------------------
    async def t_send_update(self, text: str) -> ToolResult:
        self.update(text)
        return ToolResult(True, "sent", "sent")

    # delegation (only present when a spawner is attached) --------------------
    async def t_spawn_worker(self, title: str, instructions: str) -> ToolResult:
        if not self.spawner:
            return ToolResult(False, "error: delegation is not available to this agent", "no delegation")
        return await self.spawner.spawn(title, instructions, self.agent_id)

    async def t_wait_for_workers(self, agent_ids: list | None = None) -> ToolResult:
        if not self.spawner:
            return ToolResult(False, "error: delegation is not available to this agent", "no delegation")
        return await self.spawner.wait(agent_ids or [], self.agent_id)

    async def t_request_review(self, subject: str) -> ToolResult:
        if not self.spawner:
            return ToolResult(False, "error: review is not available to this agent", "no review")
        return await self.spawner.review(subject, self.agent_id)
