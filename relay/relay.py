"""Public relay that connects a phone app to an agent that sits behind NAT.

Both sides dial OUT to this relay, so the agent machine never needs an open port:

    phone app  ──wss──►  RELAY  ◄──wss──  agent (on your Kali box)

The relay authenticates each side with a token, fans the agent's events out to every
connected app, forwards app commands to the agent, and buffers recent events so an app that
reconnects (or opens for the first time) catches up. It holds no agent logic and stores
nothing on disk.

Run it on any small public host (a $5 VPS, Fly.io, Render, …). Put it behind TLS in
production (a reverse proxy, or a platform that terminates HTTPS) so the tokens and traffic
are encrypted — then the agent uses wss:// and the app uses wss://.

Config via environment:
    RELAY_AGENT_KEY   secret the agent authenticates with   (required)
    RELAY_USER_TOKEN  secret the phone app authenticates with (required)
    RELAY_HOST        default 0.0.0.0
    RELAY_PORT        default 8900
    RELAY_BUFFER      events kept for catch-up, default 500
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from collections import deque

from aiohttp import WSMsgType, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s relay: %(message)s")
log = logging.getLogger("relay")


class Hub:
    """One agent, many apps. Buffers events; keeps the latest state snapshot."""

    def __init__(self, buffer: int):
        self.agent: web.WebSocketResponse | None = None
        self.apps: set[web.WebSocketResponse] = set()
        self.events: deque[dict] = deque(maxlen=buffer)
        self.last_id = 0
        self.state: dict | None = None

    def agent_online(self) -> bool:
        return self.agent is not None and not self.agent.closed

    async def to_apps(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False)
        for ws in list(self.apps):
            try:
                await ws.send_str(data)
            except Exception:
                self.apps.discard(ws)

    async def to_agent(self, msg: dict) -> bool:
        if not self.agent_online():
            return False
        try:
            await self.agent.send_str(json.dumps(msg, ensure_ascii=False))
            return True
        except Exception:
            return False

    def record_event(self, ev: dict) -> None:
        if ev.get("id"):
            self.last_id = max(self.last_id, ev["id"])
        self.events.append(ev)

    def catch_up(self, after: int) -> list[dict]:
        return [e for e in self.events if e.get("id", 0) > after]


def bearer(req: web.Request) -> str:
    auth = req.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return req.query.get("token", "")


def make_app() -> web.Application:
    agent_key = os.environ.get("RELAY_AGENT_KEY", "")
    user_token = os.environ.get("RELAY_USER_TOKEN", "")
    if not agent_key or not user_token:
        raise SystemExit("set RELAY_AGENT_KEY and RELAY_USER_TOKEN in the environment")
    hub = Hub(int(os.environ.get("RELAY_BUFFER", "500")))

    def ok(given: str, expected: str) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), expected.encode())

    async def health(req: web.Request):
        return web.json_response({"ok": True, "agent_online": hub.agent_online(),
                                  "apps": len(hub.apps), "buffered": len(hub.events)})

    async def check(req: web.Request):
        # lets the browser client validate the user token before opening a WebSocket
        if not ok(bearer(req), user_token):
            return web.json_response({"ok": False}, status=401)
        return web.json_response({"ok": True, "agent_online": hub.agent_online()})

    async def agent_ws(req: web.Request):
        if not ok(bearer(req), agent_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(req)
        if hub.agent_online():          # only one agent; replace a stale connection
            await hub.agent.close()
        hub.agent = ws
        log.info("agent connected from %s", req.remote)
        await hub.to_apps({"type": "presence", "agent_online": True})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "event":
                    hub.record_event(data.get("event") or {})
                    await hub.to_apps(data)
                elif kind == "state":
                    hub.state = data.get("state")
                    await hub.to_apps(data)
                elif kind == "reply":
                    await hub.to_apps(data)
        finally:
            if hub.agent is ws:
                hub.agent = None
            log.info("agent disconnected")
            await hub.to_apps({"type": "presence", "agent_online": False})
        return ws

    async def app_ws(req: web.Request):
        if not ok(bearer(req), user_token):
            return web.json_response({"error": "unauthorized"}, status=401)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(req)
        hub.apps.add(ws)
        after = int(req.query.get("after", "0") or 0)
        log.info("app connected from %s (%d total)", req.remote, len(hub.apps))
        try:
            await ws.send_str(json.dumps({"type": "hello", "agent_online": hub.agent_online(),
                                          "events": hub.catch_up(after), "state": hub.state},
                                         ensure_ascii=False))
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("type") == "command":
                    text = str(data.get("text", ""))
                    delivered = await hub.to_agent({"type": "command", "text": text,
                                                    "ref": data.get("ref"), "from": "app"})
                    if not delivered:
                        await ws.send_str(json.dumps({"type": "reply", "ref": data.get("ref"),
                                                      "html": "⚠️ The agent is offline right now."}))
        finally:
            hub.apps.discard(ws)
            log.info("app disconnected (%d left)", len(hub.apps))
        return ws

    webapp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "index.html")

    async def index(req: web.Request):
        if os.path.isfile(webapp):
            return web.FileResponse(webapp, headers={"Cache-Control": "no-store"})
        return web.Response(text="relay is running", content_type="text/plain")

    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/healthz", health),
        web.get("/check", check),
        web.get("/ws/agent", agent_ws),
        web.get("/ws/app", app_ws),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host=os.environ.get("RELAY_HOST", "0.0.0.0"),
                port=int(os.environ.get("RELAY_PORT", "8900")))
