"""WhatsApp's own Android → iPhone export (the one the Move to iOS app drives), run directly with root.

WhatsApp on Android has a complete exporter for Apple's Move to iOS app: `MessagesExporterService` turns every chat
into the same `HistorySync` protobufs multi-device history sync uses and writes them, plus a list of media files,
into a sandbox. Move to iOS reads that sandbox through a content provider that encrypts each file on the fly with a
key escrowed on WhatsApp's server, and WhatsApp on the iPhone imports it with its own code after registering.

With root we can start that exporter without Move to iOS and read the sandbox, the file list (with each file's IV)
and the escrowed key straight from WhatsApp's private storage. Finishing an export logs WhatsApp out on Android,
exactly as Move to iOS does.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import adb

PACKAGE = "com.whatsapp"
DATA = f"/data/data/{PACKAGE}"
SERVICE = f"{PACKAGE}/{PACKAGE}.migration.export.service.MessagesExporterService"
LOG = f"{DATA}/files/Logs/whatsapp.log"
MSGSTORE = f"{DATA}/databases/msgstore.db"
METADATA_DB = f"{DATA}/databases/migration_export_metadata.db"
KEY_PREFIX = "/export/enc/active/"
KEY_FIELDS = ("owner", "version", "account_hash", "server_salt", "seed")
DONE = "xpm-export-service-onComplete"
# Written by `collect` as its very last step: without it the copy on this Mac may be missing files.
COLLECTED = ".collected"
FAILED = "xpm-export-service-onError"
SHARED_MEDIA_ROOTS = (
    "/storage/emulated/0/Android/media/com.whatsapp/WhatsApp/",
    "/sdcard/Android/media/com.whatsapp/WhatsApp/",
    "/storage/emulated/0/WhatsApp/",
)
# Sent live locations that carry a final position crash WhatsApp's exporter (it asks for the sender JID of a
# from-me row and gets null), which aborts the whole export. These are cleared for the export and put back after.
SENT_FINAL_LIVE_LOCATIONS = (
    "SELECT l.message_row_id, l.live_location_final_latitude, l.live_location_final_longitude, "
    "l.live_location_final_timestamp FROM message_location l JOIN message m ON m._id = l.message_row_id "
    "WHERE m.from_me = 1 AND (l.live_location_final_latitude <> 0 OR l.live_location_final_longitude <> 0 "
    "OR l.live_location_final_timestamp <> 0)"
)


class ExportError(Exception):
    pass


@dataclass
class ExportedFile:
    local_path: str
    exported_path: str
    size: int
    required: bool
    iv: str | None


@dataclass
class EncryptionKey:
    owner: str
    version: str
    account_hash: str
    server_salt: str
    seed: str


def su(command: str) -> str:
    return adb.shell(f"su -c {shlex.quote(command)}")


def require_root() -> None:
    if "uid=0" not in su("id"):
        raise ExportError("root is needed on the Android phone: grant the shell root in Magisk/KernelSU")


def remote_size(remote: str) -> int | None:
    out = su(f"stat -c %s {shlex.quote(remote)} 2>/dev/null").strip()
    return int(out) if out.isdigit() else None


def remote_sha256(remote: str) -> str | None:
    out = su(f"sha256sum {shlex.quote(remote)} 2>/dev/null").split()
    return out[0] if out else None


def read_file(remote: str, local: Path) -> None:
    """Copy a root-only file off the phone. `adb exec-out` does not pass on the remote exit status, so the copy is
    checked against the file's size on the phone."""
    expected = remote_size(remote)
    if expected is None:
        raise ExportError(f"{remote} does not exist on the phone")
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        with local.open("wb") as out:
            subprocess.run([adb._adb(), "exec-out", f"su -c {shlex.quote(f'cat {shlex.quote(remote)}')}"], stdout=out)
        if local.stat().st_size != expected:
            raise ExportError(f"copying {remote} stopped early ({local.stat().st_size} of {expected} bytes)")
    except BaseException:
        local.unlink(missing_ok=True)  # never leave a partial copy that a later step could take for the real file
        raise


def write_file(local: Path, remote: str, before: str = "") -> None:
    """Overwrite a file in WhatsApp's storage in place (keeping its owner and SELinux label) and check the result.
    `before` runs in the same root shell right before the overwrite."""
    staging = "/data/local/tmp/wamove.upload"
    adb.run("push", str(local), staging)
    su(f"{before} cat {staging} > {shlex.quote(remote)} && rm {staging}")
    if remote_sha256(remote) != hashlib.sha256(local.read_bytes()).hexdigest():
        raise ExportError(f"writing {remote} back to the phone did not produce the expected file")


def stopped() -> bool:
    return not su(f"pidof {PACKAGE}").strip()


def stop() -> None:
    su(f"am force-stop {PACKAGE}")
    for _ in range(20):
        if stopped():
            return
        time.sleep(0.5)
    raise ExportError("WhatsApp did not stop on the Android phone")


def log_length() -> int:
    match = re.search(r"\d+", su(f"wc -l < {LOG}"))
    return int(match.group()) if match else 0


def log_since(line: int) -> str:
    return su(f"tail -n +{line + 1} {LOG}")


def _with_msgstore(scratch: Path, change) -> object:
    """Run `change(connection)` on a copy of msgstore.db and put the copy back, with WhatsApp stopped."""
    stop()
    scratch.mkdir(parents=True, exist_ok=True)
    local = scratch / "msgstore.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(local) + suffix).unlink(missing_ok=True)
        if suffix and remote_size(MSGSTORE + suffix) is None:
            continue
        read_file(MSGSTORE + suffix, Path(str(local) + suffix))
    original = scratch / "msgstore.orig"
    if not original.exists():  # the untouched database, kept from the first edit on
        original.mkdir()
        for suffix in ("", "-wal", "-shm"):
            if Path(str(local) + suffix).exists():
                shutil.copy2(Path(str(local) + suffix), original / ("msgstore.db" + suffix))
    conn = sqlite3.connect(local)
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        conn.close()
        raise ExportError("msgstore.db on the phone failed its integrity check; nothing was changed")
    result = change(conn)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    # The WAL was folded into the file above, so its stale frames must be gone before the new file lands, or
    # WhatsApp would replay them over it.
    write_file(local, MSGSTORE, before=f"am force-stop {PACKAGE} && rm -f {MSGSTORE}-wal {MSGSTORE}-shm &&")
    return result


def clear_sent_live_location_finals(scratch: Path) -> Path:
    saved = scratch / "sent_live_location_finals.json"

    def change(conn: sqlite3.Connection) -> int:
        rows = conn.execute(SENT_FINAL_LIVE_LOCATIONS).fetchall()
        saved.write_text(json.dumps([list(r) for r in rows]))
        conn.executemany(
            "UPDATE message_location SET live_location_final_latitude=0, live_location_final_longitude=0, "
            "live_location_final_timestamp=0 WHERE message_row_id=?", [(r[0],) for r in rows])
        return len(rows)

    count = _with_msgstore(scratch, change)
    print(f"  cleared the final position of {count} sent live locations for the export (saved in {saved})")
    return saved


def restore_sent_live_location_finals(scratch: Path, saved: Path) -> None:
    rows = [(lat, lon, ts, row) for row, lat, lon, ts in json.loads(saved.read_text())]

    def change(conn: sqlite3.Connection) -> None:
        conn.executemany(
            "UPDATE message_location SET live_location_final_latitude=?, live_location_final_longitude=?, "
            "live_location_final_timestamp=? WHERE message_row_id=?", rows)

    _with_msgstore(scratch, change)
    print(f"  put back the final position of {len(rows)} sent live locations")


def run_export(timeout_s: int = 6 * 3600) -> str:
    start = log_length()
    su(f"am start-foreground-service -n {SERVICE} -a ACTION_START_EXPORT")
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        time.sleep(15)
        tail = log_since(start)
        chunks = re.findall(r"export-chunk/stage=written \S+ chunkNumber=(\d+)", tail)
        if chunks and chunks[-1] != last:
            last = chunks[-1]
            print(f"\r  exported chunk {last}", end="", flush=True)
        if DONE in tail:
            print()
            return tail
        if FAILED in tail:
            print()
            cause = re.findall(r"(xpm-export-failed; exception=.*|Caused by: .*)", tail)
            raise ExportError("WhatsApp's exporter failed: " + ("; ".join(cause[:3]) or "see its log"))
    raise ExportError("WhatsApp's exporter did not finish in time")


def exported_files(metadata_db: Path) -> list[ExportedFile]:
    conn = sqlite3.connect(metadata_db)
    rows = conn.execute(
        "SELECT local_path, exported_path, file_size, required, encryption_iv FROM exported_files_metadata "
        "ORDER BY sort_id, rowid").fetchall()
    conn.close()
    return [ExportedFile(local, exported, size or 0, bool(required), iv or None)
            for local, exported, size, required, iv in rows]


def find_key(prefs_dir: Path) -> EncryptionKey:
    for xml in sorted(prefs_dir.glob("*.xml")):
        try:
            root = ET.parse(xml).getroot()
        except ET.ParseError:
            continue
        values = {el.get("name"): el.get("value", el.text) for el in root}
        if KEY_PREFIX + "seed" in values:
            return EncryptionKey(**{f: str(values.get(KEY_PREFIX + f) or "") for f in KEY_FIELDS})
    raise ExportError("the export's encryption key is not in WhatsApp's preferences")


def collect(target: Path, media_mirror: Path | None) -> tuple[list[ExportedFile], EncryptionKey]:
    """Copy the export off the phone: file list, sandbox files and the active key. Media already pulled is reused."""
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    (target / COLLECTED).unlink(missing_ok=True)
    for suffix in ("", "-wal", "-shm"):
        # A WAL left over from an earlier export would be replayed over this one's database.
        (target / f"migration_export_metadata.db{suffix}").unlink(missing_ok=True)
        if su(f"[ -f {METADATA_DB}{suffix} ] && echo y").strip() == "y":
            read_file(METADATA_DB + suffix, target / f"migration_export_metadata.db{suffix}")
    files = exported_files(target / "migration_export_metadata.db")
    prefs = target / "shared_prefs"
    shutil.rmtree(prefs, ignore_errors=True)
    for name in su(f"ls {DATA}/shared_prefs").split():
        read_file(f"{DATA}/shared_prefs/{name}", prefs / name)
    key = find_key(prefs)
    (target / "key.json").write_text(json.dumps(key.__dict__, indent=1))
    (target / "key.json").chmod(0o600)
    sandbox = target / "files"
    reused = pulled = 0
    for item in files:
        local = local_copy(item, target, media_mirror)
        # Only media from the mirror is reused: sandbox files are regenerated by every export, and some (enc.zip)
        # keep the same size with different contents.
        if media_mirror and local.is_relative_to(media_mirror):
            reused += 1
            continue
        read_file(item.local_path, sandbox / item.local_path.lstrip("/"))
        pulled += 1
    print(f"  {len(files)} files in the export: {pulled} copied from the phone, {reused} already on this Mac")
    (target / COLLECTED).write_text(json.dumps({"files": len(files), "pulled": pulled, "reused": reused}))
    return files, key


def local_copy(item: ExportedFile, target: Path, media_mirror: Path | None) -> Path:
    """Where the Mac holds `item`: the media mirror when it has the very file the export lists (same size), otherwise
    the copy `collect` pulled from the phone. A mirror file of another size is an older or partial download."""
    if media_mirror and item.size:
        for root in SHARED_MEDIA_ROOTS:
            if item.local_path.startswith(root):
                mirrored = media_mirror / item.local_path[len(root):]
                if mirrored.is_file() and mirrored.stat().st_size == item.size:
                    return mirrored
    return target / "files" / item.local_path.lstrip("/")
