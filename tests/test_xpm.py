from __future__ import annotations

import base64
import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest
from pyiosbackup import Backup as ReferenceBackup

from fixtures import mark_complete, PASSWORD, UDID, make_backup, make_chatstorage
from wamove.android import xpm as android_xpm
from wamove.cli import main
from wamove.ios import xpm as ios_xpm
from wamove.ios.backup import CHATSTORAGE, WA_DOMAIN, Backup

SEED = os.urandom(32)
KEY = {"owner": "34600000000@s.whatsapp.net", "version": "1", "account_hash": base64.b64encode(b"h" * 32).decode(),
       "server_salt": base64.b64encode(b"s" * 32).decode(), "seed": base64.b64encode(SEED).decode()}
DATA = "/data/user/0/com.whatsapp/files/migration/export/sandbox"
MEDIA = "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/Media/WhatsApp Images/IMG-1.jpg"


def make_export(export: Path, mirror: Path) -> dict[str, bytes]:
    """A small export laid out like WhatsApp's: sandbox files, the file list with IVs, and the key in prefs."""
    ivs = {"migration/messages_export.zip": os.urandom(16), "Media/12/34/56-IMG-1.jpg": os.urandom(16)}
    plain: dict[str, bytes] = {}
    messages = export / "files" / DATA.lstrip("/") / "sandbox123messages"
    messages.parent.mkdir(parents=True)
    with zipfile.ZipFile(messages, "w") as archive:
        archive.writestr("header.json", json.dumps({"header": {"os": "android"}, "messages": {
            "filename": "messages.bin", "format": "protobuf", "chunks": [{"chunk_number": 0, "messages_count": 3}]}}))
        archive.writestr("messages.bin", b"\x03abc")
    plain["migration/messages_export.zip"] = messages.read_bytes()
    image = mirror / "Media" / "WhatsApp Images" / "IMG-1.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(os.urandom(5000))
    plain["Media/12/34/56-IMG-1.jpg"] = image.read_bytes()
    enc = export / "files" / DATA.lstrip("/") / "sandbox456enc-metadata"
    with zipfile.ZipFile(enc, "w") as archive:
        archive.writestr("metadata.json", json.dumps({
            "scheme": "AES-GCM-v1", "key_id": {"version": 1, "account_hash": KEY["account_hash"],
                                                "server_salt": KEY["server_salt"]},
            "files": [{"path": p, "iv": base64.b64encode(iv).decode()} for p, iv in ivs.items()]}))
        archive.writestr("attempt.json", "{}")
    plain["migration/enc.zip"] = enc.read_bytes()
    conn = sqlite3.connect(export / "migration_export_metadata.db")
    conn.execute("CREATE TABLE exported_files_metadata (local_path TEXT, exported_path TEXT, file_size INTEGER, "
                 "required INTEGER, encryption_iv TEXT, sort_id INTEGER)")
    rows = [(f"{DATA}/sandbox123messages", "migration/messages_export.zip", 1),
            (MEDIA, "Media/12/34/56-IMG-1.jpg", 0), (f"{DATA}/sandbox456enc-metadata", "migration/enc.zip", 1)]
    for sort, (local, exported, required) in enumerate(rows):
        iv = ivs.get(exported)
        conn.execute("INSERT INTO exported_files_metadata VALUES (?,?,?,?,?,?)",
                     (local, exported, len(plain[exported]), required,
                      base64.b64encode(iv).decode() if iv else "", sort))
    conn.commit()
    conn.close()
    prefs = export / "shared_prefs"
    prefs.mkdir()
    entries = "".join(f'<string name="/export/enc/active/{k}">{v}</string>' for k, v in KEY.items())
    (prefs / "com.whatsapp_preferences_light.xml").write_text(f"<?xml version='1.0'?><map>{entries}</map>")
    (export / android_xpm.COLLECTED).write_text("{}")
    return plain


def test_gcm_matches_java_layout() -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv, data = os.urandom(16), os.urandom(3000)
    source = Path(os.environ.get("TMPDIR", "/tmp")) / "wamove-gcm-test"
    source.write_bytes(data)
    target = source.with_suffix(".enc")
    ios_xpm.encrypt(source, target, SEED, iv)
    # Java's AES/GCM/NoPadding output is ciphertext followed by the 16-byte tag, as AESGCM produces it.
    assert target.read_bytes() == AESGCM(SEED).encrypt(iv, data, None)
    assert ios_xpm.decrypt(target, SEED, iv) == data


def test_find_key(tmp_path: Path) -> None:
    make_export(tmp_path / "export", tmp_path / "WhatsApp")
    assert android_xpm.find_key(tmp_path / "export" / "shared_prefs").__dict__ == KEY


def test_plant_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: D103
    work = tmp_path / "work"
    plain = make_export(work / "android" / "export", work / "android" / "WhatsApp")
    make_chatstorage(tmp_path / CHATSTORAGE)
    make_backup(work / "ios" / "pristine" / UDID, tmp_path / CHATSTORAGE, PASSWORD)
    mark_complete(work / "ios" / "pristine" / UDID)
    with Backup(work / "ios" / "pristine" / UDID, PASSWORD) as pristine:
        pristine.prune((WA_DOMAIN, ios_xpm.APP_DOMAIN))
    monkeypatch.setenv("WAMOVE_BACKUP_PASSWORD", PASSWORD)
    assert main(["--work", str(work), "plant"]) == 0
    reference = ReferenceBackup.from_path(work / "ios" / "patched" / UDID, PASSWORD)
    files = android_xpm.exported_files(work / "android" / "export" / "migration_export_metadata.db")
    for item in files:
        stored = reference.get_entry_by_domain_and_path(ios_xpm.APP_DOMAIN, f"Library/xpm-import/{item.exported_path}")
        data = stored.read_bytes()
        if item.iv:
            target = tmp_path / "check"
            target.write_bytes(data)
            assert ios_xpm.decrypt(target, SEED, base64.b64decode(item.iv)) == plain[item.exported_path]
        else:
            assert data == plain[item.exported_path]
    assert reference.get_entry_by_domain_and_path(WA_DOMAIN, CHATSTORAGE).read_bytes()


def test_restore_refuses_incomplete_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy without its .complete marker (e.g. a build that failed halfway) must never be restored."""
    work = tmp_path / "work"
    make_chatstorage(tmp_path / CHATSTORAGE)
    make_backup(work / "ios" / "pristine" / UDID, tmp_path / CHATSTORAGE, PASSWORD)
    mark_complete(work / "ios" / "pristine" / UDID)
    make_backup(work / "ios" / "patched" / UDID, tmp_path / CHATSTORAGE, PASSWORD)
    monkeypatch.setenv("WAMOVE_BACKUP_PASSWORD", PASSWORD)
    assert main(["--work", str(work), "restore"]) == 1


def test_plant_failure_leaves_no_restorable_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    make_export(work / "android" / "export", work / "android" / "WhatsApp")
    make_chatstorage(tmp_path / CHATSTORAGE)
    make_backup(work / "ios" / "pristine" / UDID, tmp_path / CHATSTORAGE, PASSWORD)
    mark_complete(work / "ios" / "pristine" / UDID)
    with Backup(work / "ios" / "pristine" / UDID, PASSWORD) as pristine:
        pristine.prune((WA_DOMAIN, ios_xpm.APP_DOMAIN))
    monkeypatch.setenv("WAMOVE_BACKUP_PASSWORD", PASSWORD)
    calls = []

    def failing(*args):
        calls.append(args)
        if len(calls) > 1:
            raise ios_xpm.PlantError("disk full")
        return real(*args)

    real = ios_xpm.encrypt
    monkeypatch.setattr(ios_xpm, "encrypt", failing)
    assert main(["--work", str(work), "plant"]) == 1
    assert len(calls) == 2
    assert not (work / "ios" / "patched" / UDID).exists()
    assert not (work / "ios" / "patched" / f"{UDID}.complete").exists()


def test_missing_required_export_file_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    make_export(work / "android" / "export", work / "android" / "WhatsApp")
    next((work / "android" / "export" / "files").rglob("sandbox123messages")).unlink()
    make_chatstorage(tmp_path / CHATSTORAGE)
    make_backup(work / "ios" / "pristine" / UDID, tmp_path / CHATSTORAGE, PASSWORD)
    mark_complete(work / "ios" / "pristine" / UDID)
    monkeypatch.setenv("WAMOVE_BACKUP_PASSWORD", PASSWORD)
    assert main(["--work", str(work), "plant"]) == 1


def test_backup_size_is_read_only(tmp_path: Path) -> None:
    make_chatstorage(tmp_path / CHATSTORAGE)
    folder = make_backup(tmp_path / UDID, tmp_path / CHATSTORAGE, PASSWORD)
    before = (folder / "Manifest.db").read_bytes()
    from wamove.cli import size_of
    assert size_of(folder, PASSWORD) > 0
    assert (folder / "Manifest.db").read_bytes() == before


def test_partial_backup_copy_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `wamove backup` that failed halfway leaves only <udid>.partial, which must not be taken for a backup."""
    work = tmp_path / "work"
    make_chatstorage(tmp_path / CHATSTORAGE)
    make_backup(work / "ios" / "pristine" / f"{UDID}.partial", tmp_path / CHATSTORAGE, PASSWORD)
    make_export(work / "android" / "export", work / "android" / "WhatsApp")
    monkeypatch.setenv("WAMOVE_BACKUP_PASSWORD", PASSWORD)
    assert main(["--work", str(work), "plant"]) == 1
    assert not (work / "ios" / "patched").exists() or not any((work / "ios" / "patched").iterdir())


def test_restore_that_never_started_is_not_a_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wamove.ios import device

    async def locked(udid):
        raise device.DeviceError("the iPhone is locked")

    monkeypatch.setattr(device, "_lockdown", locked)
    with pytest.raises(device.RestoreNotStarted):
        device.restore(tmp_path, UDID, PASSWORD)


def test_restore_with_wrong_password_never_reaches_the_iphone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wamove.ios import device
    work = tmp_path / "work"
    make_chatstorage(tmp_path / CHATSTORAGE)
    mark_complete(make_backup(work / "ios" / "pristine" / UDID, tmp_path / CHATSTORAGE, PASSWORD))
    touched = []
    monkeypatch.setattr(device, "wait_settled", lambda *a, **k: touched.append("wait"))
    monkeypatch.setattr(device, "restore", lambda *a, **k: touched.append("restore"))
    monkeypatch.setenv("WAMOVE_BACKUP_PASSWORD", "wrong")
    assert main(["--work", str(work), "rollback"]) == 1
    assert touched == []


def _media_item(size: int) -> android_xpm.ExportedFile:
    return android_xpm.ExportedFile(MEDIA, "Media/12/34/56-IMG-1.jpg", size, False, None)


def test_mirror_file_of_another_size_is_not_used(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    mirrored = mirror / "Media" / "WhatsApp Images" / "IMG-1.jpg"
    mirrored.parent.mkdir(parents=True)
    mirrored.write_bytes(b"x" * 10)
    pulled = tmp_path / "export" / "files" / MEDIA.lstrip("/")
    assert android_xpm.local_copy(_media_item(10), tmp_path / "export", mirror) == mirrored
    # The phone's file differs from the mirror's (older or partial download): the copy pulled from the phone wins.
    assert android_xpm.local_copy(_media_item(12), tmp_path / "export", mirror) == pulled
    assert android_xpm.local_copy(_media_item(0), tmp_path / "export", mirror) == pulled


def test_check_export_rejects_a_copy_of_the_wrong_size(tmp_path: Path) -> None:
    export, mirror = tmp_path / "export", tmp_path / "mirror"
    make_export(export, mirror)
    files = android_xpm.exported_files(export / "migration_export_metadata.db")
    key = android_xpm.find_key(export / "shared_prefs")
    ios_xpm.check_export(files, key, export, mirror)
    (mirror / "Media" / "WhatsApp Images" / "IMG-1.jpg").write_bytes(b"short")
    pulled = export / "files" / MEDIA.lstrip("/")
    pulled.parent.mkdir(parents=True, exist_ok=True)
    pulled.write_bytes(b"also short")
    with pytest.raises(ios_xpm.PlantError, match="bytes"):
        ios_xpm.check_export(files, key, export, mirror)


def test_plant_refuses_an_export_that_was_not_completely_copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    work = tmp_path / "work"
    make_export(work / "android" / "export", work / "android" / "WhatsApp")
    (work / "android" / "export" / android_xpm.COLLECTED).unlink()
    make_chatstorage(tmp_path / CHATSTORAGE)
    mark_complete(make_backup(work / "ios" / "pristine" / UDID, tmp_path / CHATSTORAGE, PASSWORD))
    monkeypatch.setenv("WAMOVE_BACKUP_PASSWORD", PASSWORD)
    assert main(["--work", str(work), "plant"]) == 1
    assert "not completely copied" in capsys.readouterr().err
    assert not (work / "ios" / "patched" / UDID).exists()


def test_non_ascii_names_get_the_file_id_the_iphone_derives(tmp_path: Path) -> None:
    import hashlib
    import unicodedata
    make_chatstorage(tmp_path / CHATSTORAGE)
    root = make_backup(tmp_path / "backup", tmp_path / CHATSTORAGE, PASSWORD)
    source = tmp_path / "doc"
    source.write_bytes(b"x" * 100)
    composed = "Library/xpm-import/Media/32/17/1234567890-Caf\u00e9.docx"  # as Android names it (NFC)
    with Backup(root, PASSWORD) as backup:
        backup.put(composed, source, ios_xpm.APP_DOMAIN)
        entry = backup.get(composed, ios_xpm.APP_DOMAIN)
        assert entry is not None
        decomposed = unicodedata.normalize("NFD", composed)
        # The iPhone hashes the decomposed path; an ID from the composed one aborts its restore.
        assert entry.file_id == hashlib.sha1(f"{ios_xpm.APP_DOMAIN}-{decomposed}".encode()).hexdigest()
        assert not backup.check(ios_xpm.APP_DOMAIN)
        backup.conn.execute("UPDATE Files SET fileID=? WHERE fileID=?",
                            (hashlib.sha1(f"{ios_xpm.APP_DOMAIN}-{composed}".encode()).hexdigest(), entry.file_id))
        assert any("file ID" in p for p in backup.check(ios_xpm.APP_DOMAIN))


def test_device_path_matches_apples_file_system_representation() -> None:
    from wamove.ios.backup import device_path
    assert device_path("Caf\u00e9.docx") == "Cafe\u0301.docx"
    # Apple leaves U+2000-2FFF, U+F900-FAFF and U+2F800-2FAFF alone; plain NFD would change these.
    assert device_path("\u2126 \u212b \uf900 caf\u00e9") == "\u2126 \u212b \uf900 cafe\u0301"
    assert device_path("plain/ascii.txt") == "plain/ascii.txt"
