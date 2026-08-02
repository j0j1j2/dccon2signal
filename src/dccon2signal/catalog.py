from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dccon2signal.models import DcconPack


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class CachedLink:
    pack_id: str
    pack_key: str


class Catalog:
    """Shared catalogue for community mappings, statistics, and Signal links."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def _init_schema(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS packs (
                    package_idx TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    description TEXT NOT NULL,
                    cover_url TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stickers (
                    sticker_idx TEXT PRIMARY KEY,
                    package_idx TEXT NOT NULL REFERENCES packs(package_idx) ON DELETE CASCADE,
                    sort INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    image_url TEXT NOT NULL,
                    UNIQUE(package_idx, sort)
                );
                CREATE TABLE IF NOT EXISTS emoji_votes (
                    sticker_idx TEXT NOT NULL REFERENCES stickers(sticker_idx) ON DELETE CASCADE,
                    voter_key TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(sticker_idx, voter_key)
                );
                CREATE TABLE IF NOT EXISTS download_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_idx TEXT NOT NULL REFERENCES packs(package_idx) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS download_events_pack_time
                    ON download_events(package_idx, created_at);
                CREATE TABLE IF NOT EXISTS link_cache (
                    package_idx TEXT NOT NULL REFERENCES packs(package_idx) ON DELETE CASCADE,
                    fingerprint TEXT NOT NULL,
                    pack_id TEXT NOT NULL,
                    pack_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL,
                    PRIMARY KEY(package_idx, fingerprint)
                );
                """
            )

    def sync_pack(self, pack: DcconPack) -> None:
        now = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO packs
                   (package_idx, title, author, description, cover_url, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(package_idx) DO UPDATE SET
                     title=excluded.title, author=excluded.author,
                     description=excluded.description, cover_url=excluded.cover_url,
                     synced_at=excluded.synced_at""",
                (pack.package_idx, pack.title, pack.author, pack.description, pack.cover_url, now),
            )
            current_ids = {s.idx for s in pack.stickers}
            for sticker in pack.stickers:
                db.execute(
                    """INSERT INTO stickers
                       (sticker_idx, package_idx, sort, title, image_url)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(sticker_idx) DO UPDATE SET
                         package_idx=excluded.package_idx, sort=excluded.sort,
                         title=excluded.title, image_url=excluded.image_url""",
                    (sticker.idx, pack.package_idx, sticker.sort, sticker.title, sticker.image_url),
                )
            if current_ids:
                marks = ",".join("?" for _ in current_ids)
                db.execute(
                    f"DELETE FROM stickers WHERE package_idx=? AND sticker_idx NOT IN ({marks})",
                    (pack.package_idx, *current_ids),
                )

    def set_vote(self, sticker_idx: str, voter_key: str, emoji: str) -> None:
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM stickers WHERE sticker_idx=?", (sticker_idx,)
            ).fetchone()
            if exists is None:
                raise KeyError(sticker_idx)
            db.execute(
                """INSERT INTO emoji_votes(sticker_idx, voter_key, emoji, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(sticker_idx, voter_key) DO UPDATE SET
                     emoji=excluded.emoji, updated_at=excluded.updated_at""",
                (sticker_idx, voter_key, emoji, _now()),
            )

    def emoji_map(self, package_idx: str) -> dict[str, str]:
        with self._connect() as db:
            rows = db.execute(
                """WITH ranked AS (
                     SELECT s.sort, v.emoji, COUNT(*) AS votes,
                            ROW_NUMBER() OVER (
                              PARTITION BY s.sticker_idx
                              ORDER BY COUNT(*) DESC, MAX(v.updated_at) DESC, v.emoji
                            ) AS rank
                     FROM stickers s
                     JOIN emoji_votes v ON v.sticker_idx=s.sticker_idx
                     WHERE s.package_idx=?
                     GROUP BY s.sticker_idx, s.sort, v.emoji
                   )
                   SELECT sort, emoji FROM ranked WHERE rank=1""",
                (package_idx,),
            ).fetchall()
        return {str(row["sort"]): str(row["emoji"]) for row in rows}

    def mapping_fingerprint(
        self,
        pack: DcconPack,
        emoji_map: dict[str, str],
        *,
        remove_bg: bool,
        static_only: bool,
    ) -> str:
        payload = {
            "package_idx": pack.package_idx,
            "title": pack.title,
            "author": pack.author,
            "cover_url": pack.cover_url,
            "stickers": [
                [s.idx, s.sort, s.image_url, emoji_map.get(str(s.sort), s.emoji)]
                for s in pack.stickers
            ],
            "remove_bg": remove_bg,
            "static_only": static_only,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def get_cached_link(self, package_idx: str, fingerprint: str) -> CachedLink | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT pack_id, pack_key FROM link_cache
                   WHERE package_idx=? AND fingerprint=?""",
                (package_idx, fingerprint),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE link_cache SET last_used_at=?
                   WHERE package_idx=? AND fingerprint=?""",
                (_now(), package_idx, fingerprint),
            )
        return CachedLink(pack_id=str(row["pack_id"]), pack_key=str(row["pack_key"]))

    def save_link(self, package_idx: str, fingerprint: str, pack_id: str, pack_key: str) -> None:
        now = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO link_cache
                   (package_idx, fingerprint, pack_id, pack_key, created_at, last_used_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(package_idx, fingerprint) DO UPDATE SET
                     pack_id=excluded.pack_id, pack_key=excluded.pack_key,
                     last_used_at=excluded.last_used_at""",
                (package_idx, fingerprint, pack_id, pack_key, now, now),
            )

    def record_download(self, package_idx: str, source: str = "web") -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO download_events(package_idx, source, created_at) VALUES (?, ?, ?)",
                (package_idx, source, _now()),
            )

    def search_packs(self, query: str = "", limit: int = 50) -> list[dict[str, object]]:
        pattern = f"%{query}%"
        with self._connect() as db:
            rows = db.execute(
                """SELECT p.*, COUNT(DISTINCT s.sticker_idx) AS sticker_count,
                          COUNT(DISTINCT d.id) AS downloads
                   FROM packs p
                   LEFT JOIN stickers s ON s.package_idx=p.package_idx
                   LEFT JOIN download_events d ON d.package_idx=p.package_idx
                   WHERE p.title LIKE ? OR p.author LIKE ? OR p.package_idx LIKE ?
                   GROUP BY p.package_idx ORDER BY p.synced_at DESC LIMIT ?""",
                (pattern, pattern, pattern, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_pack(self, package_idx: str) -> dict[str, object] | None:
        with self._connect() as db:
            pack = db.execute("SELECT * FROM packs WHERE package_idx=?", (package_idx,)).fetchone()
            if pack is None:
                return None
            stickers = db.execute(
                """SELECT s.*,
                          COALESCE(
                            (SELECT emoji FROM emoji_votes v
                             WHERE v.sticker_idx=s.sticker_idx
                             GROUP BY emoji
                             ORDER BY COUNT(*) DESC, MAX(updated_at) DESC LIMIT 1),
                            '😀'
                          ) AS emoji
                   FROM stickers s WHERE package_idx=? ORDER BY sort""",
                (package_idx,),
            ).fetchall()
        result = dict(pack)
        result["stickers"] = [dict(row) for row in stickers]
        return result

    def sticker_image_url(self, sticker_idx: str) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT image_url FROM stickers WHERE sticker_idx=?", (sticker_idx,)
            ).fetchone()
        return str(row["image_url"]) if row is not None else None

    def ranking(self, days: int = 7, limit: int = 20) -> list[dict[str, object]]:
        modifier = f"-{max(days, 1)} days"
        with self._connect() as db:
            rows = db.execute(
                """SELECT p.package_idx, p.title, p.author, p.cover_url, COUNT(d.id) AS downloads
                   FROM download_events d JOIN packs p ON p.package_idx=d.package_idx
                   WHERE d.created_at >= datetime('now', ?)
                   GROUP BY p.package_idx
                   ORDER BY downloads DESC, MAX(d.created_at) DESC LIMIT ?""",
                (modifier, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_downloads(self, limit: int = 20) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT p.package_idx, p.title, p.author, p.cover_url,
                          MAX(d.created_at) AS downloaded_at
                   FROM download_events d JOIN packs p ON p.package_idx=d.package_idx
                   GROUP BY p.package_idx ORDER BY downloaded_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
