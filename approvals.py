"""Human approval of risky shell commands via Telegram inline buttons."""
from __future__ import annotations

import asyncio
import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from notifier import Notifier, code, h

log = logging.getLogger("approvals")


class Approvals:
    def __init__(self, notifier: Notifier, timeout: int):
        self.notifier = notifier
        self.timeout = timeout
        self.pending: dict[str, asyncio.Future] = {}

    async def request(self, command: str, reason: str) -> bool:
        aid = secrets.token_hex(4)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[aid] = fut
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"appr:{aid}:y"),
            InlineKeyboardButton("❌ Deny", callback_data=f"appr:{aid}:n"),
        ]])
        self.notifier.send(f"⚠️ <b>Approval needed</b>\n{code(command, 1500)}\n"
                           f"Reason: {h(reason)}\n(auto-deny in {self.timeout // 60} min)", markup=markup)
        log.info("approval %s requested: %s (%s)", aid, command, reason)
        try:
            ok = await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError:
            self.notifier.send(f"⌛ No answer — denied: {code(command, 200)}")
            ok = False
        finally:
            self.pending.pop(aid, None)
        log.info("approval %s -> %s", aid, "approved" if ok else "denied")
        return ok

    def resolve(self, aid: str, approved: bool) -> bool:
        """Called from the button handler. Returns False if the request is gone/expired."""
        fut = self.pending.get(aid)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        return True
