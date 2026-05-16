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
