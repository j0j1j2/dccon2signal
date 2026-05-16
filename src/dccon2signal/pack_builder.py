from signalstickers_client.models import LocalStickerPack, Sticker

from dccon2signal.models import DcconPack

SIGNAL_MAX_STICKERS = 200


class PackBuilderError(Exception):
    """Raised when a DcconPack cannot be converted to a Signal sticker pack."""


def build(pack: DcconPack, emoji_map: dict[str, str] | None = None) -> LocalStickerPack:
    processed = [s for s in pack.stickers if s.processed_bytes is not None]
    if not processed:
        raise PackBuilderError("Sticker list is empty after processing")
    if len(processed) > SIGNAL_MAX_STICKERS:
        raise PackBuilderError(
            f"Signal allows up to 200 stickers; got {len(processed)}"
        )
    if pack.cover_processed is None:
        raise PackBuilderError("Pack has no processed cover image")

    signal_pack = LocalStickerPack()
    signal_pack.title = pack.title
    signal_pack.author = pack.author

    cover = Sticker()
    cover.id = 0
    cover.image_data = pack.cover_processed
    signal_pack.cover = cover

    for idx, s in enumerate(processed):
        sticker = Sticker()
        sticker.id = idx
        sticker.emoji = (emoji_map or {}).get(str(s.sort), s.emoji)
        sticker.image_data = s.processed_bytes
        signal_pack._addsticker(sticker)

    return signal_pack
