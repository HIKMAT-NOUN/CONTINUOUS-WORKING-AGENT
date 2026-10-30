"""Entry point.

  python main.py              run the agent: worker + web dashboard + Telegram (if configured)
  python main.py --cli        same, plus an interactive console in this terminal
  options: --no-web  --no-telegram

systemd runs `python main.py` (see deploy/myagent.service). Only one agent may run per data
directory; `--cli` attaches to the running one instead of starting a second.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import secrets
import signal
import sys
from dataclasses import replace
from logging.handlers import RotatingFileHandler

from approvals import Approvals
from commands import Commands
from config import Config, ConfigError, load_config, redact
from db import DB
from hub import Hub
from llm import LLM
from worker import Worker

log = logging.getLogger("main")


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets_: list[str]):
        super().__init__(fmt)
        self.secrets = secrets_

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


def setup_logging(cfg: Config, console: bool) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", cfg.secrets)
    handlers: list[logging.Handler] = [RotatingFileHandler(
        cfg.log_dir / "agent.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")]
    if console:   # under systemd this goes to the journal; in --cli mode the terminal is the UI
        handlers.append(logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    # httpx logs full request URLs, which contain the bot token. Keep them quiet.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def acquire_lock(cfg: Config):
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(cfg.db_path.parent / "agent.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def ensure_web_token(cfg: Config, db: DB) -> Config:
    token = cfg.web_token or db.get("web_token")
    if not token:
        token = secrets.token_urlsafe(24)
        db.set("web_token", token)
    return replace(cfg, web_token=token)


TELEGRAM_COMMANDS = [("task", "add a task"), ("goal", "set/clear the standing goal"),
                     ("status", "what I'm doing"), ("queue", "pending tasks"), ("pause", "pause worker"),
                     ("resume", "resume worker"), ("stop", "abort current task"),
                     ("approve", "approve pending command"), ("deny", "deny pending command"),
                     ("verbose", "off | steps | full"), ("log", "last 20 steps")]


async def amain(cfg: Config, db: DB, use_cli: bool, use_web: bool, use_telegram: bool) -> int:
    hub = Hub(db, cfg.secrets, cfg.default_verbose)
    approvals = Approvals(hub, cfg.approval_timeout)
    llm = LLM(cfg.api_base_url, cfg.api_key, cfg.model, cfg.llm_timeout)
    worker = Worker(cfg, db, llm, hub, approvals)
    commands = Commands(db, worker, approvals)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    cleanups = []
    interfaces = []

    # --- web ---
    if use_web:
        from web import make_app, start_web
        try:
            runner = await start_web(make_app(cfg.web_token, db, hub, worker, commands), cfg.web_host, cfg.web_port)
            cleanups.append(runner.cleanup)
            interfaces.append(f"web http://{cfg.web_host}:{cfg.web_port}")
        except OSError as e:
            log.error("web interface could not start on %s:%s: %s", cfg.web_host, cfg.web_port, e)

    # --- telegram ---
    if use_telegram:
        from telegram import BotCommand, Update
        from telegram.ext import Application

        import telegram_ui
        from notifier import Notifier

        app = Application.builder().token(cfg.telegram_bot_token).build()
        notifier = Notifier(app.bot, cfg.allowed_user_id, cfg.secrets)
        hub.subscribe(telegram_ui.TelegramSink(hub, notifier))
        telegram_ui.register(app, cfg.allowed_user_id, commands, approvals, notifier)
        await app.initialize()
        try:
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in TELEGRAM_COMMANDS])
        except Exception as e:
            log.warning("could not set bot commands: %s", e)
        await app.start()
        await app.updater.start_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
        notifier.start()
        interfaces.append("telegram")

        async def stop_telegram():
            await notifier.stop(flush_timeout=5)
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        cleanups.insert(0, stop_telegram)

    # --- console ---
    console_task = None
    if use_cli:
        from console import BANNER, ConsoleSink, ConsoleView, print_html, read_lines
        view = ConsoleView(lambda: hub.verbosity)
        view.last_id = (db.events_after(0, 1) or [{"id": 0}])[-1]["id"]   # don't replay history
        hub.subscribe(ConsoleSink(view))
        interfaces.append("cli")

        async def on_line(line: str) -> bool:
            reply = await commands.handle(view.translate(line), source="cli")
            if reply:
                print_html(reply)
            return True

        async def run_console():
            print(f"Agent running — model {cfg.model}, workdir {cfg.workdir}")
            if use_web:
                print(f"Web dashboard: http://{cfg.web_host}:{cfg.web_port}/?token={cfg.web_token}")
            print(BANNER)
            await read_lines(on_line)
            stop_event.set()   # leaving the console stops the agent
        console_task = asyncio.create_task(run_console(), name="console")

    if not interfaces:
        log.error("no interface is enabled (web disabled/failed, no Telegram token, no --cli)")
        await llm.close()
        return 2

    worker_task = asyncio.create_task(worker.run_forever(), name="worker")
    log.info("agent started: model=%s workdir=%s interfaces=%s", cfg.model, cfg.workdir, ", ".join(interfaces))

    stopper = asyncio.create_task(stop_event.wait())
    done, _ = await asyncio.wait({worker_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
    crashed = worker_task in done
    if crashed and worker_task.exception():
        log.error("worker died", exc_info=worker_task.exception())   # systemd restarts us

    log.info("shutting down")
    hub.emit("🔌 Agent shutting down (any running task resumes on restart).", kind="alert")
    worker_task.cancel()
    await asyncio.gather(worker_task, return_exceptions=True)
    if console_task:
        console_task.cancel()
        await asyncio.gather(console_task, return_exceptions=True)
    for fn in cleanups:
        try:
            await fn()
        except Exception:
            log.exception("cleanup failed")
    await llm.close()
    return 1 if crashed else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Always-on AI worker agent")
    p.add_argument("--cli", action="store_true", help="interactive console in this terminal")
    p.add_argument("--no-web", action="store_true", help="disable the web dashboard")
    p.add_argument("--no-telegram", action="store_true", help="disable Telegram even if configured")
    args = p.parse_args()
    try:
        cfg = load_config()
    except ConfigError as e:
        sys.exit(f"config error: {e}")

    lock = acquire_lock(cfg)
    if lock is None:
        if args.cli:
            print("The agent is already running (probably the systemd service) — attaching to it.\n")
            import cli
            sys.argv = [sys.argv[0], "attach"]
            cli.main()
            return
        sys.exit("Another agent instance is already running with this data directory. "
                 "Use `python cli.py` to attach to it.")

    db = DB(cfg.db_path)
    if db.get("verbose") is None:
        db.set("verbose", cfg.default_verbose)
    cfg = ensure_web_token(cfg, db)
    setup_logging(cfg, console=not args.cli)
    code = asyncio.run(amain(cfg, db,
                             use_cli=args.cli,
                             use_web=cfg.web_enabled and not args.no_web,
                             use_telegram=cfg.telegram_enabled and not args.no_telegram))
    sys.exit(code)


if __name__ == "__main__":
    main()
