import json

import httpx

from dccon2signal.models import DcconPack, DcconSticker, ImageExt

API_URL = "https://dccon.dcinside.com/index/package_detail"
IMG_BASE = "https://dcimg5.dcinside.com/dccon.php?no="


class ScraperError(Exception):
    """Raised when the DCcon package cannot be retrieved or parsed."""


async def fetch_pack(package_idx: str, client: httpx.AsyncClient) -> DcconPack:
    body = {"package_idx": package_idx, "code": "", "inspection_state": ""}
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://dccon.dcinside.com/",
        "User-Agent": "Mozilla/5.0 (dccon2signal)",
    }
    resp = await client.post(API_URL, data=body, headers=headers, timeout=15.0)
    resp.raise_for_status()

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise ScraperError(f"Non-JSON response from dccon API: {resp.text[:200]!r}") from e

    info = data.get("info")
    detail = data.get("detail") or []
    if not isinstance(info, dict) or not info.get("package_idx"):
        raise ScraperError(f"Package {package_idx} not found or private")

    pack = DcconPack(
        package_idx=str(info["package_idx"]),
        title=str(info.get("title", "")),
        author=str(info.get("seller_name", "")),
        description=str(info.get("description", "")),
        cover_url=IMG_BASE + str(info["main_img_path"]),
    )
    for entry in detail:
        ext = str(entry["ext"]).lower()
        if ext not in ("png", "gif"):
            continue
        ext_typed: ImageExt = ext  # type: ignore[assignment]
        pack.stickers.append(
            DcconSticker(
                idx=str(entry["idx"]),
                sort=int(entry["sort"]),
                title=str(entry.get("title", "")),
                ext=ext_typed,
                image_url=IMG_BASE + str(entry["path"]),
            )
        )
    pack.stickers.sort(key=lambda s: s.sort)
    pack.tags = [str(t["tag"]) for t in data.get("tags", []) if t.get("tag")]
    return pack
