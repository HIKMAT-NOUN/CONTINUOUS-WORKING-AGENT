"""Human approval of risky shell commands. The request is broadcast to every interface
(Telegram buttons, web card, CLI prompt); the first answer from any of them wins."""
from __future__ import annotations

import asyncio
import logging
import secrets

from hub import Hub
from textfmt import code, h

log = logging.getLogger("approvals")


class Approvals:
    def __init__(self, hub: Hub, timeout: int):
        self.hub = hub
        self.timeout = timeout
        self.pending: dict[str, dict] = {}   # aid -> {"future", "command", "reason"}

    async def request(self, command: str, reason: str) -> bool:
        aid = secrets.token_hex(3)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[aid] = {"future": fut, "command": command, "reason": reason}
        self.hub.emit(f"⚠️ <b>Approval needed</b> [{aid}]\n{code(command, 1500)}\nReason: {h(reason)}\n"
                      f"(auto-deny in {self.timeout // 60} min)",
                      kind="approval", level="always", aid=aid, command=command, reason=reason,
                      timeout=self.timeout)
        log.info("approval %s requested: %s (%s)", aid, command, reason)
        try:
            ok = await asyncio.wait_for(asyncio.shield(fut), self.timeout)
            by = self.pending.get(aid, {}).get("by", "?")
        except asyncio.TimeoutError:
            ok, by = False, "timeout"
        except asyncio.CancelledError:
            self._done(aid, False, "task stopped")
            raise
        self._done(aid, ok, by)
        return ok

    def _done(self, aid: str, ok: bool, by: str) -> None:
        item = self.pending.pop(aid, None)
        if item is None:
            return
        verdict = "✅ approved" if ok else "❌ denied"
        self.hub.emit(f"{verdict} [{aid}] ({h(by)}): {code(item['command'], 200)}",
                      kind="approval_done", level="always", aid=aid, approved=ok, by=by)
        log.info("approval %s -> %s by %s", aid, verdict, by)

    def resolve(self, aid: str, approved: bool, by: str = "user") -> bool:
        """Called by any interface. Returns False if the request is unknown or already answered."""
        item = self.pending.get(aid)
        if item is None or item["future"].done():
            return False
        item["by"] = by
        item["future"].set_result(approved)
        return True

    def only_pending_id(self) -> str | None:
        return next(iter(self.pending)) if len(self.pending) == 1 else None
