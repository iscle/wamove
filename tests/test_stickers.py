from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from wamove import stickers
from wamove.android import xpm as android_xpm
from wamove.cli import main

PREFS = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <boolean name="17288" value="true" />
    <string name="2306">{&quot;1&quot;:{}}</string>
</map>
"""


def test_ab_prop_is_added_replaced_and_removed() -> None:
    on = android_xpm.with_ab_prop(PREFS, "38696", True)
    assert android_xpm.ab_prop(on, "38696") is True
    assert android_xpm.ab_prop(on, "17288") is True
    off = android_xpm.with_ab_prop(on, "38696", False)
    assert android_xpm.ab_prop(off, "38696") is False and off.count('name="38696"') == 1
    gone = android_xpm.with_ab_prop(off, "38696", None)
    assert android_xpm.ab_prop(gone, "38696") is None
    assert ET.fromstring(gone).find("string").text == '{"1":{}}'  # other entries untouched
    assert gone == PREFS


def test_ab_prop_in_an_empty_map() -> None:
    empty = "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n<map />\n"
    assert android_xpm.ab_prop(android_xpm.with_ab_prop(empty, "38696", True), "38696") is True


def _field(number: int, payload: bytes) -> bytes:
    return bytes([number << 3 | 2]) + bytes([len(payload)]) + payload if number < 16 else \
        bytes([(number << 3 | 2) & 0x7F | 0x80, (number << 3 | 2) >> 7]) + bytes([len(payload)]) + payload


def _chunk(*fields: bytes) -> bytes:
    body = b"".join(fields)
    return bytes([len(body)]) + body


def test_sticker_lists_are_counted_in_messages_bin(tmp_path: Path) -> None:
    data = _chunk(b"\x08\x00", _field(2, b"conv")) + _chunk(_field(8, b"g"), _field(11, b"r"), _field(22, b"f1"),
                                                          _field(22, b"f2"))
    assert android_xpm.history_sync_field_counts(data) == {1: 1, 2: 1, 8: 1, 11: 1, 22: 2}
    archive = tmp_path / "messages.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("messages.bin", data)
    assert stickers.carried_by_export(archive) == stickers.Saved(favorites=2, recents=1)


def make_store(store: Path, rows: list[tuple[bytes, int]], *, old_schema: bool = False) -> list[str]:
    (store / "files").mkdir(parents=True)
    conn = sqlite3.connect(store / "stickers.db")
    extra = "" if old_schema else ", hash_of_image_part TEXT, is_lottie INTEGER, is_avatar INTEGER"
    conn.execute(f"CREATE TABLE starred_stickers (plaintext_hash TEXT, timestamp INTEGER, mimetype TEXT, "
                 f"width INTEGER, height INTEGER{extra})")
    conn.execute("CREATE TABLE recent_stickers (plaintext_hash TEXT)")
    hashes = []
    for data, timestamp in rows:
        file_hash = base64.b64encode(hashlib.sha256(data).digest()).decode()
        hashes.append(file_hash)
        conn.execute("INSERT INTO starred_stickers (plaintext_hash, timestamp, mimetype, width, height) "
                     "VALUES (?, ?, 'image/webp', 0, 0)", (file_hash, timestamp))
    conn.commit()
    conn.close()
    return hashes


@pytest.mark.parametrize("old_schema", [False, True])
def test_favorites_for_the_companion_tool(tmp_path: Path, old_schema: bool) -> None:
    store = tmp_path / "stickers"
    # Find a sticker whose hash has a "/" so the file name uses "-".
    slashed = next(bytes([i]) * 7 for i in range(256)
                   if "/" in base64.b64encode(hashlib.sha256(bytes([i]) * 7).digest()).decode())
    hashes = make_store(store, [(b"newer", 2000), (slashed, 1000), (b"no file", 1500)], old_schema=old_schema)
    (store / "files" / f"{hashes[0]}.webp").write_bytes(b"newer")
    (store / "files" / f"{hashes[1].replace('/', '-')}.webp").write_bytes(slashed)
    found, missing = stickers.favorites(store)
    assert [f["hash"] for f in found] == [hashes[1], hashes[0]]  # oldest first
    assert missing == [hashes[2]]
    assert found[0]["width"] == found[0]["height"] == stickers.DEFAULT_SIDE
    assert stickers.saved_on_android(store) == stickers.Saved(favorites=3, recents=0)
    count, _ = stickers.write_favorites(store, tmp_path / "favorites.json")
    assert count == 2 and json.loads((tmp_path / "favorites.json").read_text())[1]["file"].endswith(".webp")


def test_stickers_command_needs_the_copied_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--work", str(tmp_path), "stickers", "--phone", "+34 600 000 000", "--yes"]) == 1
    assert "sticker store was not copied" in capsys.readouterr().err


def test_export_report_warns_when_favorites_are_left_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from wamove.cli import Work, report_stickers
    work = Work(tmp_path)
    make_store(work.export / "stickers", [(b"a", 1)])
    local = work.export / "files" / "sandbox" / "messages"
    local.parent.mkdir(parents=True)
    with zipfile.ZipFile(local, "w") as z:
        z.writestr("messages.bin", _chunk(_field(2, b"conv")))
    item = android_xpm.ExportedFile("/sandbox/messages", "migration/messages_export.zip", 0, True, None)
    report_stickers(work, [item], None)
    out = capsys.readouterr().out
    assert "0 favorites, 0 recents (Android has 1 and 0)" in out and "wamove stickers" in out


def test_truncated_messages_bin_is_an_export_error() -> None:
    with pytest.raises(android_xpm.ExportError):
        android_xpm.history_sync_field_counts(bytes([10]) + b"short")


def test_duplicate_favorites_are_sent_once_and_missing_tables_count_zero(tmp_path: Path) -> None:
    store = tmp_path / "stickers"
    hashes = make_store(store, [(b"same", 1), (b"same", 2)])
    (store / "files" / f"{hashes[0].replace('/', '-')}.webp").write_bytes(b"same")
    found, missing = stickers.favorites(store)
    assert len(found) == 1 and not missing
    conn = sqlite3.connect(store / "stickers.db")
    conn.execute("DROP TABLE recent_stickers")
    conn.commit()
    conn.close()
    assert stickers.saved_on_android(store) == stickers.Saved(favorites=2, recents=0)
