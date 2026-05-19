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
        bot: object,
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
        self._last_stage: Stage | None = None

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
        stage_changed = stage != self._last_stage
        # Throttle only applies to repeated updates WITHIN a stage (progress
        # ticks). Stage transitions always flush — otherwise SAVING/UPLOADING
        # get dropped because they fire immediately after a PROCESSING update.
        if not terminal and not stage_changed and now - self._last_sent_at < self._min_interval:
            return

        try:
            await self._bot.edit_message_text(  # type: ignore[attr-defined]
                text=text,
                chat_id=self._chat_id,
                message_id=self._message_id,
            )
        except Exception as e:
            logger.warning("edit_message_text failed: %s", e)
            return

        self._last_sent_at = now
        self._last_text = text
        self._last_stage = stage
