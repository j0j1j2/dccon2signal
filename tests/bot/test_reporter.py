import pytest

from dccon2signal.pipeline import Stage
from dccon2signal_bot.reporter import StatusReporter


@pytest.mark.asyncio
async def test_first_update_flushes(fake_bot, fake_clock):
    r = StatusReporter(
        fake_bot, chat_id=10, message_id=20,
        min_interval=1.5, clock=fake_clock.monotonic,
    )
    await r.update(Stage.FETCHING)
    assert len(fake_bot.edits) == 1
    assert "디시콘 정보" in fake_bot.edits[0]["text"]


@pytest.mark.asyncio
async def test_rapid_updates_are_throttled(fake_bot, fake_clock):
    r = StatusReporter(
        fake_bot, chat_id=10, message_id=20,
        min_interval=1.5, clock=fake_clock.monotonic,
    )
    await r.update(Stage.DOWNLOADING, (1, 10))
    await r.update(Stage.DOWNLOADING, (2, 10))
    await r.update(Stage.DOWNLOADING, (3, 10))
    assert len(fake_bot.edits) == 1


@pytest.mark.asyncio
async def test_update_after_interval_flushes(fake_bot, fake_clock):
    r = StatusReporter(
        fake_bot, chat_id=10, message_id=20,
        min_interval=1.5, clock=fake_clock.monotonic,
    )
    await r.update(Stage.DOWNLOADING, (1, 10))
    fake_clock.advance(2.0)
    await r.update(Stage.DOWNLOADING, (5, 10))
    assert len(fake_bot.edits) == 2


@pytest.mark.asyncio
async def test_done_always_flushes(fake_bot, fake_clock):
    r = StatusReporter(
        fake_bot, chat_id=10, message_id=20,
        min_interval=1.5, clock=fake_clock.monotonic,
    )
    await r.update(Stage.DOWNLOADING, (1, 10))
    await r.update(Stage.DONE)
    assert len(fake_bot.edits) == 2
    assert "완료" in fake_bot.edits[1]["text"]


@pytest.mark.asyncio
async def test_unchanged_text_not_resent(fake_bot, fake_clock):
    r = StatusReporter(
        fake_bot, chat_id=10, message_id=20,
        min_interval=0.0, clock=fake_clock.monotonic,
    )
    await r.update(Stage.DOWNLOADING, (5, 10))
    await r.update(Stage.DOWNLOADING, (5, 10))
    assert len(fake_bot.edits) == 1


@pytest.mark.asyncio
async def test_edit_failure_does_not_raise(fake_clock):
    class BrokenBot:
        async def edit_message_text(self, **kw):
            raise RuntimeError("message deleted")

    r = StatusReporter(
        BrokenBot(), chat_id=10, message_id=20,
        min_interval=0.0, clock=fake_clock.monotonic,
    )
    await r.update(Stage.DONE)
