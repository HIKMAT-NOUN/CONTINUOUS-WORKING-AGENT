"""Outgoing Telegram queue: at most 1 message/second, merges backlog, retries forever-ish.

All text passed in is redacted before it is queued, so secrets never leave the process.
Messages are HTML (parse_mode=HTML); use h() to escape untrusted text.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from config import redact

log = logging.getLogger("notifier")

MAX_LEN = 4000          # Telegram hard limit is 4096; leave room for merge separators
SEND_INTERVAL = 1.0     # seconds between sends


def h(text: Any) -> str:
    return html.escape(str(text), quote=False)


def code(text: str, limit: int = 300) -> str:
    return f"<code>{h(truncate(text, limit))}</code>"


def truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strip_html(text: str) -> str:
    return html.unescape(re.sub(r"</?[a-z]+[^>]*>", "", text))


@dataclass
class Outgoing:
    text: str
    markup: Any = None                     # InlineKeyboardMarkup -> never merged
    on_sent: Callable[[Any], None] | None = None


class Notifier:
    def __init__(self, bot, chat_id: int, secrets: list[str], interval: float = SEND_INTERVAL):
        self.bot = bot
        self.chat_id = chat_id
        self.secrets = secrets
        self.interval = interval
        self.queue: deque[Outgoing] = deque()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # producer side ------------------------------------------------------------
    def send(self, text_html: str, markup: Any = None, on_sent=None) -> None:
        """Queue an HTML message (caller escapes untrusted parts with h())."""
        text_html = redact(text_html, self.secrets)
        if len(text_html) > MAX_LEN:
            # Can't safely cut HTML; fall back to plain text chunks.
            self.send_plain(_strip_html(text_html), markup=markup, on_sent=on_sent)
            return
        self.queue.append(Outgoing(text_html, markup, on_sent))
        self._wake.set()

    def send_plain(self, text: str, prefix_html: str = "", markup: Any = None, on_sent=None) -> None:
        """Queue arbitrary-length plain text, split into chunks and escaped."""
        text = redact(text, self.secrets)
        room = MAX_LEN - len(prefix_html) - 20
        chunks = [text[i:i + room] for i in range(0, max(len(text), 1), room)] or [""]
        for i, chunk in enumerate(chunks):
            last = i == len(chunks) - 1
            self.queue.append(Outgoing((prefix_html if i == 0 else "") + h(chunk),
                                       markup if last else None, on_sent if last else None))
        self._wake.set()

    # consumer side ------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="notifier")

    async def stop(self, flush_timeout: float = 5.0) -> None:
        if not self._task:
            return
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + flush_timeout
            while self.queue and loop.time() < deadline:
                await asyncio.sleep(0.2)
        finally:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _next_batch(self) -> Outgoing:
        """Pop one message, or merge the backlog of plain messages into one."""
        first = self.queue.popleft()
        if first.markup is not None:
            return first
        parts = [first.text]
        size = len(first.text)
        while self.queue and self.queue[0].markup is None and size + 1 + len(self.queue[0].text) <= MAX_LEN:
            nxt = self.queue.popleft()
            parts.append(nxt.text)
            size += 1 + len(nxt.text)
        return Outgoing("\n".join(parts))

    async def _run(self) -> None:
        while True:
            try:
                if not self.queue:
                    self._wake.clear()
                    await self._wake.wait()
                    # tiny grace period so a burst of step messages lands in one message
                    await asyncio.sleep(0.05)
                batch = self._next_batch()
                await self._deliver(batch)
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the sender die
                log.exception("notifier loop error")
                await asyncio.sleep(self.interval)

    async def _deliver(self, msg: Outgoing) -> None:
        backoff = 2.0
        text, parse_mode = msg.text, "HTML"
        while True:
            try:
                sent = await self.bot.send_message(
                    chat_id=self.chat_id, text=text, parse_mode=parse_mode,
                    reply_markup=msg.markup, disable_web_page_preview=True)
                if msg.on_sent:
                    try:
                        msg.on_sent(sent)
                    except Exception:
                        log.exception("on_sent callback failed")
                return
            except RetryAfter as e:
                wait = float(getattr(e.retry_after, "total_seconds", lambda: e.retry_after)()) + 1
                log.warning("telegram flood control, waiting %.0fs", wait)
                await asyncio.sleep(wait)
            except BadRequest as e:
                if parse_mode is not None:
                    log.warning("telegram rejected HTML (%s); resending as plain text", e)
                    text, parse_mode = _strip_html(text)[:4096], None
                    continue
                log.error("telegram rejected message, dropping it: %s", e)
                return
            except (TimedOut, NetworkError, Forbidden) as e:
                log.warning("telegram send failed (%s); retrying in %.0fs", e.__class__.__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
            except Exception as e:
                log.warning("unexpected send error %r; retrying in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
