"""Telegram interface: a hub sink (rate-limited narration) plus command/button handlers.
Only ALLOWED_USER_ID can talk to the bot; everyone else is silently ignored."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from commands import NAMES
from hub import Event, Hub, visible
from notifier import Notifier

if TYPE_CHECKING:
    from approvals import Approvals
    from commands import Commands

log = logging.getLogger("telegram")


class TelegramSink:
    """Forwards hub events to Telegram according to /verbose."""

    def __init__(self, hub: Hub, notifier: Notifier):
        self.hub = hub
        self.notifier = notifier
        self.approval_msgs: dict[str, int] = {}   # aid -> telegram message id (to remove buttons)

    def deliver(self, ev: Event) -> None:
        if ev.kind == "approval":
            aid = ev.data["aid"]
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"appr:{aid}:y"),
                InlineKeyboardButton("❌ Deny", callback_data=f"appr:{aid}:n"),
            ]])
            self.notifier.send(ev.html, markup=markup,
                               on_sent=lambda m, aid=aid: self.approval_msgs.__setitem__(aid, m.message_id))
            return
        if ev.kind == "approval_done":
            msg_id = self.approval_msgs.pop(ev.data["aid"], None)
            if msg_id:
                self.notifier.remove_buttons(msg_id)
        if visible(ev.level, self.hub.verbosity):
            self.notifier.send(ev.html)


def register(app: Application, allowed_user_id: int, commands: "Commands", approvals: "Approvals",
             notifier: Notifier) -> None:
    only_me = filters.User(user_id=allowed_user_id)

    async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply = await commands.handle(update.effective_message.text or "", source="telegram")
        if reply:
            notifier.send(reply)   # rate-limited queue

    async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not q or not q.from_user or q.from_user.id != allowed_user_id:
            if q:
                await q.answer()
            return
        try:
            _, aid, choice = (q.data or "").split(":")
        except ValueError:
            return await q.answer()
        ok = approvals.resolve(aid, choice == "y", by="telegram")
        await q.answer(("Approved" if choice == "y" else "Denied") if ok else "Already answered or expired")
        if not ok:
            try:
                await q.edit_message_reply_markup(None)
            except Exception:
                pass

    app.add_handler(CommandHandler(list(NAMES), on_text, filters=only_me))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^appr:"))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, on_text))
