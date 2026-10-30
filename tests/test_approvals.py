import asyncio

from approvals import Approvals
from notifier import Notifier
from tests.test_notifier import FakeBot


async def test_approve_deny_and_timeout():
    bot = FakeBot()
    n = Notifier(bot, 1, [], interval=0.01)
    ap = Approvals(n, timeout=1)

    async def answer(choice):
        while not ap.pending:
            await asyncio.sleep(0.01)
        aid = next(iter(ap.pending))
        assert ap.resolve(aid, choice)
        assert not ap.resolve(aid, choice)  # second click is ignored

    for choice in (True, False):
        res, _ = await asyncio.gather(ap.request("rm -rf x", "test"), answer(choice))
        assert res is choice
    assert await ap.request("sudo x", "test") is False  # timeout -> deny
    buttons = n.queue[0].markup.inline_keyboard[0]
    assert [b.text for b in buttons] == ["✅ Approve", "❌ Deny"]
