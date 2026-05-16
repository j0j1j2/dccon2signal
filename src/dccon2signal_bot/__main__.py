from __future__ import annotations

import asyncio
import contextlib
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
from dccon2signal_bot.handlers import cmd_start_or_help, msg_mention
from dccon2signal_bot.queue import JobQueue
from dccon2signal_bot.worker import Worker


def _set_memory_cap_gb(gb: int) -> None:
    """Cap process address space.

    Pillow's decompression-bomb check is disabled (some DCcon GIFs trip
    false positives), so we rely on an OS-level memory ceiling instead. A
    real bomb hits the cap and raises MemoryError, which the worker's
    skip-on-error catches — the bot survives.
    """
    try:
        import resource

        max_bytes = gb * 1024 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
    except (ImportError, ValueError, OSError) as e:
        logging.getLogger(__name__).warning("Could not set RLIMIT_AS: %s", e)


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

    # Defense against decompression-bomb GIFs uploaded to DCInside.
    _set_memory_cap_gb(2)

    queue = JobQueue()

    app = Application.builder().token(cfg.telegram_token).build()
    app.bot_data["queue"] = queue

    app.add_handler(CommandHandler("start", cmd_start_or_help))
    app.add_handler(CommandHandler("help", cmd_start_or_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_mention))

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
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

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
