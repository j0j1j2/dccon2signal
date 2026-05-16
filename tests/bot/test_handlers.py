from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dccon2signal_bot.handlers import _extract_pkg, msg_mention
from dccon2signal_bot.queue import JobQueue


def test_extract_pkg_bare_id():
    assert _extract_pkg("170660") == "170660"


def test_extract_pkg_with_whitespace():
    assert _extract_pkg("  170660  \n") == "170660"


def test_extract_pkg_url_form():
    assert _extract_pkg("https://dccon.dcinside.com/foo#170660") == "170660"


def test_extract_pkg_rejects_non_numeric():
    assert _extract_pkg("hello") is None


def test_extract_pkg_rejects_mixed():
    assert _extract_pkg("12 hello") is None


def _ctx(queue: JobQueue, bot_username: str = "testbot"):
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username=bot_username)))
    return SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(bot_data={"queue": queue}),
    )


@pytest.mark.asyncio
async def test_msg_in_dm_requires_mention():
    q = JobQueue()
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="170660",
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(),
        ),
    )
    await msg_mention(update, _ctx(q))
    assert q.size == 0  # no mention → ignored


@pytest.mark.asyncio
async def test_msg_in_dm_with_mention_enqueues():
    q = JobQueue()
    reply_msg = SimpleNamespace(chat_id=11, message_id=22)
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="@testbot 170660",
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(return_value=reply_msg),
        ),
    )
    await msg_mention(update, _ctx(q))
    assert q.size == 1


@pytest.mark.asyncio
async def test_msg_in_group_requires_mention():
    q = JobQueue()
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="170660",
            chat=SimpleNamespace(type="supergroup"),
            reply_text=AsyncMock(),
        ),
    )
    await msg_mention(update, _ctx(q))
    assert q.size == 0


@pytest.mark.asyncio
async def test_msg_in_group_with_mention_enqueues():
    q = JobQueue()
    reply_msg = SimpleNamespace(chat_id=11, message_id=22)
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="@testbot 170660",
            chat=SimpleNamespace(type="supergroup"),
            reply_text=AsyncMock(return_value=reply_msg),
        ),
    )
    await msg_mention(update, _ctx(q))
    assert q.size == 1


@pytest.mark.asyncio
async def test_msg_with_mention_but_no_digits_ignored():
    q = JobQueue()
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="@testbot hello there",
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(),
        ),
    )
    await msg_mention(update, _ctx(q))
    assert q.size == 0
