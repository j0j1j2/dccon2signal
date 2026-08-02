import httpx
import pytest
import respx

from dccon2signal.catalog import Catalog
from dccon2signal.models import DcconPack, DcconSticker
from dccon2signal_web.app import _package_idx_from_query, create_app


@pytest.fixture
def app(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    pack = DcconPack("10", "고양이", "author", "desc", "cover")
    pack.stickers = [DcconSticker("100", 1, "one", "png", "image")]
    catalog.sync_pack(pack)
    return create_app(catalog)


def test_package_idx_query_accepts_id_and_dccon_url():
    assert _package_idx_from_query("171367") == "171367"
    assert _package_idx_from_query("https://dccon.dcinside.com/#171367") == "171367"
    assert _package_idx_from_query("고양이") is None


@pytest.mark.asyncio
async def test_search_vote_and_detail(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        search = await client.get("/api/packs", params={"q": "고양이"})
        assert search.status_code == 200
        assert search.json()[0]["package_idx"] == "10"

        vote = await client.put("/api/stickers/100/emoji", json={"emoji": "🐱"})
        assert vote.status_code == 200
        assert "dccon_voter" in vote.cookies

        detail = await client.get("/api/packs/10")
        assert detail.json()["stickers"][0]["emoji"] == "🐱"

        invalid = await client.put("/api/stickers/100/emoji", json={"emoji": "🐱🐶"})
        assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_emoji_catalog_and_picker(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        emojis = await client.get("/api/emojis")
        detail = await client.get("/packs/10")
    assert emojis.status_code == 200
    assert any(item["emoji"] == "👨‍👩‍👧‍👦" for item in emojis.json())
    assert "<dialog id=emoji-picker>" in detail.text
    assert "name=emoji" not in detail.text
    assert "changeEmojiPage(-1)" in detail.text
    assert "changeEmojiPage(1)" in detail.text
    assert "class=emoji-form" in detail.text
    assert "class=vote-button" in detail.text
    assert "submitEmoji(this.previousElementSibling,this)" in detail.text
    assert "ontouchend" in detail.text


@pytest.mark.asyncio
async def test_download_appears_in_ranking_and_recent(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/packs/10/downloads")).status_code == 204
        assert (await client.get("/api/rankings")).json()[0]["downloads"] == 1
        assert (await client.get("/api/recent-downloads")).json()[0]["package_idx"] == "10"


@pytest.mark.asyncio
async def test_home_uses_stickergen_mobile_tabs(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "<title>StickerGen</title>" in response.text
    assert "role=tablist" in response.text
    assert response.text.count("role=tabpanel") == 3
    assert "최근 다운로드" in response.text
    assert "/media/dccon-cover?url=" in response.text


@pytest.mark.asyncio
@respx.mock
async def test_cover_proxy_supplies_dcinside_referer(app):
    image_url = "https://dcimg5.dcinside.com/dccon.php?no=abc"
    route = respx.get(image_url).mock(
        return_value=httpx.Response(200, content=b"jpeg", headers={"content-type": "image/jpeg"})
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/media/dccon-cover", params={"url": image_url})
    assert response.status_code == 200
    assert response.content == b"jpeg"
    assert route.calls[0].request.headers["referer"] == "https://dccon.dcinside.com/"


@pytest.mark.asyncio
async def test_cover_proxy_rejects_other_hosts(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/media/dccon-cover", params={"url": "https://example.com/image.png"}
        )
    assert response.status_code == 400
