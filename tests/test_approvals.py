import asyncio

from approvals import Approvals
from db import DB
from hub import Hub


class Collector:
    def __init__(self):
        self.events = []

    def deliver(self, ev):
        self.events.append(ev)


async def test_approve_deny_timeout_broadcast():
    hub = Hub(DB(":memory:"), [])
    sink = Collector()
    hub.subscribe(sink)
    ap = Approvals(hub, timeout=1)

    async def answer(choice):
        while not ap.pending:
            await asyncio.sleep(0.01)
        aid = ap.only_pending_id()
        assert ap.resolve(aid, choice, by="web")
        assert not ap.resolve(aid, choice)  # a second answer (other interface) is ignored

    for choice in (True, False):
        res, _ = await asyncio.gather(ap.request("rm -rf x", "test"), answer(choice))
        assert res is choice
    assert await ap.request("sudo x", "test") is False  # timeout -> deny
    kinds = [e.kind for e in sink.events]
    assert kinds == ["approval", "approval_done"] * 3
    done = [e.data for e in sink.events if e.kind == "approval_done"]
    assert [(d["approved"], d["by"]) for d in done] == [(True, "web"), (False, "web"), (False, "timeout")]
    assert not ap.pending


async def test_cancel_cleans_up():
    hub = Hub(DB(":memory:"), [])
    ap = Approvals(hub, timeout=30)
    t = asyncio.create_task(ap.request("sudo x", "r"))
    await asyncio.sleep(0.05)
    t.cancel()
    await asyncio.gather(t, return_exceptions=True)
    assert not ap.pending
