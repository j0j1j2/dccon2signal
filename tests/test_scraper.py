import httpx
import pytest
import respx

from dccon2signal.scraper import ScraperError, fetch_pack


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pack_parses_response(package_detail_json: str):
    respx.post("https://dccon.dcinside.com/index/package_detail").respond(
        200, text=package_detail_json
    )
    async with httpx.AsyncClient() as client:
        pack = await fetch_pack("170660", client)

    assert pack.package_idx == "170660"
    assert pack.title == "개패고싶은 구구가가"
    assert pack.author == "도끼도끼"
    assert pack.cover_url.startswith("https://dcimg5.dcinside.com/dccon.php?no=")
    assert len(pack.stickers) == 45
    first = pack.stickers[0]
    assert first.sort == 1
    assert first.ext == "png"
    assert first.image_url.startswith("https://dcimg5.dcinside.com/dccon.php?no=")
    assert "구구가가" in pack.tags


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pack_404_raises():
    respx.post("https://dccon.dcinside.com/index/package_detail").respond(
        200, json={"info": [], "detail": []}
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(ScraperError, match="not found"):
            await fetch_pack("999999", client)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_pack_sends_correct_body():
    route = respx.post("https://dccon.dcinside.com/index/package_detail").respond(
        200,
        json={
            "info": {
                "package_idx": "1",
                "title": "t",
                "seller_name": "a",
                "description": "d",
                "main_img_path": "p",
            },
            "detail": [],
            "tags": [],
        },
    )
    async with httpx.AsyncClient() as client:
        await fetch_pack("1", client)
    sent = route.calls.last.request
    assert b"package_idx=1" in sent.content
    assert sent.headers.get("X-Requested-With") == "XMLHttpRequest"
