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
            f"""<header class=hero><span class=eyebrow>DCON / EMOJI COMMONS</span>
            <h1>디시콘을 찾고,<br>이모지를 고르세요.</h1>
            <p>함께 만드는 Signal 스티커 이모지 데이터베이스.</p></header>
            <form class=search><input name=q value="{html.escape(q)}"
              placeholder="디시콘 이름, 제작자, package ID" aria-label="디시콘 검색"><button>SEARCH</button></form>
            <section><div class=section-head><h2>검색 결과</h2><span>SEARCH</span></div><div class=grid>{cards(packs)}</div></section>
            <section><div class=section-head><h2>주간 랭킹</h2><span>7 DAYS</span></div><div class=grid>{cards(ranking, "downloads")}</div></section>
            <section><div class=section-head><h2>최근 다운로드</h2><span>RECENT</span></div><div class=grid>{cards(recent)}</div></section>""",
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
            f"""<a class=back href=/>&larr; BACK</a><header class=pack-head>
            <span class=eyebrow>PACKAGE {html.escape(package_idx)}</span>
            <h1>{html.escape(str(pack["title"]))}</h1>
            <p>BY {html.escape(str(pack["author"]))}</p></header>
            <div class=stickers>{cells}</div>
            <script>async function vote(e,id){{e.preventDefault();let emoji=e.target.emoji.value;
            let r=await fetch('/api/stickers/'+id+'/emoji',{{method:'PUT',headers:{{'content-type':'application/json'}},body:JSON.stringify({{emoji}})}});
            if(!r.ok) alert(await r.text()); else e.target.querySelector('button').textContent='완료';}}</script>""",
        )

    return app


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width">
    <title>{html.escape(title)}</title><style>
    :root{{--bg:#fff;--fg:#090909;--muted:#6b6b6b;--line:#d8d8d8;--soft:#f4f4f4;--invert:#fff;color-scheme:light dark}}
    *{{box-sizing:border-box}}html{{font-family:Inter,"Helvetica Neue",Arial,sans-serif}}
    body{{margin:0 auto;max-width:1280px;padding:28px 32px 80px;background:var(--bg);color:var(--fg);font-size:15px;line-height:1.4}}
    a{{color:inherit;text-decoration:none}}h1,h2,p{{margin-top:0}}button,input{{font:inherit;color:inherit}}
    .hero{{padding:11vh 0 64px;border-bottom:1px solid var(--line)}}
    .hero h1,.pack-head h1{{max-width:900px;margin:18px 0 24px;font-size:clamp(3rem,8vw,7.5rem);font-weight:600;line-height:.92;letter-spacing:-.065em}}
    .hero p,.pack-head p{{color:var(--muted);font-size:1rem}}
    .eyebrow,.section-head span,.back{{font-size:.72rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase}}
    .search{{display:grid;grid-template-columns:1fr auto;margin:32px 0 80px;border:1px solid var(--fg)}}
    .search input{{min-width:0;padding:19px 20px;background:transparent;border:0;outline:0}}
    .search input:focus{{box-shadow:inset 0 0 0 2px var(--fg)}}
    button{{padding:0 24px;border:0;background:var(--fg);color:var(--invert);font-size:.75rem;font-weight:700;letter-spacing:.12em;cursor:pointer}}
    button:hover{{opacity:.72}}section{{margin-top:72px}}
    .section-head{{display:flex;align-items:baseline;justify-content:space-between;padding-bottom:14px;border-bottom:1px solid var(--fg)}}
    .section-head h2{{margin:0;font-size:1rem;font-weight:600}}.section-head span,small,.muted{{color:var(--muted)}}
    .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}}
    .card{{display:grid;grid-template-columns:76px 1fr;gap:18px;align-items:center;min-height:108px;padding:16px 0;border-bottom:1px solid var(--line)}}
    .card:nth-child(odd){{padding-right:24px;border-right:1px solid var(--line)}}.card:nth-child(even){{padding-left:24px}}
    .card img{{width:76px;height:76px;object-fit:contain;filter:grayscale(1);transition:filter .18s,transform .18s}}
    .card:hover img{{filter:none;transform:scale(1.04)}}.card b{{font-size:1rem;font-weight:600}}small{{display:block;margin-top:6px}}
    .back{{display:inline-block;margin:12px 0 80px}}.pack-head{{padding-bottom:56px;border-bottom:1px solid var(--fg)}}
    .pack-head h1{{font-size:clamp(3rem,7vw,6rem)}}
    .stickers{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border-left:1px solid var(--line)}}
    .sticker{{padding:14px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}}
    .sticker>img{{width:100%;aspect-ratio:1;object-fit:contain;background:var(--soft)}}
    .sticker>div{{height:40px;padding-top:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.78rem}}
    .sticker form{{display:grid;grid-template-columns:1fr auto;border:1px solid var(--line)}}
    .sticker input{{min-width:0;width:100%;padding:9px;background:transparent;border:0;outline:0}}
    .sticker form button{{padding:0 12px;font-size:.65rem}}
    @media(prefers-color-scheme:dark){{:root{{--bg:#090909;--fg:#f5f5f5;--muted:#999;--line:#303030;--soft:#171717;--invert:#090909}}}}
    @media(max-width:760px){{body{{padding:20px 16px 56px}}.hero{{padding-top:8vh}}.grid{{grid-template-columns:1fr}}.card:nth-child(n){{padding:14px 0;border-right:0}}.stickers{{grid-template-columns:repeat(2,minmax(0,1fr))}}.search{{margin-bottom:56px}}}}
    </style><body>{body}</body></html>"""


app = create_app()
