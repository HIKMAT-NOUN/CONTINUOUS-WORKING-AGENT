"""Command-line client for the agent.

  python cli.py              attach to the running agent (service) and chat with it live
  python cli.py run          run the agent in this terminal (same as: python main.py --cli)
  python cli.py send TEXT    send one task or /command and print the reply
  python cli.py url          print the web dashboard login URL (contains the token)
  python cli.py token        print just the web token
"""
from __future__ import annotations

import asyncio
import json
import sys

import aiohttp

from config import ConfigError, load_config
from console import BANNER, ConsoleView, print_html, read_lines
from db import DB


def web_token(cfg) -> str:
    if cfg.web_token:
        return cfg.web_token
    tok = DB(cfg.db_path).get("web_token")
    if not tok:
        sys.exit("No web token yet — start the agent once (python main.py) to generate it.")
    return tok


def base_url(cfg) -> str:
    host = "127.0.0.1" if cfg.web_host in ("0.0.0.0", "::", "") else cfg.web_host
    return f"http://{host}:{cfg.web_port}"


async def attach(cfg) -> None:
    url, token = base_url(cfg), web_token(cfg)
    verbosity = {"v": cfg.default_verbose}
    view = ConsoleView(lambda: verbosity["v"])
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession(headers=headers) as http:
        try:
            async with http.get(f"{url}/api/state") as r:
                if r.status == 401:
                    sys.exit("Web token rejected.")
                st = (await r.json())["status"]
        except aiohttp.ClientConnectorError:
            sys.exit(f"Agent is not reachable at {url}. Is it running? (sudo systemctl status myagent)\n"
                     f"Or run it here with: python cli.py run")
        verbosity["v"] = st["verbose"]
        print(f"Attached to agent at {url} — {st['state']}, model {st['model']}, "
              f"{st['queue_count']} queued.")
        print(BANNER)
        stop = asyncio.Event()

        async def receive():
            while not stop.is_set():
                try:
                    async with http.ws_connect(f"{url}/ws?after={view.last_id}&source=cli",
                                               heartbeat=25) as ws:
                        state["ws"] = ws
                        async for msg in ws:
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            data = json.loads(msg.data)
                            if data["type"] == "history":
                                # show only the tail of the history on first connect
                                evs = data["events"] if view.last_id else data["events"][-15:]
                                for ev in evs:
                                    view.show(ev)
                                view.last_id = max([view.last_id] + [e["id"] for e in data["events"]])
                            elif data["type"] == "event":
                                view.show(data["event"])
                            elif data["type"] == "reply" and data.get("html"):
                                print_html(data["html"])
                except aiohttp.ClientError:
                    pass
                state["ws"] = None
                if not stop.is_set():
                    print_html("<i>connection lost, reconnecting…</i>")
                    await asyncio.sleep(3)

        state: dict = {"ws": None}
        rx = asyncio.create_task(receive())

        async def on_line(line: str) -> bool:
            text = view.translate(line)
            if text.startswith("/verbose "):
                verbosity["v"] = text.split(maxsplit=1)[1].strip().lower()
            ws = state["ws"]
            if ws is not None and not ws.closed:
                await ws.send_str(json.dumps({"type": "command", "text": text}))
            else:
                async with http.post(f"{url}/api/command", json={"text": text, "source": "cli"}) as r:
                    reply = (await r.json()).get("reply")
                    if reply:
                        print_html(reply)
            return True

        try:
            await read_lines(on_line)
        finally:
            stop.set()
            if state["ws"] is not None:
                await state["ws"].close()
            rx.cancel()
            await asyncio.gather(rx, return_exceptions=True)


async def send_once(cfg, text: str) -> None:
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {web_token(cfg)}"}) as http:
        try:
            async with http.post(f"{base_url(cfg)}/api/command", json={"text": text, "source": "cli"}) as r:
                data = await r.json()
        except aiohttp.ClientConnectorError:
            sys.exit(f"Agent is not reachable at {base_url(cfg)}.")
        print_html(data.get("reply") or data.get("error") or "ok")


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "attach"
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return
    if cmd == "run":
        import main as agent_main
        sys.argv = [sys.argv[0], "--cli", *args[1:]]
        agent_main.main()
        return
    try:
        cfg = load_config()
    except ConfigError as e:
        sys.exit(f"config error: {e}")
    if cmd == "url":
        print(f"{base_url(cfg)}/?token={web_token(cfg)}")
    elif cmd == "token":
        print(web_token(cfg))
    elif cmd == "send":
        if len(args) < 2:
            sys.exit('usage: python cli.py send "task text or /command"')
        asyncio.run(send_once(cfg, " ".join(args[1:])))
    elif cmd == "attach":
        try:
            asyncio.run(attach(cfg))
        except KeyboardInterrupt:
            pass
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
