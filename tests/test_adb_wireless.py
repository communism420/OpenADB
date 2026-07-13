from __future__ import annotations

import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from openadb.core.adb import (
    ADBClient,
    _find_mdns_service,
    _looks_like_wireless_serial,
    _normalize_adb_connect_result,
    _normalize_adb_pair_result,
    _wireless_connect_candidates_from_services,
    is_mdns_wireless_serial,
)
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.core.wireless_qr import WirelessQrPayload


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

    def list_devices(self, cancel_event=None) -> list[DeviceInfo]:
        self.device_reads += 1
        if self.device_reads == 1:
            return []
        return [DeviceInfo(serial=self.MDNS_SERIAL, mode="ADB", state="device")]

    def _discover_wireless_mdns_services(self, wait_seconds=0.5, cancel_event=None):
        return [
            {
                "name": "studio-pairing-service",
                "type": "_adb-tls-pairing._tcp",
                "target": "192.0.2.59:37001",
                "source": "zeroconf",
            },
            {
                "name": self.MDNS_SERIAL,
                "type": "_adb-tls-connect._tcp",
                "target": "192.0.2.59:40765",
                "source": "zeroconf",
            },
        ]

    def pair_wireless_target(self, target: str, pairing_code: str, cancel_event=None) -> CommandResult:
        return successful_result("pair", target, pairing_code)

    def connect_wireless_target(self, target: str, timeout=35, cancel_event=None) -> CommandResult:
        self.connect_targets.append(target)
        return successful_result("connect", target)


class FirstRunQrAdb(QrPairingAdb):
    def __init__(self) -> None:
        super().__init__()
        self.connected = False

    def list_devices(self, cancel_event=None) -> list[DeviceInfo]:
        self.device_reads += 1
        if self.device_reads == 1:
            return []
        state = "device" if self.connected else "offline"
        mode = "ADB" if self.connected else "Offline"
        return [DeviceInfo(serial=self.MDNS_SERIAL, mode=mode, state=state)]

    def _wait_for_new_wireless_device(
        self,
        before,
        deadline,
        progress_callback=None,
        cancel_event=None,
        seconds=4.0,
        expected_targets=(),
    ) -> str:
        for _attempt in range(3):
            serial = self._new_wireless_device_serial(before, expected_targets=expected_targets)
            if serial:
                return serial
        return ""

    def _wait_for_wireless_connect_candidates(
        self,
        pairing_target,
        deadline,
        progress_callback=None,
        cancel_event=None,
    ) -> list[str]:
        return [self.MDNS_SERIAL]

    def connect_wireless_target(self, target: str, timeout=35, cancel_event=None) -> CommandResult:
        self.connect_targets.append(target)
        self.connected = True
        return successful_result("connect", target)


class NoConnectServiceQrAdb(QrPairingAdb):
    def _wait_for_new_wireless_device(self, *args, **kwargs) -> str:
        return ""

    def _wait_for_wireless_connect_candidates(self, *args, **kwargs) -> list[str]:
        return []

    def _new_wireless_device_serial(self, before) -> str:
        return ""


class PreExistingWirelessQrAdb(QrPairingAdb):
    OLD_SERIAL = "adb-unrelated-old._adb-tls-connect._tcp"

    def list_devices(self, cancel_event=None) -> list[DeviceInfo]:
        return [DeviceInfo(serial=self.OLD_SERIAL, mode="ADB", state="device")]

    def _wait_for_new_wireless_device(self, before, *args, **kwargs) -> str:
        return self._new_wireless_device_serial(before)

    def _wait_for_wireless_connect_candidates(self, *args, **kwargs) -> list[str]:
        return []


class CancelDuringPairQrAdb(QrPairingAdb):
    def __init__(self) -> None:
        super().__init__()
        self.received_cancel_event = None

    def pair_wireless_target(self, target: str, pairing_code: str, cancel_event=None) -> CommandResult:
        self.received_cancel_event = cancel_event
        cancel_event.set()
        return successful_result("pair", target, pairing_code)


class CancelWhileWaitingCandidatesQrAdb(QrPairingAdb):
    def _wait_for_wireless_connect_candidates(
        self,
        pairing_target,
        deadline,
        progress_callback=None,
        cancel_event=None,
    ) -> list[str]:
        cancel_event.set()
        return []


class CancelOnFinalReadyWaitQrAdb(QrPairingAdb):
    def __init__(self) -> None:
        super().__init__()
        self.ready_waits = 0

    def _new_wireless_device_serial(
        self,
        before,
        expected_targets=(),
        cancel_event=None,
    ) -> str:
        return ""

    def _wireless_connect_candidates(
        self,
        pairing_target,
        wait_seconds=0.5,
        cancel_event=None,
    ) -> list[str]:
        return [self.MDNS_SERIAL]

    def _wait_for_new_wireless_device(
        self,
        before,
        deadline,
        progress_callback=None,
        cancel_event=None,
        seconds=4.0,
        expected_targets=(),
    ) -> str:
        self.ready_waits += 1
        if self.ready_waits == 8:
            cancel_event.set()
        return ""


class WirelessQrTests(unittest.TestCase):
    def test_qr_payload_repr_never_contains_pairing_credentials(self) -> None:
        payload = WirelessQrPayload(
            "studio-private",
            "QrPassword12",
            "WIFI:T:ADB;S:studio-private;P:QrPassword12;;",
        )

        rendered = repr(payload)
        self.assertEqual(rendered, "WirelessQrPayload()")
        self.assertNotIn("studio-private", rendered)
        self.assertNotIn("QrPassword12", rendered)

    def test_pairing_secret_is_written_to_stdin_and_never_added_to_argv(self) -> None:
        secret = "QrPassword12"
        captured: dict[str, object] = {}

        class RecordingRunner:
            @contextmanager
            def scoped_log_command(self, command, *, sensitive_values=()):
                captured["display_command"] = list(command)
                captured["sensitive_values"] = tuple(sensitive_values)
                yield

            def run_with_input_stream(self, command, *, input_writer, **_kwargs):
                stream = BytesIO()
                input_writer(stream)
                captured["command"] = list(command)
                captured["stdin"] = stream.getvalue()
                result = successful_result("pair", "192.0.2.5:37123", secret)
                result.stdout = f"debug echo: {secret}"
                return result

        adb = ADBClient(SimpleNamespace(adb_path="adb"), RecordingRunner())
        result = adb.pair_wireless_target("192.0.2.5:37123", secret)

        self.assertTrue(result.success)
        self.assertNotIn(secret, captured["command"])
        self.assertNotIn(secret, captured["display_command"])
        self.assertEqual(captured["stdin"], (secret + "\n").encode())
        self.assertEqual(captured["sensitive_values"], (secret,))
        self.assertNotIn(secret, result.command)
        self.assertNotIn(secret, result.stdout)

    def test_pairing_secret_rejects_line_break_injection(self) -> None:
        adb = ADBClient(SimpleNamespace(adb_path="adb"), SimpleNamespace())
        with self.assertRaisesRegex(ValueError, "invalid characters"):
            adb.pair_wireless_target("192.0.2.5:37123", "123456\nsecond-command")

    def test_recognizes_android_mdns_device_serial_as_wireless(self) -> None:
        self.assertTrue(_looks_like_wireless_serial("adb-serial-token._adb-tls-connect._tcp"))
        self.assertTrue(_looks_like_wireless_serial("adb-serial-token._adb-tls-connect._tcp."))
        self.assertTrue(_looks_like_wireless_serial("192.0.2.59:44195"))
        self.assertFalse(_looks_like_wireless_serial("3A131FDJG000SZ"))
        self.assertTrue(is_mdns_wireless_serial("adb-serial-token._adb-tls-connect._tcp"))

    def test_qr_pairing_does_not_add_ip_connection_after_mdns_auto_connect(self) -> None:
        adb = QrPairingAdb()

        result = adb.pair_wireless_qr("studio-pairing-service", "secret", timeout=10)

        self.assertTrue(result.success)
        self.assertIn(QrPairingAdb.MDNS_SERIAL, result.stdout)
        self.assertEqual(adb.connect_targets, [])

    def test_first_qr_pair_ignores_transient_offline_transport_and_connects_mdns(self) -> None:
        adb = FirstRunQrAdb()

        result = adb.pair_wireless_qr("studio-pairing-service", "secret", timeout=10)

        self.assertTrue(result.success)
        self.assertEqual(adb.connect_targets, [FirstRunQrAdb.MDNS_SERIAL])
        self.assertIn(f"connected device: {FirstRunQrAdb.MDNS_SERIAL}", result.stdout)

    def test_qr_pairing_never_reports_success_without_ready_connection(self) -> None:
        result = NoConnectServiceQrAdb().pair_wireless_qr(
            "studio-pairing-service",
            "secret",
            timeout=10,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "connection_not_ready")
        self.assertIn("no ready Wireless ADB connection", result.status)

    def test_qr_does_not_reuse_unrelated_pre_existing_wireless_device(self) -> None:
        adb = PreExistingWirelessQrAdb()

        result = adb.pair_wireless_qr("studio-pairing-service", "secret", timeout=10)

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "connection_not_ready")
        self.assertNotIn(PreExistingWirelessQrAdb.OLD_SERIAL, result.stdout)
        self.assertEqual(adb.connect_targets, [])

    def test_expected_ready_target_may_be_reused_only_when_explicitly_tied_to_attempt(self) -> None:
        adb = PreExistingWirelessQrAdb()

        self.assertEqual(adb._new_wireless_device_serial({adb.OLD_SERIAL}), "")
        self.assertEqual(
            adb._new_wireless_device_serial(
                {adb.OLD_SERIAL},
                expected_targets=[adb.OLD_SERIAL],
            ),
            adb.OLD_SERIAL,
        )

    def test_expected_target_rejects_a_new_unrelated_wireless_transport(self) -> None:
        adb = PreExistingWirelessQrAdb()

        self.assertEqual(
            adb._new_wireless_device_serial(
                set(),
                expected_targets=["adb-current-attempt._adb-tls-connect._tcp"],
            ),
            "",
        )

    def test_connect_discovery_rejects_unrelated_service_when_pairing_host_is_known(self) -> None:
        services = [
            {
                "name": "adb-unrelated._adb-tls-connect._tcp",
                "type": "_adb-tls-connect._tcp",
                "target": "198.51.100.99:40000",
                "source": "zeroconf",
            }
        ]

        self.assertEqual(
            _wireless_connect_candidates_from_services(
                services,
                "203.0.113.10:37000",
            ),
            [],
        )

    def test_pairing_discovery_rejects_an_unrelated_single_studio_service(self) -> None:
        services = [
            {
                "name": "studio-unrelated",
                "type": "_adb-tls-pairing._tcp",
                "target": "198.51.100.99:37000",
                "source": "zeroconf",
            }
        ]

        self.assertIsNone(
            _find_mdns_service(
                services,
                "studio-current",
                "_adb-tls-pairing._tcp",
            )
        )

    def test_qr_cancellation_during_pair_stops_before_connect(self) -> None:
        adb = CancelDuringPairQrAdb()
        cancel_event = threading.Event()

        result = adb.pair_wireless_qr(
            "studio-pairing-service",
            "secret",
            timeout=10,
            cancel_event=cancel_event,
        )

        self.assertIs(adb.received_cancel_event, cancel_event)
        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "cancelled")
        self.assertEqual(adb.connect_targets, [])

    def test_qr_cancellation_while_waiting_candidates_is_not_reported_as_not_ready(self) -> None:
        adb = CancelWhileWaitingCandidatesQrAdb()
        cancel_event = threading.Event()

        result = adb.pair_wireless_qr(
            "studio-pairing-service",
            "secret",
            timeout=10,
            cancel_event=cancel_event,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "cancelled")
        self.assertNotEqual(result.error_type, "connection_not_ready")
        self.assertEqual(adb.connect_targets, [])

    def test_qr_cancellation_on_final_ready_wait_wins_over_attempt_exhaustion(self) -> None:
        adb = CancelOnFinalReadyWaitQrAdb()
        cancel_event = threading.Event()

        with patch("openadb.core.adb.time.sleep", return_value=None):
            result = adb._connect_wireless_qr_target_until_ready(
                [adb.MDNS_SERIAL],
                "192.0.2.59:37001",
                set(),
                time.monotonic() + 30,
                cancel_event=cancel_event,
            )

        self.assertEqual(adb.ready_waits, 8)
        self.assertEqual(len(adb.connect_targets), 8)
        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "cancelled")

    def test_list_devices_forwards_cancellation_and_discards_cancelled_output(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        cancel_event = threading.Event()
        received_events = []

        def run_raw(args, timeout=120, use_serial=True, cancel_event=None):
            received_events.append(cancel_event)
            result = successful_result(*args)
            result.stdout = "device-one\tdevice transport_id:1"
            cancel_event.set()
            return result

        adb.run_raw = run_raw

        self.assertEqual(adb.list_devices(cancel_event=cancel_event), [])
        self.assertEqual(received_events, [cancel_event])

    def test_wireless_normalizers_preserve_runner_cancellation(self) -> None:
        connect_result = successful_result("connect", "203.0.113.10:40000")
        connect_result.success = False
        connect_result.error_type = "cancelled"
        connect_result.status = "Cancelled"
        pair_result = successful_result("pair", "203.0.113.10:37000")
        pair_result.success = False
        pair_result.error_type = "cancelled"
        pair_result.status = "Cancelled"

        self.assertIs(
            _normalize_adb_connect_result(connect_result, "203.0.113.10:40000"),
            connect_result,
        )
        self.assertEqual(connect_result.error_type, "cancelled")
        self.assertIs(
            _normalize_adb_pair_result(pair_result, "203.0.113.10:37000"),
            pair_result,
        )
        self.assertEqual(pair_result.error_type, "cancelled")

    def test_wireless_normalizers_refine_ordinary_command_failures(self) -> None:
        connect_result = successful_result("connect", "203.0.113.10:40000")
        connect_result.success = False
        connect_result.exit_code = 1
        connect_result.stderr = "failed to connect to 203.0.113.10:40000"
        connect_result.error_type = "command_failed"
        pair_result = successful_result("pair", "203.0.113.10:37000")
        pair_result.success = False
        pair_result.exit_code = 1
        pair_result.stderr = "Failed: pairing refused"
        pair_result.error_type = "command_failed"

        _normalize_adb_connect_result(connect_result, "203.0.113.10:40000")
        _normalize_adb_pair_result(pair_result, "203.0.113.10:37000")

        self.assertEqual(connect_result.error_type, "connection_failed")
        self.assertEqual(pair_result.error_type, "pairing_failed")

    def test_offline_wireless_serial_is_not_ready(self) -> None:
        adb = QrPairingAdb()
        adb.list_devices = lambda cancel_event=None: [
            DeviceInfo(serial=QrPairingAdb.MDNS_SERIAL, mode="Offline", state="offline")
        ]

        self.assertEqual(adb._wireless_device_serials(), set())


if __name__ == "__main__":
    unittest.main()
