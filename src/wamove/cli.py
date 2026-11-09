from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .android import adb, crypt15, msgstore
from .android import xpm as android_xpm
from .ios import backup as ios_backup
from .ios import chatstorage, device
from .ios import xpm as ios_xpm
from .model import Archive

CHATSTORAGE = ios_backup.CHATSTORAGE


class Failure(Exception):
    pass


class Work:
    def __init__(self, root: Path):
        self.root = root
        self.android = root / "android"
        self.crypt15 = self.android / "Databases" / "msgstore.db.crypt15"
        self.msgstore = self.android / "msgstore.db"
        self.media = self.android / "Media"
        self.contacts = self.android / "contacts.json"
        self.export = self.android / "export"
        self.mirror = self.android / "WhatsApp"
        self.full = root / "ios" / "full"
        self.pristine = root / "ios" / "pristine"
        self.patched = root / "ios" / "patched"
        self.build = root / "ios" / "build"

    def udid(self) -> str:
        found = [p.name for p in self.pristine.iterdir()
                 if not p.name.endswith(".partial") and (p / "Manifest.db").is_file() and completed(p).is_file()] \
            if self.pristine.is_dir() else []
        if not found:
            raise Failure("there is no complete iPhone backup yet; run `wamove backup` first")
        if len(found) > 1:
            raise Failure(f"{self.pristine} holds more than one iPhone; remove the one you are not moving to")
        return found[0]


def secret(env: str, prompt: str, file: Path | None = None) -> str:
    if file:
        return file.read_text().strip()
    if os.environ.get(env):
        return os.environ[env]
    if not sys.stdin.isatty():
        raise Failure(f"{prompt} needed: run this in a terminal or set {env}")
    return getpass.getpass(f"{prompt}: ")


def backup_password(path: Path) -> str | None:
    if not ios_backup.is_encrypted(path):
        return None
    password = secret("WAMOVE_BACKUP_PASSWORD", "iPhone backup password")
    if not password:
        raise Failure("the iPhone backup is encrypted, so its password is needed")
    return password


def staging(final: Path) -> Path:
    """A fresh folder to build `final` in; `publish` moves it into place only once it is complete."""
    partial = final.with_name(final.name + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    shutil.rmtree(final, ignore_errors=True)
    completed(final).unlink(missing_ok=True)
    return partial


def completed(final: Path) -> Path:
    return final.with_name(final.name + ".complete")


def publish(partial: Path, final: Path) -> None:
    try:
        if final.exists():
            shutil.rmtree(final)
        partial.rename(final)
    except OSError as e:
        raise Failure(f"could not move {partial} into place: {e}") from e
    completed(final).write_text(time.strftime("%Y-%m-%d %H:%M:%S\n"))


def gigabytes(value: int) -> str:
    return f"{value / 1e9:.1f} GB"


def load_archive(work: Work, vcf: Path | None) -> Archive:
    if not work.msgstore.is_file():
        raise Failure("the Android chats are not decrypted yet; run `wamove decrypt` first")
    contacts = json.loads(work.contacts.read_text()) if work.contacts.is_file() else {}
    if vcf:
        contacts.update(msgstore.load_vcf(vcf))
    return msgstore.parse(work.msgstore, contacts)


def print_stats(archive: Archive) -> None:
    stats = archive.stats()
    print(f"  {stats['chats']} chats ({stats['groups']} groups), {stats['messages']} messages, "
          f"{stats['media']} with media")


def doctor(work: Work, args: argparse.Namespace) -> None:
    try:
        model = adb.device()
        root = adb.whatsapp_root()
        print(f"Android: {model}, WhatsApp backup found in {root}")
    except adb.AdbError as e:
        print(f"Android: {e}")
    try:
        phone = device.phone(args.udid)
        find_my = {True: "on", False: "off", None: "unknown"}[phone.find_my]
        used = gigabytes(phone.used_bytes) if phone.used_bytes else "unknown"
        print(f"iPhone: {phone.name}, iOS {phone.ios}, backups {'encrypted' if phone.encrypts_backups else 'not encrypted'}, "
              f"{used} used, Find My {find_my}")
    except device.DeviceError as e:
        print(f"iPhone: {e}")
    work.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    print(f"Mac: {gigabytes(shutil.disk_usage(work.root).free)} free in {work.root}")
    steps = [
        ("pull", work.crypt15.is_file()),
        ("decrypt", work.msgstore.is_file()),
        ("backup", work.pristine.is_dir() and any(completed(p).is_file() for p in work.pristine.iterdir()
                                                  if p.is_dir() and not p.name.endswith(".partial"))),
        ("build", work.patched.is_dir() and any(completed(p).is_file() for p in work.patched.iterdir()
                                                 if p.is_dir() and not p.name.endswith(".partial"))),
    ]
    print("Done so far: " + (", ".join(name for name, done in steps if done) or "nothing"))


def pull(work: Work, args: argparse.Namespace) -> None:
    print(f"Android: {adb.device()}")
    root = adb.whatsapp_root()
    work.android.mkdir(mode=0o700, parents=True, exist_ok=True)
    print("Copying the chat backup…")
    adb.pull(f"{root}/Databases/msgstore.db.crypt15", work.crypt15.parent)
    names = adb.contacts()
    work.contacts.write_text(json.dumps(names, ensure_ascii=False, indent=1))
    print(f"  {len(names)} contact numbers saved for naming chats")
    if args.no_media:
        return
    needed = adb.remote_bytes(f"{root}/Media")
    free = shutil.disk_usage(work.root).free
    if needed > free:
        raise Failure(f"the media needs {gigabytes(needed)} but only {gigabytes(free)} is free")
    print(f"Copying {gigabytes(needed)} of media…")
    for folder in adb.media_dirs(root):
        adb.pull(f"{root}/Media/{folder}", work.media)


def decrypt(work: Work, args: argparse.Namespace) -> None:
    if not work.crypt15.is_file():
        raise Failure("there is no Android backup yet; run `wamove pull` first")
    key = crypt15.parse_key(secret("WAMOVE_KEY", "64-digit WhatsApp backup key", args.key_file))
    crypt15.decrypt_file(work.crypt15, work.msgstore, key)
    os.chmod(work.msgstore, 0o600)
    print("Decrypted the Android chats:")
    print_stats(load_archive(work, args.contacts))


def backup(work: Work, args: argparse.Namespace) -> None:
    phone = device.phone(args.udid)
    print(f"iPhone: {phone.name}, iOS {phone.ios}")
    work.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    free = shutil.disk_usage(work.root).free
    if phone.used_bytes and phone.used_bytes > free:
        raise Failure(f"the backup needs about {gigabytes(phone.used_bytes)} but only {gigabytes(free)} is free")
    print("Taking a full backup of the iPhone. Keep it unlocked and plugged in; it may ask for its passcode.")
    udid = device.backup(work.full, args.udid)
    full = work.full / udid
    password = backup_password(full)
    staging(work.patched / udid)
    partial = staging(work.pristine / udid)
    ios_backup.clone_tree(full, partial)
    with ios_backup.Backup(partial, password) as saved:
        saved.prune((ios_backup.WA_DOMAIN, ios_xpm.APP_DOMAIN))
        if saved.get("", ios_xpm.APP_DOMAIN) is None:
            raise Failure("WhatsApp is not in this backup: install it on the iPhone and back up again")
        if not saved.get(CHATSTORAGE):
            print("  WhatsApp has no chat database yet (fresh install): only `wamove plant` can use this copy")
        count = saved.count() + saved.count(ios_xpm.APP_DOMAIN)
    publish(partial, work.pristine / udid)
    print(f"  full backup kept in {full}")
    print(f"  WhatsApp-only copy with {count} entries in {work.pristine / udid}")


def build(work: Work, args: argparse.Namespace) -> None:
    udid = work.udid()
    pristine = work.pristine / udid
    password = backup_password(pristine)
    archive = load_archive(work, args.contacts)
    print("Android chats:")
    print_stats(archive)
    shutil.rmtree(work.build, ignore_errors=True)
    work.build.mkdir(parents=True)
    partial = staging(work.patched / udid)
    ios_backup.clone_tree(pristine, partial)
    database = work.build / CHATSTORAGE
    with ios_backup.Backup(partial, password) as target:
        for name in (CHATSTORAGE, CHATSTORAGE + "-wal"):
            target.read(name, work.build / name)
        writer = chatstorage.Writer(database, archive.lids)
        media = None if args.no_media or not work.media.is_dir() else work.media
        (work.build / "thumbs").mkdir()
        report = writer.import_archive(archive, media, work.build / "thumbs")
        writer.close()
        problems = chatstorage.verify(database)
        if problems:
            raise Failure("the converted database failed its checks:\n  " + "\n  ".join(problems))
        print(f"  {report.sessions_created} chats created, {report.sessions_reused} merged into existing ones")
        print(f"  {report.messages_written} messages added, {report.messages_skipped} were already there")
        print(f"  {report.media_linked} media files found, {report.media_missing} missing on the Android phone, "
              f"{report.thumbnails} thumbnails made")
        target.put(CHATSTORAGE, database)
        empty = work.build / "empty"
        empty.write_bytes(b"")
        for suffix in ("-wal", "-shm"):
            target.put(CHATSTORAGE + suffix, empty)
        total = len(report.media_files)
        for done, item in enumerate(report.media_files, 1):
            target.put(item.relative_path, Path(item.source))
            if done % 100 == 0 or done == total:
                sys.stdout.write(f"\r  added {done}/{total} media files to the backup")
                sys.stdout.flush()
        if total:
            sys.stdout.write("\n")
        problems = target.check()
        if problems:
            raise Failure("the modified backup failed its checks:\n  " + "\n  ".join(problems[:20]))
    publish(partial, work.patched / udid)
    print(f"Ready to restore: {work.patched / udid}")


def export(work: Work, args: argparse.Namespace) -> None:
    print(f"Android: {adb.device()}")
    android_xpm.require_root()
    if work.export.exists() and any(work.export.iterdir()):
        # Every export has its own key and IVs, so nothing of an earlier one may mix into it. Keep it aside.
        previous = work.export.with_name(f"{work.export.name}.{time.strftime('%Y%m%d-%H%M%S')}")
        work.export.rename(previous)
        print(f"  kept the previous export in {previous}")
    work.export.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = work.export / "msgstore-edit"
    saved = None if args.keep_live_locations else android_xpm.clear_sent_live_location_finals(scratch)
    try:
        print("Running WhatsApp's own Move to iOS export. WhatsApp on Android logs out when it finishes.")
        android_xpm.run_export()
    finally:
        if saved:
            android_xpm.restore_sent_live_location_finals(scratch, saved)
    collect(work, args)


def collect(work: Work, args: argparse.Namespace) -> None:
    """Copy the finished export off the Android phone (again) without running the exporter."""
    android_xpm.require_root()
    media = work.mirror if work.mirror.is_dir() else None
    files, key = android_xpm.collect(work.export, media)
    summary = ios_xpm.check_export(files, key, work.export, media)
    chunks = summary["header"]["messages"]["chunks"]
    print(f"  {len(chunks)} chunks, {sum(c['messages_count'] for c in chunks)} messages, {len(files)} files")


def plant(work: Work, args: argparse.Namespace) -> None:
    udid = work.udid()
    pristine = work.pristine / udid
    password = backup_password(pristine)
    metadata = work.export / "migration_export_metadata.db"
    if not metadata.is_file():
        raise Failure("there is no Android export yet; run `wamove export` first")
    if not (work.export / android_xpm.COLLECTED).is_file():
        raise Failure("the Android export was not completely copied to this Mac; run `wamove collect` again")
    files = android_xpm.exported_files(metadata)
    key = android_xpm.find_key(work.export / "shared_prefs")
    media = work.mirror if work.mirror.is_dir() else None
    ios_xpm.check_export(files, key, work.export, media)
    partial = staging(work.patched / udid)
    ios_backup.clone_tree(pristine, partial)

    def progress(done: int, total: int) -> None:
        if done % 200 == 0 or done == total:
            sys.stdout.write(f"\r  added {done}/{total} export files to the backup")
            sys.stdout.flush()

    template = None
    if (work.full / udid / "Manifest.db").is_file():
        full = ios_backup.Backup(work.full / udid, password)
        try:
            template = full.borrow_file_template(ios_xpm.APP_DOMAIN)
        finally:
            full.close()
    with ios_backup.Backup(partial, password) as target:
        if target.get("", ios_xpm.APP_DOMAIN) is None:
            raise Failure("WhatsApp's app container is not in the iPhone backup; run `wamove backup` again")
        target.file_template = template
        ios_xpm.plant(target, files, key, work.export, media, work.build, progress)
        print()
        problems = target.check(ios_xpm.APP_DOMAIN) + target.check()
        if problems:
            raise Failure("the modified backup failed its checks:\n  " + "\n  ".join(problems[:20]))
    publish(partial, work.patched / udid)
    print(f"Ready to restore: {work.patched / udid}")


def inspect(work: Work, args: argparse.Namespace) -> None:
    if work.msgstore.is_file():
        print("Android chats:")
        print_stats(load_archive(work, args.contacts))
    udid = work.udid()
    pristine = work.pristine / udid
    with tempfile.TemporaryDirectory() as scratch:
        saved = ios_backup.Backup(pristine, backup_password(pristine))
        try:
            for name in (CHATSTORAGE, CHATSTORAGE + "-wal"):
                saved.read(name, Path(scratch) / name)
            print(f"iPhone backup: {saved.count()} WhatsApp entries, encrypted: {saved.encrypted}")
        finally:
            saved.close()
        database = Path(scratch) / CHATSTORAGE
        conn = sqlite3.connect(database)
        for table in ("ZWACHATSESSION", "ZWAMESSAGE", "ZWAMEDIAITEM", "ZWAGROUPMEMBER"):
            print(f"  {table}: {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]} rows")
        conn.close()
        conventions = chatstorage.Writer(database).conv
        conventions.pop("required")
        print("  conventions: " + json.dumps(conventions))


def restore(work: Work, args: argparse.Namespace, source: Path) -> None:
    udid = work.udid()
    if args.udid and args.udid != udid:
        raise Failure(f"the backup belongs to iPhone {udid}, not {args.udid}")
    if not (source / udid / "Manifest.db").is_file() or not completed(source / udid).is_file():
        what = "`wamove backup`" if source == work.pristine else "`wamove build` or `wamove plant`"
        raise Failure(f"there is no complete copy to restore in {source}; run {what} first")
    password = backup_password(source / udid)
    try:
        restoring = size_of(source / udid, password)  # also proves the password before the iPhone is involved
    except ios_backup.BackupError as e:
        raise Failure(f"{e}; nothing was sent to the iPhone") from e
    added = restoring - size_of(work.pristine / udid, password) if source != work.pristine else 0
    apps = device.apps_in_the_way()
    if apps:
        raise Failure(f"quit {', '.join(apps)} first: it talks to the iPhone, and the iPhone then cancels the restore")
    print("Waiting until the iPhone is plugged in, unlocked and has finished starting up…")
    device.wait_settled(udid)
    phone = device.phone(udid)
    if phone.find_my:
        raise Failure("turn off Find My on the iPhone first (Settings > your name > Find My), then run this again")
    if phone.find_my is None:
        print("Could not tell whether Find My is on. If the restore fails, turn it off and try again.")
    hotspot = device.usb_hotspot_interface(udid)
    if hotspot:
        raise Failure("turn off Personal Hotspot on the iPhone first (Settings > Personal Hotspot > Allow Others to "
                      "Join): the iPhone drops it when the restore begins, and the USB reconfiguration that follows "
                      "cuts the restore off")
    if hotspot is None and device.CONNECTION == "USB":
        print("Could not see the iPhone's USB interfaces. If the restore fails at 0%, turn off Personal Hotspot.")
    stopped = device.stop_device_agents()
    if stopped:
        print(f"  stopped background helpers that talk to iPhones: {', '.join(stopped)} (they restart by themselves)")
    apps = device.apps_in_the_way()
    if apps:
        raise Failure(f"quit {', '.join(apps)} first: it talks to the iPhone, and the iPhone then cancels the restore")
    print("Restoring WhatsApp's data to the iPhone. Keep it unlocked and plugged in, and enter its passcode if it")
    print("asks. Do not open WhatsApp until this finishes. The iPhone restarts at the end.")
    try:
        clean = device.restore(source, udid, password, trace=work.root / "logs" / f"restore-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    except device.RestoreNotStarted as e:
        raise Failure(f"{e}. Nothing on the iPhone was changed; fix that and run this again.") from e
    except device.RestoreCut as cut:
        print(f"\n{cut}\n{cut.details}")
        print("The iPhone restarts by itself after a failed restore; waiting for it before going on.")
        device.wait_reboot(udid)
        raise Failure("the restore did not finish. Do not open WhatsApp: its data may be half restored. "
                      "Run `wamove restore` again, or `wamove rollback`.") from cut
    if not clean:
        print("\n  the iPhone reported 100% and dropped the connection while restarting")
    print("The restore finished. The iPhone restarts now and may show its setup screens: unlock it and finish them.")
    try:
        device.wait_reboot(udid)
        after = device.phone(udid).used_bytes
    except device.DeviceError as e:
        print(f"  could not check the iPhone afterwards ({e}); check its storage before opening WhatsApp")
        after = None
    if added > 0 and phone.used_bytes is not None and after is not None:
        grown = after - phone.used_bytes
        print(f"  the iPhone's used space grew by {gigabytes(grown)}; the restore added about {gigabytes(added)}")
        if grown < added // 2:
            print("  warning: that is much less than expected, so the data may not have landed. Check before "
                  "opening WhatsApp.")
    print("Done. Open WhatsApp, register your number, then turn Find My back on.")


def size_of(path: Path, password: str | None) -> int:
    saved = ios_backup.Backup(path, password)
    try:
        return saved.total_size()
    finally:
        saved.close()  # read-only: leaving a `with` block would save and rewrite Manifest.db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wamove", description="Move WhatsApp chats from Android to an iPhone.")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--work", type=Path, default=Path(os.environ.get("WAMOVE_WORK", Path.home() / "wamove")),
                        help="folder for everything wamove copies and builds (default ~/wamove)")
    parser.add_argument("--udid", help="the iPhone to use when several are connected")
    parser.add_argument("--contacts", type=Path, help="a .vcf export to name chats with")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="check both phones and this Mac")
    command = commands.add_parser("pull", help="copy the WhatsApp backup and media off the Android phone")
    command.add_argument("--no-media", action="store_true")
    command = commands.add_parser("decrypt", help="decrypt the Android backup with its 64-digit key")
    command.add_argument("--key-file", type=Path)
    command = commands.add_parser("export", help="run WhatsApp's own Move to iOS export on the rooted Android phone")
    command.add_argument("--keep-live-locations", action="store_true",
                         help="do not work around the exporter crash on sent live locations")
    commands.add_parser("collect", help="copy a finished export off the Android phone again, without exporting")
    commands.add_parser("backup", help="back up the iPhone and keep a WhatsApp-only copy")
    commands.add_parser("plant", help="put the Android export where WhatsApp on the iPhone imports it from")
    command = commands.add_parser("build", help="add the Android chats to the WhatsApp-only copy")
    command.add_argument("--no-media", action="store_true")
    commands.add_parser("inspect", help="show what is in the Android chats and the iPhone backup")
    commands.add_parser("restore", help="put the built WhatsApp data on the iPhone")
    commands.add_parser("rollback", help="put the untouched WhatsApp data back on the iPhone")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="  %(message)s")
    work = Work(args.work.expanduser())
    actions = {
        "doctor": lambda: doctor(work, args),
        "pull": lambda: pull(work, args),
        "decrypt": lambda: decrypt(work, args),
        "export": lambda: export(work, args),
        "collect": lambda: collect(work, args),
        "backup": lambda: backup(work, args),
        "plant": lambda: plant(work, args),
        "build": lambda: build(work, args),
        "inspect": lambda: inspect(work, args),
        "restore": lambda: restore(work, args, work.patched),
        "rollback": lambda: restore(work, args, work.pristine),
    }
    known = (Failure, adb.AdbError, crypt15.Crypt15Error, msgstore.MsgstoreError, ios_backup.BackupError,
             chatstorage.ChatStorageError, device.DeviceError, android_xpm.ExportError,
             ios_xpm.PlantError)
    try:
        actions[args.command]()
    except known as e:
        print(f"wamove: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
