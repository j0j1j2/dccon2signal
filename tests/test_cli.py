import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dccon2signal.cli import main
from dccon2signal.models import DcconPack, DcconSticker


@pytest.fixture
def fake_pack() -> DcconPack:
    pack = DcconPack(
        package_idx="170660",
        title="t",
        author="a",
        description="d",
        cover_url="u",
        cover_bytes=b"COVER",
    )
    pack.stickers.append(
        DcconSticker(
            idx="1",
            sort=1,
            title="1",
            ext="png",
            image_url="u",
            image_bytes=b"S",
        )
    )
    return pack


def test_convert_only_no_upload(tmp_path: Path, fake_pack):
    out_dir = tmp_path / "out"

    async def _scrape(_idx, _client):
        return fake_pack

    async def _download(_pack, _client, **_kw):
        return None

    with (
        patch("dccon2signal.cli.scraper.fetch_pack", side_effect=_scrape),
        patch("dccon2signal.cli.downloader.download_all", side_effect=_download),
        patch("dccon2signal.cli.image_proc.process_pack") as proc,
    ):

        def _fake_proc(pack, **_kw):
            pack.cover_processed = b"\x89PNG\r\n\x1a\nFAKE"
            for s in pack.stickers:
                s.processed_bytes = b"\x89PNG\r\n\x1a\nFAKE"
                s.processed_ext = "png"

        proc.side_effect = _fake_proc
        runner = CliRunner()
        result = runner.invoke(main, ["170660", "--no-upload", "--out-dir", str(out_dir)])

    assert result.exit_code == 0, result.output
    manifest = json.loads((out_dir / "170660" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["title"] == "t"


def test_upload_path_invokes_uploader(tmp_path: Path, fake_pack):
    out_dir = tmp_path / "out"
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"username": "+1", "password": "p"}', encoding="utf-8")

    async def _scrape(_idx, _client):
        return fake_pack

    async def _download(_pack, _client, **_kw):
        return None

    async def _upload(_signal_pack, _auth):
        return "abc", "def"

    with (
        patch("dccon2signal.cli.scraper.fetch_pack", side_effect=_scrape),
        patch("dccon2signal.cli.downloader.download_all", side_effect=_download),
        patch("dccon2signal.cli.image_proc.process_pack") as proc,
        patch("dccon2signal.cli.uploader.upload", side_effect=_upload),
    ):

        def _fake_proc(pack, **_kw):
            pack.cover_processed = b"\x89PNG\r\n\x1a\nFAKE"
            for s in pack.stickers:
                s.processed_bytes = b"\x89PNG\r\n\x1a\nFAKE"
                s.processed_ext = "png"

        proc.side_effect = _fake_proc

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["170660", "--out-dir", str(out_dir), "--auth", str(auth_path)],
        )

    assert result.exit_code == 0, result.output
    assert "pack_id=abc" in result.output
    assert "pack_key=def" in result.output
