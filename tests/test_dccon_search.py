import httpx
import pytest
import respx

from dccon2signal.dccon_search import search_dccon, search_dccon_page


@pytest.mark.asyncio
@respx.mock
async def test_search_dccon_parses_public_catalogue():
    html = """
    <p>검색결과(31건)</p><ul><li class="div_package" package_idx="173689">
      <a><img class="thumb_img" src="https://img/cover">
      <strong class="dcon_name">브더재화콘</strong>
      <span class="dcon_seller">상평통보</span></a>
    </li></ul>
    """
    respx.get("https://dccon.dcinside.com/new/1/title/%EB%B8%8C%EB%8D%94").mock(
        return_value=httpx.Response(200, text=html)
    )
    async with httpx.AsyncClient() as client:
        result = await search_dccon("브더", client)
    assert result == [
        {
            "package_idx": "173689",
            "cover_url": "https://img/cover",
            "title": "브더재화콘",
            "author": "상평통보",
        }
    ]


@pytest.mark.asyncio
@respx.mock
async def test_search_dccon_page_uses_requested_page_and_reports_total():
    route = respx.get("https://dccon.dcinside.com/new/2/title/cat").mock(
        return_value=httpx.Response(200, text="<p>검색결과(31건)</p>")
    )
    async with httpx.AsyncClient() as client:
        result = await search_dccon_page("cat", client, 2)
    assert route.called
    assert result.page == 2
    assert result.total == 31
    assert result.page_count == 3
