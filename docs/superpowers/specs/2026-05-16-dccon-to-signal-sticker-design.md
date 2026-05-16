# DCInside DCcon → Signal Sticker Pack Converter (`dccon2signal`)

**Status:** Design approved 2026-05-16
**Author:** cloudchamb3r
**Type:** CLI tool

## 1. Purpose

Convert a DCInside DCcon (디시콘) package into a Signal Messenger sticker pack and upload it to Signal's servers, returning an install link. Supports both static (PNG) and animated (GIF → APNG) cons.

## 2. Goals & Non-Goals

**Goals**
- One-command flow: DCcon package_idx → Signal install link
- Preserve animation when possible (GIF → APNG)
- Auto-transparency (white background → alpha) by default
- Resilient: partial failures leave usable artifacts on disk for retry
- Single-user CLI; no shared service, no web UI

**Non-Goals**
- No GUI / web app (out of scope; library boundaries should not preclude future addition)
- No DCInside account login flow (only public DCcon previews)
- No automatic emoji mapping from sticker titles (placeholder + manual override only)
- No bulk crawling of multiple packs via auto-discovery (user provides explicit IDs)

## 3. User-Facing Behavior

### 3.1 Invocation

```bash
$ dccon2signal 170660
# or multiple at once
$ dccon2signal 170660 67890 99999

# auth setup (interactive)
$ dccon2signal auth init
$ dccon2signal auth verify
```

### 3.2 Output (happy path)

```
✓ Fetched pack "개패고싶은 구구가가" (45 stickers, 38 animated) by "도끼도끼"
✓ Downloaded 45 images
✓ Processed: 45/45 (transparency, resize, APNG conversion)
✓ Uploaded to Signal
  Install: https://signal.art/addstickers/#pack_id=abc123&pack_key=def456
  Saved to: ./out/170660/
```

### 3.3 Flags

| Flag | Default | Effect |
|---|---|---|
| `--title TEXT` | scraped value | Override pack title |
| `--author TEXT` | scraped value | Override author name |
| `--out-dir PATH` | `./out/<package_idx>/` | Where converted images + manifest are saved |
| `--no-upload` | false | Skip Signal upload (convert only) |
| `--no-bg-removal` | false | Disable white-background-to-alpha |
| `--static-only` | false | Convert GIFs to single-frame PNG |
| `--emoji-map PATH` | – | JSON `{"<sort>": "😀"}` keyed by sticker `sort` (1-based display order) |
| `--auth PATH` | `~/.config/dccon2signal/auth.json` | Signal credentials file |
| `-v, --verbose` | false | Detailed logs |
| `--from-dir PATH` | – | Skip scrape+download, use already-processed dir (retry path) |

### 3.4 Auth file

`~/.config/dccon2signal/auth.json` (mode 0600):

```json
{
  "username": "+821012345678",
  "password": "<signal-cli or Signal Desktop derived password>"
}
```

`auth init` prints platform-specific instructions for obtaining these (signal-cli registration or extraction from `~/Library/Application Support/Signal/config.json`).

## 4. Architecture

### 4.1 Pipeline

```
┌──────────────┐   ┌───────────────┐   ┌──────────────┐   ┌──────────────┐
│  Scraper     │ → │  Image Proc   │ → │  Pack Builder│ → │  Uploader    │
│  (dcinside)  │   │  (Pillow)     │   │              │   │  (signal)    │
└──────────────┘   └───────────────┘   └──────────────┘   └──────────────┘
       ↓                  ↓                   ↓                  ↓
   원본 이미지          512×512 PNG         StickerPack         packId+
   + 메타데이터          /APNG, 투명배경     object              packKey
```

Each stage is independently testable: Scraper with fake HTML/JSON fixtures, Image Processor with sample images, Pack Builder with plain dataclass inputs, Uploader behind a thin interface that can be mocked.

### 4.2 Project layout

```
signalsticker/
├── pyproject.toml              # uv-managed
├── README.md
├── src/dccon2signal/
│   ├── __init__.py
│   ├── __main__.py             # `python -m dccon2signal`
│   ├── cli.py                  # Click-based CLI parsing + orchestration
│   ├── scraper.py              # DCInside API → DcconPack
│   ├── downloader.py           # Parallel image fetch (httpx.AsyncClient)
│   ├── image_proc.py           # bg removal, resize, GIF→APNG
│   ├── pack_builder.py         # DcconPack → signalstickers_client.StickerPack
│   ├── uploader.py             # Signal upload + install link assembly
│   ├── auth.py                 # auth.json read/write/verify
│   └── models.py               # DcconPack, DcconSticker dataclasses
└── tests/
    ├── fixtures/
    ├── test_scraper.py         # respx HTTP mocking
    ├── test_image_proc.py      # real image fixtures
    ├── test_pack_builder.py
    └── test_cli.py
```

### 4.3 Dependencies

- **httpx** — async HTTP client for scraping and image download
- **Pillow** — image processing (resize, bg removal, GIF/APNG handling)
- **signalstickers-client** — Signal sticker upload (battle-tested, handles auth/encryption/upload)
- **click** — CLI parsing
- **respx** — HTTP mocking for tests
- **pytest**, **pytest-asyncio**
- **ruff** — lint + format
- **mypy** — type checking for core modules (scraper, image_proc, pack_builder, models)

Python 3.12+. Managed via **uv** for lockfile and fast installs.

## 5. Data Model

```python
# models.py
from dataclasses import dataclass, field
from typing import Literal

@dataclass
class DcconSticker:
    idx: str                       # API's per-sticker id, e.g. "6032016"
    sort: int                      # display order, 1-based
    title: str                     # API's "title" field; often numeric, used as hint only
    ext: Literal["png", "gif"]
    image_url: str                 # fully assembled dcimg5 URL
    image_bytes: bytes | None = None        # set after Downloader
    processed_bytes: bytes | None = None    # set after Image Proc
    processed_ext: Literal["png", "apng"] | None = None
    emoji: str = "😀"              # placeholder; overridden by --emoji-map

@dataclass
class DcconPack:
    package_idx: str
    title: str
    author: str                    # seller_name
    description: str
    cover_url: str
    cover_bytes: bytes | None = None
    cover_processed: bytes | None = None
    stickers: list[DcconSticker] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
```

The pack is mutated in place across pipeline stages. This is deliberate — each stage is a synchronous-looking transformation on a single in-memory object, and dataclass mutation is simpler than threading new objects through every stage. Stages are independently tested with fresh objects.

## 6. Module Contracts

### 6.1 Scraper (`scraper.py`)

```python
async def fetch_pack(package_idx: str, client: httpx.AsyncClient) -> DcconPack: ...
```

Steps (all verified against live API on 2026-05-16):
1. POST `https://dccon.dcinside.com/index/package_detail` with body `package_idx=<id>&code=&inspection_state=`. **No CSRF token required** — the endpoint accepts the request without `ci_t`. Use `X-Requested-With: XMLHttpRequest` header to mimic the browser AJAX call.
2. Parse JSON response (see [memory: dccon-api](../../../.claude/projects/-Users-cloudchamb3r-projects-signalsticker/memory/dccon-api.md) for shape).
3. For each `detail[]` entry, build image URL: `https://dcimg5.dcinside.com/dccon.php?no=<path>`. The `date` query parameter mentioned in some references is **not required**.
4. Return populated `DcconPack` (without image bytes).

### 6.2 Downloader (`downloader.py`)

```python
async def download_all(pack: DcconPack, *, concurrency: int = 8, retries: int = 3) -> None: ...
```

- Parallel image fetch (asyncio.Semaphore for concurrency cap)
- **`Referer: https://dccon.dcinside.com/` header is required** — without it the image server returns 403 (verified 2026-05-16).
- On final-attempt failure, log and leave `image_bytes = None`. Caller decides what to do with partial packs.
- Also downloads cover image.
- **Source images are 200×200 JPEG** for static cons. The cover served at `info.main_img_path` is the same. Upscaling to 512×512 introduces visible blurring — see §9.4 for resize quality decision.
- **Animated cons are 200×200 GIF89a** and can easily be **>300KB** (sample observed: 379KB). This means Image Processor MUST run frame-reduction / re-encoding for most animated cons to fit Signal's 300KB-per-sticker limit. APNG with reduced frames often compresses better than the original GIF.

### 6.3 Image Processor (`image_proc.py`)

```python
def process_pack(pack: DcconPack, *, remove_bg: bool = True, static_only: bool = False) -> None: ...
```

For each sticker with `image_bytes`:
1. Open via Pillow.
2. If `ext == "gif"` and not `static_only`: extract all frames, convert to APNG.
3. If `ext == "gif"` and `static_only`: take frame 0 as PNG.
4. If `ext == "png"`: keep as PNG.
5. Resize: fit longest side to 512, pad shorter side with transparent pixels to 512×512.
6. If `remove_bg`: convert near-white pixels (RGB > threshold) to alpha 0 with anti-alias edge softening.
7. Size check: if encoded result > 300KB, progressively reduce APNG frame count (every other frame, then every 4th) or apply PNG quantize until ≤ 300KB. If still oversized after reductions, fall back to first-frame static.
8. Store final bytes + chosen extension on the sticker.

Cover image processed similarly but always static PNG.

### 6.4 Pack Builder (`pack_builder.py`)

```python
def build(pack: DcconPack, emoji_map: dict[str, str] | None = None) -> StickerPack: ...
```

- Translate `DcconPack` → `signalstickers_client.StickerPack`
- Apply `emoji_map` lookup by sticker `sort` (1-based display order). Falls back to default `😀` for unmapped stickers.
- Fail loudly if Signal pack rules are violated (e.g. >200 stickers, missing cover)

### 6.5 Uploader (`uploader.py`)

```python
async def upload(pack: StickerPack, auth: SignalAuth) -> tuple[str, str]:
    """Returns (pack_id, pack_key) on success."""
```

- Use `signalstickers-client`'s upload flow
- Wrap any auth/network errors with actionable messages
- Caller composes install URL: `https://signal.art/addstickers/#pack_id={pack_id}&pack_key={pack_key}`

### 6.6 Auth (`auth.py`)

```python
@dataclass
class SignalAuth:
    username: str
    password: str

def load(path: Path) -> SignalAuth: ...
def save(auth: SignalAuth, path: Path) -> None: ...  # writes mode 0600
async def verify(auth: SignalAuth) -> bool: ...      # lightweight Signal connectivity check
```

## 7. Error Handling

| Stage | Failure | Behavior |
|---|---|---|
| Scraper | API response shape changed | Explicit error with raw response excerpt and a pointer to the spec's verified shape |
| Scraper | Unknown / private `package_idx` | Error: "package not found or private" |
| Downloader | Image 404 / network error | 3 retries, then skip that sticker. Final summary lists skipped. |
| Image Proc | Processed bytes > 300KB | Progressive reduction (frames, quantize). Fall back to static if needed. |
| Image Proc | Non-square aspect | Pad with transparent pixels |
| Uploader | Auth failure | Error directs user to `auth verify` |
| Uploader | Mid-upload disconnect | Processed images remain on disk; user re-runs with `--from-dir` |

The `out-dir/<package_idx>/` always contains:
- `manifest.json` — pack title, author, sticker list with file names and emojis
- `cover.png`
- `stickers/<sort>.<png|apng>` — final processed bytes

This ensures graceful degradation: even if Signal upload fails, the user has a ready-to-upload pack folder they can manually load into Signal Desktop.

## 8. Testing Strategy

**Unit**
- `scraper`: `respx` mocks DCInside endpoints; fixture JSON captured from real API.
- `image_proc`: assert 512×512 dimensions, alpha channel present where expected, byte size ≤ 300KB, APNG validity (Pillow `is_animated`).
- `pack_builder`: assert field mapping, emoji defaults, Signal pack-rule violations raise.
- `auth`: file mode 0600, missing-key errors are explicit.

**Integration**
- 3-4 sticker fixture pack flowing through scraper → downloader (mocked) → image_proc → pack_builder. Uploader mocked.

**Manual checklist** (not automated)
- Convert one real DCcon end-to-end, install on Signal mobile, visually verify in light + dark mode, verify APNG animates.

**Lint/format**: `ruff check && ruff format --check`. `mypy --strict` on `scraper.py`, `image_proc.py`, `pack_builder.py`, `models.py`. `cli.py` and `uploader.py` excluded due to heavy external library types.

No CI workflows for this initial cut — single-user tool, `pytest -q` is the gate.

## 9. Open Questions / TBD at Implementation

1. **`signalstickers-client` exact API surface** — verify against current README before writing `uploader.py`.
2. **APNG support in Signal mobile** — Signal docs say animated stickers are supported; verify Signal's `image/apng` MIME constraint at upload time.
3. **Image resize quality** — 200×200 source → 512×512 PNG/APNG. Lanczos resize is the default expectation, but quality loss is inherent. Consider whether to upscale to 512 (Signal pref) or stay at 200 + pad to 512 (no blur, but visually small). Pick after manual visual comparison on a sample pack.

## 10. Out of Scope (Future Work)

- Web UI wrapper (architecture preserves option since core logic is library-shaped)
- Multi-user / hosted variant
- LLM-based emoji tag inference from sticker titles
- DCInside login for purchased (non-preview) high-resolution images
