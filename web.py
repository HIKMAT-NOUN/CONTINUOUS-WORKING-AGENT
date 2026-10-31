"""Web interface: dashboard + live feed over a WebSocket, plus a small JSON API.

Auth: a single token (WEB_TOKEN, or generated and stored in the DB). Open
http://HOST:PORT/?token=... once; the browser then keeps an HttpOnly, SameSite=Strict cookie.
API clients may send "Authorization: Bearer <token>" instead.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from hub import Event, Hub

if TYPE_CHECKING:
    from commands import Commands
    from db import DB
    from worker import Worker

log = logging.getLogger("web")
STATIC = Path(__file__).resolve().parent / "static"
COOKIE = "agent_session"

LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent login</title><style>body{font:15px system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:grid;place-items:center;height:100vh;margin:0}
form{background:#171a21;padding:28px;border-radius:12px;border:1px solid #2a2f3a;width:min(360px,90vw)}input,button{width:100%;box-sizing:border-box;padding:10px;margin-top:10px;border-radius:8px;border:1px solid #2a2f3a;background:#0f1115;color:inherit;font:inherit}
button{background:#3b82f6;border:0;cursor:pointer}p{color:#9aa3b2;font-size:13px}</style></head><body><form method="post" action="/login">
<b>Agent dashboard</b><p>Paste the web token. Get it on the server with <code>python cli.py url</code>.</p>
<input name="token" type="password" placeholder="token" autofocus><button>Log in</button>{msg}</form></body></html>"""


class WebSink:
    def __init__(self):
        self.clients: dict[asyncio.Queue, web.WebSocketResponse] = {}

    def deliver(self, ev: Event) -> None:
        msg = json.dumps({"type": "event", "event": ev.to_json()}, ensure_ascii=False)
        for q in list(self.clients):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:   # client too slow: close it; the page reconnects and catches up
                ws = self.clients.pop(q, None)
                if ws is not None:
                    asyncio.get_running_loop().create_task(ws.close())


def make_app(token: str, db: "DB", hub: Hub, worker: "Worker", commands: "Commands") -> web.Application:
    sink = WebSink()
    hub.subscribe(sink)

    def authed(req: web.Request) -> bool:
        given = req.cookies.get(COOKIE) or ""
        auth = req.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            given = auth[7:]
        return bool(given) and hmac.compare_digest(given.encode(), token.encode())

    def same_origin(req: web.Request) -> bool:
        origin = req.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == req.host

    def set_cookie(resp: web.StreamResponse, req: web.Request) -> None:
        resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict", max_age=60 * 60 * 24 * 90,
                        secure=req.secure)

    @web.middleware
    async def guard(req: web.Request, handler):
        if req.path in ("/login", "/favicon.ico"):
            return await handler(req)
        if req.path == "/" and req.query.get("token"):
            if hmac.compare_digest(req.query["token"].encode(), token.encode()):
                resp = web.HTTPFound("/")
                set_cookie(resp, req)
                raise resp
            return web.Response(text=LOGIN_PAGE.replace("{msg}", "<p>Wrong token.</p>"), content_type="text/html", status=401)
        if not authed(req):
            if req.path == "/":
                return web.Response(text=LOGIN_PAGE.replace("{msg}", ""), content_type="text/html", status=401)
            return web.json_response({"error": "unauthorized"}, status=401)
        if req.method != "GET" and not same_origin(req):
            return web.json_response({"error": "bad origin"}, status=403)
        resp = await handler(req)
        if isinstance(resp, web.Response):
            resp.headers.setdefault("Cache-Control", "no-store")
            resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    async def login(req: web.Request):
        form = await req.post()
        if hmac.compare_digest(str(form.get("token", "")).encode(), token.encode()):
            resp = web.HTTPFound("/")
            set_cookie(resp, req)
            raise resp
        await asyncio.sleep(1)   # slow down guessing
        return web.Response(text=LOGIN_PAGE.replace("{msg}", "<p>Wrong token.</p>"), content_type="text/html", status=401)

    async def index(req: web.Request):
        return web.FileResponse(STATIC / "index.html")

    async def favicon(req: web.Request):
        return web.Response(status=204)

    async def state(req: web.Request):
        return web.json_response({"status": worker.status_dict(),
                                  "recent": db.recent_tasks(15),
                                  "pending_approvals": [
                                      {"aid": a, "command": p["command"], "reason": p["reason"],
                                       "agent": p.get("agent", "main")}
                                      for a, p in worker.approvals.pending.items()]})

    async def events(req: web.Request):
        after = int(req.query.get("after", 0) or 0)
        return web.json_response({"events": db.events_after(after, 300)})

    async def task_detail(req: web.Request):
        tid = int(req.match_info["id"])
        t = db.get_task(tid)
        if not t:
            return web.json_response({"error": "not found"}, status=404)
        t.pop("messages", None)
        return web.json_response({"task": t, "steps": db.task_steps(tid)})

    async def command(req: web.Request):
        body = await req.json()
        reply = await commands.handle(str(body.get("text", "")), source=body.get("source") or "web")
        return web.json_response({"reply": reply})

    async def upload(req: web.Request):
        if not same_origin(req):
            return web.json_response({"error": "bad origin"}, status=403)
        import re as _re
        from pathlib import Path as _Path
        updir = _Path(worker.cfg.workdir) / "uploads"
        updir.mkdir(parents=True, exist_ok=True)
        saved = []
        reader = await req.multipart()
        async for part in reader:
            if not getattr(part, "filename", None):
                continue
            raw = part.filename.replace("\\", "/").split("/")[-1]
            name = _re.sub(r"[^A-Za-z0-9._-]", "_", raw)[:120] or "file"
            dest = updir / name
            i = 1
            while dest.exists():
                dest = updir / f"{dest.stem}-{i}{dest.suffix}"
                i += 1
            size = 0
            with open(dest, "wb") as fh:
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 30 * 1024 * 1024:
                        fh.close(); dest.unlink(missing_ok=True)
                        return web.json_response({"error": "file too large (max 30 MB)"}, status=413)
                    fh.write(chunk)
            rel = f"uploads/{dest.name}"
            saved.append({"name": dest.name, "path": rel, "size": size})
            hub.emit(f"📎 received <code>{dest.name}</code> ({size // 1024 or 1} KB) → <code>{rel}</code>",
                     kind="info")
        if not saved:
            return web.json_response({"error": "no file in request"}, status=400)
        return web.json_response({"files": saved})

    async def ws_handler(req: web.Request):
        if not same_origin(req):
            return web.json_response({"error": "bad origin"}, status=403)
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(req)
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        after = int(req.query.get("after", 0) or 0)
        await ws.send_str(json.dumps({"type": "history", "events": db.events_after(after, 300)},
                                     ensure_ascii=False))
        sink.clients[q] = ws
        source = req.query.get("source") or "web"

        async def pump():
            while True:
                await ws.send_str(await q.get())

        sender = asyncio.create_task(pump())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if data.get("type") == "command":
                    reply = await commands.handle(str(data.get("text", "")), source=source)
                    await ws.send_str(json.dumps({"type": "reply", "html": reply, "ref": data.get("ref")},
                                                 ensure_ascii=False))
        finally:
            sink.clients.pop(q, None)
            sender.cancel()
        return ws

    app = web.Application(middlewares=[guard], client_max_size=32 * 1024 * 1024)
    app.add_routes([
        web.get("/", index), web.post("/login", login), web.get("/favicon.ico", favicon),
        web.get("/api/state", state), web.get("/api/events", events),
        web.get("/api/tasks/{id}", task_detail), web.post("/api/command", command),
        web.post("/api/upload", upload),
        web.get("/ws", ws_handler),
    ])
    return app


async def start_web(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("web interface on http://%s:%d", host, port)
    return runner
