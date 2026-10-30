"""Entry point: python main.py  (systemd runs this; see deploy/myagent.service)."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from telegram import BotCommand, Update
from telegram.ext import Application

import bot
from approvals import Approvals
from config import Config, ConfigError, load_config, redact
from db import DB
from llm import LLM
from notifier import Notifier
from worker import Worker

log = logging.getLogger("main")


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        self.secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", cfg.secrets)
    file_h = RotatingFileHandler(cfg.log_dir / "agent.log", maxBytes=10_000_000, backupCount=5,
                                 encoding="utf-8")
    console = logging.StreamHandler(sys.stdout)
    for handler in (file_h, console):
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file_h, console], force=True)
    # httpx logs full request URLs, which contain the bot token. Keep them quiet.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


COMMANDS = [("task", "add a task"), ("goal", "set/clear the standing goal"), ("status", "what I'm doing"),
            ("queue", "pending tasks"), ("pause", "pause worker"), ("resume", "resume worker"),
            ("stop", "abort current task"), ("verbose", "off | steps | full"), ("log", "last 20 steps")]


async def amain(cfg: Config) -> None:
    db = DB(cfg.db_path)
    if db.get("verbose") is None:
        db.set("verbose", cfg.default_verbose)
    app = Application.builder().token(cfg.telegram_bot_token).build()
    notifier = Notifier(app.bot, cfg.allowed_user_id, cfg.secrets)
    approvals = Approvals(notifier, cfg.approval_timeout)
    llm = LLM(cfg.api_base_url, cfg.api_key, cfg.model, cfg.llm_timeout)
    worker = Worker(cfg, db, llm, notifier, approvals)
    bot.register(app, cfg.allowed_user_id, db, worker, approvals, notifier)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    async with app:
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        except Exception as e:
            log.warning("could not set bot commands: %s", e)
        await app.start()
        await app.updater.start_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
        notifier.start()
        worker_task = asyncio.create_task(worker.run_forever(), name="worker")
        log.info("agent started: model=%s workdir=%s", cfg.model, cfg.workdir)

        stopper = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait({worker_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if worker_task in done and worker_task.exception():
            log.error("worker died", exc_info=worker_task.exception())  # systemd will restart us

        log.info("shutting down")
        notifier.send("🔌 Agent shutting down (any running task resumes on restart).")
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await notifier.stop(flush_timeout=5)
        await app.updater.stop()
        await app.stop()
    await llm.close()
    if worker_task in done:
        sys.exit(1)


def main() -> None:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    setup_logging(cfg)
    asyncio.run(amain(cfg))


if __name__ == "__main__":
    main()
