"""Put WhatsApp's Android export where WhatsApp on the iPhone looks for a Move to iOS import.

On the iPhone, the files Move to iOS delivers end up in the app's own data container under
`Library/xpm-import/` (AppMigrationExtension: container root + "Library" + `migrationImportDirName`; the app reads
`userLibraryDirectoryURL` + `migrationImportDirName`), laid out by the paths the Android provider reports
(`migration/messages_export.zip`, `migration/enc.zip`, `Media/NN/NN/<hash>-<name>`). Every file except enc.zip arrives
encrypted exactly as the Android provider streams it: AES-GCM with the escrowed key and the file's own 16-byte IV,
the 128-bit tag appended. WhatsApp on the iPhone fetches the same key from its server after registering.
"""
from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..android.xpm import EncryptionKey, ExportedFile, local_copy
from .backup import Backup

APP_DOMAIN = "AppDomain-net.whatsapp.WhatsApp"
IMPORT_ROOT = "Library/xpm-import"
CHUNK = 1 << 20


class PlantError(Exception):
    pass


def encrypt(source: Path, target: Path, key: bytes, iv: bytes) -> None:
    encryptor = Cipher(algorithms.AES(key), modes.GCM(iv)).encryptor()
    with source.open("rb") as src, target.open("wb") as dst:
        for chunk in iter(lambda: src.read(CHUNK), b""):
            dst.write(encryptor.update(chunk))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)


def decrypt(source: Path, key: bytes, iv: bytes) -> bytes:
    data = source.read_bytes()
    decryptor = Cipher(algorithms.AES(key), modes.GCM(iv, data[-16:])).decryptor()
    return decryptor.update(data[:-16]) + decryptor.finalize()


def check_export(files: list[ExportedFile], key: EncryptionKey, export_dir: Path, media: Path | None) -> dict:
    """Read back what the iPhone will read: enc.zip's metadata against the file list, and the messages header."""
    by_path = {f.exported_path: f for f in files}
    enc = by_path.get("migration/enc.zip")
    messages = by_path.get("migration/messages_export.zip")
    if not enc or not messages:
        raise PlantError("the export has no migration/enc.zip or migration/messages_export.zip")
    for item in files:
        local = local_copy(item, export_dir, media)
        if not local.is_file():
            if item.required:
                raise PlantError(f"{item.exported_path} is missing from the copied export ({item.local_path}); "
                                 "run `wamove export` again")
            continue
        if item.size and local.stat().st_size != item.size:
            raise PlantError(f"{item.exported_path}: the copy on this Mac ({local}) is {local.stat().st_size} bytes, "
                             f"the export lists {item.size}; run `wamove export` again")
    with zipfile.ZipFile(local_copy(enc, export_dir, media)) as archive:
        metadata = json.loads(archive.read("metadata.json"))
    key_id = metadata.get("key_id", {})
    if (str(key_id.get("account_hash")), str(key_id.get("server_salt"))) != (key.account_hash, key.server_salt):
        raise PlantError("enc.zip names a different key than the one in WhatsApp's preferences")
    listed = {f["path"]: f.get("iv") for f in metadata.get("files", [])}
    for f in files:
        if f.exported_path != "migration/enc.zip" and listed.get(f.exported_path) != f.iv:
            raise PlantError(f"{f.exported_path}: its IV in enc.zip does not match the export's file list")
    with zipfile.ZipFile(local_copy(messages, export_dir, media)) as archive:
        header = json.loads(archive.read("header.json"))
    return {"metadata": metadata, "header": header}


def plant(backup: Backup, files: list[ExportedFile], key: EncryptionKey, export_dir: Path, media: Path | None,
          scratch: Path, progress=None) -> int:
    seed = base64.b64decode(key.seed)
    scratch.mkdir(parents=True, exist_ok=True)
    staged = scratch / "staged"
    for done, item in enumerate(files, 1):
        source = local_copy(item, export_dir, media)
        if not source.is_file():
            if item.required:
                raise PlantError(f"{item.exported_path}: {item.local_path} is missing")
            continue
        relative = f"{IMPORT_ROOT}/{item.exported_path}"
        if item.iv:
            encrypt(source, staged, seed, base64.b64decode(item.iv))
            backup.put(relative, staged, APP_DOMAIN)
        else:
            backup.put(relative, source, APP_DOMAIN)
        if progress:
            progress(done, len(files))
    staged.unlink(missing_ok=True)
    return len(files)
