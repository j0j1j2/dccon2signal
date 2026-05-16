# dccon2signal Telegram Bot Frontend

**Status:** Design approved 2026-05-16
**Author:** cloudchamb3r
**Type:** Telegram bot — frontend over existing `dccon2signal` library
**Parent:** [dccon-to-signal-sticker design](2026-05-16-dccon-to-signal-sticker-design.md)

## 1. Purpose

Let users trigger DCcon → Signal sticker pack conversion from a Telegram chat. The bot accepts a DCcon `package_idx`, processes it serially via an in-memory queue, and replies with the Signal install link. The same Telegram message is edited live to show stage progression (Queued → Fetching → Downloading → Processing → Uploading → Done).

## 2. Goals & Non-Goals

**Goals**
- Phone-friendly trigger: no SSH needed
- Real-time progress: one message edited through 4-5 stages
- Serial processing: one job at a time → natural Signal rate-limit protection
- Public bot: anyone can send `/con2signal <id>`; serial queue is the throttle
- Reuse existing `dccon2signal` library — no logic forks

**Non-Goals**
- No Telegram sticker-pack output (Signal only)
- No persistent queue (in-memory; bot restart drops pending jobs)
- No per-user rate limits beyond the serial queue
- No admin commands (no priority bumps, no cancel, etc.) — keep v0 minimal
- No payment / quota / signup flow

## 3. User-Facing Behavior

### 3.1 Commands and message triggers

| Trigger | Where | Effect |
|---|---|---|
| `/start`, `/help` | anywhere | Brief description + usage example |
| `/con2signal <package_idx>` | anywhere | Enqueue a DCcon → Signal conversion job |
| Plain message `<package_idx>` (numeric) | **DM only** | Same as `/con2signal <id>` |
| `@<botusername> <package_idx>` (mention + ID) | **group** | Same as `/con2signal <id>` |

The handler that catches the loose forms accepts only messages whose stripped content is a single numeric token (optionally preceded by the bot mention). Any other text is silently ignored — the bot does not chat. Spaces and a single optional URL prefix `https://dccon.dcinside.com/...#170660` are tolerated and the trailing digits extracted.

The `/con2signal` command remains the canonical form (auto-completed by Telegram clients) and is always accepted.

### 3.2 Message lifecycle (happy path)

User sends `/con2signal 170660`. Bot immediately replies once and **edits that same message** through the lifecycle:

```
1. ⏳ 큐 대기 중 (3번째)
2. 📥 디시콘 정보 가져오는 중...
3. 📥 이미지 다운로드 중 (12/45)
4. ✨ 이미지 변환 중 (28/45)
5. 🚀 Signal 업로드 중...
6. ✅ 완료!
   팩: "개패고싶은 구구가가" (45개)
   https://signal.art/addstickers/#pack_id=...&pack_key=...
```

If the user is the only one in queue, step 1 may be skipped (or shown for under a second).

### 3.3 Error replies

| Condition | Message |
|---|---|
| Non-numeric argument | `디시콘 패키지 ID는 숫자여야 합니다. 예: /con2signal 170660` |
| `ScraperError` | `❌ 패키지 {id} 를 찾을 수 없거나 비공개입니다` |
| Partial download failure | `⚠️ {N}/{M} 만 다운로드 (나머지 스킵). 계속 진행...` (transient — flows on to next stage) |
| All downloads failed | `❌ 모든 이미지 다운로드 실패. 디시콘 서버 일시 장애일 수 있음` |
| `PackBuilderError` | `❌ {error message}` (e.g. ">200 stickers") |
| `UploaderError` 401 | User: `❌ 봇 측 인증 문제, 관리자에게 알림`. Admin (if configured): full detail. |
| `UploaderError` 413/429 | `❌ Signal rate limit. 잠시 후 자동 재시도` + worker sleeps 60s before pulling next job. The failed job is re-enqueued at the tail of the queue (one retry; if it fails again, surface as a final error) |
| Unexpected exception | User: `❌ 알 수 없는 오류, 관리자에게 알림`. Logs: full traceback. |

Worker is wrapped in `try/except` per job so one failure doesn't kill the worker.

## 4. Architecture

### 4.1 Pipeline

```
┌──────────────────────┐
│  Telegram Bot        │  /con2signal 170660
│  (PTB Application)   │  ─────────────────►
│                      │
│  - CommandHandler    │  message_object ─┐
│  - Queue producer    │                  │
└──────────┬───────────┘                  │
           │ asyncio.Queue                │
           ▼                              │
┌──────────────────────┐                  │
│  Worker              │  StatusCallback  │
│  (single coroutine)  ├─────────────────►│ message.edit_text(...)
│                      │  (throttled)     │
│  for each job:       │                  │
│    convert_pack(...) │                  │
└──────────┬───────────┘                  │
           │ uses                         │
           ▼                              │
┌──────────────────────┐                  │
│  dccon2signal lib    │  on_status ──────┘
│  - pipeline (new)    │
│  - scraper           │
│  - downloader        │
│  - image_proc        │
│  - pack_builder      │
│  - uploader          │
└──────────────────────┘
```

### 4.2 Module layout

```
signalsticker/
├── src/
│   ├── dccon2signal/
│   │   ├── pipeline.py          # NEW: convert_pack() shared high-level entry
│   │   ├── cli.py               # MODIFIED: call pipeline.convert_pack
│   │   ├── scraper.py / downloader.py / image_proc.py / ...   (unchanged)
│   │   └── ...
│   │
│   └── dccon2signal_bot/        # NEW: Telegram bot subpackage
│       ├── __init__.py
│       ├── __main__.py          # python -m dccon2signal_bot
│       ├── config.py            # Env-based config (token, paths)
│       ├── status.py            # Stage enum + Korean message formatter
│       ├── reporter.py          # StatusReporter — throttled edit_message_text
│       ├── queue.py             # Job dataclass + queue wrapper
│       ├── worker.py            # Queue consumer, calls convert_pack
│       └── handlers.py          # /con2signal, /start, /help command handlers
│
└── tests/
    └── bot/
        ├── test_status.py
        ├── test_reporter.py     # throttling behaviour
        ├── test_queue.py
        └── test_worker.py
```

### 4.3 New top-level dependency

`python-telegram-bot[ext]` (PTB v21+, async-native).

No other new deps. The `[ext]` extra brings rate-limited request retries, persistence helpers, and JobQueue — most we don't use, but it's the canonical install.

## 5. Library Refactor — `pipeline.py`

The current `cli.py::_run_one` is the only entry point for the full conversion flow. It does scraping, downloading, processing, persistence, and uploading inline, with `click.echo` for status. The bot can't reuse this because it needs status pushed through a callback, not `stdout`.

Extract a shared async function:

```python
# src/dccon2signal/pipeline.py
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class Stage(str, Enum):
    QUEUED = "queued"        # set by the bot, not by pipeline
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
) -> ConvertResult: ...
```

CLI changes:
- `cli.py::_run_one` becomes a thin wrapper that builds an `on_status` callback printing to stdout, then calls `convert_pack`.
- All existing CLI flags pass through.

The bot uses the same function with `on_status` wired to `StatusReporter.update`.

Progress hooks inside the library:
- `downloader.download_all` — accept optional `on_progress: Callable[[int, int], Awaitable[None]] | None` and call it after each fetch completes (success or fail).
- `image_proc.process_pack` — same shape, called per sticker.

`pipeline.convert_pack` then constructs these per-stage callbacks from the single `on_status` it received, so the bot sees `("downloading", (12, 45))`, `("processing", (28, 45))`, etc.

## 6. Bot Components

### 6.1 `config.py`

Env-var-driven configuration. Fail fast on missing required vars.

```python
@dataclass(frozen=True)
class BotConfig:
    telegram_token: str           # TELEGRAM_BOT_TOKEN (required)
    auth_path: Path               # DCCON2SIGNAL_AUTH (default ~/.config/dccon2signal/auth.json)
    out_dir: Path                 # DCCON2SIGNAL_OUT_DIR (default ./out)
    admin_chat_id: int | None     # BOT_ADMIN_CHAT_ID (optional)
    log_level: str                # LOG_LEVEL (default INFO)


def load() -> BotConfig: ...
```

### 6.2 `status.py`

Imports `Stage` from `dccon2signal.pipeline` (single source of truth) and adds the Korean label table + `render()` helper.

```python
from dccon2signal.pipeline import Stage


_LABELS_KO = {
    Stage.QUEUED:      "⏳ 큐 대기 중",
    Stage.FETCHING:    "📥 디시콘 정보 가져오는 중...",
    Stage.DOWNLOADING: "📥 이미지 다운로드 중",
    Stage.PROCESSING:  "✨ 이미지 변환 중",
    Stage.SAVING:      "💾 저장 중...",
    Stage.UPLOADING:   "🚀 Signal 업로드 중...",
    Stage.DONE:        "✅ 완료!",
    Stage.FAILED:      "❌",
}


def render(stage: Stage, progress: tuple[int, int] | None = None, detail: str = "") -> str:
    base = _LABELS_KO[stage]
    if progress is not None:
        base = f"{base} ({progress[0]}/{progress[1]})"
    if detail:
        base = f"{base}\n{detail}"
    return base
```

### 6.3 `reporter.py`

Wraps a single Telegram message and offers `update()` that no-ops if the rendered text is unchanged or if last update was < `min_interval` ago. Final stages (`DONE`/`FAILED`) always flush.

```python
class StatusReporter:
    def __init__(
        self,
        bot,
        chat_id: int,
        message_id: int,
        min_interval: float = 1.5,
    ): ...

    async def update(
        self,
        stage: Stage,
        progress: tuple[int, int] | None = None,
        detail: str = "",
    ) -> None: ...
```

### 6.4 `queue.py`

```python
@dataclass
class Job:
    package_idx: str
    chat_id: int
    message_id: int
    submitted_at: float


class JobQueue:
    """Thin asyncio.Queue wrapper that also exposes 'position' for a job."""

    async def submit(self, job: Job) -> int:  # returns 1-based position
        ...

    async def get(self) -> Job:
        ...

    @property
    def size(self) -> int:
        ...
```

### 6.5 `worker.py`

```python
class Worker:
    def __init__(self, queue: JobQueue, bot, config: BotConfig): ...

    async def run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._process(job)
            except Exception:
                logger.exception("Job %s failed", job)
                # Update message with failure; do NOT propagate.
            finally:
                self._queue.task_done()

    async def _process(self, job: Job) -> None:
        reporter = StatusReporter(self._bot, job.chat_id, job.message_id)
        try:
            result = await pipeline.convert_pack(
                job.package_idx,
                auth_path=self._config.auth_path,
                out_dir=self._config.out_dir,
                on_status=reporter.update,
            )
            await reporter.flush_done(result)
        except (ScraperError, PackBuilderError, UploaderError) as e:
            await reporter.flush_failed(e)
            # Admin notification is optional and runs only when:
            # - admin_chat_id is configured, AND
            # - the error is a server-side concern the operator must fix
            #   (auth 401, unexpected 5xx). Network blips / user input errors
            #   never page the admin.
            if self._should_notify_admin(e) and self._config.admin_chat_id:
                await self._bot.send_message(
                    chat_id=self._config.admin_chat_id,
                    text=f"⚠️ Bot error processing pkg {job.package_idx}: {type(e).__name__}: {e}",
                )
```

### 6.6 `handlers.py`

Three handlers feed the same `_enqueue(...)` helper so the queuing logic isn't duplicated.

```python
_PKG_RE = re.compile(r"(?:dccon\.dcinside\.com/[^\s]*#)?(\d{3,})\s*$")
"""Matches a bare ID like '170660' or a URL like 'https://dccon.dcinside.com/...#170660'."""


def _extract_pkg(text: str) -> str | None:
    m = _PKG_RE.search(text.strip())
    return m.group(1) if m else None


async def _enqueue(update: Update, context: CallbackContext, pkg: str) -> None:
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


async def cmd_con2signal(update: Update, context: CallbackContext) -> None:
    """/con2signal <id> — canonical command form."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "사용법: /con2signal <package_idx>\n예: /con2signal 170660"
        )
        return
    await _enqueue(update, context, context.args[0])


async def msg_loose(update: Update, context: CallbackContext) -> None:
    """Loose form: bare ID in DM, or '@bot <id>' mention in group.

    Bound to filters.TEXT & ~filters.COMMAND. We narrow by chat type +
    optional mention check, then by regex.
    """
    msg = update.message
    text = msg.text or ""

    if msg.chat.type == "private":
        # DM: accept '<id>' or 'https://...#<id>'.
        pkg = _extract_pkg(text)
    else:
        # Group: require explicit mention of the bot.
        me = await context.bot.get_me()
        mention = f"@{me.username}"
        if mention not in text:
            return
        pkg = _extract_pkg(text.replace(mention, "", 1))

    if pkg is not None:
        await _enqueue(update, context, pkg)


async def cmd_start_or_help(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        "DCcon → Signal 스티커팩 변환 봇\n\n"
        "사용법:\n"
        "  /con2signal <package_idx>\n"
        "  또는 DM 에서 그냥 숫자만\n"
        "  또는 그룹에서 '@봇이름 <package_idx>'\n\n"
        "예: /con2signal 170660"
    )
```

### 6.7 `__main__.py`

Boots PTB Application, creates the `JobQueue`, attaches it to `application.bot_data["queue"]` (PTB's standard place for shared state), spawns a `Worker.run()` task, registers handlers, starts long-polling. Handles graceful shutdown (SIGTERM/Ctrl-C drains worker before exit).

Handlers read the queue via `context.application.bot_data["queue"]` (so the `job_queue_size` / `job_queue_obj` references in §6.6 are sketch — the real code uses `bot_data`).

## 7. Telegram Rate Limits & Throttling

Telegram allows ~1 message edit per second per chat. With 45 download progress events firing in ~5 seconds, that's a flood. `StatusReporter`:

- Default `min_interval = 1.5s`
- Skip update if rendered text unchanged
- Always flush on `DONE` / `FAILED`
- Best-effort: if edit fails (e.g. user deleted message), log and continue — don't crash the worker

## 8. Deployment

systemd user service (Linux user with `signal-cli` already linked). Lives at `~/.config/systemd/user/dccon2signal-bot.service`:

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

Env file (`~/.config/dccon2signal-bot.env`, mode 0600):
```
TELEGRAM_BOT_TOKEN=...
DCCON2SIGNAL_AUTH=/home/user/.config/dccon2signal/auth.json
DCCON2SIGNAL_OUT_DIR=/home/user/projects/signalsticker/out
BOT_ADMIN_CHAT_ID=...
LOG_LEVEL=INFO
```

Operate:
```bash
systemctl --user daemon-reload
systemctl --user enable --now dccon2signal-bot
systemctl --user status dccon2signal-bot
journalctl --user -u dccon2signal-bot -f
```

README adds a "Telegram bot" section with: BotFather setup, env file template, systemd unit, basic troubleshooting.

## 9. Testing Strategy

**Unit**
- `test_status.py` — `render(...)` output for each stage; progress and detail combine correctly.
- `test_reporter.py` — Inject fake clock + fake bot. Multiple updates inside `min_interval` → only one `edit_message_text` call. `DONE` flushes immediately.
- `test_queue.py` — FIFO, position computation, `task_done` after `get`.
- `test_worker.py` — Patch `pipeline.convert_pack` with a fake that emits status callbacks. Verify success path edits the message through the stages and ends with install URL; verify failure path edits with error and worker keeps running for the next job.

**Integration**
- Build a PTB Application in test mode, inject fake `Update` objects through dispatcher, assert handlers enqueue / reply correctly. Use `pytest-telegram-bot` or PTB's own `Application` test helpers if available.

**Manual E2E (in deployment task)**
- Bot deployed via systemd, send `/con2signal 170660` from a Telegram client, watch message edit through stages, install resulting pack on Signal mobile.

## 10. Open Questions / TBD at Implementation

1. **PTB API specifics** — verify exact `Application` builder usage and that `message.edit_text` returns the Message object cleanly. PTB v21+ docs at implementation time.
2. **Throttle `min_interval` tuning** — start with 1.5s; adjust if Telegram returns `Too Many Requests`.
3. **Korean text length** — Telegram message edit limit is 4096 chars. The "completed" template easily fits but cap detail at 500 chars defensively.

## 11. Out of Scope (Future Work)

- Persistent queue (SQLite) — only relevant if uptime requirements grow
- Telegram inline-button cancel for a queued job
- Per-user rate limits (e.g. 5 packs/day)
- Pack preview (send first sticker as image) before upload
- Webhook deployment instead of long-polling
- Telegram sticker-pack output (separate parallel project)
