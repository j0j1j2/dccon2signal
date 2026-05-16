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
    monkeypatch.delenv("LOG_LEVEL", raising=False)
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
