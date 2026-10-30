"""The relay forwards events agent->app and commands app->agent, and checks auth."""
import json
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

os.environ.setdefault("RELAY_AGENT_KEY", "akey")
os.environ.setdefault("RELAY_USER_TOKEN", "utok")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "relay"))
import relay as relaymod  # noqa: E402


async def test_relay_forwarding_and_auth():
    app = relaymod.make_app()
    async with TestClient(TestServer(app)) as client:
        # bad auth is refused on both endpoints
        assert (await client.get("/ws/agent", headers={"Authorization": "Bearer wrong"})).status == 401
        assert (await client.get("/ws/app?token=wrong")).status == 401

        agent = await client.ws_connect("/ws/agent", headers={"Authorization": "Bearer akey"})
        # app connects and gets a hello with agent online
        appws = await client.ws_connect("/ws/app?token=utok")
        hello = json.loads((await appws.receive()).data)
        assert hello["type"] == "hello" and hello["agent_online"] is True

        # agent -> app event (also buffered for catch-up)
        await agent.send_str(json.dumps({"type": "event", "event": {"id": 5, "kind": "task", "html": "hi"}}))
        msg = json.loads((await appws.receive()).data)
        assert msg["type"] == "event" and msg["event"]["id"] == 5

        # app -> agent command
        await appws.send_str(json.dumps({"type": "command", "text": "/status", "ref": "r1"}))
        cmd = json.loads((await agent.receive()).data)
        assert cmd["type"] == "command" and cmd["text"] == "/status" and cmd["from"] == "app"

        # a second app catches up on the buffered event
        app2 = await client.ws_connect("/ws/app?token=utok&after=0")
        hello2 = json.loads((await app2.receive()).data)
        assert any(e["id"] == 5 for e in hello2["events"])

        await agent.close()
        # with the agent gone, a command gets an offline reply
        await appws.send_str(json.dumps({"type": "command", "text": "/status", "ref": "r2"}))
        while True:
            m = json.loads((await appws.receive()).data)
            if m["type"] == "reply":
                assert "offline" in m["html"] and m["ref"] == "r2"
                break
        await appws.close(); await app2.close()
