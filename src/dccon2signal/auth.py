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
