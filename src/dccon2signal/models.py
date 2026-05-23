from dataclasses import dataclass, field
from typing import Literal

ImageExt = Literal["png", "gif"]
ProcessedExt = Literal["png", "apng"]


@dataclass
class DcconSticker:
    idx: str
    sort: int
    title: str
    ext: ImageExt
    image_url: str
    image_bytes: bytes | None = None
    processed_bytes: bytes | None = None
    processed_ext: ProcessedExt | None = None
    emoji: str = "😀"


@dataclass
class DcconPack:
    package_idx: str
    title: str
    author: str
    description: str
    cover_url: str
    cover_bytes: bytes | None = None
    cover_processed: bytes | None = None
    stickers: list[DcconSticker] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class SignalAuth:
    username: str
    password: str
