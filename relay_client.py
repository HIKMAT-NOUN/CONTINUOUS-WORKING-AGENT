"""Outbound relay client: the agent dials OUT to a public relay so a phone app can reach it
without any open port on this machine.

It mirrors the web interface, but as a client instead of a server:
- subscribes to the hub and streams every event to the relay,
- pushes a periodic status snapshot (so the app shows the agent tree, queue, budget, …),
- receives commands from the app (via the relay) and runs them through the shared Commands.

Enabled when RELAY_URL and RELAY_AGENT_KEY are set. Reconnects forever with backoff; a relay
outage never touches the agent's own work.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import aiohttp

from config import redact
from hub import Event, Hub

if TYPE_CHECKING:
    from commands import Commands

log = logging.getLogger("relay")


class RelayClient:
    def __init__(self, url: str, agent_key: str, hub: Hub, commands: "Commands",
                 status_fn: Callable[[], dict], secrets: list[str], state_interval: float = 2.0):
        self.url = url
        self.agent_key = agent_key
        self.hub = hub
        self.commands = commands
        self.status_fn = status_fn
        self.secrets = secrets
        self.state_interval = state_interval
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=4000)
        self._task: asyncio.Task | None = None
        self._subscribed = False

    # hub sink ----------------------------------------------------------------
    def deliver(self, ev: Event) -> None:
        msg = json.dumps({"type": "event", "event": ev.to_json()}, ensure_ascii=False)
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:          # app/relay too slow: drop oldest, keep newest
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(msg)
            except asyncio.QueueEmpty:
                pass

    def _state_msg(self) -> str:
        state = self.status_fn()
        return redact(json.dumps({"type": "state", "state": state}, ensure_ascii=False), self.secrets)

    # lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        self.hub.subscribe(self)
        self._subscribed = True
        self._task = asyncio.create_task(self._run(), name="relay")

    async def stop(self) -> None:
        if self._subscribed:
            self.hub.unsubscribe(self)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        backoff = 2.0
        while True:
            try:
                async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {self.agent_key}"}) as s:
                    async with s.ws_connect(self.url, heartbeat=30, max_msg_size=48 * 1024 * 1024) as ws:
                        log.info("connected to relay %s", self.url)
                        backoff = 2.0
                        await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("relay connection failed (%s); retrying in %.0fs",
                            e.__class__.__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _session(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # Drain any backlog so a reconnecting app sees recent history, then live events + state.
        await ws.send_str(self._state_msg())
        sender = asyncio.create_task(self._sender(ws))
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                t = data.get("type")
                if t == "command":
                    await self._handle_command(ws, data)
                elif t == "upload":
                    await self._handle_upload(ws, data)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def _sender(self, ws) -> None:
        last_state = ""
        while True:
            try:
                msg = await asyncio.wait_for(self.queue.get(), self.state_interval)
                await ws.send_str(msg)
            except asyncio.TimeoutError:
                pass
            # push state when it changes (bounded by state_interval)
            state = self._state_msg()
            if state != last_state:
                await ws.send_str(state)
                last_state = state

    async def _handle_command(self, ws, data: dict) -> None:
        text = str(data.get("text", ""))
        try:
            reply = await self.commands.handle(text, source="app")
        except Exception as e:
            log.exception("relay command failed")
            reply = f"error: {e.__class__.__name__}"
        reply = redact(reply or "", self.secrets)
        await ws.send_str(json.dumps({"type": "reply", "ref": data.get("ref"), "html": reply},
                                     ensure_ascii=False))
        # nudge a fresh state snapshot right after a command
        await ws.send_str(self._state_msg())

    async def _handle_upload(self, ws, data: dict) -> None:
        """Save a file the app sent (base64) into WORKDIR/uploads and ack with its path."""
        ref = data.get("ref")
        try:
            raw = base64.b64decode((data.get("data") or "").encode(), validate=False)
        except (binascii.Error, ValueError):
            raw = b""
        if not raw:
            await ws.send_str(json.dumps({"type": "upload_ack", "ref": ref, "error": "empty or invalid file"}))
            return
        if len(raw) > 25 * 1024 * 1024:
            await ws.send_str(json.dumps({"type": "upload_ack", "ref": ref, "error": "file too large (max 25 MB)"}))
            return
        workdir = Path(self.commands.worker.cfg.workdir)
        updir = workdir / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        base = (data.get("name") or "file").replace("\\", "/").split("/")[-1]
        name = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120] or "file"
        dest = updir / name
        i = 1
        while dest.exists():
            dest = updir / f"{dest.stem}-{i}{dest.suffix}"
            i += 1
        try:
            dest.write_bytes(raw)
        except OSError as e:
            await ws.send_str(json.dumps({"type": "upload_ack", "ref": ref, "error": str(e)}))
            return
        rel = f"uploads/{dest.name}"
        self.hub.emit(f"📎 received <code>{dest.name}</code> ({len(raw) // 1024 or 1} KB) → <code>{rel}</code>",
                      kind="info")
        await ws.send_str(json.dumps({"type": "upload_ack", "ref": ref,
                                      "files": [{"name": dest.name, "path": rel, "size": len(raw)}]}))
