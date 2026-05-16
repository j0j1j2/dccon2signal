from dccon2signal.models import DcconPack, DcconSticker, SignalAuth


def test_sticker_defaults():
    s = DcconSticker(idx="123", sort=1, title="hi", ext="png", image_url="http://x/y")
    assert s.image_bytes is None
    assert s.processed_bytes is None
    assert s.processed_ext is None
    assert s.emoji == "😀"


def test_pack_holds_stickers():
    pack = DcconPack(
        package_idx="170660",
        title="t",
        author="a",
        description="d",
        cover_url="http://x/c",
    )
    pack.stickers.append(
        DcconSticker(idx="1", sort=1, title="t", ext="png", image_url="http://x/1")
    )
    assert len(pack.stickers) == 1
    assert pack.tags == []


def test_signal_auth_fields():
    auth = SignalAuth(username="+821012345678", password="pw")
    assert auth.username == "+821012345678"
    assert auth.password == "pw"
