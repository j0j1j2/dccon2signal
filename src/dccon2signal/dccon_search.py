from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import quote

import httpx


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, object]] = []
        self._item: dict[str, object] | None = None
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "li" and "div_package" in classes and values.get("package_idx"):
            self._item = {"package_idx": values["package_idx"]}
        elif self._item is not None and tag == "img" and "thumb_img" in classes:
            self._item["cover_url"] = values.get("src", "")
        elif self._item is not None and tag in {"strong", "span"}:
            if "dcon_name" in classes:
                self._field = "title"
            elif "dcon_seller" in classes:
                self._field = "author"

    def handle_data(self, data: str) -> None:
        if self._item is not None and self._field:
            self._item[self._field] = str(self._item.get(self._field, "")) + data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag in {"strong", "span"}:
            self._field = None
        elif tag == "li" and self._item is not None:
            if self._item.get("title"):
                self.results.append(self._item)
            self._item = None


async def search_dccon(query: str, client: httpx.AsyncClient) -> list[dict[str, object]]:
    """Search DCInside's public DCCon catalogue by title."""
    if not query.strip():
        return []
    url = f"https://dccon.dcinside.com/new/1/title/{quote(query.strip(), safe='')}"
    response = await client.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (dccon2signal)"},
        timeout=15.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    parser = _SearchParser()
    parser.feed(response.text)
    return parser.results
