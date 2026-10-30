import asyncio

from telegram.error import NetworkError, RetryAfter

from notifier import MAX_LEN, Notifier


class FakeBot:
    def __init__(self, fail=0, exc=NetworkError("boom")):
        self.sent = []
        self.fail = fail
        self.exc = exc

    async def send_message(self, **kw):
        if self.fail:
            self.fail -= 1
            raise self.exc
        self.sent.append(kw)
        return kw


async def drain(n: Notifier, bot: FakeBot, count: int, timeout=5):
    async def wait():
        while len(bot.sent) < count or n.queue:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


async def test_merges_backlog_and_redacts():
    bot = FakeBot()
    n = Notifier(bot, 1, ["SECRETKEY123"], interval=0.05)
    for i in range(50):
        n.send(f"step {i}")
    n.send("leak SECRETKEY123")
    n.start()
    await drain(n, bot, 1)
    await n.stop()
    text = "\n".join(m["text"] for m in bot.sent)
    assert len(bot.sent) < 5                      # merged, not 51 separate messages
    assert all(f"step {i}" in text for i in range(50))  # nothing dropped
    assert "SECRETKEY123" not in text


async def test_markup_never_merged_and_long_split():
    bot = FakeBot()
    n = Notifier(bot, 1, [], interval=0.01)
    n.send("a")
    n.send("approve?", markup="BUTTONS")
    n.send("b")
    n.send_plain("x" * 9000)
    n.start()
    await drain(n, bot, 4)
    await n.stop()
    assert any(m["reply_markup"] == "BUTTONS" and m["text"] == "approve?" for m in bot.sent)
    assert all(len(m["text"]) <= 4096 for m in bot.sent)
    assert sum(m["text"].count("x") for m in bot.sent) == 9000


async def test_retries_on_errors():
    bot = FakeBot(fail=2, exc=RetryAfter(0))
    n = Notifier(bot, 1, [], interval=0.01)
    n.send("hello")
    n.start()
    await drain(n, bot, 1, timeout=10)
    await n.stop()
    assert bot.sent[0]["text"] == "hello"
