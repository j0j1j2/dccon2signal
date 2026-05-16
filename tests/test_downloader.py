import httpx
import pytest
import respx

from dccon2signal.downloader import download_all
from dccon2signal.models import DcconPack, DcconSticker


def _make_pack() -> DcconPack:
    pack = DcconPack(
        package_idx="1",
        title="t",
        author="a",
        description="d",
        cover_url="https://dcimg5.dcinside.com/dccon.php?no=COVER",
    )
    pack.stickers = [
        DcconSticker(
            idx="a",
            sort=1,
            title="1",
            ext="png",
            image_url="https://dcimg5.dcinside.com/dccon.php?no=S1",
        ),
        DcconSticker(
            idx="b",
            sort=2,
            title="2",
            ext="gif",
            image_url="https://dcimg5.dcinside.com/dccon.php?no=S2",
        ),
    ]
    return pack


@pytest.mark.asyncio
@respx.mock
async def test_download_all_populates_bytes(sample_static_png, sample_animated_gif):
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=COVER").respond(
        200, content=sample_static_png, headers={"content-type": "image/jpeg"}
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S1").respond(
        200, content=sample_static_png, headers={"content-type": "image/jpeg"}
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S2").respond(
        200, content=sample_animated_gif, headers={"content-type": "image/gif"}
    )

    pack = _make_pack()
    async with httpx.AsyncClient() as client:
        await download_all(pack, client)

    assert pack.cover_bytes == sample_static_png
    assert pack.stickers[0].image_bytes == sample_static_png
    assert pack.stickers[1].image_bytes == sample_animated_gif


@pytest.mark.asyncio
@respx.mock
async def test_download_all_skips_failed(sample_static_png):
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=COVER").respond(
        200, content=sample_static_png
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S1").respond(
        200, content=sample_static_png
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S2").respond(404)

    pack = _make_pack()
    async with httpx.AsyncClient() as client:
        await download_all(pack, client, retries=1)

    assert pack.stickers[0].image_bytes == sample_static_png
    assert pack.stickers[1].image_bytes is None


@pytest.mark.asyncio
@respx.mock
async def test_download_sends_referer(sample_static_png):
    route = respx.get("https://dcimg5.dcinside.com/dccon.php?no=COVER").respond(
        200, content=sample_static_png
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S1").respond(
        200, content=sample_static_png
    )
    respx.get("https://dcimg5.dcinside.com/dccon.php?no=S2").respond(
        200, content=sample_static_png
    )

    pack = _make_pack()
    async with httpx.AsyncClient() as client:
        await download_all(pack, client)

    assert route.calls.last.request.headers["Referer"] == "https://dccon.dcinside.com/"
