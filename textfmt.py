"""HTML formatting helpers shared by every interface (messages are Telegram-style HTML)."""
from __future__ import annotations

import html
from typing import Any


def h(text: Any) -> str:
    return html.escape(str(text), quote=False)


def truncate(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def code(text: str, limit: int = 300) -> str:
    return f"<code>{h(truncate(text, limit))}</code>"
