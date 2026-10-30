"""Event hub: the agent emits events once; every interface (Telegram, web, CLI) subscribes.

Each event has a level: 'always' (task start/end, alerts, approvals), 'steps' (narration) or
'full' (tool output). Interfaces decide what to show; Telegram and the CLI follow /verbose,
the web feed receives everything. All text is redacted here, before any interface sees it.
"""
from __future__ import annotations

import html as _html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from config import redact

if TYPE_CHECKING:
    from db import DB

log = logging.getLogger("hub")

LEVELS = ("off", "steps", "full")


@dataclass
class Event:
    id: int
    ts: float
    kind: str      # info | task | thinking | step | result | output | update | final | alert | approval | approval_done | reply | agent
    level: str     # always | steps | full
    html: str
    data: dict = field(default_factory=dict)
    agent: str = "main"

    def to_json(self) -> dict:
        return {"id": self.id, "ts": self.ts, "kind": self.kind, "level": self.level,
                "html": self.html, "data": self.data, "agent": self.agent}


class Sink(Protocol):
    def deliver(self, event: Event) -> None: ...


def visible(level: str, verbosity: str) -> bool:
    return level == "always" or (level == "steps" and verbosity in ("steps", "full")) or (
        level == "full" and verbosity == "full")


class Hub:
    def __init__(self, db: "DB", secrets: list[str], default_verbose: str = "steps"):
        self.db = db
        self.secrets = secrets
        self.default_verbose = default_verbose
        self.sinks: list[Sink] = []

    @property
    def verbosity(self) -> str:
        v = self.db.get("verbose", self.default_verbose)
        return v if v in LEVELS else "steps"

    def subscribe(self, sink: Sink) -> None:
        self.sinks.append(sink)

    def unsubscribe(self, sink: Sink) -> None:
        if sink in self.sinks:
            self.sinks.remove(sink)

    def emit(self, html: str, kind: str = "info", level: str = "always", agent: str = "main",
             **data) -> Event:
        html = redact(html, self.secrets)
        data = {k: redact(v, self.secrets) if isinstance(v, str) else v for k, v in data.items()}
        ts = time.time()
        eid = self.db.add_event(ts, kind, level, html, data, agent)
        ev = Event(eid, ts, kind, level, html, data, agent)
        for sink in list(self.sinks):
            try:
                sink.deliver(ev)
            except Exception:  # one broken interface must not affect the others
                log.exception("sink %r failed", sink)
        return ev


# --- rendering helpers shared by the terminal interfaces --------------------------------
_ANSI = {"b": "\033[1m", "/b": "\033[0m", "code": "\033[36m", "/code": "\033[0m",
         "pre": "\033[2m", "/pre": "\033[0m"}


def html_to_terminal(text: str, color: bool = True) -> str:
    def tag(m):
        name = m.group(1).lower()
        return _ANSI.get(name, "") if color else ""
    return _html.unescape(re.sub(r"<(/?[a-z]+)[^>]*>", tag, text))
