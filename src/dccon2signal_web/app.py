from __future__ import annotations

import html
import os
import secrets
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel

from dccon2signal.catalog import Catalog
from dccon2signal.dccon_search import search_dccon
from dccon2signal.scraper import fetch_pack


class EmojiVote(BaseModel):
    emoji: str


def _catalog_path() -> Path:
    return Path(os.environ.get("DCCON2SIGNAL_CATALOG_DB", "./out/catalog.sqlite3"))


def _voter_key(request: Request, response: Response) -> str:
    key = request.cookies.get("dccon_voter")
    if key:
        return key
    key = secrets.token_urlsafe(24)
    response.set_cookie("dccon_voter", key, max_age=31536000, httponly=True, samesite="lax")
    return key


def create_app(catalog: Catalog | None = None) -> FastAPI:
    app = FastAPI(title="DCCon Emoji Commons", version="0.1.0")
    store = catalog or Catalog(_catalog_path())
    app.state.catalog = store

    @app.get("/api/packs")
    async def search_packs(q: str = "") -> list[dict[str, object]]:
        return store.search_packs(q.strip())

    @app.get("/api/dccon-search")
    async def dccon_search(q: str) -> list[dict[str, object]]:
        async with httpx.AsyncClient() as client:
            return await search_dccon(q, client)

    @app.post("/api/packs/{package_idx}/sync")
    async def sync_pack(package_idx: str) -> dict[str, object]:
        if not package_idx.isdigit():
            raise HTTPException(400, "package_idx must be numeric")
        async with httpx.AsyncClient() as client:
            pack = await fetch_pack(package_idx, client)
        store.sync_pack(pack)
        result = store.get_pack(package_idx)
        assert result is not None
        return result

    @app.get("/api/packs/{package_idx}")
    async def pack_detail(package_idx: str) -> dict[str, object]:
        result = store.get_pack(package_idx)
        if result is None:
            raise HTTPException(404, "pack not found; sync it first")
        return result

    @app.put("/api/stickers/{sticker_idx}/emoji")
    async def vote_emoji(
        sticker_idx: str, vote: EmojiVote, request: Request, response: Response
    ) -> dict[str, str]:
        emoji = vote.emoji.strip()
        if not emoji or len(emoji) > 16 or any(ch.isalnum() for ch in emoji):
            raise HTTPException(400, "one emoji is required")
        try:
            store.set_vote(sticker_idx, _voter_key(request, response), emoji)
        except KeyError:
            raise HTTPException(404, "sticker not found") from None
        return {"sticker_idx": sticker_idx, "emoji": emoji}

    @app.get("/api/rankings")
    async def rankings(days: int = 7) -> list[dict[str, object]]:
        if days < 1 or days > 3650:
            raise HTTPException(400, "days must be between 1 and 3650")
        return store.ranking(days)

    @app.get("/api/recent-downloads")
    async def recent_downloads() -> list[dict[str, object]]:
        return store.recent_downloads()

    @app.get("/media/stickers/{sticker_idx}")
    async def sticker_image(sticker_idx: str) -> FastAPIResponse:
        image_url = store.sticker_image_url(sticker_idx)
        if image_url is None:
            raise HTTPException(404, "sticker not found")
        async with httpx.AsyncClient() as client:
            image = await client.get(
                image_url,
                headers={
                    "Referer": "https://dccon.dcinside.com/",
                    "User-Agent": "Mozilla/5.0 (dccon2signal)",
                },
                timeout=15.0,
            )
        image.raise_for_status()
        return FastAPIResponse(
            content=image.content,
            media_type=image.headers.get("content-type", "image/png"),
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.post("/api/packs/{package_idx}/downloads", status_code=204)
    async def record_download(package_idx: str) -> Response:
        if store.get_pack(package_idx) is None:
            raise HTTPException(404, "pack not found")
        store.record_download(package_idx, "web")
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    async def home(q: str = "") -> str:
        if q.strip():
            async with httpx.AsyncClient() as client:
                packs = await search_dccon(q, client)
        else:
            packs = store.search_packs()
        ranking = store.ranking(7, 10)
        recent = store.recent_downloads(10)

        def cards(items: list[dict[str, object]], metric: str = "") -> str:
            if not items:
                return "<p class=muted>아직 데이터가 없습니다.</p>"
            return "".join(
                f'<a class=card href="/packs/{html.escape(str(x["package_idx"]))}">'
                f'<img src="{html.escape(str(x["cover_url"]))}" loading=lazy>'
                f"<span><b>{html.escape(str(x['title']))}</b><small>"
                f"{html.escape(str(x['author']))}"
                + (f" · {x.get(metric, 0)}회" if metric else "")
                + "</small></span></a>"
                for x in items
            )

        return _page(
            "디시콘 이모지 광장",
            f"""<h1>디시콘 이모지 광장</h1>
            <form class=search><input name=q value="{html.escape(q)}"
              placeholder="이름, 제작자 또는 package_idx"><button>검색</button></form>
            <p class=hint>없는 팩은 <code>/api/packs/&lt;package_idx&gt;/sync</code>로 등록할 수 있습니다.</p>
            <h2>검색 결과</h2><div class=grid>{cards(packs)}</div>
            <h2>주간 다운로드 랭킹</h2><div class=grid>{cards(ranking, "downloads")}</div>
            <h2>최근 다운로드</h2><div class=grid>{cards(recent)}</div>""",
        )

    @app.get("/packs/{package_idx}", response_class=HTMLResponse)
    async def pack_page(package_idx: str) -> str:
        pack = store.get_pack(package_idx)
        if pack is None:
            if not package_idx.isdigit():
                raise HTTPException(404, "pack not found")
            async with httpx.AsyncClient() as client:
                fetched = await fetch_pack(package_idx, client)
            store.sync_pack(fetched)
            pack = store.get_pack(package_idx)
            assert pack is not None
        stickers = pack["stickers"]
        assert isinstance(stickers, list)
        cells = "".join(
            f"""<article class=sticker><img src="/media/stickers/{html.escape(str(s["sticker_idx"]))}" loading=lazy>
            <div>#{s["sort"]} {html.escape(str(s["title"]))}</div>
            <form onsubmit="vote(event,'{html.escape(str(s["sticker_idx"]))}')">
              <input name=emoji value="{html.escape(str(s.get("emoji") or ""))}" placeholder="😀" maxlength=16>
              <button>투표</button></form></article>"""
            for s in stickers
            if isinstance(s, dict)
        )
        return _page(
            str(pack["title"]),
            f"""<a href=/>&larr; 검색</a><h1>{html.escape(str(pack["title"]))}</h1>
            <p>{html.escape(str(pack["author"]))} · package {html.escape(package_idx)}</p>
            <div class=stickers>{cells}</div>
            <script>async function vote(e,id){{e.preventDefault();let emoji=e.target.emoji.value;
            let r=await fetch('/api/stickers/'+id+'/emoji',{{method:'PUT',headers:{{'content-type':'application/json'}},body:JSON.stringify({{emoji}})}});
            if(!r.ok) alert(await r.text()); else e.target.querySelector('button').textContent='완료';}}</script>""",
        )

    return app


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width">
    <title>{html.escape(title)}</title><style>
    :root{{--bg:#f6f3ed;--ink:#24211d;--accent:#e45235;--card:#fff}}*{{box-sizing:border-box}}
    body{{margin:auto;max-width:1080px;padding:32px 20px;background:var(--bg);color:var(--ink);font:16px system-ui}}
    h1{{font-size:clamp(2rem,6vw,4rem);margin:.3em 0}}h2{{margin-top:2.5rem}}a{{color:inherit;text-decoration:none}}
    input,button{{font:inherit;padding:.7rem;border:1px solid #bbb;border-radius:9px}}button{{background:var(--accent);color:#fff;border:0;cursor:pointer}}
    .search{{display:flex;gap:8px}}.search input{{flex:1}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}}
    .card{{display:flex;gap:12px;align-items:center;background:var(--card);padding:10px;border-radius:14px}}.card img{{width:64px;height:64px;object-fit:contain}}
    small{{display:block;color:#716b63;margin-top:4px}}.muted,.hint{{color:#716b63}}.stickers{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}}
    .sticker{{background:#fff;padding:10px;border-radius:14px}}.sticker>img{{width:100%;aspect-ratio:1;object-fit:contain}}.sticker form{{display:flex;gap:5px;margin-top:7px}}.sticker input{{min-width:0;width:100%}}
    </style><body>{body}</body></html>"""


app = create_app()
