from __future__ import annotations

import unittest
from datetime import datetime

from openadb.core.adb import ADBClient, _looks_like_wireless_serial, is_mdns_wireless_serial
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo


def successful_result(*args: str) -> CommandResult:
    now = datetime.now()
    return CommandResult(
        command=["adb", *args],
        exit_code=0,
        stdout="",
        stderr="",
        duration=0.0,
        started_at=now,
        finished_at=now,
        success=True,
    )


class QrPairingAdb(ADBClient):
    MDNS_SERIAL = "adb-3A131FDJG000SZ-example._adb-tls-connect._tcp"

    def __init__(self) -> None:
        self.serial = ""
        self.device_reads = 0
        self.connect_targets: list[str] = []

    def _base(self, serial: str | None = None) -> list[str]:
        return ["adb"]

    def run_raw(self, args, timeout=120, use_serial=True, cancel_event=None):
        return successful_result(*args)

    def list_devices(self) -> list[DeviceInfo]:
        self.device_reads += 1
        if self.device_reads == 1:
            return []
        return [DeviceInfo(serial=self.MDNS_SERIAL, mode="ADB", state="device")]

    def _discover_wireless_mdns_services(self, wait_seconds=0.5):
        return [
            {
                "name": "studio-pairing-service",
                "type": "_adb-tls-pairing._tcp",
                "target": "192.168.0.159:37001",
                "source": "zeroconf",
            }
        ]

    def pair_wireless_target(self, target: str, pairing_code: str) -> CommandResult:
        return successful_result("pair", target, pairing_code)

    def connect_wireless_target(self, target: str, timeout=35) -> CommandResult:
        self.connect_targets.append(target)
        return successful_result("connect", target)


class WirelessQrTests(unittest.TestCase):
    def test_recognizes_android_mdns_device_serial_as_wireless(self) -> None:
        self.assertTrue(_looks_like_wireless_serial("adb-serial-token._adb-tls-connect._tcp"))
        self.assertTrue(_looks_like_wireless_serial("adb-serial-token._adb-tls-connect._tcp."))
        self.assertTrue(_looks_like_wireless_serial("192.168.0.159:44195"))
        self.assertFalse(_looks_like_wireless_serial("3A131FDJG000SZ"))
        self.assertTrue(is_mdns_wireless_serial("adb-serial-token._adb-tls-connect._tcp"))

    def test_qr_pairing_does_not_add_ip_connection_after_mdns_auto_connect(self) -> None:
        adb = QrPairingAdb()

        result = adb.pair_wireless_qr("studio-pairing-service", "secret", timeout=10)

        self.assertTrue(result.success)
        self.assertIn(QrPairingAdb.MDNS_SERIAL, result.stdout)
        self.assertEqual(adb.connect_targets, [])


if __name__ == "__main__":
    unittest.main()
