"""Terminal interface shared by `main.py --cli` (in-process) and `cli.py` (attach over the web API).

Prints narration as it happens and reads commands from stdin. Plain text becomes a task;
while an approval is pending, typing y / n answers it.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from typing import Awaitable, Callable

from hub import html_to_terminal, visible

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
DIM, RESET, YELLOW = ("\033[2m", "\033[0m", "\033[33m") if COLOR else ("", "", "")

BANNER = (f"{DIM}Type a task in plain text, or a /command (/help). "
          f"y / n answers a pending approval. /quit or Ctrl+D leaves the console.{RESET}")


def print_html(html: str, ts: float | None = None, prefix: str = "") -> None:
    stamp = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else datetime.now().strftime("%H:%M:%S")
    text = html_to_terminal(html, COLOR)
    lines = text.splitlines() or [""]
    pad = " " * 10
    out = [f"{DIM}{stamp}{RESET}  {prefix}{lines[0]}"] + [pad + l for l in lines[1:]]
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


class ConsoleView:
    """Renders event dicts (same shape as Event.to_json()) and tracks pending approvals."""

    def __init__(self, verbosity: Callable[[], str]):
        self.verbosity = verbosity
        self.pending: list[str] = []
        self.last_id = 0

    def show(self, ev: dict) -> None:
        if ev["id"] and ev["id"] <= self.last_id:
            return
        self.last_id = ev["id"] or self.last_id
        kind, data = ev["kind"], ev.get("data") or {}
        if kind == "approval":
            if data.get("aid") not in self.pending:
                self.pending.append(data["aid"])
            print_html(ev["html"], ev["ts"])
            print_html(f"<b>→ type y to approve or n to deny</b> (or /approve {data['aid']})", ev["ts"], YELLOW)
            return
        if kind == "approval_done" and data.get("aid") in self.pending:
            self.pending.remove(data["aid"])
        if visible(ev["level"], self.verbosity()):
            agent = ev.get("agent", "main")
            prefix = "" if agent == "main" else f"{YELLOW}[{agent}]{RESET} "
            print_html(ev["html"], ev["ts"], prefix)

    def translate(self, line: str) -> str:
        """y / n -> /approve / /deny for the oldest pending approval."""
        word = line.strip().lower()
        if self.pending and word in ("y", "yes", "n", "no"):
            return f"/{'approve' if word.startswith('y') else 'deny'} {self.pending[0]}"
        return line


async def read_lines(on_line: Callable[[str], Awaitable[bool]]) -> None:
    """Feed stdin lines to on_line until it returns False or stdin closes. Uses add_reader so
    shutdown never hangs on a blocked input() thread."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def readable():
        line = sys.stdin.readline()
        queue.put_nowait(line if line else None)

    loop.add_reader(sys.stdin.fileno(), readable)
    try:
        while True:
            line = await queue.get()
            if line is None:
                return
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit", "quit", "exit"):
                return
            if not await on_line(line):
                return
    finally:
        loop.remove_reader(sys.stdin.fileno())


class ConsoleSink:
    """Hub sink for the in-process console."""

    def __init__(self, view: ConsoleView):
        self.view = view

    def deliver(self, ev) -> None:
        self.view.show(ev.to_json())
