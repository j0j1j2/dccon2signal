# dccon2signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python CLI `dccon2signal` that takes a DCInside DCcon `package_idx`, scrapes the package, converts images to Signal sticker format (512×512 PNG/APNG with transparency), and uploads as a Signal sticker pack.

**Architecture:** Four-stage async pipeline — Scraper (HTTP) → Downloader (parallel HTTP) → Image Processor (Pillow) → Pack Builder + Uploader (signalstickers-client). Each stage operates on a mutable `DcconPack` dataclass. Output saved to `out/<package_idx>/` for retry safety.

**Tech Stack:** Python 3.12+, uv, httpx, Pillow, signalstickers-client, click, respx (test mocking), pytest, ruff, mypy.

**Spec:** [`docs/superpowers/specs/2026-05-16-dccon-to-signal-sticker-design.md`](../specs/2026-05-16-dccon-to-signal-sticker-design.md)

---

## File Structure

**To create:**
- `pyproject.toml` — uv-managed deps, ruff/mypy/pytest config
- `.gitignore` — Python/IDE/output ignores
- `src/dccon2signal/__init__.py`
- `src/dccon2signal/__main__.py` — `python -m dccon2signal` entry
- `src/dccon2signal/models.py` — `DcconSticker`, `DcconPack`, `SignalAuth` dataclasses
- `src/dccon2signal/scraper.py` — DCInside API client
- `src/dccon2signal/downloader.py` — parallel image fetch
- `src/dccon2signal/image_proc.py` — bg removal, resize, GIF→APNG
- `src/dccon2signal/pack_builder.py` — `DcconPack` → `signalstickers_client.LocalStickerPack`
- `src/dccon2signal/auth.py` — auth.json read/write/verify
- `src/dccon2signal/uploader.py` — Signal upload thin wrapper
- `src/dccon2signal/cli.py` — Click CLI orchestration
- `src/dccon2signal/persistence.py` — save pack to `out/<id>/` (manifest + images)
- `tests/__init__.py`
- `tests/conftest.py` — shared fixtures
- `tests/fixtures/package_detail_170660.json` — captured API response
- `tests/fixtures/sample_static_200x200.jpg` — captured DCcon static sticker
- `tests/fixtures/sample_animated_200x200.gif` — captured DCcon animated sticker
- `tests/test_models.py`
- `tests/test_scraper.py`
- `tests/test_downloader.py`
- `tests/test_image_proc.py`
- `tests/test_pack_builder.py`
- `tests/test_auth.py`
- `tests/test_persistence.py`
- `tests/test_cli.py` — Click smoke tests with mocked pipeline

**Not tested with unit tests:** `uploader.py` (thin wrapper over external library, validated via manual E2E in Task 12).

Each module is ≤150 lines. Splits by responsibility, not layer.

---

## Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/dccon2signal/__init__.py`, `tests/__init__.py`, `tests/conftest.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "dccon2signal"
version = "0.1.0"
description = "Convert DCInside DCcon packages to Signal sticker packs"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.27",
    "pillow>=10.3",
    "signalstickers-client>=3.3",
    "click>=8.1",
]

[project.scripts]
dccon2signal = "dccon2signal.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "respx>=0.21",
    "ruff>=0.4",
    "mypy>=1.10",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/dccon2signal"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.12"
strict = true
files = ["src/dccon2signal/models.py", "src/dccon2signal/scraper.py", "src/dccon2signal/image_proc.py", "src/dccon2signal/pack_builder.py", "src/dccon2signal/persistence.py"]
```

- [ ] **Step 2: Create `.gitignore`**

```
__pycache__/
*.pyc
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
dist/
build/
*.egg-info/
out/
.env
.DS_Store
```

- [ ] **Step 3: Create empty package files**

```python
# src/dccon2signal/__init__.py
__version__ = "0.1.0"
```

```python
# tests/__init__.py
```

- [ ] **Step 4: Create `tests/conftest.py`**

```python
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_static_jpg(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "sample_static_200x200.jpg").read_bytes()


@pytest.fixture
def sample_animated_gif(fixtures_dir: Path) -> bytes:
    return (fixtures_dir / "sample_animated_200x200.gif").read_bytes()


@pytest.fixture
def package_detail_json(fixtures_dir: Path) -> str:
    return (fixtures_dir / "package_detail_170660.json").read_text(encoding="utf-8")
```

- [ ] **Step 5: Copy captured fixtures into the repo**

```bash
mkdir -p tests/fixtures
cp /tmp/dccon_no_cit.json tests/fixtures/package_detail_170660.json
cp /tmp/dccon_cover.bin tests/fixtures/sample_static_200x200.jpg
cp /tmp/dccon_anim.bin tests/fixtures/sample_animated_200x200.gif
```

If `/tmp` files are missing, re-capture with:

```bash
curl -s -X POST 'https://dccon.dcinside.com/index/package_detail' \
  -H 'X-Requested-With: XMLHttpRequest' \
  --data 'package_idx=170660&code=&inspection_state=' \
  -o tests/fixtures/package_detail_170660.json
# Then extract one static (sort=1, ext=png) and one animated (sort=2, ext=gif)
# image path from the JSON and curl each with `Referer: https://dccon.dcinside.com/`.
```

- [ ] **Step 6: Verify `uv sync` succeeds**

Run: `uv sync`
Expected: "Resolved N packages" — no errors.

- [ ] **Step 7: Verify `pytest` collects zero tests successfully**

Run: `uv run pytest -q`
Expected: `no tests ran in 0.XXs`

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore src/ tests/
git commit -m "Scaffold dccon2signal package with uv + pytest"
```

---

## Task 2: Data models

**Files:**
- Create: `src/dccon2signal/models.py`
- Test: `tests/test_models.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_models.py
from dccon2signal.models import DcconPack, DcconSticker, SignalAuth


def test_sticker_defaults():
    s = DcconSticker(idx="123", sort=1, title="hi", ext="png", image_url="http://x/y")
    assert s.image_bytes is None
    assert s.processed_bytes is None
    assert s.processed_ext is None
    assert s.emoji == "😀"


def test_pack_holds_stickers():
    pack = DcconPack(
        package_idx="170660",
        title="t",
        author="a",
        description="d",
        cover_url="http://x/c",
    )
    pack.stickers.append(
        DcconSticker(idx="1", sort=1, title="t", ext="png", image_url="http://x/1")
    )
    assert len(pack.stickers) == 1
    assert pack.tags == []


def test_signal_auth_fields():
    auth = SignalAuth(username="+821012345678", password="pw")
    assert auth.username == "+821012345678"
    assert auth.password == "pw"
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: dccon2signal.models`

- [ ] **Step 3: Implement `models.py`**

```python
# src/dccon2signal/models.py
from dataclasses import dataclass, field
from typing import Literal

ImageExt = Literal["png", "gif"]
ProcessedExt = Literal["png", "apng"]


@dataclass
class DcconSticker:
    idx: str
    sort: int
    title: str
    ext: ImageExt
    image_url: str
    image_bytes: bytes | None = None
    processed_bytes: bytes | None = None
    processed_ext: ProcessedExt | None = None
    emoji: str = "😀"


@dataclass
class DcconPack:
    package_idx: str
    title: str
    author: str
    description: str
    cover_url: str
    cover_bytes: bytes | None = None
    cover_processed: bytes | None = None
    stickers: list[DcconSticker] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class SignalAuth:
    username: str
    password: str
```

- [ ] **Step 4: Run tests — expect pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal/models.py tests/test_models.py
git commit -m "Add DcconPack, DcconSticker, SignalAuth dataclasses"
```

---

## Task 3: Scraper

**Files:**
- Create: `src/dccon2signal/scraper.py`
- Test: `tests/test_scraper.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_scraper.py
import httpx
import pytest
import respx

from dccon2signal.scraper import ScraperError, fetch_pack


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pack_parses_response(package_detail_json: str):
    respx.post("https://dccon.dcinside.com/index/package_detail").respond(
        200, text=package_detail_json
    )
    async with httpx.AsyncClient() as client:
        pack = await fetch_pack("170660", client)

    assert pack.package_idx == "170660"
    assert pack.title == "개패고싶은 구구가가"
    assert pack.author == "도끼도끼"
    assert pack.cover_url.startswith("https://dcimg5.dcinside.com/dccon.php?no=")
    # Per the captured fixture this pack has 45 detail entries.
    assert len(pack.stickers) == 45
    first = pack.stickers[0]
    assert first.sort == 1
    assert first.ext == "png"
    assert first.image_url.startswith("https://dcimg5.dcinside.com/dccon.php?no=")
    # Tag list should be populated
    assert "구구가가" in pack.tags


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pack_404_raises():
    respx.post("https://dccon.dcinside.com/index/package_detail").respond(
        200, json={"info": [], "detail": []}
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ScraperError, match="not found"):
            await fetch_pack("999999", client)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pack_sends_correct_body():
    route = respx.post("https://dccon.dcinside.com/index/package_detail").respond(
        200, json={"info": {"package_idx": "1", "title": "t", "seller_name": "a",
                            "description": "d", "main_img_path": "p"},
                   "detail": [], "tags": []}
    )
    async with httpx.AsyncClient() as client:
        await fetch_pack("1", client)
    sent = route.calls.last.request
    assert b"package_idx=1" in sent.content
    assert sent.headers.get("X-Requested-With") == "XMLHttpRequest"
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest tests/test_scraper.py -v`
Expected: FAIL — `cannot import name 'fetch_pack'`

- [ ] **Step 3: Implement `scraper.py`**

```python
# src/dccon2signal/scraper.py
import json

import httpx

from dccon2signal.models import DcconPack, DcconSticker, ImageExt

API_URL = "https://dccon.dcinside.com/index/package_detail"
IMG_BASE = "https://dcimg5.dcinside.com/dccon.php?no="


class ScraperError(Exception):
    """Raised when the DCcon package cannot be retrieved or parsed."""


async def fetch_pack(package_idx: str, client: httpx.AsyncClient) -> DcconPack:
    body = {"package_idx": package_idx, "code": "", "inspection_state": ""}
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://dccon.dcinside.com/",
        "User-Agent": "Mozilla/5.0 (dccon2signal)",
    }
    resp = await client.post(API_URL, data=body, headers=headers, timeout=15.0)
    resp.raise_for_status()

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise ScraperError(f"Non-JSON response from dccon API: {resp.text[:200]!r}") from e

    info = data.get("info")
    detail = data.get("detail") or []
    if not isinstance(info, dict) or not info.get("package_idx"):
        raise ScraperError(f"Package {package_idx} not found or private")

    pack = DcconPack(
        package_idx=str(info["package_idx"]),
        title=str(info.get("title", "")),
        author=str(info.get("seller_name", "")),
        description=str(info.get("description", "")),
        cover_url=IMG_BASE + str(info["main_img_path"]),
    )
    for entry in detail:
        ext = str(entry["ext"]).lower()
        if ext not in ("png", "gif"):
            continue
        pack.stickers.append(
            DcconSticker(
                idx=str(entry["idx"]),
                sort=int(entry["sort"]),
                title=str(entry.get("title", "")),
                ext=ext,  # type: ignore[arg-type]
                image_url=IMG_BASE + str(entry["path"]),
            )
        )
    pack.stickers.sort(key=lambda s: s.sort)
    pack.tags = [str(t["tag"]) for t in data.get("tags", []) if t.get("tag")]
    return pack
```

- [ ] **Step 4: Run tests — expect pass**

Run: `uv run pytest tests/test_scraper.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal/scraper.py tests/test_scraper.py
git commit -m "Add DCInside package_detail scraper"
```

---

## Task 4: Downloader

**Files:**
- Create: `src/dccon2signal/downloader.py`
- Test: `tests/test_downloader.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_downloader.py
import httpx
import pytest
import respx

from dccon2signal.downloader import download_all
from dccon2signal.models import DcconPack, DcconSticker


def _make_pack() -> DcconPack:
    pack = DcconPack(
        package_idx="1",
        title="t",
        author="a",
        description="d",
        cover_url="https://dcimg5.dcinside.com/dccon.php?no=COVER",
    )
    pack.stickers = [
        DcconSticker(idx="a", sort=1, title="1", ext="png",
                     image_url="https://dcimg5.dcinside.com/dccon.php?no=S1"),
        DcconSticker(idx="b", sort=2, title="2", ext="gif",
                     image_url="https://dcimg5.dcinside.com/dccon.php?no=S2"),
    ]
    return pack


@pytest.mark.asyncio
@respx.mock
async def test_download_all_populates_bytes(sample_static_jpg, sample_animated_gif):
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=COVER").respond(
        200, content=sample_static_jpg, headers={"content-type": "image/jpeg"}
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S1").respond(
        200, content=sample_static_jpg, headers={"content-type": "image/jpeg"}
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S2").respond(
        200, content=sample_animated_gif, headers={"content-type": "image/gif"}
    )

    pack = _make_pack()
    async with httpx.AsyncClient() as client:
        await download_all(pack, client)

    assert pack.cover_bytes == sample_static_jpg
    assert pack.stickers[0].image_bytes == sample_static_jpg
    assert pack.stickers[1].image_bytes == sample_animated_gif


@pytest.mark.asyncio
@respx.mock
async def test_download_all_skips_failed(sample_static_jpg):
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=COVER").respond(
        200, content=sample_static_jpg
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S1").respond(200, content=sample_static_jpg)
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S2").respond(404)

    pack = _make_pack()
    async with httpx.AsyncClient() as client:
        await download_all(pack, client, retries=1)

    assert pack.stickers[0].image_bytes == sample_static_jpg
    assert pack.stickers[1].image_bytes is None


@pytest.mark.asyncio
@respx.mock
async def test_download_sends_referer(sample_static_jpg):
    route = respx.get("https://dcimg5.dcinside.com/dccon.php?no=COVER").respond(
        200, content=sample_static_jpg
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S1").respond(200, content=sample_static_jpg)
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S2").respond(200, content=sample_static_jpg)

    pack = _make_pack()
    async with httpx.AsyncClient() as client:
        await download_all(pack, client)

    assert route.calls.last.request.headers["Referer"] == "https://dccon.dcinside.com/"
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest tests/test_downloader.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `downloader.py`**

```python
# src/dccon2signal/downloader.py
import asyncio
import logging

import httpx

from dccon2signal.models import DcconPack, DcconSticker

logger = logging.getLogger(__name__)

HEADERS = {
    "Referer": "https://dccon.dcinside.com/",
    "User-Agent": "Mozilla/5.0 (dccon2signal)",
}


async def _fetch_one(
    client: httpx.AsyncClient, url: str, *, retries: int
) -> bytes | None:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, headers=HEADERS, timeout=15.0)
            resp.raise_for_status()
            return resp.content
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_error = e
            if attempt < retries:
                await asyncio.sleep(0.5 * (attempt + 1))
    logger.warning("Failed to fetch %s after %d attempts: %s", url, retries + 1, last_error)
    return None


async def download_all(
    pack: DcconPack,
    client: httpx.AsyncClient,
    *,
    concurrency: int = 8,
    retries: int = 3,
) -> None:
    sem = asyncio.Semaphore(concurrency)

    async def _bounded_fetch(url: str) -> bytes | None:
        async with sem:
            return await _fetch_one(client, url, retries=retries)

    async def _fetch_cover() -> None:
        pack.cover_bytes = await _bounded_fetch(pack.cover_url)

    async def _fetch_sticker(s: DcconSticker) -> None:
        s.image_bytes = await _bounded_fetch(s.image_url)

    await asyncio.gather(
        _fetch_cover(),
        *(_fetch_sticker(s) for s in pack.stickers),
    )
```

- [ ] **Step 4: Run tests — expect pass**

Run: `uv run pytest tests/test_downloader.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal/downloader.py tests/test_downloader.py
git commit -m "Add parallel image downloader with retries"
```

---

## Task 5: Image processor — static images

**Files:**
- Create: `src/dccon2signal/image_proc.py`
- Test: `tests/test_image_proc.py`

- [ ] **Step 1: Write failing test for static processing**

```python
# tests/test_image_proc.py
from io import BytesIO

import pytest
from PIL import Image

from dccon2signal.image_proc import (
    SIGNAL_MAX_BYTES,
    SIGNAL_SIZE,
    process_pack,
    process_sticker_bytes,
)
from dccon2signal.models import DcconPack, DcconSticker


def _decode(b: bytes) -> Image.Image:
    return Image.open(BytesIO(b))


def test_process_static_png_returns_512_rgba(sample_static_jpg):
    out, ext = process_sticker_bytes(sample_static_jpg, source_ext="png", remove_bg=True)
    assert ext == "png"
    img = _decode(out)
    assert img.size == (SIGNAL_SIZE, SIGNAL_SIZE)
    assert img.mode == "RGBA"
    assert len(out) <= SIGNAL_MAX_BYTES


def test_process_static_removes_white_background(sample_static_jpg):
    out, _ = process_sticker_bytes(sample_static_jpg, source_ext="png", remove_bg=True)
    img = _decode(out)
    # The top-left corner of the source JPEG is solid white;
    # after bg removal alpha should be 0 there.
    pixel = img.getpixel((2, 2))
    assert isinstance(pixel, tuple)
    assert pixel[3] == 0


def test_process_static_keeps_background_when_disabled(sample_static_jpg):
    out, _ = process_sticker_bytes(sample_static_jpg, source_ext="png", remove_bg=False)
    img = _decode(out)
    pixel = img.getpixel((2, 2))
    assert isinstance(pixel, tuple)
    assert pixel[3] == 255
```

- [ ] **Step 2: Run test — expect ImportError**

Run: `uv run pytest tests/test_image_proc.py -v`
Expected: FAIL

- [ ] **Step 3: Implement static path in `image_proc.py`**

```python
# src/dccon2signal/image_proc.py
from io import BytesIO

from PIL import Image, ImageSequence

from dccon2signal.models import DcconPack, ImageExt, ProcessedExt

SIGNAL_SIZE = 512
SIGNAL_MAX_BYTES = 300 * 1024
WHITE_THRESHOLD = 240  # 0-255 per channel; treated as background


def _remove_white_bg(img: Image.Image) -> Image.Image:
    rgba = img.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD:
                pixels[x, y] = (r, g, b, 0)
    return rgba


def _fit_512(img: Image.Image) -> Image.Image:
    img = img.convert("RGBA")
    img.thumbnail((SIGNAL_SIZE, SIGNAL_SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (SIGNAL_SIZE, SIGNAL_SIZE), (0, 0, 0, 0))
    ox = (SIGNAL_SIZE - img.width) // 2
    oy = (SIGNAL_SIZE - img.height) // 2
    canvas.paste(img, (ox, oy), img)
    return canvas


def _encode_png(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _shrink_png_under_limit(img: Image.Image) -> bytes:
    out = _encode_png(img)
    if len(out) <= SIGNAL_MAX_BYTES:
        return out
    # Progressive quantize until under limit.
    for colors in (256, 128, 64, 32):
        quant = img.quantize(colors=colors).convert("RGBA")
        buf = BytesIO()
        quant.save(buf, format="PNG", optimize=True)
        candidate = buf.getvalue()
        if len(candidate) <= SIGNAL_MAX_BYTES:
            return candidate
    return out  # Best effort; caller may surface a warning.


def process_sticker_bytes(
    data: bytes,
    *,
    source_ext: ImageExt,
    remove_bg: bool,
    static_only: bool = False,
) -> tuple[bytes, ProcessedExt]:
    img = Image.open(BytesIO(data))
    is_animated = bool(getattr(img, "is_animated", False))

    if source_ext == "gif" and is_animated and not static_only:
        return _process_animated(img, remove_bg=remove_bg)

    if source_ext == "gif" and is_animated and static_only:
        img.seek(0)
        img = img.copy()

    processed = _remove_white_bg(img) if remove_bg else img.convert("RGBA")
    fitted = _fit_512(processed)
    return _shrink_png_under_limit(fitted), "png"


def _process_animated(img: Image.Image, *, remove_bg: bool) -> tuple[bytes, ProcessedExt]:
    # Placeholder until animated implementation in Task 6.
    img.seek(0)
    first = img.copy()
    processed = _remove_white_bg(first) if remove_bg else first.convert("RGBA")
    fitted = _fit_512(processed)
    return _shrink_png_under_limit(fitted), "png"


def process_pack(pack: DcconPack, *, remove_bg: bool = True, static_only: bool = False) -> None:
    if pack.cover_bytes is not None:
        pack.cover_processed, _ = process_sticker_bytes(
            pack.cover_bytes, source_ext="png", remove_bg=remove_bg, static_only=True
        )
    for s in pack.stickers:
        if s.image_bytes is None:
            continue
        s.processed_bytes, s.processed_ext = process_sticker_bytes(
            s.image_bytes,
            source_ext=s.ext,
            remove_bg=remove_bg,
            static_only=static_only,
        )
```

- [ ] **Step 4: Run static tests — expect pass**

Run: `uv run pytest tests/test_image_proc.py -v -k static`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal/image_proc.py tests/test_image_proc.py
git commit -m "Add static image processing (resize, bg removal, size guard)"
```

---

## Task 6: Image processor — animated images

**Files:**
- Modify: `src/dccon2signal/image_proc.py` (replace `_process_animated`)
- Modify: `tests/test_image_proc.py` (add animated tests)

- [ ] **Step 1: Add failing animated test**

Append to `tests/test_image_proc.py`:

```python
def test_animated_gif_becomes_apng_under_limit(sample_animated_gif):
    out, ext = process_sticker_bytes(
        sample_animated_gif, source_ext="gif", remove_bg=True
    )
    assert ext == "apng"
    img = _decode(out)
    assert getattr(img, "is_animated", False) is True
    assert img.size == (SIGNAL_SIZE, SIGNAL_SIZE)
    assert len(out) <= SIGNAL_MAX_BYTES


def test_animated_gif_static_only_returns_png(sample_animated_gif):
    out, ext = process_sticker_bytes(
        sample_animated_gif, source_ext="gif", remove_bg=True, static_only=True
    )
    assert ext == "png"
    img = _decode(out)
    assert img.size == (SIGNAL_SIZE, SIGNAL_SIZE)
    # A static PNG should still load (is_animated may be False or attribute missing)
    assert not getattr(img, "is_animated", False)


def test_process_pack_populates_processed_fields(sample_static_jpg, sample_animated_gif):
    pack = DcconPack(package_idx="1", title="t", author="a", description="d",
                     cover_url="u", cover_bytes=sample_static_jpg)
    pack.stickers.append(DcconSticker(
        idx="1", sort=1, title="s", ext="gif",
        image_url="u", image_bytes=sample_animated_gif,
    ))
    process_pack(pack)
    assert pack.cover_processed is not None
    assert pack.stickers[0].processed_bytes is not None
    assert pack.stickers[0].processed_ext == "apng"
```

- [ ] **Step 2: Run tests — expect failure on animated cases**

Run: `uv run pytest tests/test_image_proc.py -v`
Expected: New tests FAIL (animated returns "png" not "apng" with current placeholder).

- [ ] **Step 3: Replace `_process_animated` in `image_proc.py`**

Replace the placeholder `_process_animated` with this implementation:

```python
def _process_animated(img: Image.Image, *, remove_bg: bool) -> tuple[bytes, ProcessedExt]:
    raw_frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        f = frame.convert("RGBA")
        if remove_bg:
            f = _remove_white_bg(f)
        raw_frames.append(_fit_512(f))
        durations.append(frame.info.get("duration", 100))

    return _encode_apng_under_limit(raw_frames, durations)


def _encode_apng(frames: list[Image.Image], durations: list[int]) -> bytes:
    buf = BytesIO()
    frames[0].save(
        buf,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
    )
    return buf.getvalue()


def _encode_apng_under_limit(
    frames: list[Image.Image], durations: list[int]
) -> tuple[bytes, ProcessedExt]:
    # Try full frames first, then progressively skip frames.
    for stride in (1, 2, 3, 4):
        sub_frames = frames[::stride]
        sub_durations = [sum(durations[i:i + stride]) for i in range(0, len(durations), stride)]
        out = _encode_apng(sub_frames, sub_durations)
        if len(out) <= SIGNAL_MAX_BYTES:
            return out, "apng"

    # Fallback: static first frame.
    static_out = _shrink_png_under_limit(frames[0])
    return static_out, "png"
```

(Also keep the existing `_remove_white_bg`, `_fit_512`, `_encode_png`, `_shrink_png_under_limit`, `process_sticker_bytes`, `process_pack`.)

- [ ] **Step 4: Run all image_proc tests — expect pass**

Run: `uv run pytest tests/test_image_proc.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal/image_proc.py tests/test_image_proc.py
git commit -m "Encode animated stickers as APNG with size-aware frame stride"
```

---

## Task 7: Persistence (save converted pack to disk)

**Files:**
- Create: `src/dccon2signal/persistence.py`
- Test: `tests/test_persistence.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_persistence.py
import json
from pathlib import Path

import pytest

from dccon2signal.models import DcconPack, DcconSticker
from dccon2signal.persistence import save_pack


def test_save_pack_writes_manifest_and_images(tmp_path: Path):
    pack = DcconPack(
        package_idx="170660",
        title="구구가가",
        author="도끼도끼",
        description="설명",
        cover_url="u",
        cover_processed=b"\x89PNG\r\n\x1a\nFAKE_COVER",
    )
    pack.stickers.append(DcconSticker(
        idx="1", sort=1, title="1", ext="png", image_url="u",
        processed_bytes=b"\x89PNG\r\n\x1a\nFAKE_STICKER",
        processed_ext="png",
        emoji="😀",
    ))
    pack.stickers.append(DcconSticker(
        idx="2", sort=2, title="2", ext="gif", image_url="u",
        processed_bytes=b"\x89PNG\r\n\x1a\nFAKE_APNG",
        processed_ext="apng",
        emoji="😂",
    ))

    out_dir = tmp_path / "170660"
    save_pack(pack, out_dir)

    assert (out_dir / "cover.png").read_bytes() == b"\x89PNG\r\n\x1a\nFAKE_COVER"
    assert (out_dir / "stickers" / "1.png").exists()
    assert (out_dir / "stickers" / "2.apng").exists()

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_idx"] == "170660"
    assert manifest["title"] == "구구가가"
    assert manifest["author"] == "도끼도끼"
    assert manifest["stickers"][1]["emoji"] == "😂"
    assert manifest["stickers"][1]["file"] == "stickers/2.apng"


def test_save_pack_skips_unprocessed(tmp_path: Path):
    pack = DcconPack(package_idx="1", title="t", author="a", description="d",
                     cover_url="u")
    pack.stickers.append(DcconSticker(
        idx="1", sort=1, title="t", ext="png", image_url="u",
        processed_bytes=None,
    ))
    out_dir = tmp_path / "1"
    save_pack(pack, out_dir)
    assert not (out_dir / "stickers" / "1.png").exists()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stickers"] == []
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest tests/test_persistence.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `persistence.py`**

```python
# src/dccon2signal/persistence.py
import json
from pathlib import Path

from dccon2signal.models import DcconPack


def save_pack(pack: DcconPack, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stickers_dir = out_dir / "stickers"
    stickers_dir.mkdir(exist_ok=True)

    if pack.cover_processed is not None:
        (out_dir / "cover.png").write_bytes(pack.cover_processed)

    manifest_stickers = []
    for s in pack.stickers:
        if s.processed_bytes is None or s.processed_ext is None:
            continue
        filename = f"{s.sort}.{s.processed_ext}"
        (stickers_dir / filename).write_bytes(s.processed_bytes)
        manifest_stickers.append({
            "sort": s.sort,
            "idx": s.idx,
            "title": s.title,
            "emoji": s.emoji,
            "file": f"stickers/{filename}",
        })

    manifest = {
        "package_idx": pack.package_idx,
        "title": pack.title,
        "author": pack.author,
        "description": pack.description,
        "tags": pack.tags,
        "stickers": manifest_stickers,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
```

- [ ] **Step 4: Run tests — expect pass**

Run: `uv run pytest tests/test_persistence.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal/persistence.py tests/test_persistence.py
git commit -m "Add out-dir persistence (manifest.json + cover + stickers)"
```

---

## Task 8: Pack Builder

**Files:**
- Create: `src/dccon2signal/pack_builder.py`
- Test: `tests/test_pack_builder.py`

> **Implementation note:** Before writing this task's code, open `https://github.com/signalstickers/signalstickers-client/blob/master/README.md` (or `uv pip show signalstickers-client` then inspect the installed module) and confirm the names of `LocalStickerPack`, `Sticker`, `Sticker.id`, `Sticker.emoji`, `Sticker.image_data`, `LocalStickerPack.cover`, and the method that adds a sticker (historically `_addsticker`). If the names differ, adjust this task's code to match — do not rename them away from the library's choices.

- [ ] **Step 1: Write failing test**

```python
# tests/test_pack_builder.py
import pytest

from dccon2signal.models import DcconPack, DcconSticker
from dccon2signal.pack_builder import PackBuilderError, build


def _pack_with_two_stickers() -> DcconPack:
    pack = DcconPack(
        package_idx="1",
        title="t",
        author="a",
        description="d",
        cover_url="u",
        cover_processed=b"COVER",
    )
    pack.stickers.extend([
        DcconSticker(idx="1", sort=1, title="x", ext="png", image_url="u",
                     processed_bytes=b"S1", processed_ext="png", emoji="😀"),
        DcconSticker(idx="2", sort=2, title="y", ext="gif", image_url="u",
                     processed_bytes=b"S2", processed_ext="apng", emoji="😀"),
    ])
    return pack


def test_build_sets_metadata():
    pack = _pack_with_two_stickers()
    signal_pack = build(pack)
    assert signal_pack.title == "t"
    assert signal_pack.author == "a"


def test_build_applies_emoji_map():
    pack = _pack_with_two_stickers()
    signal_pack = build(pack, emoji_map={"1": "🐱", "2": "🐶"})
    emojis = sorted(s.emoji for s in signal_pack.stickers)
    assert emojis == ["🐱", "🐶"]


def test_build_rejects_missing_cover():
    pack = _pack_with_two_stickers()
    pack.cover_processed = None
    with pytest.raises(PackBuilderError, match="cover"):
        build(pack)


def test_build_rejects_no_stickers():
    pack = DcconPack(package_idx="1", title="t", author="a", description="d",
                     cover_url="u", cover_processed=b"COVER")
    with pytest.raises(PackBuilderError, match="empty"):
        build(pack)


def test_build_rejects_over_200_stickers():
    pack = _pack_with_two_stickers()
    pack.cover_processed = b"COVER"
    for i in range(3, 250):
        pack.stickers.append(DcconSticker(
            idx=str(i), sort=i, title=str(i), ext="png", image_url="u",
            processed_bytes=b"X", processed_ext="png",
        ))
    with pytest.raises(PackBuilderError, match="200"):
        build(pack)
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest tests/test_pack_builder.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `pack_builder.py`**

```python
# src/dccon2signal/pack_builder.py
from signalstickers_client.models import LocalStickerPack, Sticker

from dccon2signal.models import DcconPack

SIGNAL_MAX_STICKERS = 200


class PackBuilderError(Exception):
    """Raised when a DcconPack cannot be converted to a Signal sticker pack."""


def build(pack: DcconPack, emoji_map: dict[str, str] | None = None) -> LocalStickerPack:
    processed = [s for s in pack.stickers if s.processed_bytes is not None]
    if not processed:
        raise PackBuilderError("Sticker list is empty after processing")
    if len(processed) > SIGNAL_MAX_STICKERS:
        raise PackBuilderError(f"Signal allows up to 200 stickers; got {len(processed)}")
    if pack.cover_processed is None:
        raise PackBuilderError("Pack has no processed cover image")

    signal_pack = LocalStickerPack()
    signal_pack.title = pack.title
    signal_pack.author = pack.author

    cover = Sticker()
    cover.id = 0
    cover.image_data = pack.cover_processed
    signal_pack.cover = cover

    for idx, s in enumerate(processed):
        sticker = Sticker()
        sticker.id = idx
        sticker.emoji = (emoji_map or {}).get(str(s.sort), s.emoji)
        sticker.image_data = s.processed_bytes
        signal_pack._addsticker(sticker)

    return signal_pack
```

- [ ] **Step 4: Run tests — expect pass**

Run: `uv run pytest tests/test_pack_builder.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal/pack_builder.py tests/test_pack_builder.py
git commit -m "Add pack builder: DcconPack → signalstickers LocalStickerPack"
```

---

## Task 9: Auth file handling

**Files:**
- Create: `src/dccon2signal/auth.py`
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_auth.py
import json
import os
import stat
from pathlib import Path

import pytest

from dccon2signal.auth import AuthError, load, save
from dccon2signal.models import SignalAuth


def test_save_writes_0600(tmp_path: Path):
    path = tmp_path / "auth.json"
    save(SignalAuth(username="+821012345678", password="pw"), path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_round_trip(tmp_path: Path):
    path = tmp_path / "auth.json"
    save(SignalAuth(username="+1", password="pw"), path)
    loaded = load(path)
    assert loaded.username == "+1"
    assert loaded.password == "pw"


def test_load_missing_path_raises(tmp_path: Path):
    with pytest.raises(AuthError, match="not found"):
        load(tmp_path / "missing.json")


def test_load_missing_key_raises(tmp_path: Path):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"username": "+1"}), encoding="utf-8")
    with pytest.raises(AuthError, match="password"):
        load(path)


def test_save_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "nested" / "auth.json"
    save(SignalAuth(username="+1", password="pw"), path)
    assert path.exists()
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest tests/test_auth.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `auth.py`**

```python
# src/dccon2signal/auth.py
import json
import os
from pathlib import Path

from dccon2signal.models import SignalAuth


class AuthError(Exception):
    """Raised when Signal credentials cannot be loaded or saved."""


def save(auth: SignalAuth, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"username": auth.username, "password": auth.password})
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def load(path: Path) -> SignalAuth:
    if not path.exists():
        raise AuthError(f"Auth file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise AuthError(f"Auth file is not valid JSON: {path}") from e
    if not isinstance(data, dict) or "username" not in data:
        raise AuthError(f"Auth file missing 'username': {path}")
    if "password" not in data:
        raise AuthError(f"Auth file missing 'password': {path}")
    return SignalAuth(username=str(data["username"]), password=str(data["password"]))
```

- [ ] **Step 4: Run tests — expect pass**

Run: `uv run pytest tests/test_auth.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add src/dccon2signal/auth.py tests/test_auth.py
git commit -m "Add auth.json save/load with 0600 permissions"
```

---

## Task 10: Uploader (Signal upload wrapper)

**Files:**
- Create: `src/dccon2signal/uploader.py`

> **No unit tests** — this module is a thin wrapper over `signalstickers-client`'s authenticated upload flow. Live network and credentials are required. It will be exercised by the manual E2E in Task 12.

- [ ] **Step 1: Verify the library's upload entry point**

Run: `uv run python -c "from signalstickers_client import StickersClient; help(StickersClient)"`

Confirm the class exposes an `async upload_pack(pack)` method that returns `(pack_id, pack_key)`. If the actual API name or signature differs, adjust the implementation below accordingly — keep the wrapper signature stable.

- [ ] **Step 2: Implement `uploader.py`**

```python
# src/dccon2signal/uploader.py
from signalstickers_client import StickersClient
from signalstickers_client.models import LocalStickerPack

from dccon2signal.models import SignalAuth

SIGNAL_INSTALL_URL = "https://signal.art/addstickers/#pack_id={pack_id}&pack_key={pack_key}"


class UploaderError(Exception):
    """Raised when uploading a sticker pack to Signal fails."""


async def upload(pack: LocalStickerPack, auth: SignalAuth) -> tuple[str, str]:
    try:
        async with StickersClient(auth.username, auth.password) as client:
            pack_id, pack_key = await client.upload_pack(pack)
    except Exception as e:  # narrow once the library's exceptions are confirmed
        raise UploaderError(f"Signal upload failed: {e}") from e
    return pack_id, pack_key


def install_url(pack_id: str, pack_key: str) -> str:
    return SIGNAL_INSTALL_URL.format(pack_id=pack_id, pack_key=pack_key)
```

- [ ] **Step 3: Smoke-import check**

Run: `uv run python -c "from dccon2signal.uploader import upload, install_url; print(install_url('a', 'b'))"`
Expected: `https://signal.art/addstickers/#pack_id=a&pack_key=b`

- [ ] **Step 4: Commit**

```bash
git add src/dccon2signal/uploader.py
git commit -m "Add Signal uploader wrapper"
```

---

## Task 11: CLI orchestration

**Files:**
- Create: `src/dccon2signal/cli.py`, `src/dccon2signal/__main__.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI smoke test**

```python
# tests/test_cli.py
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from dccon2signal.cli import main
from dccon2signal.models import DcconPack, DcconSticker


@pytest.fixture
def fake_pack() -> DcconPack:
    pack = DcconPack(package_idx="170660", title="t", author="a", description="d",
                     cover_url="u", cover_bytes=b"COVER")
    pack.stickers.append(DcconSticker(idx="1", sort=1, title="1", ext="png",
                                       image_url="u", image_bytes=b"S"))
    return pack


def test_convert_only_no_upload(tmp_path: Path, fake_pack):
    out_dir = tmp_path / "out"

    async def _scrape(_idx, _client):
        return fake_pack

    async def _download(_pack, _client, **_kw):
        return None

    with patch("dccon2signal.cli.scraper.fetch_pack", side_effect=_scrape), \
         patch("dccon2signal.cli.downloader.download_all", side_effect=_download), \
         patch("dccon2signal.cli.image_proc.process_pack") as proc:

        def _fake_proc(pack, **_kw):
            pack.cover_processed = b"\x89PNG\r\n\x1a\nFAKE"
            for s in pack.stickers:
                s.processed_bytes = b"\x89PNG\r\n\x1a\nFAKE"
                s.processed_ext = "png"

        proc.side_effect = _fake_proc
        runner = CliRunner()
        result = runner.invoke(main, ["170660", "--no-upload", "--out-dir", str(out_dir)])

    assert result.exit_code == 0, result.output
    manifest = json.loads((out_dir / "170660" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["title"] == "t"


def test_upload_path_invokes_uploader(tmp_path: Path, fake_pack):
    out_dir = tmp_path / "out"
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"username": "+1", "password": "p"}', encoding="utf-8")

    async def _scrape(_idx, _client):
        return fake_pack

    async def _download(_pack, _client, **_kw):
        return None

    async def _upload(_signal_pack, _auth):
        return "abc", "def"

    with patch("dccon2signal.cli.scraper.fetch_pack", side_effect=_scrape), \
         patch("dccon2signal.cli.downloader.download_all", side_effect=_download), \
         patch("dccon2signal.cli.image_proc.process_pack") as proc, \
         patch("dccon2signal.cli.uploader.upload", side_effect=_upload):

        def _fake_proc(pack, **_kw):
            pack.cover_processed = b"\x89PNG\r\n\x1a\nFAKE"
            for s in pack.stickers:
                s.processed_bytes = b"\x89PNG\r\n\x1a\nFAKE"
                s.processed_ext = "png"

        proc.side_effect = _fake_proc

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["170660", "--out-dir", str(out_dir), "--auth", str(auth_path)],
        )

    assert result.exit_code == 0, result.output
    assert "pack_id=abc" in result.output
    assert "pack_key=def" in result.output
```

- [ ] **Step 2: Run tests — expect ImportError**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `cli.py`**

```python
# src/dccon2signal/cli.py
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import click
import httpx

from dccon2signal import (
    auth as auth_mod,
    downloader,
    image_proc,
    pack_builder,
    persistence,
    scraper,
    uploader,
)
from dccon2signal.models import DcconPack

DEFAULT_AUTH_PATH = Path.home() / ".config" / "dccon2signal" / "auth.json"


@click.command()
@click.argument("package_idx", nargs=-1, required=True)
@click.option("--title", default=None, help="Override pack title")
@click.option("--author", default=None, help="Override author name")
@click.option("--out-dir", default="./out", type=click.Path(path_type=Path),
              help="Where to save processed images + manifest")
@click.option("--no-upload", is_flag=True, default=False, help="Skip Signal upload")
@click.option("--no-bg-removal", is_flag=True, default=False, help="Disable white background removal")
@click.option("--static-only", is_flag=True, default=False, help="Convert GIFs to static PNG only")
@click.option("--emoji-map", "emoji_map_path", type=click.Path(path_type=Path),
              default=None, help="JSON {sort: emoji} for per-sticker emoji")
@click.option("--auth", "auth_path", type=click.Path(path_type=Path),
              default=DEFAULT_AUTH_PATH, help="Signal credentials file")
@click.option("-v", "--verbose", is_flag=True, default=False)
def main(
    package_idx: tuple[str, ...],
    title: str | None,
    author: str | None,
    out_dir: Path,
    no_upload: bool,
    no_bg_removal: bool,
    static_only: bool,
    emoji_map_path: Path | None,
    auth_path: Path,
    verbose: bool,
) -> None:
    """Convert DCInside DCcon package(s) to Signal sticker pack(s)."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(message)s")

    emoji_map: dict[str, str] | None = None
    if emoji_map_path is not None:
        emoji_map = json.loads(emoji_map_path.read_text(encoding="utf-8"))

    asyncio.run(_run_all(
        package_idx, title, author, out_dir,
        no_upload=no_upload, no_bg_removal=no_bg_removal,
        static_only=static_only, emoji_map=emoji_map, auth_path=auth_path,
    ))


async def _run_all(
    ids: tuple[str, ...],
    title_override: str | None,
    author_override: str | None,
    out_dir: Path,
    *,
    no_upload: bool,
    no_bg_removal: bool,
    static_only: bool,
    emoji_map: dict[str, str] | None,
    auth_path: Path,
) -> None:
    async with httpx.AsyncClient() as client:
        for pid in ids:
            await _run_one(
                pid, client, title_override, author_override, out_dir,
                no_upload=no_upload, no_bg_removal=no_bg_removal,
                static_only=static_only, emoji_map=emoji_map, auth_path=auth_path,
            )


async def _run_one(
    package_idx: str,
    client: httpx.AsyncClient,
    title_override: str | None,
    author_override: str | None,
    out_dir: Path,
    *,
    no_upload: bool,
    no_bg_removal: bool,
    static_only: bool,
    emoji_map: dict[str, str] | None,
    auth_path: Path,
) -> None:
    click.echo(f"→ {package_idx}")
    pack: DcconPack = await scraper.fetch_pack(package_idx, client)
    if title_override:
        pack.title = title_override
    if author_override:
        pack.author = author_override
    click.echo(f"  Fetched: {pack.title!r} ({len(pack.stickers)} stickers) by {pack.author!r}")

    await downloader.download_all(pack, client)
    successful = sum(1 for s in pack.stickers if s.image_bytes is not None)
    click.echo(f"  Downloaded: {successful}/{len(pack.stickers)} images")

    image_proc.process_pack(pack, remove_bg=not no_bg_removal, static_only=static_only)
    processed_count = sum(1 for s in pack.stickers if s.processed_bytes is not None)
    click.echo(f"  Processed: {processed_count}/{len(pack.stickers)}")

    pack_dir = out_dir / pack.package_idx
    persistence.save_pack(pack, pack_dir)
    click.echo(f"  Saved to {pack_dir}")

    if no_upload:
        return

    auth = auth_mod.load(auth_path)
    signal_pack = pack_builder.build(pack, emoji_map=emoji_map)
    pack_id, pack_key = await uploader.upload(signal_pack, auth)
    click.echo(f"  Install: {uploader.install_url(pack_id, pack_key)}")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Implement `__main__.py`**

```python
# src/dccon2signal/__main__.py
from dccon2signal.cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run CLI tests — expect pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 2 PASS

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -q`
Expected: all PASS, no warnings about unawaited coroutines.

- [ ] **Step 7: Commit**

```bash
git add src/dccon2signal/cli.py src/dccon2signal/__main__.py tests/test_cli.py
git commit -m "Add CLI: orchestrate scrape → download → process → save → upload"
```

---

## Task 12: Manual end-to-end verification

**No code changes.** This task validates the integrated tool against the real DCInside and Signal services.

- [ ] **Step 1: Lint and type-check**

Run: `uv run ruff check && uv run ruff format --check && uv run mypy`
Expected: clean.

- [ ] **Step 2: Convert-only run (no Signal upload)**

```bash
uv run dccon2signal 170660 --no-upload --out-dir ./out
```

Expected:
- `out/170660/cover.png` exists (512×512, transparent background).
- `out/170660/stickers/1.png` … `45.{png|apng}` exist.
- `out/170660/manifest.json` lists 45 stickers.

Open `cover.png` and a few stickers in an image viewer. Confirm:
- Sizes are 512×512.
- Background is transparent (looks correct on both light and dark surfaces).
- Animated cons (sort 2, 3, etc.) actually animate when opened in Chrome or another APNG-capable viewer.

- [ ] **Step 3: Auth setup**

Pick the option you have available:

**Option A — signal-cli** (if already installed):
```bash
signal-cli -u "+82YOURNUMBER" register
# Receive SMS code, then:
signal-cli -u "+82YOURNUMBER" verify <code>
# Read the registered password:
jq -r '.password' ~/.local/share/signal-cli/data/+82YOURNUMBER.json
```

**Option B — Signal Desktop extract**:
- macOS: `cat "$HOME/Library/Application Support/Signal/config.json"` and pull `number` and `password`.

Write the credentials to the auth file:
```bash
mkdir -p ~/.config/dccon2signal
cat > ~/.config/dccon2signal/auth.json <<'EOF'
{"username": "+82...", "password": "..."}
EOF
chmod 600 ~/.config/dccon2signal/auth.json
```

- [ ] **Step 4: Full upload run**

```bash
uv run dccon2signal 170660 --out-dir ./out
```

Expected output ends with:
```
  Install: https://signal.art/addstickers/#pack_id=...&pack_key=...
```

- [ ] **Step 5: Install on Signal and verify**

Open the install link on a phone with Signal installed. Add the pack. In a chat:
- Send a static sticker — appears at 512×512, transparent background.
- Send an animated sticker — animates correctly.
- Toggle phone to dark mode — stickers still look correct.

- [ ] **Step 6: Final commit (if any tweaks were needed)**

If any code changed during E2E debugging, commit those fixes with a focused message (e.g. `"Fix sticker emoji default after Signal client name change"`).

---

## Done criteria

- `uv run pytest -q` is green (≥ 25 tests).
- `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` all clean.
- Manual E2E: a real DCcon (`170660` or another public pack) round-trips to a Signal install link, installs on a phone, and renders correctly in both light and dark mode.

---

## Deferred from spec (not in this plan)

These items appear in the design spec but are deliberately deferred from the initial cut to keep the scope tight. Reopen them as separate plans once the v0.1 above ships.

- **`--from-dir <path>` retry flag** (spec §3.3, §7). Lets the user re-run the upload step against an already-processed `out/<id>/` directory after a network failure. Mid-upload retry is rare for single-user use, and the manifest already lets the user fall back to Signal Desktop's import for now.
- **`dccon2signal auth init` / `auth verify` subcommands** (spec §3.1). Interactive wizard that detects signal-cli or Signal Desktop config and writes `auth.json` automatically. Task 12 documents the manual setup, which is acceptable for v0.1.
- **`auth.verify()` async helper** (spec §6.6). Tied to the `auth verify` subcommand above. Not required as long as the uploader produces a clear error on bad credentials (Task 10's `UploaderError` already surfaces this).
