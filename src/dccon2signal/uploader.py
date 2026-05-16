from signalstickers_client import StickersClient
from signalstickers_client.models import LocalStickerPack

from dccon2signal.models import SignalAuth

SIGNAL_INSTALL_URL = "https://signal.art/addstickers/#pack_id={pack_id}&pack_key={pack_key}"


class UploaderError(Exception):
    """Raised when uploading a sticker pack to Signal fails."""


async def upload(pack: LocalStickerPack, auth: SignalAuth) -> tuple[str, str]:
    try:
        async with StickersClient(auth.username, auth.password) as client:
            pack_id, pack_key = await client.upload_pack(pack)
    except Exception as e:
        raise UploaderError(f"Signal upload failed: {e}") from e
    return pack_id, pack_key


def install_url(pack_id: str, pack_key: str) -> str:
    return SIGNAL_INSTALL_URL.format(pack_id=pack_id, pack_key=pack_key)
