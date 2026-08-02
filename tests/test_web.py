import httpx
import pytest

from dccon2signal.catalog import Catalog
from dccon2signal.models import DcconPack, DcconSticker
from dccon2signal_web.app import create_app


@pytest.fixture
def app(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    pack = DcconPack("10", "고양이", "author", "desc", "cover")
    pack.stickers = [DcconSticker("100", 1, "one", "png", "image")]
    catalog.sync_pack(pack)
    return create_app(catalog)


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
