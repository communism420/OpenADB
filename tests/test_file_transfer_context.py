from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.file_transfer import FileTransferManager


def context(serial: str, transport_id: str, generation: int) -> DeviceContext:
    root = Path("profiles") / serial
    return DeviceContext(
        serial=serial,
        mode="ADB",
        transport_id=transport_id,
        profile_key=serial,
        profile_kind="Phone",
        profile_path=root,
        backups_path=root / "backups",
        temp_path=root / "temp",
        logs_path=root / "logs",
        generation=generation,
    )


class RecordingADB:
    def __init__(self) -> None:
        self.bound_contexts: list[DeviceContext] = []
        self.calls: list[tuple] = []

    def for_context(self, captured: DeviceContext):
        self.bound_contexts.append(captured)
        owner = self

        class Bound:
            def push(self, source, destination, *, timeout, cancel_event):
                owner.calls.append(
                    ("push", captured.serial, captured.transport_id, source, destination, timeout, cancel_event)
                )
                return SimpleNamespace(success=True)

            def pull(self, source, destination, *, timeout, cancel_event):
                owner.calls.append(
                    ("pull", captured.serial, captured.transport_id, source, destination, timeout, cancel_event)
                )
                return SimpleNamespace(success=True)

        return Bound()


class FileTransferContextTests(unittest.TestCase):
    def test_bound_transfer_keeps_captured_transport_after_active_switch(self) -> None:
        adb = RecordingADB()
        manager = FileTransferManager(adb)  # type: ignore[arg-type]
        captured = context("device-a", "transport-1", 3)
        transfer = manager.for_context(captured)

        # A queued transfer owns the immutable context even if application state
        # later points at a same-serial replacement transport.
        replacement = context("device-a", "transport-2", 4)
        self.assertNotEqual(captured.transport_id, replacement.transport_id)
        cancel_event = threading.Event()
        transfer.push(
            "demo.bin",
            "/sdcard/demo.bin",
            cancel_event=cancel_event,
        )

        self.assertEqual(adb.bound_contexts, [captured])
        self.assertEqual(adb.calls[0][0:3], ("push", "device-a", "transport-1"))
        self.assertEqual(adb.calls[0][-2], 300)
        self.assertIs(adb.calls[0][-1], cancel_event)

    def test_unbound_legacy_facade_requires_a_context_source(self) -> None:
        manager = FileTransferManager(RecordingADB())  # type: ignore[arg-type]

        with self.assertRaises(DeviceContextUnavailable):
            manager.pull("/sdcard/demo.bin", "demo.bin")

    def test_facade_captures_context_once_and_forwards_timeout_and_cancel(self) -> None:
        adb = RecordingADB()
        captured = context("device-a", "transport-9", 8)

        class Devices:
            calls = 0

            def require_context(self, allowed_modes=None):
                self.calls += 1
                self.allowed_modes = allowed_modes
                return captured

        devices = Devices()
        manager = FileTransferManager(adb, devices)  # type: ignore[arg-type]
        cancel_event = threading.Event()

        manager.pull(
            "/sdcard/demo.bin",
            "demo.bin",
            timeout=45,
            cancel_event=cancel_event,
        )

        self.assertEqual(devices.calls, 1)
        self.assertEqual(devices.allowed_modes, {"ADB", "Recovery"})
        self.assertEqual(adb.calls[0][0:3], ("pull", "device-a", "transport-9"))
        self.assertEqual(adb.calls[0][-2], 45)
        self.assertIs(adb.calls[0][-1], cancel_event)


if __name__ == "__main__":
    unittest.main()
