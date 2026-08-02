from dccon2signal.catalog import Catalog
from dccon2signal.models import DcconPack, DcconSticker


def _pack() -> DcconPack:
    pack = DcconPack("10", "pack", "author", "desc", "cover")
    pack.stickers = [DcconSticker("100", 1, "one", "png", "image-1")]
    return pack


def test_community_vote_winner_and_mapping_fingerprint(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    pack = _pack()
    catalog.sync_pack(pack)
    catalog.set_vote("100", "alice", "🐱")
    catalog.set_vote("100", "bob", "🐱")
    catalog.set_vote("100", "carol", "🐶")

    mapping = catalog.emoji_map("10")
    assert mapping == {"1": "🐱"}
    before = catalog.mapping_fingerprint(pack, mapping, remove_bg=False, static_only=False)

    catalog.set_vote("100", "alice", "🐶")
    after_mapping = catalog.emoji_map("10")
    after = catalog.mapping_fingerprint(pack, after_mapping, remove_bg=False, static_only=False)
    assert after_mapping == {"1": "🐶"}
    assert before != after


def test_link_cache_rankings_and_recent_downloads(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.sync_pack(_pack())
    catalog.save_link("10", "fingerprint", "pack-id", "pack-key")

    cached = catalog.get_cached_link("10", "fingerprint")
    assert cached is not None
    assert cached.pack_id == "pack-id"
    assert catalog.get_cached_link("10", "different") is None

    catalog.record_download("10", "test")
    catalog.record_download("10", "test")
    assert catalog.ranking()[0]["downloads"] == 2
    assert catalog.recent_downloads()[0]["package_idx"] == "10"


def test_search_and_pack_detail(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite3")
    catalog.sync_pack(_pack())
    assert catalog.search_packs("author")[0]["package_idx"] == "10"
    detail = catalog.get_pack("10")
    assert detail is not None
    assert detail["stickers"][0]["sticker_idx"] == "100"
    assert detail["stickers"][0]["emoji"] == "😀"
