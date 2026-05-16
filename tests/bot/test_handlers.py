from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dccon2signal_bot.handlers import _extract_pkg, cmd_con2signal, msg_loose
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


@pytest.mark.asyncio
async def test_cmd_rejects_non_numeric():
    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    context = SimpleNamespace(
        args=["abc"],
        application=SimpleNamespace(bot_data={"queue": JobQueue()}),
    )
    await cmd_con2signal(update, context)
    update.message.reply_text.assert_awaited()
    sent = update.message.reply_text.await_args.args[0]
    assert "숫자" in sent


@pytest.mark.asyncio
async def test_cmd_enqueues():
    q = JobQueue()
    reply_msg = SimpleNamespace(chat_id=11, message_id=22)
    update = SimpleNamespace(
        message=SimpleNamespace(reply_text=AsyncMock(return_value=reply_msg)),
    )
    context = SimpleNamespace(
        args=["170660"],
        application=SimpleNamespace(bot_data={"queue": q}),
    )
    await cmd_con2signal(update, context)
    assert q.size == 1
    job = await q.get()
    assert job.package_idx == "170660"
    assert (job.chat_id, job.message_id) == (11, 22)


@pytest.mark.asyncio
async def test_msg_loose_in_dm_accepts_digits():
    q = JobQueue()
    reply_msg = SimpleNamespace(chat_id=11, message_id=22)
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="170660",
            chat=SimpleNamespace(type="private"),
            reply_text=AsyncMock(return_value=reply_msg),
        ),
    )
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")))
    context = SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(bot_data={"queue": q}),
    )
    await msg_loose(update, context)
    assert q.size == 1


@pytest.mark.asyncio
async def test_msg_loose_in_group_requires_mention():
    q = JobQueue()
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="170660",
            chat=SimpleNamespace(type="supergroup"),
            reply_text=AsyncMock(),
        ),
    )
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")))
    context = SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(bot_data={"queue": q}),
    )
    await msg_loose(update, context)
    assert q.size == 0


@pytest.mark.asyncio
async def test_msg_loose_in_group_with_mention_enqueues():
    q = JobQueue()
    reply_msg = SimpleNamespace(chat_id=11, message_id=22)
    update = SimpleNamespace(
        message=SimpleNamespace(
            text="@testbot 170660",
            chat=SimpleNamespace(type="supergroup"),
            reply_text=AsyncMock(return_value=reply_msg),
        ),
    )
    bot = SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")))
    context = SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(bot_data={"queue": q}),
    )
    await msg_loose(update, context)
    assert q.size == 1
