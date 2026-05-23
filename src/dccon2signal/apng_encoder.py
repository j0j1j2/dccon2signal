"""Animated APNG encoder backed by the Rust `dccon-apng` binary.

The Rust pipeline does GIF decode → shared-palette imagequant → APNG write with
inter-frame diff → libdeflater re-compress, in one process. This is roughly
20× faster than the previous Python + apngasm + oxipng pipeline and produces
smaller files thanks to real palette-mode frame diffing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BUNDLED_BINARY = _REPO_ROOT / "apng-encoder" / "target" / "release" / "dccon-apng"
_ENV_BINARY = os.environ.get("DCCON_APNG_BINARY")


class EncoderUnavailable(RuntimeError):
    """Raised when the dccon-apng binary cannot be located."""


def _binary_path() -> str:
    """Locate the encoder binary, preferring (in order): DCCON_APNG_BINARY env
    var, the release build inside this repo, then `dccon-apng` on PATH."""
    if _ENV_BINARY:
        return _ENV_BINARY
    if _BUNDLED_BINARY.is_file() and os.access(_BUNDLED_BINARY, os.X_OK):
        return str(_BUNDLED_BINARY)
    path_lookup = shutil.which("dccon-apng")
    if path_lookup is not None:
        return path_lookup
    raise EncoderUnavailable(
        "dccon-apng binary not found. Build it with "
        "`cd apng-encoder && cargo build --release`, or set DCCON_APNG_BINARY."
    )


def encode_animated_apng(gif_bytes: bytes, max_bytes: int) -> bytes | None:
    """Encode `gif_bytes` (raw GIF) to an APNG ≤ `max_bytes`.

    Returns the APNG bytes, or None if the binary couldn't find a fit
    (caller falls back to a static PNG).
    """
    binary = _binary_path()
    with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as tmp:
        tmp.write(gif_bytes)
        gif_path = tmp.name
    try:
        res = subprocess.run(
            [binary, gif_path, str(max_bytes)],
            capture_output=True,
        )
    finally:
        try:
            os.unlink(gif_path)
        except OSError:
            pass

    if res.returncode == 0:
        return res.stdout
    err = res.stderr.decode(errors="replace").strip()
    # The binary's "no fit" exit is treated as a soft failure so the caller
    # can fall back to a static PNG. Anything else surfaces as a RuntimeError.
    if "no ladder step fit" in err:
        logger.warning("dccon-apng: %s", err)
        return None
    raise RuntimeError(f"dccon-apng failed (exit {res.returncode}): {err[:500]}")


__all__ = ("EncoderUnavailable", "encode_animated_apng")
