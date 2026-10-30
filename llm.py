"""Minimal OpenAI-compatible /chat/completions client with tool calling and retries."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("llm")


class LLMError(Exception):
    pass


@dataclass
class LLMResponse:
    message: dict                          # assistant message, ready to append to history
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = None              # provider-reported cost (OpenRouter), if any


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int = 180):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=20),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://github.com/myagent", "X-Title": "myagent"})

    async def close(self) -> None:
        await self.client.aclose()

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        body: dict = {"model": self.model, "messages": messages}
        if tools:
            body["tools"] = tools
        delay = 2.0
        for attempt in range(1, 7):
            try:
                r = await self.client.post(self.url, json=body)
            except httpx.HTTPError as e:
                err = f"network error: {e.__class__.__name__}"
            else:
                if r.status_code == 200:
                    try:
                        return self._parse(r.json())
                    except (ValueError, KeyError, IndexError, TypeError) as e:
                        err = f"malformed response: {e}"
                else:
                    err = f"HTTP {r.status_code}: {r.text[:300]}"
                    if r.status_code in (400, 401, 403, 404, 422):
                        raise LLMError(err)  # not retryable
            if attempt == 6:
                raise LLMError(err)
            log.warning("LLM call failed (%s), retry %d in %.0fs", err, attempt, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        raise LLMError("unreachable")

    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        if "error" in data and not data.get("choices"):
            raise ValueError(str(data["error"])[:300])
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        tool_calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            tool_calls.append({"id": tc.get("id") or f"call_{i}", "type": "function",
                               "function": {"name": fn.get("name", ""),
                                            "arguments": fn.get("arguments") or "{}"}})
        clean = {"role": "assistant", "content": content or (None if tool_calls else "")}
        if tool_calls:
            clean["tool_calls"] = tool_calls
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        return LLMResponse(clean, content, tool_calls,
                           int(usage.get("prompt_tokens") or 0),
                           int(usage.get("completion_tokens") or 0),
                           float(cost) if isinstance(cost, (int, float)) else None)
