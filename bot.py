"""Telegram command handlers. Every handler is restricted to ALLOWED_USER_ID."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from notifier import h, truncate

if TYPE_CHECKING:
    from approvals import Approvals
    from db import DB
    from notifier import Notifier
    from worker import Worker

log = logging.getLogger("bot")

HELP = """<b>Commands</b>
/task &lt;text&gt; — add a task to the queue
/goal &lt;text&gt; — set standing goal (/goal clear to remove, /goal to show)
/status — current task, step, queue, tokens and cost today
/queue — list pending tasks
/pause, /resume — pause or resume the worker (/resume force ignores the daily budget)
/stop — abort the current task
/verbose off|steps|full — narration level
/log — last 20 steps"""


def register(app: Application, allowed_user_id: int, db: "DB", worker: "Worker",
             approvals: "Approvals", notifier: "Notifier") -> None:
    only_me = filters.User(user_id=allowed_user_id)

    def reply(update: Update, text_html: str) -> None:
        # Route through the rate-limited queue so we never trip flood limits.
        notifier.send(text_html)

    def arg_text(update: Update) -> str:
        text = update.effective_message.text or ""
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply(update, "👋 Agent is running.\n\n" + HELP)

    async def task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = arg_text(update)
        if not text:
            return reply(update, "Usage: /task &lt;description&gt;")
        tid = db.add_task(text)
        worker.poke()
        ahead = db.count_pending() - 1 + (1 if worker.current else 0)
        extra = " (worker is paused)" if worker.paused else (f" · {ahead} ahead of it" if ahead else "")
        reply(update, f"📥 Task #{tid} queued{extra}.")

    async def goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = arg_text(update)
        if not text:
            g = db.get("goal")
            return reply(update, f"🎯 Goal: {h(g)}" if g else "No standing goal. Set one with /goal &lt;text&gt;.")
        if text.lower() in ("clear", "none", "off"):
            worker.set_goal(None)
            return reply(update, "🎯 Standing goal cleared.")
        worker.set_goal(text)
        reply(update, f"🎯 Standing goal set. When the queue is empty I'll work on it every "
                      f"{worker.cfg.goal_interval_min:g} min.")

    async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply(update, worker.status_html())

    async def queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        rows = db.pending(30)
        if not rows:
            return reply(update, "📭 Queue is empty.")
        lines = [f"<b>Pending ({db.count_pending()})</b>"]
        for r in rows:
            tag = "🎯 " if r["kind"] == "goal" else ""
            lines.append(f"#{r['id']} {tag}{h(truncate(r['description'].splitlines()[0], 120))}")
        reply(update, "\n".join(lines))

    async def pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        worker.pause("manual")
        cur = f" Task #{worker.current['id']} will continue after /resume." if worker.current else ""
        reply(update, f"⏸ Worker paused (takes effect before the next LLM call).{cur}")

    async def resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        err = worker.resume(force=arg_text(update).lower() == "force")
        reply(update, f"⛔ {h(err)}" if err else "▶️ Worker resumed.")

    async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        tid = worker.stop_current()
        reply(update, f"🛑 Stopping task #{tid}…" if tid else "Nothing is running.")

    async def verbose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        level = arg_text(update).lower()
        if level not in ("off", "steps", "full"):
            cur = db.get("verbose", worker.cfg.default_verbose)
            return reply(update, f"Verbose is <b>{h(cur)}</b>. Usage: /verbose off|steps|full")
        db.set("verbose", level)
        reply(update, f"🔊 Verbose set to <b>{level}</b>.")

    async def steplog(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        rows = db.last_steps(20)
        if not rows:
            return reply(update, "No steps logged yet.")
        icons = {"ok": "✅", "error": "❌", "denied": "🚫"}
        lines = ["<b>Last steps</b>"]
        for r in rows:
            ts = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
            lines.append(f"{icons.get(r['status'], '•')} {ts} #{r['task_id']}.{r['step_no']} "
                         f"<b>{h(r['tool'])}</b> <code>{h(truncate(r['args'] or '', 90))}</code>")
        reply(update, "\n".join(lines))

    async def other(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        reply(update, "Use /task &lt;text&gt; to give me work, or /help.")

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
        approved = choice == "y"
        ok = approvals.resolve(aid, approved)
        await q.answer("Approved" if approved and ok else "Denied" if ok else "Expired")
        try:
            verdict = ("✅ Approved" if approved else "❌ Denied") if ok else "⌛ Expired"
            await q.edit_message_text(f"{q.message.text_html}\n\n<b>{verdict}</b>", parse_mode="HTML")
        except Exception as e:  # editing is cosmetic
            log.debug("could not edit approval message: %s", e)

    for name, fn in (("start", start), ("help", start), ("task", task), ("goal", goal),
                     ("status", status), ("queue", queue), ("pause", pause), ("resume", resume),
                     ("stop", stop), ("verbose", verbose), ("log", steplog)):
        app.add_handler(CommandHandler(name, fn, filters=only_me))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^appr:"))
    app.add_handler(MessageHandler(only_me & ~filters.COMMAND, other))
    # Messages from anyone else match no handler and are silently ignored.
