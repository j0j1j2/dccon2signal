# dccon2signal Telegram Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a public Telegram bot that runs alongside the existing `dccon2signal` CLI on the same Linux host, accepts a DCcon package ID via `/con2signal`, bare numeric DM, or `@bot <id>` group mention, processes jobs serially through an in-memory queue, and edits a single Telegram message through the lifecycle stages until it shows the Signal install link.

**Architecture:** Refactor the existing CLI pipeline into a shared async `convert_pack(...)` function with an `on_status` callback. Add a new `dccon2signal_bot` subpackage (PTB v21+ Application) that consumes an `asyncio.Queue` with one worker coroutine, calling `convert_pack` and routing status callbacks through a throttled `StatusReporter` that edits the user's Telegram message.

**Tech Stack:** Python 3.12+, uv, `python-telegram-bot[ext]>=21`, existing `dccon2signal` library (httpx, Pillow, signalstickers-client), pytest + pytest-asyncio.

**Spec:** [`docs/superpowers/specs/2026-05-16-dccon2signal-telegram-bot-design.md`](../specs/2026-05-16-dccon2signal-telegram-bot-design.md)

---

## File Structure

**To create:**
- `src/dccon2signal/pipeline.py` — shared async `convert_pack`, `Stage` enum, `ConvertResult`, `StatusCallback` protocol
- `src/dccon2signal_bot/__init__.py`
- `src/dccon2signal_bot/__main__.py` — boot Application, queue, worker; long-polling
- `src/dccon2signal_bot/config.py` — env-driven `BotConfig`
- `src/dccon2signal_bot/status.py` — Korean label table + `render(stage, progress, detail)`
- `src/dccon2signal_bot/reporter.py` — `StatusReporter` with throttled `edit_message_text`
- `src/dccon2signal_bot/queue.py` — `Job` dataclass + `JobQueue` wrapper
- `src/dccon2signal_bot/worker.py` — queue consumer, glue to `convert_pack`
- `src/dccon2signal_bot/handlers.py` — `/con2signal`, `/start`, `/help`, loose triggers
- `tests/bot/__init__.py`
- `tests/bot/conftest.py` — fake clock, fake Telegram bot
- `tests/bot/test_status.py`
- `tests/bot/test_reporter.py`
- `tests/bot/test_queue.py`
- `tests/bot/test_worker.py`
- `tests/bot/test_handlers.py`
- `tests/test_pipeline.py` — covers the refactored `convert_pack`
- `deploy/dccon2signal-bot.service` — systemd unit template

**To modify:**
- `pyproject.toml` — add `python-telegram-bot[ext]>=21` and a `dccon2signal-bot` script entry; widen mypy `files` to include `pipeline.py`, `dccon2signal_bot/*.py` selectively
- `src/dccon2signal/cli.py` — `_run_one` becomes a thin shell over `pipeline.convert_pack`
- `src/dccon2signal/downloader.py` — accept optional `on_progress: Callable[[int, int], Awaitable[None]] | None`
- `src/dccon2signal/image_proc.py` — `process_pack` accepts optional `on_progress`
- `README.md` — add "Telegram bot" section
- `.gitignore` — already covers `.env`; leave as is

Each new module is ≤200 lines. The bot subpackage is split by responsibility, not by Telegram concept.

---

## Task 1: Library refactor — `Stage`, `convert_pack`, progress hooks

**Files:**
- Create: `src/dccon2signal/pipeline.py`
- Modify: `src/dccon2signal/downloader.py`, `src/dccon2signal/image_proc.py`, `src/dccon2signal/cli.py`
- Test: `tests/test_pipeline.py`, update `tests/test_downloader.py`, `tests/test_image_proc.py`

- [ ] **Step 1: Write failing test for `convert_pack` orchestration**

```python
# tests/test_pipeline.py
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from dccon2signal.models import DcconPack, DcconSticker, SignalAuth
from dccon2signal.pipeline import ConvertResult, Stage, convert_pack


@pytest.fixture
def fake_pack() -> DcconPack:
    pack = DcconPack(
        package_idx="170660", title="t", author="a", description="d",
        cover_url="u", cover_bytes=b"COVER",
    )
    pack.stickers.append(DcconSticker(
        idx="1", sort=1, title="1", ext="png", image_url="u", image_bytes=b"S",
    ))
    return pack


@pytest.mark.asyncio
async def test_convert_pack_emits_stage_callbacks(tmp_path, fake_pack):
    seen: list[tuple[Stage, tuple[int, int] | None]] = []

    async def cb(stage, progress=None, detail=""):
        seen.append((stage, progress))

    async def fake_scrape(_idx, _client):
        return fake_pack

    async def fake_download(pack, _client, **_kw):
        on_progress = _kw.get("on_progress")
        if on_progress:
            await on_progress(1, 1)

    def fake_process(pack, **_kw):
        pack.cover_processed = b"\x89PNG\r\n\x1a\nFAKE"
        for s in pack.stickers:
            s.processed_bytes = b"\x89PNG\r\n\x1a\nFAKE"
            s.processed_ext = "png"

    auth = tmp_path / "auth.json"
    auth.write_text('{"username": "+1", "password": "p"}', encoding="utf-8")

    async def fake_upload(_pack, _auth):
        return "abc", "def"

    with patch("dccon2signal.pipeline.scraper.fetch_pack", side_effect=fake_scrape), \
         patch("dccon2signal.pipeline.downloader.download_all", side_effect=fake_download), \
         patch("dccon2signal.pipeline.image_proc.process_pack", side_effect=fake_process), \
         patch("dccon2signal.pipeline.uploader.upload", side_effect=fake_upload):
        result = await convert_pack(
            "170660",
            auth_path=auth,
            out_dir=tmp_path / "out",
            on_status=cb,
        )

    assert isinstance(result, ConvertResult)
    assert result.pack_id == "abc"
    assert result.pack_key == "def"
    assert result.install_url.endswith("pack_id=abc&pack_key=def")

    stages = [s for s, _ in seen]
    assert Stage.FETCHING in stages
    assert Stage.DOWNLOADING in stages
    assert Stage.PROCESSING in stages
    assert Stage.SAVING in stages
    assert Stage.UPLOADING in stages
    assert stages[-1] == Stage.DONE


@pytest.mark.asyncio
async def test_convert_pack_no_upload(tmp_path, fake_pack):
    async def fake_scrape(_idx, _client):
        return fake_pack

    async def fake_download(pack, _client, **_kw):
        pass

    def fake_process(pack, **_kw):
        pack.cover_processed = b"\x89PNG\r\n\x1a\nFAKE"
        for s in pack.stickers:
            s.processed_bytes = b"\x89PNG\r\n\x1a\nFAKE"
            s.processed_ext = "png"

    with patch("dccon2signal.pipeline.scraper.fetch_pack", side_effect=fake_scrape), \
         patch("dccon2signal.pipeline.downloader.download_all", side_effect=fake_download), \
         patch("dccon2signal.pipeline.image_proc.process_pack", side_effect=fake_process):
        result = await convert_pack(
            "170660", auth_path=tmp_path / "missing.json",
            out_dir=tmp_path / "out", upload=False,
        )

    assert result.pack_id == ""
    assert result.pack_key == ""
    assert result.install_url == ""
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: FAIL — `cannot import name 'convert_pack'`.

- [ ] **Step 3: Implement `src/dccon2signal/pipeline.py`**

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

import httpx

from dccon2signal import auth as auth_mod
from dccon2signal import downloader, image_proc, pack_builder, persistence, scraper, uploader
from dccon2signal.uploader import install_url as _install_url


class Stage(str, Enum):
    QUEUED = "queued"           # bot-only; pipeline never emits this
    FETCHING = "fetching"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    SAVING = "saving"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"


class StatusCallback(Protocol):
    async def __call__(
        self,
        stage: Stage,
        progress: tuple[int, int] | None = None,
        detail: str = "",
    ) -> None: ...


@dataclass
class ConvertResult:
    pack_id: str
    pack_key: str
    title: str
    author: str
    sticker_count: int
    install_url: str


async def _noop(*_args, **_kwargs) -> None:  # default callback
    return None


async def convert_pack(
    package_idx: str,
    *,
    auth_path: Path,
    out_dir: Path,
    upload: bool = True,
    remove_bg: bool = False,
    static_only: bool = False,
    title_override: str | None = None,
    author_override: str | None = None,
    emoji_map: dict[str, str] | None = None,
    on_status: StatusCallback | None = None,
) -> ConvertResult:
    on_status = on_status or _noop  # type: ignore[assignment]

    async with httpx.AsyncClient() as client:
        await on_status(Stage.FETCHING)
        pack = await scraper.fetch_pack(package_idx, client)
        if title_override:
            pack.title = title_override
        if author_override:
            pack.author = author_override

        total = len(pack.stickers)

        async def dl_progress(done: int, total_: int) -> None:
            await on_status(Stage.DOWNLOADING, (done, total_))

        await on_status(Stage.DOWNLOADING, (0, total))
        await downloader.download_all(pack, client, on_progress=dl_progress)

        async def proc_progress(done: int, total_: int) -> None:
            await on_status(Stage.PROCESSING, (done, total_))

        await on_status(Stage.PROCESSING, (0, total))
        image_proc.process_pack(
            pack, remove_bg=remove_bg, static_only=static_only, on_progress=proc_progress,
        )

        await on_status(Stage.SAVING)
        pack_dir = out_dir / pack.package_idx
        persistence.save_pack(pack, pack_dir)

        if not upload:
            await on_status(Stage.DONE)
            return ConvertResult(
                pack_id="",
                pack_key="",
                title=pack.title,
                author=pack.author,
                sticker_count=total,
                install_url="",
            )

        await on_status(Stage.UPLOADING)
        auth = auth_mod.load(auth_path)
        signal_pack = pack_builder.build(pack, emoji_map=emoji_map)
        pack_id, pack_key = await uploader.upload(signal_pack, auth)

        await on_status(Stage.DONE)
        return ConvertResult(
            pack_id=pack_id,
            pack_key=pack_key,
            title=pack.title,
            author=pack.author,
            sticker_count=total,
            install_url=_install_url(pack_id, pack_key),
        )
```

- [ ] **Step 4: Add `on_progress` hook to `downloader.download_all`**

Open `src/dccon2signal/downloader.py` and replace the `download_all` function with this version (keep `_fetch_one`, `HEADERS`, `logger` as is):

```python
async def download_all(
    pack: DcconPack,
    client: httpx.AsyncClient,
    *,
    concurrency: int = 8,
    retries: int = 3,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> None:
    sem = asyncio.Semaphore(concurrency)
    total = len(pack.stickers) + 1  # +1 for cover
    done = 0
    lock = asyncio.Lock()

    async def _bumped_fetch(url: str) -> bytes | None:
        nonlocal done
        async with sem:
            data = await _fetch_one(client, url, retries=retries)
        async with lock:
            done += 1
            current = done
        if on_progress is not None:
            await on_progress(current, total)
        return data

    async def _fetch_cover() -> None:
        pack.cover_bytes = await _bumped_fetch(pack.cover_url)

    async def _fetch_sticker(s: DcconSticker) -> None:
        s.image_bytes = await _bumped_fetch(s.image_url)

    await asyncio.gather(
        _fetch_cover(),
        *(_fetch_sticker(s) for s in pack.stickers),
    )
```

Also add the imports at the top of `downloader.py`:
```python
from collections.abc import Awaitable, Callable
```

- [ ] **Step 5: Add `on_progress` hook to `image_proc.process_pack`**

In `src/dccon2signal/image_proc.py`, replace `process_pack` with:

```python
def process_pack(
    pack: DcconPack,
    *,
    remove_bg: bool = False,
    static_only: bool = False,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> None:
    total = len(pack.stickers) + (1 if pack.cover_bytes is not None else 0)
    done = 0

    if pack.cover_bytes is not None:
        pack.cover_processed, _ = process_sticker_bytes(
            pack.cover_bytes, source_ext="png", remove_bg=remove_bg, static_only=True
        )
        done += 1
        if on_progress is not None:
            _schedule_async(on_progress(done, total))

    for s in pack.stickers:
        if s.image_bytes is None:
            done += 1
            if on_progress is not None:
                _schedule_async(on_progress(done, total))
            continue
        s.processed_bytes, s.processed_ext = process_sticker_bytes(
            s.image_bytes,
            source_ext=s.ext,
            remove_bg=remove_bg,
            static_only=static_only,
        )
        done += 1
        if on_progress is not None:
            _schedule_async(on_progress(done, total))


def _schedule_async(coro) -> None:
    """Fire an awaitable from sync code while the asyncio loop is running.

    process_pack is intentionally synchronous (Pillow is sync). The bot worker
    invokes it from an async context, so a running loop is available; we just
    schedule the callback so it gets a turn.
    """
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (CLI sync context). Drop the callback.
        return
    loop.create_task(coro)
```

Add to imports at the top of `image_proc.py`:
```python
from collections.abc import Awaitable, Callable
```

- [ ] **Step 6: Update CLI `_run_one` to call `convert_pack`**

In `src/dccon2signal/cli.py`, replace the `_run_one` function with a thin shell:

```python
async def _run_one(
    package_idx: str,
    client: httpx.AsyncClient,
    title_override: str | None,
    author_override: str | None,
    out_dir: Path,
    *,
    no_upload: bool,
    remove_bg: bool,
    static_only: bool,
    emoji_map: dict[str, str] | None,
    auth_path: Path,
) -> None:
    click.echo(f"→ {package_idx}")

    from dccon2signal.pipeline import Stage, convert_pack

    async def echo(stage: Stage, progress=None, detail=""):
        msg = stage.value
        if progress is not None:
            msg = f"{msg} {progress[0]}/{progress[1]}"
        if detail:
            msg = f"{msg} — {detail}"
        click.echo(f"  {msg}")

    result = await convert_pack(
        package_idx,
        auth_path=auth_path,
        out_dir=out_dir,
        upload=not no_upload,
        remove_bg=remove_bg,
        static_only=static_only,
        title_override=title_override,
        author_override=author_override,
        emoji_map=emoji_map,
        on_status=echo,
    )

    click.echo(f"  Fetched: {result.title!r} ({result.sticker_count} stickers) by {result.author!r}")
    if result.install_url:
        click.echo(f"  Install: {result.install_url}")
```

Remove the unused imports from `cli.py`'s top section if any are no longer referenced after the refactor (the existing imports `from dccon2signal import auth as auth_mod`, `from dccon2signal import downloader, image_proc, pack_builder, persistence, scraper, uploader` are no longer needed in `cli.py`).

Replace those import lines with:
```python
from dccon2signal.models import DcconPack  # not used elsewhere — drop if unused
```

Actually `DcconPack` is no longer referenced in cli.py either. Remove it. The minimal needed imports at the top of `cli.py` become:

```python
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
import httpx
```

(Plus `from dccon2signal.pipeline import Stage, convert_pack` inside `_run_one` to avoid a cyclic-import risk at module load time.)

- [ ] **Step 7: Update existing `tests/test_downloader.py` and `tests/test_image_proc.py` for the new kwarg**

`tests/test_downloader.py` and `tests/test_image_proc.py` should still pass because the new `on_progress` parameter is optional (`None` default) and not required by existing assertions. Confirm:

Run: `uv run pytest tests/test_downloader.py tests/test_image_proc.py -v`
Expected: all existing tests still PASS.

- [ ] **Step 8: Run the new pipeline test**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: 2 PASS.

- [ ] **Step 9: Run the existing CLI test**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 2 PASS (the CLI still works because pipeline output is the same).

- [ ] **Step 10: Run the full suite**

Run: `uv run pytest -q`
Expected: all previous tests + 2 new pipeline tests pass (33 total).

- [ ] **Step 11: Commit**

```bash
git add src/dccon2signal/pipeline.py \
        src/dccon2signal/downloader.py \
        src/dccon2signal/image_proc.py \
        src/dccon2signal/cli.py \
        tests/test_pipeline.py
git commit -m "Extract pipeline.convert_pack with progress callbacks

Adds dccon2signal.pipeline as the shared high-level entry point for both
the CLI and the upcoming Telegram bot. downloader.download_all and
image_proc.process_pack now accept an optional on_progress async callback.
CLI is simplified to a thin shell over convert_pack."
```

---

## Task 2: Add bot dependencies and skeleton package

**Files:**
- Modify: `pyproject.toml`
- Create: `src/dccon2signal_bot/__init__.py`, `tests/bot/__init__.py`, `tests/bot/conftest.py`

- [ ] **Step 1: Modify `pyproject.toml`**

Update the `[project]` section to add the bot dep:

```toml
[project]
name = "dccon2signal"
version = "0.1.0"
description = "Convert DCInside DCcon packages to Signal sticker packs"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.24,<0.25",
    "pillow>=10.3",
    "signalstickers-client>=3.3",
    "click>=8.1",
    "python-telegram-bot[ext]>=21",
]

[project.scripts]
dccon2signal = "dccon2signal.cli:main"
dccon2signal-bot = "dccon2signal_bot.__main__:run"
```

Also update the wheel package list and mypy `files` list:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/dccon2signal", "src/dccon2signal_bot"]

[tool.mypy]
python_version = "3.12"
strict = true
files = [
    "src/dccon2signal/models.py",
    "src/dccon2signal/scraper.py",
    "src/dccon2signal/image_proc.py",
    "src/dccon2signal/pack_builder.py",
    "src/dccon2signal/persistence.py",
    "src/dccon2signal/pipeline.py",
    "src/dccon2signal_bot/config.py",
    "src/dccon2signal_bot/status.py",
    "src/dccon2signal_bot/reporter.py",
    "src/dccon2signal_bot/queue.py",
]
```

(Worker, handlers, and `__main__` stay outside strict mypy because they're PTB-heavy with poorly-typed external surfaces.)

- [ ] **Step 2: Create skeleton files**

```python
# src/dccon2signal_bot/__init__.py
__version__ = "0.1.0"
```

```python
# tests/bot/__init__.py
```

```python
# tests/bot/conftest.py
import time

import pytest


class FakeClock:
    """Manually-advanced monotonic clock for testing throttling."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


class FakeBot:
    """Minimal stand-in for telegram.Bot capturing edit_message_text calls."""

    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.sent: list[dict] = []

    async def edit_message_text(self, text, chat_id, message_id, **kw) -> None:
        self.edits.append({"text": text, "chat_id": chat_id, "message_id": message_id, **kw})

    async def send_message(self, chat_id, text, **kw) -> None:
        self.sent.append({"chat_id": chat_id, "text": text, **kw})


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()
```

- [ ] **Step 3: Run `uv sync` to install the new dependency**

Run: `uv sync`
Expected: PTB installed (no resolution errors).

- [ ] **Step 4: Verify import works**

Run: `uv run python -c "import dccon2signal_bot; print(dccon2signal_bot.__version__)"`
Expected: `0.1.0`.

Run: `uv run python -c "from telegram.ext import Application; print('ok')"`
Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock src/dccon2signal_bot/__init__.py \
        tests/bot/__init__.py tests/bot/conftest.py
git commit -m "Scaffold dccon2signal_bot package and add PTB dependency"
```

---

## Task 3: `config.py` — environment-driven config

**Files:**
- Create: `src/dccon2signal_bot/config.py`
- Test: `tests/bot/test_config.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_config.py
from pathlib import Path

import pytest

from dccon2signal_bot.config import BotConfig, ConfigError, load


def test_load_requires_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load()


def test_load_uses_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:123")
    monkeypatch.delenv("DCCON2SIGNAL_AUTH", raising=False)
    monkeypatch.delenv("DCCON2SIGNAL_OUT_DIR", raising=False)
    monkeypatch.delenv("BOT_ADMIN_CHAT_ID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load()
    assert isinstance(cfg, BotConfig)
    assert cfg.telegram_token == "abc:123"
    assert cfg.auth_path == tmp_path / ".config" / "dccon2signal" / "auth.json"
    assert cfg.out_dir == Path("./out")
    assert cfg.admin_chat_id is None
    assert cfg.log_level == "INFO"


def test_load_honours_explicit_envs(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:123")
    monkeypatch.setenv("DCCON2SIGNAL_AUTH", str(tmp_path / "a.json"))
    monkeypatch.setenv("DCCON2SIGNAL_OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("BOT_ADMIN_CHAT_ID", "12345")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    cfg = load()
    assert cfg.auth_path == tmp_path / "a.json"
    assert cfg.out_dir == tmp_path / "out"
    assert cfg.admin_chat_id == 12345
    assert cfg.log_level == "DEBUG"
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/bot/test_config.py -v`
Expected: FAIL — `cannot import name 'BotConfig'`.

- [ ] **Step 3: Implement `src/dccon2signal_bot/config.py`**

```python
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Raised when required env vars are missing or invalid."""


@dataclass(frozen=True)
class BotConfig:
    telegram_token: str
    auth_path: Path
    out_dir: Path
    admin_chat_id: int | None
    log_level: str


def load() -> BotConfig:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN environment variable is required")

    home = Path(os.environ.get("HOME", str(Path.home())))
    auth_path = Path(
        os.environ.get(
            "DCCON2SIGNAL_AUTH",
            str(home / ".config" / "dccon2signal" / "auth.json"),
        )
    )
    out_dir = Path(os.environ.get("DCCON2SIGNAL_OUT_DIR", "./out"))
    admin_raw = os.environ.get("BOT_ADMIN_CHAT_ID")
    admin_chat_id = int(admin_raw) if admin_raw else None
    log_level = os.environ.get("LOG_LEVEL", "INFO")

    return BotConfig(
        telegram_token=token,
        auth_path=auth_path,
        out_dir=out_dir,
        admin_chat_id=admin_chat_id,
        log_level=log_level,
    )
```

- [ ] **Step 4: Run test — expect pass**

Run: `uv run pytest tests/bot/test_config.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal_bot/config.py tests/bot/test_config.py
git commit -m "Add BotConfig env loader"
```

---

## Task 4: `status.py` — Stage labels and rendering

**Files:**
- Create: `src/dccon2signal_bot/status.py`
- Test: `tests/bot/test_status.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_status.py
from dccon2signal.pipeline import Stage
from dccon2signal_bot.status import render


def test_render_simple_stage():
    out = render(Stage.FETCHING)
    assert "디시콘 정보" in out


def test_render_with_progress():
    out = render(Stage.DOWNLOADING, progress=(12, 45))
    assert "(12/45)" in out
    assert "이미지 다운로드" in out


def test_render_with_detail_line():
    out = render(Stage.FAILED, detail="패키지 999999 를 찾을 수 없습니다")
    assert "❌" in out
    assert "999999" in out


def test_render_done_and_failed_have_distinct_prefixes():
    done = render(Stage.DONE)
    failed = render(Stage.FAILED, detail="something broke")
    assert "✅" in done
    assert "❌" in failed
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/bot/test_status.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `src/dccon2signal_bot/status.py`**

```python
from dccon2signal.pipeline import Stage

_LABELS_KO: dict[Stage, str] = {
    Stage.QUEUED: "⏳ 큐 대기 중",
    Stage.FETCHING: "📥 디시콘 정보 가져오는 중...",
    Stage.DOWNLOADING: "📥 이미지 다운로드 중",
    Stage.PROCESSING: "✨ 이미지 변환 중",
    Stage.SAVING: "💾 저장 중...",
    Stage.UPLOADING: "🚀 Signal 업로드 중...",
    Stage.DONE: "✅ 완료!",
    Stage.FAILED: "❌ 실패",
}


def render(
    stage: Stage,
    progress: tuple[int, int] | None = None,
    detail: str = "",
) -> str:
    base = _LABELS_KO[stage]
    if progress is not None:
        base = f"{base} ({progress[0]}/{progress[1]})"
    if detail:
        base = f"{base}\n{detail}"
    return base
```

- [ ] **Step 4: Run test — expect pass**

Run: `uv run pytest tests/bot/test_status.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal_bot/status.py tests/bot/test_status.py
git commit -m "Add Korean stage label rendering"
```

---

## Task 5: `reporter.py` — throttled message edits

**Files:**
- Create: `src/dccon2signal_bot/reporter.py`
- Test: `tests/bot/test_reporter.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_reporter.py
import pytest

from dccon2signal.pipeline import Stage
from dccon2signal_bot.reporter import StatusReporter


@pytest.mark.asyncio
async def test_first_update_flushes(fake_bot, fake_clock):
    r = StatusReporter(fake_bot, chat_id=10, message_id=20,
                       min_interval=1.5, clock=fake_clock.monotonic)
    await r.update(Stage.FETCHING)
    assert len(fake_bot.edits) == 1
    assert "디시콘 정보" in fake_bot.edits[0]["text"]


@pytest.mark.asyncio
async def test_rapid_updates_are_throttled(fake_bot, fake_clock):
    r = StatusReporter(fake_bot, chat_id=10, message_id=20,
                       min_interval=1.5, clock=fake_clock.monotonic)
    await r.update(Stage.DOWNLOADING, (1, 10))
    await r.update(Stage.DOWNLOADING, (2, 10))
    await r.update(Stage.DOWNLOADING, (3, 10))
    # Only the first lands; later ones are inside the interval.
    assert len(fake_bot.edits) == 1


@pytest.mark.asyncio
async def test_update_after_interval_flushes(fake_bot, fake_clock):
    r = StatusReporter(fake_bot, chat_id=10, message_id=20,
                       min_interval=1.5, clock=fake_clock.monotonic)
    await r.update(Stage.DOWNLOADING, (1, 10))
    fake_clock.advance(2.0)
    await r.update(Stage.DOWNLOADING, (5, 10))
    assert len(fake_bot.edits) == 2


@pytest.mark.asyncio
async def test_done_always_flushes(fake_bot, fake_clock):
    r = StatusReporter(fake_bot, chat_id=10, message_id=20,
                       min_interval=1.5, clock=fake_clock.monotonic)
    await r.update(Stage.DOWNLOADING, (1, 10))
    await r.update(Stage.DONE)  # immediately after — must flush
    assert len(fake_bot.edits) == 2
    assert "완료" in fake_bot.edits[1]["text"]


@pytest.mark.asyncio
async def test_unchanged_text_not_resent(fake_bot, fake_clock):
    r = StatusReporter(fake_bot, chat_id=10, message_id=20,
                       min_interval=0.0, clock=fake_clock.monotonic)
    await r.update(Stage.DOWNLOADING, (5, 10))
    await r.update(Stage.DOWNLOADING, (5, 10))  # identical
    assert len(fake_bot.edits) == 1


@pytest.mark.asyncio
async def test_edit_failure_does_not_raise(fake_clock):
    class BrokenBot:
        async def edit_message_text(self, **kw):
            raise RuntimeError("message deleted")

    r = StatusReporter(BrokenBot(), chat_id=10, message_id=20,
                       min_interval=0.0, clock=fake_clock.monotonic)
    # Should not raise — reporter swallows + logs.
    await r.update(Stage.DONE)
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/bot/test_reporter.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `src/dccon2signal_bot/reporter.py`**

```python
import logging
import time
from collections.abc import Callable

from dccon2signal.pipeline import Stage
from dccon2signal_bot.status import render

logger = logging.getLogger(__name__)


class StatusReporter:
    """Throttle and dedupe edits to a single Telegram message."""

    def __init__(
        self,
        bot,
        *,
        chat_id: int,
        message_id: int,
        min_interval: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._min_interval = min_interval
        self._clock = clock
        self._last_sent_at = -1e9
        self._last_text: str | None = None

    async def update(
        self,
        stage: Stage,
        progress: tuple[int, int] | None = None,
        detail: str = "",
    ) -> None:
        text = render(stage, progress, detail)
        if text == self._last_text:
            return

        now = self._clock()
        terminal = stage in (Stage.DONE, Stage.FAILED)
        if not terminal and now - self._last_sent_at < self._min_interval:
            return

        try:
            await self._bot.edit_message_text(
                text=text, chat_id=self._chat_id, message_id=self._message_id,
            )
        except Exception as e:
            logger.warning("edit_message_text failed: %s", e)
            return

        self._last_sent_at = now
        self._last_text = text
```

- [ ] **Step 4: Run test — expect pass**

Run: `uv run pytest tests/bot/test_reporter.py -v`
Expected: 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal_bot/reporter.py tests/bot/test_reporter.py
git commit -m "Add throttled StatusReporter for live message edits"
```

---

## Task 6: `queue.py` — Job and JobQueue

**Files:**
- Create: `src/dccon2signal_bot/queue.py`
- Test: `tests/bot/test_queue.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_queue.py
import time

import pytest

from dccon2signal_bot.queue import Job, JobQueue


def _job(pkg: str) -> Job:
    return Job(package_idx=pkg, chat_id=1, message_id=2, submitted_at=time.time())


@pytest.mark.asyncio
async def test_submit_returns_1_based_position():
    q = JobQueue()
    pos1 = await q.submit(_job("1"))
    pos2 = await q.submit(_job("2"))
    pos3 = await q.submit(_job("3"))
    assert (pos1, pos2, pos3) == (1, 2, 3)
    assert q.size == 3


@pytest.mark.asyncio
async def test_fifo_order():
    q = JobQueue()
    await q.submit(_job("a"))
    await q.submit(_job("b"))
    first = await q.get()
    second = await q.get()
    assert first.package_idx == "a"
    assert second.package_idx == "b"


@pytest.mark.asyncio
async def test_task_done_after_get():
    q = JobQueue()
    await q.submit(_job("a"))
    job = await q.get()
    q.task_done()
    assert q.size == 0
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/bot/test_queue.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `src/dccon2signal_bot/queue.py`**

```python
import asyncio
from dataclasses import dataclass


@dataclass
class Job:
    package_idx: str
    chat_id: int
    message_id: int
    submitted_at: float


class JobQueue:
    """asyncio.Queue wrapper that also exposes current size and 1-based submit position."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[Job] = asyncio.Queue()

    async def submit(self, job: Job) -> int:
        await self._q.put(job)
        return self._q.qsize()

    async def get(self) -> Job:
        return await self._q.get()

    def task_done(self) -> None:
        self._q.task_done()

    @property
    def size(self) -> int:
        return self._q.qsize()
```

- [ ] **Step 4: Run test — expect pass**

Run: `uv run pytest tests/bot/test_queue.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal_bot/queue.py tests/bot/test_queue.py
git commit -m "Add Job and JobQueue"
```

---

## Task 7: `worker.py` — queue consumer

**Files:**
- Create: `src/dccon2signal_bot/worker.py`
- Test: `tests/bot/test_worker.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_worker.py
import asyncio
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
            pack_id="abc", pack_key="def", title="t", author="a",
            sticker_count=1,
            install_url="https://signal.art/addstickers/#pack_id=abc&pack_key=def",
        )

    q = JobQueue()
    await q.submit(Job(package_idx="1", chat_id=10, message_id=20,
                       submitted_at=time.time()))

    w = Worker(queue=q, bot=fake_bot, config=_cfg(tmp_path))

    with patch("dccon2signal_bot.worker.convert_pack", side_effect=fake_convert):
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # Last edit should include install URL
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
            pack_id="ok", pack_key="ok", title="t", author="a",
            sticker_count=0,
            install_url="https://signal.art/addstickers/#pack_id=ok&pack_key=ok",
        )

    q = JobQueue()
    await q.submit(Job(package_idx="1", chat_id=10, message_id=20, submitted_at=0.0))
    await q.submit(Job(package_idx="2", chat_id=10, message_id=21, submitted_at=0.0))

    w = Worker(queue=q, bot=fake_bot, config=_cfg(tmp_path))

    with patch("dccon2signal_bot.worker.convert_pack", side_effect=flaky_convert):
        task = asyncio.create_task(w.run())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert calls["n"] == 2  # the second job still ran after the first failed
    # The first message got an error edit
    err_texts = [e["text"] for e in fake_bot.edits if e["message_id"] == 20]
    assert any("❌" in t for t in err_texts)
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/bot/test_worker.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `src/dccon2signal_bot/worker.py`**

```python
import logging

from dccon2signal.auth import AuthError
from dccon2signal.pack_builder import PackBuilderError
from dccon2signal.pipeline import Stage, convert_pack
from dccon2signal.scraper import ScraperError
from dccon2signal.uploader import UploaderError
from dccon2signal_bot.config import BotConfig
from dccon2signal_bot.queue import Job, JobQueue
from dccon2signal_bot.reporter import StatusReporter
from dccon2signal_bot.status import render

logger = logging.getLogger(__name__)

# Bot-side-only exceptions that should page the admin (if configured).
_ADMIN_PAGE = (AuthError, UploaderError)


class Worker:
    def __init__(self, *, queue: JobQueue, bot, config: BotConfig) -> None:
        self._queue = queue
        self._bot = bot
        self._config = config

    async def run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            except Exception:
                logger.exception("Unhandled error in worker for pkg %s", job.package_idx)
            finally:
                self._queue.task_done()

    async def _process(self, job: Job) -> None:
        reporter = StatusReporter(
            self._bot, chat_id=job.chat_id, message_id=job.message_id,
        )
        try:
            result = await convert_pack(
                job.package_idx,
                auth_path=self._config.auth_path,
                out_dir=self._config.out_dir,
                on_status=reporter.update,
            )
        except (ScraperError, PackBuilderError, AuthError, UploaderError) as e:
            await self._report_failure(reporter, e, job)
        except Exception as e:
            logger.exception("Unexpected error for pkg %s", job.package_idx)
            await self._report_failure(reporter, e, job, hide_detail=True)
        else:
            final = (
                f"✅ 완료!\n"
                f"팩: {result.title!r} ({result.sticker_count}개)\n"
                f"{result.install_url}"
            )
            try:
                await self._bot.edit_message_text(
                    text=final, chat_id=job.chat_id, message_id=job.message_id,
                )
            except Exception:
                logger.warning("Could not edit final message", exc_info=True)

    async def _report_failure(
        self,
        reporter: StatusReporter,
        exc: Exception,
        job: Job,
        *,
        hide_detail: bool = False,
    ) -> None:
        msg = (
            "알 수 없는 오류, 관리자에게 알림"
            if hide_detail
            else f"{type(exc).__name__}: {exc}"
        )
        await reporter.update(Stage.FAILED, detail=msg)
        if isinstance(exc, _ADMIN_PAGE) and self._config.admin_chat_id:
            try:
                await self._bot.send_message(
                    chat_id=self._config.admin_chat_id,
                    text=f"⚠️ pkg {job.package_idx}: {type(exc).__name__}: {exc}",
                )
            except Exception:
                logger.warning("Admin notification failed", exc_info=True)
```

- [ ] **Step 4: Run test — expect pass**

Run: `uv run pytest tests/bot/test_worker.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal_bot/worker.py tests/bot/test_worker.py
git commit -m "Add Worker that consumes JobQueue and runs convert_pack"
```

---

## Task 8: `handlers.py` — command + loose triggers

**Files:**
- Create: `src/dccon2signal_bot/handlers.py`
- Test: `tests/bot/test_handlers.py`

- [ ] **Step 1: Write failing test**

```python
# tests/bot/test_handlers.py
import time
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
    context = SimpleNamespace(args=["abc"], application=SimpleNamespace(bot_data={"queue": JobQueue()}))
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
    context = SimpleNamespace(args=["170660"], application=SimpleNamespace(bot_data={"queue": q}))

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
    assert q.size == 0  # ignored — no mention


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
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/bot/test_handlers.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `src/dccon2signal_bot/handlers.py`**

```python
import re
import time

from dccon2signal_bot.queue import Job

_PKG_RE = re.compile(r"(?:dccon\.dcinside\.com/[^\s]*#)?(\d{3,})\s*$")


def _extract_pkg(text: str) -> str | None:
    stripped = text.strip()
    m = _PKG_RE.fullmatch(stripped) if "://" not in stripped else _PKG_RE.search(stripped)
    return m.group(1) if m else None


async def _enqueue(update, context, pkg: str) -> None:
    queue = context.application.bot_data["queue"]
    position = queue.size + 1
    reply = await update.message.reply_text(f"⏳ 큐 대기 중 ({position}번째)")
    job = Job(
        package_idx=pkg,
        chat_id=reply.chat_id,
        message_id=reply.message_id,
        submitted_at=time.time(),
    )
    await queue.submit(job)


async def cmd_con2signal(update, context) -> None:
    args = getattr(context, "args", None) or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "사용법: /con2signal <package_idx>\n예: /con2signal 170660\n"
            "디시콘 패키지 ID는 숫자여야 합니다."
        )
        return
    await _enqueue(update, context, args[0])


async def msg_loose(update, context) -> None:
    msg = update.message
    text = msg.text or ""

    if msg.chat.type == "private":
        pkg = _extract_pkg(text)
    else:
        me = await context.bot.get_me()
        mention = f"@{me.username}"
        if mention not in text:
            return
        pkg = _extract_pkg(text.replace(mention, "", 1))

    if pkg is not None:
        await _enqueue(update, context, pkg)


async def cmd_start_or_help(update, context) -> None:
    await update.message.reply_text(
        "DCcon → Signal 스티커팩 변환 봇\n\n"
        "사용법:\n"
        "  /con2signal <package_idx>\n"
        "  DM 에서는 그냥 숫자만 보내도 됩니다\n"
        "  그룹에서는 '@봇이름 <package_idx>'\n\n"
        "예: /con2signal 170660"
    )
```

- [ ] **Step 4: Run test — expect pass**

Run: `uv run pytest tests/bot/test_handlers.py -v`
Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal_bot/handlers.py tests/bot/test_handlers.py
git commit -m "Add command + loose trigger handlers (DM digits / group mention)"
```

---

## Task 9: `__main__.py` — boot the bot

**Files:**
- Create: `src/dccon2signal_bot/__main__.py`

This task has no unit test (it's the integration point). It's exercised manually in Task 11.

- [ ] **Step 1: Implement `src/dccon2signal_bot/__main__.py`**

```python
from __future__ import annotations

import asyncio
import logging
import signal as signal_mod
import sys

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from dccon2signal_bot.config import ConfigError, load
from dccon2signal_bot.handlers import cmd_con2signal, cmd_start_or_help, msg_loose
from dccon2signal_bot.queue import JobQueue
from dccon2signal_bot.worker import Worker


async def _main() -> int:
    try:
        cfg = load()
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger(__name__)

    queue = JobQueue()

    app = Application.builder().token(cfg.telegram_token).build()
    app.bot_data["queue"] = queue

    app.add_handler(CommandHandler("start", cmd_start_or_help))
    app.add_handler(CommandHandler("help", cmd_start_or_help))
    app.add_handler(CommandHandler("con2signal", cmd_con2signal))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_loose))

    worker = Worker(queue=queue, bot=app.bot, config=cfg)

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    worker_task = asyncio.create_task(worker.run(), name="worker")

    stop = asyncio.Event()

    def _request_stop(*_args) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal_mod.SIGINT, signal_mod.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            # add_signal_handler isn't supported on some platforms (e.g. Windows).
            pass

    logger.info("Bot started, polling for updates")
    await stop.wait()
    logger.info("Shutting down...")

    worker_task.cancel()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    return 0


def run() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    run()
```

- [ ] **Step 2: Smoke-test imports**

Run: `uv run python -c "from dccon2signal_bot.__main__ import run; print('ok')"`
Expected: `ok` (the module imports cleanly).

- [ ] **Step 3: Smoke-test config error path**

Run: `unset TELEGRAM_BOT_TOKEN; uv run dccon2signal-bot 2>&1 | head -2`
Expected: `Config error: TELEGRAM_BOT_TOKEN environment variable is required` and a non-zero exit code.

- [ ] **Step 4: Commit**

```bash
git add src/dccon2signal_bot/__main__.py
git commit -m "Boot the Telegram bot Application with queue and worker"
```

---

## Task 10: systemd unit + README update

**Files:**
- Create: `deploy/dccon2signal-bot.service`, `deploy/dccon2signal-bot.env.example`
- Modify: `README.md`

- [ ] **Step 1: Write systemd unit template**

Create `deploy/dccon2signal-bot.service`:

```ini
[Unit]
Description=DCcon to Signal sticker bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/projects/signalsticker
ExecStart=%h/projects/signalsticker/.venv/bin/python -m dccon2signal_bot
Restart=on-failure
RestartSec=10
EnvironmentFile=%h/.config/dccon2signal-bot.env

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Write env file example**

Create `deploy/dccon2signal-bot.env.example`:

```
TELEGRAM_BOT_TOKEN=123456:replace_with_token_from_botfather
DCCON2SIGNAL_AUTH=/home/youruser/.config/dccon2signal/auth.json
DCCON2SIGNAL_OUT_DIR=/home/youruser/projects/signalsticker/out
# BOT_ADMIN_CHAT_ID=12345678   # optional — your Telegram chat ID for error pings
LOG_LEVEL=INFO
```

- [ ] **Step 3: Add a "Telegram bot" section to README**

Append to `README.md`:

```markdown
## 텔레그램 봇 (선택 사항)

`dccon2signal` 을 텔레그램으로 호출할 수 있는 봇이 같이 들어있어요. 큐가 직렬이라 봇 하나로 여러 사람이 써도 Signal rate limit 에 안 걸립니다.

### 1. BotFather 에서 봇 생성

폰 텔레그램에서 [@BotFather](https://t.me/BotFather) 와 채팅:
- `/newbot` → 봇 이름 + username 입력
- 받은 **HTTP API token** (e.g. `123456:ABC-...`) 을 저장

### 2. 환경 변수 파일

```bash
cp deploy/dccon2signal-bot.env.example ~/.config/dccon2signal-bot.env
chmod 600 ~/.config/dccon2signal-bot.env
# 그리고 ~/.config/dccon2signal-bot.env 열어서 TELEGRAM_BOT_TOKEN 채우기
```

`DCCON2SIGNAL_AUTH` 는 `auth.json` 셋업이 이미 끝난 상태여야 동작합니다 (위 "자동 업로드 — Signal 인증 설정" 섹션 참조).

### 3. systemd 유저 서비스로 실행

```bash
mkdir -p ~/.config/systemd/user
cp deploy/dccon2signal-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now dccon2signal-bot
systemctl --user status dccon2signal-bot
journalctl --user -u dccon2signal-bot -f      # 실시간 로그
```

수동으로 한번 돌려보고 싶으면:
```bash
set -a; source ~/.config/dccon2signal-bot.env; set +a
uv run dccon2signal-bot
```

### 4. 사용

폰 텔레그램에서 봇을 찾아 다음 중 하나로 호출:

| 어디서 | 어떻게 |
|---|---|
| DM | `170660` (숫자만 보내도 됨) |
| DM | `/con2signal 170660` |
| 그룹 | `@봇username 170660` |
| 그룹 | `/con2signal 170660` |

봇이 보낸 메시지가 단계별로 갱신되다가 마지막에 Signal 설치 링크로 바뀝니다.

### 5. 트러블슈팅

- 봇이 아무 응답 안함 → 토큰 확인 (`systemctl --user status dccon2signal-bot` 로 로그 보기)
- `❌ 봇 측 인증 문제` → `auth.json` 만료. signal-cli 재링크 + auth.json 재생성
- `Signal rate limit` → 60초 후 자동 재시도 (1회). 그래도 안되면 디시콘 너무 자주 변환했을 가능성
```

- [ ] **Step 4: Commit**

```bash
git add deploy/ README.md
git commit -m "Add systemd unit, env example, and README bot section"
```

---

## Task 11: Manual end-to-end verification

**No code changes.** This task validates the bot against a live Telegram client.

- [ ] **Step 1: Lint and type-check**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy`
Expected: clean.

- [ ] **Step 2: Run the full test suite**

Run: `uv run pytest -q`
Expected: all PASS (≥ ~48 tests).

- [ ] **Step 3: Create the bot in BotFather (if not done already)**

Follow Task 10 step 1. Save the token.

- [ ] **Step 4: Configure env**

Follow Task 10 step 2. Make sure `DCCON2SIGNAL_AUTH` points at a valid `auth.json` (`auth verify` semantically: a previous CLI upload should have succeeded with these credentials).

- [ ] **Step 5: Run the bot**

```bash
set -a; source ~/.config/dccon2signal-bot.env; set +a
uv run dccon2signal-bot
```

Expected: log line `Bot started, polling for updates`.

- [ ] **Step 6: Drive it from Telegram**

In a DM with the bot, send:
- `/start` — Expected: usage instructions reply.
- `/con2signal 170660` — Expected: a single message starting at `⏳ 큐 대기 중 (1번째)` then transitioning through `📥 디시콘 정보 가져오는 중...`, `📥 이미지 다운로드 중 (M/N)`, `✨ 이미지 변환 중 (M/N)`, `🚀 Signal 업로드 중...`, ending in `✅ 완료!` with an `https://signal.art/addstickers/...` URL.
- A bare `170660` message — Expected: same flow as the command form.
- In a group with the bot added, `@<botusername> 170660` — Expected: same flow.

Install the resulting pack on Signal mobile and verify the stickers render.

- [ ] **Step 7: Install as a systemd service**

Follow Task 10 step 3. Verify with:
```bash
systemctl --user status dccon2signal-bot
journalctl --user -u dccon2signal-bot -n 20
```

Send another conversion through Telegram. Verify the flow still works under systemd.

- [ ] **Step 8: Final commit (if any fixes were needed)**

If anything had to be tweaked during E2E debugging, commit those fixes with a focused message.

---

## Done criteria

- `uv run pytest -q` is green with at least the new tests added by Tasks 1, 3-8 plus the existing suite.
- `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` all clean.
- Manual E2E: a real DCcon round-trips from a Telegram `/con2signal` (or loose-form) request to a Signal install link, and the result is installable and renders on a real device.
- README's Telegram bot section is published and accurate.

## Deferred from spec (not in this plan)

- Persistent (SQLite) queue
- Inline-button cancel for queued jobs
- Per-user rate limits beyond serial queue
- Sticker pre-preview before upload
- Webhook deployment mode
- Telegram sticker-pack output (parallel project)
