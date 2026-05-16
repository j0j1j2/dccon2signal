from unittest.mock import patch

import pytest

from dccon2signal.models import DcconPack, DcconSticker
from dccon2signal.pipeline import ConvertResult, Stage, convert_pack


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


@pytest.mark.asyncio
async def test_convert_pack_emits_stage_callbacks(tmp_path, fake_pack):
    seen: list[tuple[Stage, tuple[int, int] | None]] = []

    async def cb(stage, progress=None, detail=""):
        seen.append((stage, progress))

    async def fake_scrape(_idx, _client):
        return fake_pack

    async def fake_download(pack, _client, **_kw):
        on_progress = _kw.get("on_progress")
        if on_progress:
            await on_progress(1, 1)

    def fake_process(pack, **_kw):
        pack.cover_processed = b"\x89PNG\r\n\x1a\nFAKE"
        for s in pack.stickers:
            s.processed_bytes = b"\x89PNG\r\n\x1a\nFAKE"
            s.processed_ext = "png"

    auth = tmp_path / "auth.json"
    auth.write_text('{"username": "+1", "password": "p"}', encoding="utf-8")

    async def fake_upload(_pack, _auth):
        return "abc", "def"

    with (
        patch("dccon2signal.pipeline.scraper.fetch_pack", side_effect=fake_scrape),
        patch("dccon2signal.pipeline.downloader.download_all", side_effect=fake_download),
        patch("dccon2signal.pipeline.image_proc.process_pack", side_effect=fake_process),
        patch("dccon2signal.pipeline.uploader.upload", side_effect=fake_upload),
    ):
        result = await convert_pack(
            "170660",
            auth_path=auth,
            out_dir=tmp_path / "out",
            on_status=cb,
        )

    assert isinstance(result, ConvertResult)
    assert result.pack_id == "abc"
    assert result.pack_key == "def"
    assert result.install_url.endswith("pack_id=abc&pack_key=def")

    stages = [s for s, _ in seen]
    assert Stage.FETCHING in stages
    assert Stage.DOWNLOADING in stages
    assert Stage.PROCESSING in stages
    assert Stage.SAVING in stages
    assert Stage.UPLOADING in stages
    assert stages[-1] == Stage.DONE


@pytest.mark.asyncio
async def test_convert_pack_no_upload(tmp_path, fake_pack):
    async def fake_scrape(_idx, _client):
        return fake_pack

    async def fake_download(pack, _client, **_kw):
        pass

    def fake_process(pack, **_kw):
        pack.cover_processed = b"\x89PNG\r\n\x1a\nFAKE"
        for s in pack.stickers:
            s.processed_bytes = b"\x89PNG\r\n\x1a\nFAKE"
            s.processed_ext = "png"

    with (
        patch("dccon2signal.pipeline.scraper.fetch_pack", side_effect=fake_scrape),
        patch("dccon2signal.pipeline.downloader.download_all", side_effect=fake_download),
        patch("dccon2signal.pipeline.image_proc.process_pack", side_effect=fake_process),
    ):
        result = await convert_pack(
            "170660",
            auth_path=tmp_path / "missing.json",
            out_dir=tmp_path / "out",
            upload=False,
        )

    assert result.pack_id == ""
    assert result.pack_key == ""
    assert result.install_url == ""
