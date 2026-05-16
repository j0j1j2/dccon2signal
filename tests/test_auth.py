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
