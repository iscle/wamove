from __future__ import annotations

import subprocess

import pytest

from wamove.ios import device

UDID = "00008130-001A2B3C4D5E6F70"
CONFIG_6 = """+-o iPhone@00100000  <class IOUSBHostDevice, id 0x1, registered, matched, active, busy 0 (1 ms), retain 68>
  | {
  |   "USB Serial Number" = "00008130001A2B3C4D5E6F70"
  |   "kUSBCurrentConfiguration" = 6
  | }
  |
  +-o Apple USB Multiplexor@1  <class IOUSBHostInterface, id 0x2, registered, matched, active, busy 0, retain 11>
  +-o AppleUSBEthernet@2  <class IOUSBHostInterface, id 0x3, registered, matched, active, busy 0, retain 9>
  +-o NCM Control@3  <class IOUSBHostInterface, id 0x4, registered, matched, active, busy 0, retain 10>

+-o Xiaomi 15@01100000  <class IOUSBHostDevice, id 0x5, registered, matched, active, busy 0 (10 ms), retain 32>
  | {
  |   "USB Serial Number" = "0123abcd"
  | }
"""
CONFIG_5 = CONFIG_6.replace("  +-o AppleUSBEthernet@2  <class IOUSBHostInterface, id 0x3, registered, matched, active, busy 0, retain 9>\n", "")


@pytest.mark.parametrize(("listing", "expected"), [(CONFIG_6, True), (CONFIG_5, False), ("", None)])
def test_usb_hotspot_interface(monkeypatch: pytest.MonkeyPatch, listing: str, expected: bool | None) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=listing))
    assert device.usb_hotspot_interface(UDID) is expected
