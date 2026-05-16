import logging

from dccon2signal.auth import AuthError
from dccon2signal.pack_builder import PackBuilderError
from dccon2signal.pipeline import Stage, convert_pack
from dccon2signal.scraper import ScraperError
from dccon2signal.uploader import UploaderError
from dccon2signal_bot.config import BotConfig
from dccon2signal_bot.queue import Job, JobQueue
from dccon2signal_bot.reporter import StatusReporter

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
