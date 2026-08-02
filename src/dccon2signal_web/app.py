from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import secrets
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import emoji as emoji_lib
import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel

from dccon2signal.catalog import Catalog
from dccon2signal.dccon_search import SearchPage, search_dccon_page
from dccon2signal.pipeline import convert_pack
from dccon2signal.scraper import fetch_pack


class EmojiVote(BaseModel):
    emoji: str


EMOJI_CATEGORIES = {
    "smileys": "표정",
    "people": "사람",
    "animals": "동물·자연",
    "food": "음식",
    "travel": "여행·장소",
    "activities": "활동",
    "objects": "사물",
    "symbols": "기호·국기",
}
SEARCH_CACHE_TTL_SECONDS = 300
MEDIA_CACHE_MAX_BYTES = 256 * 1024 * 1024
MEDIA_CACHE_TARGET_BYTES = 224 * 1024 * 1024


def _emoji_category(value: str, name: str) -> str:
    label = name.strip(":").lower()
    codepoints = {ord(char) for char in value}
    keyword_groups = (
        ("symbols", ("flag", "button", "arrow", "sign", "symbol", "mark", "keycap", "zodiac")),
        (
            "food",
            (
                "food",
                "fruit",
                "vegetable",
                "bread",
                "rice",
                "cake",
                "drink",
                "cup",
                "bottle",
                "fork",
                "spoon",
                "meat",
                "fish_cake",
                "egg",
                "cheese",
            ),
        ),
        (
            "animals",
            (
                "animal",
                "dog",
                "cat",
                "monkey",
                "bird",
                "fish",
                "bug",
                "flower",
                "tree",
                "plant",
                "moon",
                "sun",
                "weather",
                "earth",
                "nature",
            ),
        ),
        (
            "activities",
            (
                "sport",
                "ball",
                "game",
                "medal",
                "trophy",
                "musical",
                "performing",
                "skier",
                "skiing",
                "skate",
                "swim",
                "climb",
                "wrestl",
                "fenc",
                "dancer",
            ),
        ),
        (
            "travel",
            (
                "vehicle",
                "car",
                "bus",
                "train",
                "airplane",
                "ship",
                "boat",
                "building",
                "house",
                "city",
                "map",
                "mountain",
                "camping",
                "beach",
                "hotel",
                "place",
            ),
        ),
        (
            "objects",
            (
                "phone",
                "computer",
                "book",
                "tool",
                "weapon",
                "money",
                "mail",
                "clock",
                "camera",
                "light",
                "lock",
                "key",
                "gift",
                "clothing",
                "sound",
                "office",
            ),
        ),
        (
            "people",
            (
                "person",
                "people",
                "man",
                "woman",
                "boy",
                "girl",
                "baby",
                "adult",
                "hand",
                "finger",
                "body",
                "hair",
                "family",
                "couple",
                "kiss",
                "bust",
                "ear",
                "eye",
                "mouth",
                "leg",
                "foot",
            ),
        ),
    )
    if any(0x1F1E6 <= cp <= 0x1F1FF for cp in codepoints):
        return "symbols"
    for category, keywords in keyword_groups:
        if any(keyword in label for keyword in keywords):
            return category
    if any(0x1F600 <= cp <= 0x1F64F for cp in codepoints):
        return "smileys"
    return "symbols"


def _catalog_path() -> Path:
    return Path(os.environ.get("DCCON2SIGNAL_CATALOG_DB", "./out/catalog.sqlite3"))


def _auth_path() -> Path:
    return Path(
        os.environ.get(
            "DCCON2SIGNAL_AUTH", str(Path.home() / ".config" / "dccon2signal" / "auth.json")
        )
    )


def _out_dir() -> Path:
    return Path(os.environ.get("DCCON2SIGNAL_OUT_DIR", "./out"))


@lru_cache(maxsize=1)
def _emoji_catalog() -> list[dict[str, str]]:
    return [
        {
            "emoji": value,
            "name": str(data.get("en", "")),
            "category": _emoji_category(value, str(data.get("en", ""))),
        }
        for value, data in emoji_lib.EMOJI_DATA.items()
        if data.get("status", 0) <= 2
    ]


def _media_type(data: bytes, fallback: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return fallback


def _prune_media_cache(cache_dir: Path) -> None:
    files = [path for path in cache_dir.iterdir() if path.is_file() and path.suffix == ".bin"]
    total = sum(path.stat().st_size for path in files)
    if total <= MEDIA_CACHE_MAX_BYTES:
        return
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        size = path.stat().st_size
        path.unlink(missing_ok=True)
        total -= size
        if total <= MEDIA_CACHE_TARGET_BYTES:
            break


def _voter_key(request: Request, response: Response) -> str:
    key = request.cookies.get("dccon_voter")
    if key:
        return key
    key = secrets.token_urlsafe(24)
    response.set_cookie("dccon_voter", key, max_age=31536000, httponly=True, samesite="lax")
    return key


def _package_idx_from_query(query: str) -> str | None:
    value = query.strip()
    if value.isdigit():
        return value
    match = re.search(r"[#/](\d+)/?$", value)
    return match.group(1) if match else None


def create_app(catalog: Catalog | None = None) -> FastAPI:
    app = FastAPI(title="StickerGen", version="0.1.0")
    store = catalog or Catalog(_catalog_path())
    generation_locks: dict[str, asyncio.Lock] = {}
    media_locks: dict[str, asyncio.Lock] = {}
    search_cache: dict[tuple[str, int], tuple[float, SearchPage]] = {}
    media_cache_dir = store.path.parent / "media-cache"
    media_cache_dir.mkdir(parents=True, exist_ok=True)
    app.state.catalog = store

    async def cached_search(query: str, page: int) -> SearchPage:
        key = (query.strip(), page)
        cached = search_cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < SEARCH_CACHE_TTL_SECONDS:
            return cached[1]
        async with httpx.AsyncClient() as client:
            result = await search_dccon_page(query, client, page)
        search_cache[key] = (now, result)
        if len(search_cache) > 256:
            oldest = min(search_cache, key=lambda item: search_cache[item][0])
            search_cache.pop(oldest, None)
        return result

    async def cached_image(url: str, fallback_type: str) -> tuple[bytes, str, str]:
        digest = hashlib.sha256(url.encode()).hexdigest()
        path = media_cache_dir / f"{digest}.bin"
        lock = media_locks.setdefault(digest, asyncio.Lock())
        async with lock:
            if path.exists():
                data = path.read_bytes()
                path.touch()
                return data, _media_type(data, fallback_type), digest
            async with httpx.AsyncClient() as client:
                image = await client.get(
                    url,
                    headers={
                        "Referer": "https://dccon.dcinside.com/",
                        "User-Agent": "Mozilla/5.0 (dccon2signal)",
                    },
                    timeout=15.0,
                )
            image.raise_for_status()
            data = image.content
            temporary = path.with_suffix(f".{secrets.token_hex(6)}.tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
            _prune_media_cache(media_cache_dir)
            return data, image.headers.get("content-type", fallback_type), digest

    @app.get("/api/packs")
    async def search_packs(q: str = "") -> list[dict[str, object]]:
        return store.search_packs(q.strip())

    @app.get("/api/dccon-search")
    async def dccon_search(q: str, page: int = 1) -> dict[str, object]:
        if page < 1:
            raise HTTPException(400, "page must be positive")
        result = await cached_search(q, page)
        return {
            "items": result.items,
            "page": result.page,
            "total": result.total,
            "page_count": result.page_count,
        }

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
        if not emoji_lib.is_emoji(emoji):
            raise HTTPException(400, "one valid Unicode emoji is required")
        try:
            store.set_vote(sticker_idx, _voter_key(request, response), emoji)
        except KeyError:
            raise HTTPException(404, "sticker not found") from None
        return {"sticker_idx": sticker_idx, "emoji": emoji}

    @app.get("/api/emojis")
    async def emojis() -> JSONResponse:
        return JSONResponse(_emoji_catalog(), headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/rankings")
    async def rankings(days: int = 7) -> list[dict[str, object]]:
        if days < 1 or days > 3650:
            raise HTTPException(400, "days must be between 1 and 3650")
        return store.ranking(days)

    @app.get("/api/recent-downloads")
    async def recent_downloads() -> list[dict[str, object]]:
        return store.recent_downloads()

    @app.post("/api/packs/{package_idx}/generate")
    async def generate_pack(package_idx: str) -> dict[str, object]:
        if not package_idx.isdigit():
            raise HTTPException(400, "package_idx must be numeric")
        lock = generation_locks.setdefault(package_idx, asyncio.Lock())
        async with lock:
            try:
                result = await asyncio.to_thread(
                    lambda: asyncio.run(
                        convert_pack(
                            package_idx,
                            auth_path=_auth_path(),
                            out_dir=_out_dir(),
                            catalog_path=store.path,
                            download_source="web",
                        )
                    )
                )
            except Exception as error:
                raise HTTPException(500, f"팩 생성 실패: {error}") from error
        return {
            "package_idx": package_idx,
            "title": result.title,
            "sticker_count": result.sticker_count,
            "install_url": result.install_url,
        }

    @app.get("/media/stickers/{sticker_idx}")
    async def sticker_image(sticker_idx: str, request: Request) -> FastAPIResponse:
        image_url = store.sticker_image_url(sticker_idx)
        if image_url is None:
            raise HTTPException(404, "sticker not found")
        data, media_type, etag = await cached_image(image_url, "image/png")
        if request.headers.get("if-none-match") == etag:
            return FastAPIResponse(status_code=304, headers={"ETag": etag})
        return FastAPIResponse(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400, immutable", "ETag": etag},
        )

    @app.get("/media/dccon-cover")
    async def dccon_cover(url: str, request: Request) -> FastAPIResponse:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "dcimg5.dcinside.com"
            or parsed.path != "/dccon.php"
        ):
            raise HTTPException(400, "unsupported image URL")
        data, media_type, etag = await cached_image(url, "image/jpeg")
        if request.headers.get("if-none-match") == etag:
            return FastAPIResponse(status_code=304, headers={"ETag": etag})
        return FastAPIResponse(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400, immutable", "ETag": etag},
        )

    @app.post("/api/packs/{package_idx}/downloads", status_code=204)
    async def record_download(package_idx: str) -> Response:
        if store.get_pack(package_idx) is None:
            raise HTTPException(404, "pack not found")
        store.record_download(package_idx, "web")
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    async def home(q: str = "", page: int = 1) -> str:
        if page < 1:
            raise HTTPException(400, "page must be positive")
        search_page = SearchPage([], page, 0, 0)
        packs: list[dict[str, object]]
        if q.strip():
            async with httpx.AsyncClient() as client:
                package_idx = _package_idx_from_query(q)
                if package_idx:
                    fetched = await fetch_pack(package_idx, client)
                    store.sync_pack(fetched)
                    packs = [
                        {
                            "package_idx": fetched.package_idx,
                            "title": fetched.title,
                            "author": fetched.author,
                            "cover_url": fetched.cover_url,
                        }
                    ]
                    search_page = SearchPage(packs, 1, 1, 1)
                else:
                    try:
                        search_page = await cached_search(q, page)
                        packs = search_page.items
                    except httpx.HTTPStatusError:
                        packs = []
        else:
            packs = store.search_packs()
        ranking = store.ranking(7, 10)
        recent = store.recent_downloads(10)

        def pagination() -> str:
            if search_page.page_count <= 1:
                return ""
            links = []
            if search_page.page > 1:
                prev = urlencode({"q": q, "page": search_page.page - 1})
                links.append(f'<a rel=prev href="/?{html.escape(prev)}">&larr; 이전</a>')
            links.append(
                f"<span>{search_page.page} / {search_page.page_count}"
                f" <small>({search_page.total}개)</small></span>"
            )
            if search_page.page < search_page.page_count:
                next_ = urlencode({"q": q, "page": search_page.page + 1})
                links.append(f'<a rel=next href="/?{html.escape(next_)}">다음 &rarr;</a>')
            return (
                '<nav class=pagination aria-label="검색 결과 페이지">' + "".join(links) + "</nav>"
            )

        def cards(items: list[dict[str, object]], metric: str = "") -> str:
            if not items:
                return "<p class=muted>아직 데이터가 없습니다.</p>"
            return "".join(
                f'<a class=card href="/packs/{html.escape(str(x["package_idx"]))}">'
                f'<img src="/media/dccon-cover?url={quote(str(x["cover_url"]), safe="")}" loading=lazy>'
                f"<span><b>{html.escape(str(x['title']))}</b><small>"
                f"{html.escape(str(x['author']))}"
                + (f" · {x.get(metric, 0)}회" if metric else "")
                + "</small></span></a>"
                for x in items
            )

        return _page(
            "StickerGen",
            f"""<header class=hero><span class=eyebrow>STICKERGEN</span>
            <h1>StickerGen</h1>
            <p>함께 만드는 Signal 스티커 이모지 데이터베이스.</p></header>
            <form class=search><input name=q value="{html.escape(q)}"
              placeholder="디시콘 이름, 제작자, package ID" aria-label="디시콘 검색"><button>SEARCH</button></form>
            <div class=tabs role=tablist aria-label="디시콘 목록">
              <button type=button role=tab aria-selected=true aria-controls=search-panel id=search-tab>검색 결과</button>
              <button type=button role=tab aria-selected=false aria-controls=ranking-panel id=ranking-tab>주간 랭킹</button>
              <button type=button role=tab aria-selected=false aria-controls=recent-panel id=recent-tab>최근 다운로드</button>
            </div>
            <section class=tab-panel id=search-panel role=tabpanel aria-labelledby=search-tab>
              <div class=section-head><h2>검색 결과</h2><span>SEARCH</span></div><div class=grid>{cards(packs)}</div>{pagination()}</section>
            <section class=tab-panel id=ranking-panel role=tabpanel aria-labelledby=ranking-tab hidden>
              <div class=section-head><h2>주간 랭킹</h2><span>7 DAYS</span></div><div class=grid>{cards(ranking, "downloads")}</div></section>
            <section class=tab-panel id=recent-panel role=tabpanel aria-labelledby=recent-tab hidden>
              <div class=section-head><h2>최근 다운로드</h2><span>RECENT</span></div><div class=grid>{cards(recent)}</div></section>
            <script>const tabs=[...document.querySelectorAll('[role="tab"]')];
            tabs.forEach((tab,index)=>{{tab.onclick=()=>selectTab(tab);tab.onkeydown=e=>{{
            if(e.key==='ArrowRight'||e.key==='ArrowLeft'){{e.preventDefault();let step=e.key==='ArrowRight'?1:-1;
            let next=tabs[(index+step+tabs.length)%tabs.length];selectTab(next);next.focus();}}}}}});
            function selectTab(active){{tabs.forEach(tab=>{{let selected=tab===active;tab.setAttribute('aria-selected',selected);
            document.getElementById(tab.getAttribute('aria-controls')).hidden=!selected;}});}}</script>""",
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
            <div class=sticker-title>#{s["sort"]} {html.escape(str(s["title"]))}</div>
            <div class=emoji-form><button type=button class=emoji-choice data-sticker="{html.escape(str(s["sticker_idx"]))}"
              data-current="{html.escape(str(s.get("emoji") or "😀"))}"
              onclick="openPicker(this)">{html.escape(str(s.get("emoji") or "😀"))}</button>
              <button type=button class=vote-button onclick="submitEmoji(this.previousElementSibling,this)">투표</button></div></article>"""
            for s in stickers
            if isinstance(s, dict)
        )
        return _page(
            str(pack["title"]),
            f"""<a class=back href=/>&larr; BACK</a><header class=pack-head>
            <span class=eyebrow>PACKAGE {html.escape(package_idx)}</span>
            <h1>{html.escape(str(pack["title"]))}</h1>
            <p>BY {html.escape(str(pack["author"]))}</p>
            <div class=pack-actions><button type=button id=generate-button onclick="generateSignalPack()">SIGNAL 팩 생성</button>
              <div id=generate-result aria-live=polite></div></div></header>
            <div class=stickers>{cells}</div>
            <dialog id=emoji-picker><header><b>이모지 선택</b><button type=button onclick="picker.close()" aria-label="닫기">&times;</button></header>
              <input id=emoji-search placeholder="이모지 이름 검색" autocomplete=off>
              <div class=emoji-categories role=tablist aria-label="이모지 분류">
                <button type=button data-category=all class=active>전체</button>
                {"".join(f'<button type=button data-category="{key}">{label}</button>' for key, label in EMOJI_CATEGORIES.items())}
              </div>
              <div id=emoji-grid aria-live=polite><span class=muted>불러오는 중…</span></div>
              <footer><button type=button class=picker-arrow onclick="changeEmojiPage(-1)" aria-label="이전 페이지">&larr;</button>
                <span id=emoji-page>1 / 1</span>
                <button type=button class=picker-arrow onclick="changeEmojiPage(1)" aria-label="다음 페이지">&rarr;</button></footer></dialog>
            <script>const picker=document.getElementById('emoji-picker'),emojiGrid=document.getElementById('emoji-grid'),
            emojiSearch=document.getElementById('emoji-search'),pageLabel=document.getElementById('emoji-page');
            let targetButton,emojiData=[],filtered=[],emojiPage=0,selectedEmoji='',touchX=0,activeCategory='all';
            const pageSize=()=>matchMedia('(max-width:480px)').matches?35:40;
            async function openPicker(button){{targetButton=button;selectedEmoji=button.dataset.current||'';emojiPage=0;
            emojiSearch.value='';activeCategory='all';document.querySelectorAll('[data-category]').forEach(item=>item.classList.toggle('active',item.dataset.category==='all'));
            picker.showModal();emojiSearch.focus();if(!emojiData.length)emojiData=await (await fetch('/api/emojis')).json();filterEmojis();}}
            function renderEmojis(){{let size=pageSize(),pages=Math.max(1,Math.ceil(filtered.length/size));emojiPage=Math.max(0,Math.min(emojiPage,pages-1));
            emojiGrid.innerHTML=filtered.slice(emojiPage*size,(emojiPage+1)*size).map(item=>
            `<button type=button title="${{item.name}}" data-emoji="${{item.emoji}}" class="${{item.emoji===selectedEmoji?'selected':''}}">${{item.emoji}}</button>`).join('');
            pageLabel.textContent=`${{emojiPage+1}} / ${{pages}}`;}}
            function changeEmojiPage(step){{emojiPage+=step;renderEmojis();}}
            function filterEmojis(){{let q=emojiSearch.value.trim().toLowerCase();filtered=emojiData.filter(item=>
            (activeCategory==='all'||item.category===activeCategory)&&(!q||item.name.toLowerCase().includes(q)));emojiPage=0;renderEmojis();}}
            emojiSearch.oninput=filterEmojis;document.querySelector('.emoji-categories').onclick=event=>{{let button=event.target.closest('[data-category]');if(!button)return;
            activeCategory=button.dataset.category;document.querySelectorAll('[data-category]').forEach(item=>item.classList.toggle('active',item===button));filterEmojis();}};
            emojiGrid.onclick=event=>{{let button=event.target.closest('[data-emoji]');if(!button)return;
            selectedEmoji=button.dataset.emoji;targetButton.textContent=selectedEmoji;targetButton.dataset.current=selectedEmoji;picker.close();}};
            async function submitEmoji(field,submitButton){{let chosen=field.dataset.current;if(!chosen){{openPicker(field);return;}}
            let response=await fetch('/api/stickers/'+field.dataset.sticker+'/emoji',{{method:'PUT',headers:{{'content-type':'application/json'}},body:JSON.stringify({{emoji:chosen}})}});
            if(!response.ok){{alert(await response.text());return;}}submitButton.textContent='완료';setTimeout(()=>submitButton.textContent='투표',1200);}}
            emojiGrid.ontouchstart=event=>{{touchX=event.changedTouches[0].screenX;}};
            emojiGrid.ontouchend=event=>{{let delta=event.changedTouches[0].screenX-touchX;if(Math.abs(delta)>45)changeEmojiPage(delta<0?1:-1);}};
            picker.onclick=event=>{{if(event.target===picker)picker.close();}};
            async function generateSignalPack(){{let button=document.getElementById('generate-button'),result=document.getElementById('generate-result');
            button.disabled=true;button.textContent='생성 중…';result.textContent='이미지 변환과 업로드에는 몇 분이 걸릴 수 있습니다.';
            try{{let response=await fetch('/api/packs/{html.escape(package_idx)}/generate',{{method:'POST'}}),data=await response.json();
            if(!response.ok)throw new Error(data.detail||'생성하지 못했습니다.');result.innerHTML=`<a href="${{data.install_url}}" target="_blank" rel="noopener">Signal에 스티커 팩 추가 &rarr;</a>`;
            button.textContent='링크 생성 완료';}}catch(error){{result.textContent=error.message;button.textContent='다시 시도';button.disabled=false;}}}}</script>""",
        )

    return app


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width">
    <title>{html.escape(title)}</title><style>
    @font-face{{font-family:"StickerGen Emoji";font-style:normal;font-weight:400;font-display:block;src:url("https://fonts.gstatic.com/s/notocoloremoji/v39/Yq6P-KqIXTD0t4D9z1ESnKM3-HpFab4.ttf") format("truetype")}}
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
    button:hover{{opacity:.72}}.tabs{{display:flex;overflow-x:auto;border-bottom:1px solid var(--fg);scrollbar-width:none}}
    .tabs::-webkit-scrollbar{{display:none}}.tabs button{{flex:0 0 auto;padding:18px 24px;background:transparent;color:var(--muted);border-bottom:3px solid transparent;letter-spacing:0}}
    .tabs button[aria-selected=true]{{color:var(--fg);border-color:var(--fg)}}.tabs button:focus-visible{{outline:2px solid var(--fg);outline-offset:-4px}}
    .tab-panel{{margin-top:32px}}.tab-panel[hidden]{{display:none}}
    .pagination{{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px;margin-top:28px;padding-top:20px;border-top:1px solid var(--line)}}
    .pagination a{{padding:12px 0;font-size:.78rem;font-weight:700;letter-spacing:.08em}}.pagination a:last-child{{text-align:right}}
    .pagination span{{grid-column:2;text-align:center;font-variant-numeric:tabular-nums}}.pagination small{{display:inline}}
    .section-head{{display:flex;align-items:baseline;justify-content:space-between;padding-bottom:14px;border-bottom:1px solid var(--fg)}}
    .section-head h2{{margin:0;font-size:1rem;font-weight:600}}.section-head span,small,.muted{{color:var(--muted)}}
    .grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}}
    .card{{display:grid;grid-template-columns:76px 1fr;gap:18px;align-items:center;min-height:108px;padding:16px 0;border-bottom:1px solid var(--line)}}
    .card:nth-child(odd){{padding-right:24px;border-right:1px solid var(--line)}}.card:nth-child(even){{padding-left:24px}}
    .card img{{width:76px;height:76px;object-fit:contain;transition:transform .18s}}
    .card:hover img{{transform:scale(1.04)}}.card b{{font-size:1rem;font-weight:600}}small{{display:block;margin-top:6px}}
    .back{{display:inline-block;margin:12px 0 80px}}.pack-head{{padding-bottom:56px;border-bottom:1px solid var(--fg)}}
    .pack-head h1{{font-size:clamp(3rem,7vw,6rem)}}
    .pack-actions{{display:flex;align-items:center;gap:18px;margin-top:28px}}.pack-actions>button{{min-height:48px}}
    #generate-result{{font-size:.82rem;color:var(--muted)}}#generate-result a{{color:var(--fg);font-weight:700;text-decoration:underline;text-underline-offset:4px}}
    .stickers{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));border-left:1px solid var(--line)}}
    .sticker{{padding:14px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}}
    .sticker>img{{width:100%;aspect-ratio:1;object-fit:contain;background:var(--soft)}}
    .sticker-title{{height:40px;padding-top:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:.78rem}}
    .emoji-form{{display:grid;grid-template-columns:1fr auto;border:1px solid var(--fg)}}
    .emoji-choice{{min-width:0;min-height:42px;padding:8px 12px;border:0;background:transparent;color:var(--fg);font-family:"StickerGen Emoji","Noto Color Emoji","Apple Color Emoji","Segoe UI Emoji",sans-serif;font-size:1.25rem;letter-spacing:0;text-align:left}}
    .vote-button{{padding:0 14px;border-left:1px solid var(--fg)}}
    dialog{{width:min(430px,calc(100vw - 28px));padding:0;background:var(--bg);color:var(--fg);border:1px solid var(--fg)}}
    dialog::backdrop{{background:rgba(0,0,0,.68)}}dialog header{{display:flex;align-items:center;justify-content:space-between;padding:16px 18px;border-bottom:1px solid var(--line)}}
    dialog header button{{width:38px;height:38px;padding:0;background:transparent;color:var(--fg);font-size:1.6rem;letter-spacing:0}}
    #emoji-search{{width:calc(100% - 32px);margin:14px 16px 10px;padding:11px;background:transparent;border:1px solid var(--line);outline:0}}
    .emoji-categories{{display:flex;gap:5px;overflow-x:auto;padding:0 16px 10px;scrollbar-width:none}}.emoji-categories::-webkit-scrollbar{{display:none}}
    .emoji-categories button{{flex:0 0 auto;padding:7px 10px;background:transparent;color:var(--muted);border:1px solid var(--line);font-size:.68rem;letter-spacing:0}}
    .emoji-categories button.active{{background:var(--fg);color:var(--invert);border-color:var(--fg)}}
    #emoji-search:focus{{border-color:var(--fg)}}#emoji-grid{{display:grid;grid-template-columns:repeat(8,1fr);grid-template-rows:repeat(5,1fr);gap:1px;min-height:250px;padding:0 14px;touch-action:pan-y}}
    #emoji-grid>button{{aspect-ratio:1;padding:0;background:transparent;color:inherit;font-family:"StickerGen Emoji","Noto Color Emoji","Apple Color Emoji","Segoe UI Emoji",sans-serif;font-size:1.65rem;letter-spacing:0;border:1px solid transparent}}
    #emoji-grid>button:hover,#emoji-grid>button:focus-visible,#emoji-grid>button.selected{{opacity:1;border-color:var(--fg);background:var(--soft)}}
    dialog footer{{display:grid;grid-template-columns:48px 1fr 48px;align-items:stretch;margin-top:10px;border-top:1px solid var(--line)}}
    dialog footer button{{min-height:46px;padding:0}}dialog footer .picker-arrow{{background:transparent;color:var(--fg);font-size:1rem}}
    #emoji-page{{display:grid;place-items:center;color:var(--muted);font-size:.75rem;font-variant-numeric:tabular-nums}}
    @media(prefers-color-scheme:dark){{:root{{--bg:#090909;--fg:#f5f5f5;--muted:#999;--line:#303030;--soft:#171717;--invert:#090909}}}}
    @media(max-width:760px){{body{{padding:16px 14px 48px}}.hero{{padding:7vh 0 40px}}.hero h1{{font-size:clamp(2.8rem,14vw,5rem)}}
    .search{{margin:20px 0 36px}}.search input{{padding:16px 14px}}.search button{{padding:0 16px}}
    .tabs{{margin:0 -14px;padding:0 14px}}.tabs button{{padding:15px 18px}}.tab-panel{{margin-top:24px}}
    .pagination{{position:sticky;bottom:0;margin:20px -14px -16px;padding:12px 14px;background:var(--bg);border-top:1px solid var(--fg)}}
    .grid{{grid-template-columns:1fr}}.card:nth-child(n){{grid-template-columns:68px 1fr;min-height:96px;padding:13px 0;border-right:0}}.card img{{width:68px;height:68px}}
    .stickers{{grid-template-columns:repeat(2,minmax(0,1fr))}}.pack-head h1{{font-size:clamp(2.8rem,14vw,5rem)}}}}
    @media(max-width:600px){{.pack-actions{{align-items:stretch;flex-direction:column}}.pack-actions>button{{width:100%}}}}
    @media(max-width:480px){{dialog{{width:100%;max-width:none;margin:auto 0 0;border-width:1px 0 0}}#emoji-grid{{grid-template-columns:repeat(7,1fr);grid-template-rows:repeat(5,1fr);min-height:245px;padding:0 10px}}}}
    </style><body>{body}</body></html>"""


app = create_app()
