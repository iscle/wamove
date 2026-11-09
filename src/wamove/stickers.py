"""Saved stickers: what Android has, what the export carries, and the input for the companion fallback.

WhatsApp keeps saved stickers outside the chats: `starred_stickers` (favorites) and `recent_stickers` in
`databases/stickers.db`, with each sticker's file in `files/Stickers/<base64 sha256>.webp` (a `/` in the hash
becomes `-` in the file name). Its exporter carries them as HistorySync `recentStickers` (11) and
`favoriteStickers` (22) only when a server A/B property is on; see `android.xpm.STICKER_EXPORT_PROP`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .android.xpm import history_sync_field_counts

RECENT_FIELD = 11
FAVORITE_FIELD = 22
DEFAULT_SIDE = 512


@dataclass
class Saved:
    favorites: int
    recents: int


def saved_on_android(store: Path) -> Saved | None:
    """Counts from the copied `stickers.db`, or None when it was not copied."""
    db = store / "stickers.db"
    if not db.is_file():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def count(table: str) -> int:
            return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if table in tables else 0
        return Saved(count("starred_stickers"), count("recent_stickers"))
    finally:
        conn.close()


def carried_by_export(messages_zip: Path) -> Saved:
    with zipfile.ZipFile(messages_zip) as archive:
        name = json.loads(archive.read("header.json")).get("messages", {}).get("filename", "messages.bin") \
            if "header.json" in archive.namelist() else "messages.bin"
        with archive.open(name) as stream:
            counts = history_sync_field_counts(stream)
    return Saved(counts.get(FAVORITE_FIELD, 0), counts.get(RECENT_FIELD, 0))


def sticker_file(files: Path, file_hash: str) -> Path | None:
    for name in (file_hash, file_hash.replace("/", "-"), file_hash.replace("/", "_")):
        path = files / f"{name}.webp"
        if path.is_file():
            return path
    return None


def favorites(store: Path) -> tuple[list[dict], list[str]]:
    """Every favorite with a sticker file, oldest first, as the companion tool takes it; plus the hashes of
    favorites whose file is missing."""
    conn = sqlite3.connect(f"file:{store / 'stickers.db'}?mode=ro", uri=True)
    try:
        present = {row[1] for row in conn.execute("PRAGMA table_info(starred_stickers)")}
        if not present:
            return [], []
        wanted = ("plaintext_hash", "timestamp", "mimetype", "width", "height", "hash_of_image_part", "is_lottie",
                  "is_avatar")
        # Older versions of WhatsApp lack some of these columns.
        columns = ", ".join(c if c in present else f"NULL AS {c}" for c in wanted)
        rows = conn.execute(f"SELECT {columns} FROM starred_stickers ORDER BY timestamp").fetchall()
    finally:
        conn.close()
    found, missing, seen = [], [], set()
    for file_hash, timestamp, mimetype, width, height, image_hash, lottie, avatar in rows:
        if file_hash in seen:  # one mutation per sticker
            continue
        seen.add(file_hash)
        path = sticker_file(store / "files", file_hash or "")
        if path is None:
            missing.append(file_hash)
            continue
        data = path.read_bytes()
        found.append({
            "file": str(path.resolve()),
            # The index carries the hash of the file that is uploaded, so the two always agree.
            "hash": base64.b64encode(hashlib.sha256(data).digest()).decode(),
            "timestamp_ms": int(timestamp or 0),
            "mimetype": mimetype or "image/webp",
            "width": width or DEFAULT_SIDE,
            "height": height or DEFAULT_SIDE,
            "image_hash": image_hash or "",
            "lottie": bool(lottie),
            "avatar": bool(avatar),
        })
    return found, missing


def write_favorites(store: Path, target: Path) -> tuple[int, list[str]]:
    found, missing = favorites(store)
    target.write_text(json.dumps(found, indent=1))
    return len(found), missing
