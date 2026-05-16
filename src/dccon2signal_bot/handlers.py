import re
import time

from dccon2signal_bot.queue import Job

_PKG_RE = re.compile(r"(?:dccon\.dcinside\.com/[^\s]*#)?(\d{3,})\s*$")


def _extract_pkg(text: str) -> str | None:
    stripped = text.strip()
    # For bare input we need the entire (trimmed) string to match; for URLs we
    # search anywhere because the URL has additional prefix characters.
    m = _PKG_RE.search(stripped) if "://" in stripped else _PKG_RE.fullmatch(stripped)
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


async def msg_mention(update, context) -> None:
    """Single trigger: '@botname <package_idx>' in any chat type.

    Mention required even in DM, so a stray '170660' doesn't accidentally
    enqueue work.
    """
    msg = update.message
    text = msg.text or ""

    me = await context.bot.get_me()
    mention = f"@{me.username}"
    if mention not in text:
        return

    pkg = _extract_pkg(text.replace(mention, "", 1))
    if pkg is not None:
        await _enqueue(update, context, pkg)


async def cmd_start_or_help(update, context) -> None:
    me = await context.bot.get_me()
    await update.message.reply_text(
        "DCcon → Signal 스티커팩 변환 봇\n\n"
        f"사용법: @{me.username} <package_idx>\n"
        f"예: @{me.username} 170660"
    )
