from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pymobiledevice3.exceptions import (ConnectionFailedError, ConnectionTerminatedError, DeviceNotFoundError,
                                        NoDeviceConnectedError, PairingError, PasswordRequiredError,
                                        StartServiceError)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.device_link import DeviceLink
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
from pymobiledevice3.usbmux import select_device

CONNECTION = os.environ.get("WAMOVE_CONNECTION", "USB")
# The iPhone counts as booted and settled once it has answered for this long without interruption, and its
# backup service then starts. Right after a reboot or a failed restore it answers lockdown well before the
# backup agent is ready again, so a short window is not enough.
SETTLED_S = 60.0
# A restore (successful or not) ends with a reboot: the iPhone first disappears, then comes back.
REBOOT_GONE_S = 180.0
REBOOT_BACK_S = 900.0
POLL_S = 5.0
FINISHED_PERCENT = 100.0
BACKUP_SERVICE = "com.apple.mobilebackup2"
# Apps that open their own sessions to the iPhone. The iPhone cancels a restore when another client starts a
# session, so these must be closed.
APPS_IN_THE_WAY = {
    "Xcode": "/Xcode.app/Contents/MacOS/Xcode",
    "Photos": "/Photos.app/Contents/MacOS/Photos",
    "Image Capture": "/Image Capture.app/Contents/MacOS/",
    "QuickTime Player": "/QuickTime Player.app/Contents/MacOS/",
    "Console": "/Console.app/Contents/MacOS/Console",
    "AltServer": "/AltServer.app/Contents/MacOS/AltServer",
}
# When a restore begins, the iPhone withdraws its Personal Hotspot USB interface ("iPhone USB", an AppleUSBEthernet
# interface) and the Mac's USB driver switches the iPhone to the USB configuration without it. Switching
# configurations resets the usbmux link the restore runs over, and the iPhone abandons the restore. With Personal
# Hotspot off the iPhone is already in that configuration, so nothing changes mid-restore.
HOTSPOT_INTERFACE = "+-o AppleUSBEthernet@"
# Per-user helpers that do the same in the background. They are started on demand and come back by themselves
# when needed, so wamove stops them right before a restore.
DEVICE_AGENTS = {
    "CoreDeviceService": "CoreDeviceService.xpc/Contents/MacOS/CoreDeviceService",
    "MDRemoteServiceSupport": "MDRemoteServiceSupport.xpc/Contents/MacOS/MDRemoteServiceSupport",
    "remotepairingd": "remotepairingd.xpc/Contents/MacOS/remotepairingd",
    "AMPDevicesAgent": "AMPDevices.framework/Versions/A/Support/AMPDevicesAgent",
    "AMPDeviceDiscoveryAgent": "AMPDevices.framework/Versions/A/Support/AMPDeviceDiscoveryAgent",
    "devicesui": "com.apple.amp.devicesui.xpc/Contents/MacOS/com.apple.amp.devicesui",
    "AppleMobileDeviceHelper": "AppleMobileDeviceHelper.app/Contents/MacOS/AppleMobileDeviceHelper",
    "MobileDeviceUpdater": "MobileDeviceUpdater.app/Contents/MacOS/MobileDeviceUpdater",
}
UNREACHABLE = (NoDeviceConnectedError, DeviceNotFoundError, ConnectionFailedError, ConnectionError, OSError,
               asyncio.IncompleteReadError)
# What the connection dying looks like, as opposed to the iPhone reporting an error.
LINK_LOST = (ConnectionTerminatedError, ConnectionError, OSError, asyncio.IncompleteReadError)
SERVICE_RETRY_S = 5.0


class DeviceError(Exception):
    pass


class RestoreNotStarted(DeviceError):
    pass


class RestoreCut(DeviceError):
    def __init__(self, reached: float, cause: BaseException):
        self.reached = reached
        self.cause = cause
        self.details = "".join(traceback.format_exception(cause)).rstrip()
        super().__init__(f"the iPhone ended the restore at {reached:.1f}% ({type(cause).__name__}: {cause})")


@dataclass
class Phone:
    udid: str
    name: str
    ios: str
    encrypts_backups: bool
    used_bytes: int | None
    find_my: bool | None


def _progress(label: str, record: list[float], started: list[bool] | None = None) -> Callable[[float], None]:
    def show(percent: float) -> None:
        record[0] = max(record[0], percent)
        if started is not None:
            started[0] = True
        sys.stdout.write(f"\r  {label} {percent:5.1f}%")
        sys.stdout.flush()
        if percent >= FINISHED_PERCENT:
            sys.stdout.write("\n")
    return show


async def _lockdown(udid: str | None):
    try:
        # USB by default. WAMOVE_CONNECTION=Network uses Wi-Fi sync instead.
        return await create_using_usbmux(serial=udid, connection_type=CONNECTION)
    except PasswordRequiredError as e:
        raise DeviceError("the iPhone is locked: unlock it and keep it unlocked") from e
    except PairingError as e:
        raise DeviceError("the iPhone does not trust this Mac yet: unlock it and tap Trust") from e
    except UNREACHABLE as e:
        raise DeviceError(f"no iPhone reachable over {CONNECTION}: plug it in, unlock it, and wait for it to finish "
                          "starting up") from e


async def _reachable(udid: str) -> bool:
    try:
        if await select_device(udid, connection_type=CONNECTION) is None:
            return False
        lockdown = await create_using_usbmux(serial=udid, connection_type=CONNECTION, autopair=False)
    except Exception:
        return False
    try:
        return bool(lockdown.paired) and bool(await lockdown.get_value(key="DeviceName"))
    except Exception:
        return False
    finally:
        await lockdown.close()


async def _backup_service_ready(udid: str) -> bool:
    """Whether the iPhone's backup agent starts, i.e. no earlier backup or restore is still winding down."""
    try:
        lockdown = await create_using_usbmux(serial=udid, connection_type=CONNECTION, autopair=False)
    except Exception:
        return False
    try:
        service = await lockdown.start_lockdown_service(BACKUP_SERVICE, include_escrow_bag=True)
        await service.close()
        return True
    except Exception:
        return False
    finally:
        await lockdown.close()


def wait_settled(udid: str, timeout_s: float = REBOOT_BACK_S, say: Callable[[str], None] = print) -> None:
    """Return once the iPhone has answered for SETTLED_S seconds without interruption and its backup agent starts."""
    async def go() -> None:
        deadline = time.monotonic() + timeout_s
        since = None
        told = told_service = False
        while time.monotonic() < deadline:
            if await _reachable(udid):
                since = since or time.monotonic()
                if time.monotonic() - since >= SETTLED_S:
                    if await _backup_service_ready(udid):
                        return
                    if not told_service:
                        say("  the iPhone answers but its backup service is still busy; waiting (keep it unlocked)…")
                        told_service = True
                    since = None
            else:
                since = None
                if not told:
                    say(f"  waiting for the iPhone over {CONNECTION} (plugged in, unlocked, finished starting up)…")
                    told = True
            await asyncio.sleep(POLL_S)
        raise DeviceError(f"the iPhone was not ready over {CONNECTION} within {int(timeout_s)} s")

    asyncio.run(go())


def wait_reboot(udid: str, say: Callable[[str], None] = print) -> None:
    """Wait for the reboot that ends a restore, successful or not, to finish."""
    async def gone() -> bool:
        deadline = time.monotonic() + REBOOT_GONE_S
        while time.monotonic() < deadline:
            try:
                if await select_device(udid, connection_type=CONNECTION) is None:
                    return True
            except Exception:
                return True
            await asyncio.sleep(POLL_S)
        return False

    say("  waiting for the iPhone to restart…")
    if not asyncio.run(gone()):
        say("  it did not disconnect; waiting until it is settled anyway")
    wait_settled(udid, say=say)


def apps_in_the_way() -> list[str]:
    listing = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
    return [name for name, marker in APPS_IN_THE_WAY.items() if marker in listing]


def usb_hotspot_interface(udid: str) -> bool | None:
    """Whether the iPhone currently offers Personal Hotspot over USB; None when it is not attached over USB."""
    listing = subprocess.run(["ioreg", "-w0", "-l", "-r", "-c", "IOUSBHostDevice"], capture_output=True,
                             text=True).stdout
    serial = f'"USB Serial Number" = "{udid.replace("-", "")}"'
    blocks = listing.split("\n+-o ")
    for block in blocks:
        if serial in block:
            return HOTSPOT_INTERFACE in block
    return None


def stop_device_agents() -> list[str]:
    uid = str(os.getuid())
    stopped = []
    for name, marker in DEVICE_AGENTS.items():
        if subprocess.run(["pkill", "-9", "-u", uid, "-f", marker], capture_output=True).returncode == 0:
            stopped.append(name)
    return stopped


async def _service(lockdown) -> Mobilebackup2Service:
    for retry in (True, False):
        service = Mobilebackup2Service(lockdown)
        try:
            await service.connect()
            return service
        except (StartServiceError, *UNREACHABLE) as e:
            if not retry:
                raise DeviceError("the iPhone's backup service did not start; wait for it to finish starting up") from e
            await asyncio.sleep(SERVICE_RETRY_S)
    raise DeviceError("the iPhone's backup service did not start")


async def _optional(lockdown, domain: str | None, key: str | None):
    try:
        return await lockdown.get_value(domain, key)
    except Exception:
        return None


def phone(udid: str | None = None) -> Phone:
    async def go() -> Phone:
        lockdown = await _lockdown(udid)
        try:
            usage = await _optional(lockdown, "com.apple.disk_usage", None) or {}
            capacity, available = usage.get("TotalDataCapacity"), usage.get("TotalDataAvailable")
            find_my = await _optional(lockdown, "com.apple.fmip", "IsAssociated")
            return Phone(
                udid=lockdown.udid,
                name=await lockdown.get_value(key="DeviceName") or "",
                ios=await lockdown.get_value(key="ProductVersion") or "",
                encrypts_backups=bool(await _optional(lockdown, "com.apple.mobile.backup", "WillEncrypt")),
                used_bytes=capacity - available if capacity and available else None,
                find_my=None if find_my is None else bool(find_my),
            )
        finally:
            await lockdown.close()

    return asyncio.run(go())


def backup(root: Path, udid: str | None = None) -> str:
    async def go() -> str:
        lockdown = await _lockdown(udid)
        try:
            service = await _service(lockdown)
            try:
                await service.backup(full=True, backup_directory=root, progress_callback=_progress("backing up", [0.0]))
            finally:
                await service.close()
            return lockdown.udid
        finally:
            await lockdown.close()

    root.mkdir(parents=True, exist_ok=True)
    return asyncio.run(go())


class _FileTrace:
    """Record every batch of files the iPhone asks for during a restore, so a cut can be traced to its file."""

    def __init__(self, path: Path | None):
        self.path = path
        self.original = DeviceLink.download_files

    def __enter__(self) -> _FileTrace:
        if self.path is None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        log = self.path.open("w", buffering=1)
        original = self.original

        def note(text: str) -> None:
            try:  # the trace must never be what cuts a restore (e.g. a full disk on the Mac)
                log.write(text)
            except (OSError, ValueError):
                pass

        async def download_files(link: DeviceLink, message) -> None:
            note(f"{time.strftime('%H:%M:%S')} batch progress={message[3]!r} files={len(message[1])}\n"
                 + "".join(f"  {name}\n" for name in message[1]))
            await original(link, message)
            note(f"{time.strftime('%H:%M:%S')} batch sent\n")

        self.log = log
        DeviceLink.download_files = download_files
        return self

    def __exit__(self, *_: object) -> None:
        if self.path is not None:
            DeviceLink.download_files = self.original
            self.log.close()


def restore(root: Path, udid: str, password: str | None, trace: Path | None = None) -> bool:
    """Restore `root/udid` onto the iPhone `udid`.

    Returns True when the device finished cleanly, False when it reported 100% and then the link died while it
    rebooted (pymobiledevice3's cleanup fails on the dead link then). Raises RestoreNotStarted when the restore
    never began (nothing on the iPhone changed, it does not reboot), and RestoreCut when it began and did not
    finish, including Ctrl-C (the iPhone then reboots by itself).
    """
    reached = [0.0]
    started = [False]

    async def go() -> None:
        lockdown = await _lockdown(udid)
        try:
            service = await _service(lockdown)
            try:
                await service.restore(
                    backup_directory=root,
                    system=True,
                    reboot=True,
                    copy=False,
                    settings=True,
                    remove=False,
                    password=password or "",
                    source=udid,
                    progress_callback=_progress("restoring", reached, started),
                    skip_apps=True,
                )
            finally:
                try:
                    await service.close()
                except Exception:
                    pass
        finally:
            try:
                await lockdown.close()
            except Exception:
                pass

    try:
        with _FileTrace(trace):
            asyncio.run(go())
    except (Exception, KeyboardInterrupt) as e:
        if not started[0]:
            if isinstance(e, KeyboardInterrupt):
                raise
            raise RestoreNotStarted(f"the restore did not start: {type(e).__name__}: {e}") from e
        if reached[0] >= FINISHED_PERCENT and isinstance(e, LINK_LOST):
            return False
        raise RestoreCut(reached[0], e) from e
    if not started[0]:
        # pymobiledevice3 returns quietly when it refuses to start (e.g. an encrypted backup without a password)
        raise RestoreNotStarted("the iPhone never asked for any file of the backup")
    return True
