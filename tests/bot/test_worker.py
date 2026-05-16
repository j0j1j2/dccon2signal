import asyncio
import contextlib
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from dccon2signal.pipeline import ConvertResult, Stage
from dccon2signal_bot.config import BotConfig
from dccon2signal_bot.queue import Job, JobQueue
from dccon2signal_bot.worker import Worker


def _cfg(tmp_path: Path) -> BotConfig:
    return BotConfig(
        telegram_token="x",
        auth_path=tmp_path / "auth.json",
        out_dir=tmp_path / "out",
        admin_chat_id=None,
        log_level="INFO",
    )


@pytest.mark.asyncio
async def test_worker_runs_success_path(fake_bot, tmp_path):
    async def fake_convert(_pkg, **kw):
        cb = kw["on_status"]
        await cb(Stage.FETCHING)
        await cb(Stage.DOWNLOADING, (1, 1))
        await cb(Stage.PROCESSING, (1, 1))
        await cb(Stage.UPLOADING)
        await cb(Stage.DONE)
        return ConvertResult(
            pack_id="abc",
            pack_key="def",
            title="t",
            author="a",
            sticker_count=1,
            install_url="https://signal.art/addstickers/#pack_id=abc&pack_key=def",
        )

    q = JobQueue()
    await q.submit(Job(package_idx="1", chat_id=10, message_id=20, submitted_at=time.time()))

    w = Worker(queue=q, bot=fake_bot, config=_cfg(tmp_path))

    with patch("dccon2signal_bot.worker.convert_pack", side_effect=fake_convert):
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    texts = [e["text"] for e in fake_bot.edits]
    assert any("pack_id=abc" in t for t in texts)
    assert any("pack_key=def" in t for t in texts)


@pytest.mark.asyncio
async def test_worker_survives_exception(fake_bot, tmp_path):
    calls = {"n": 0}

    async def flaky_convert(pkg, **kw):
        calls["n"] += 1
        cb = kw["on_status"]
        if pkg == "1":
            raise RuntimeError("boom")
        await cb(Stage.DONE)
        return ConvertResult(
            pack_id="ok",
            pack_key="ok",
            title="t",
            author="a",
            sticker_count=0,
            install_url="https://signal.art/addstickers/#pack_id=ok&pack_key=ok",
        )

    q = JobQueue()
    await q.submit(Job(package_idx="1", chat_id=10, message_id=20, submitted_at=0.0))
    await q.submit(Job(package_idx="2", chat_id=10, message_id=21, submitted_at=0.0))

    w = Worker(queue=q, bot=fake_bot, config=_cfg(tmp_path))

    with patch("dccon2signal_bot.worker.convert_pack", side_effect=flaky_convert):
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert calls["n"] == 2
    err_texts = [e["text"] for e in fake_bot.edits if e["message_id"] == 20]
    assert any("❌" in t for t in err_texts)
