from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from openadb.core.backup_manager import BackupManager
from openadb.core.backup_operation_coordinator import BackupOperationCoordinator
from openadb.core.device_context import (
    DeviceContext,
    DeviceContextUnavailable,
    StaleDeviceContext,
)
from openadb.models.app_info import AppInfo
from openadb.models.backup_info import BackupInfo
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo


class ProfileSettings:
    def __init__(self, root: Path) -> None:
        self.config_dir = root
        self.backups_folder = root / "backups"
        self.temp_folder = root / "temp"
        self.logs_folder = root / "logs"


def successful_result(command: list[str], serial: str) -> CommandResult:
    now = datetime.now()
    return CommandResult(
        command=command,
        exit_code=0,
        stdout="Success",
        stderr="",
        duration=0.01,
        started_at=now,
        finished_at=now,
        success=True,
        status="Success",
        device_serial=serial,
    )


class BoundAdb:
    def __init__(self, owner: "RecordingAdb", context: DeviceContext) -> None:
        self.owner = owner
        self.device_context = context
        self.serial = context.serial

    def install_apk(self, apk_path: Path, cancel_event=None) -> CommandResult:
        self.owner.calls.append(("install_apk", self.serial, cancel_event))
        return successful_result(["adb", "-s", self.serial, "install", str(apk_path)], self.serial)

    def install_multiple(self, apk_paths, cancel_event=None) -> CommandResult:
        self.owner.calls.append(("install_multiple", self.serial, cancel_event))
        return successful_result(["adb", "-s", self.serial, "install-multiple"], self.serial)

    def restore_existing_package(self, package_name: str, cancel_event=None) -> CommandResult:
        self.owner.calls.append(("install_existing", self.serial, cancel_event))
        return successful_result(
            ["adb", "-s", self.serial, "shell", "cmd", "package", "install-existing", package_name],
            self.serial,
        )

    def get_package_path(self, package_name: str, cancel_event=None) -> list[str]:
        self.owner.calls.append(("get_package_path", self.serial, cancel_event))
        return [f"/data/app/{package_name}/base.apk"]

    def pull(self, remote: str, local: Path, timeout=300, cancel_event=None) -> CommandResult:
        self.owner.calls.append(("pull", self.serial, cancel_event))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"apk")
        return successful_result(["adb", "-s", self.serial, "pull", remote, str(local)], self.serial)


class RecordingAdb:
    def __init__(self, serial: str = "device-A") -> None:
        self.serial = serial
        self.calls: list[tuple[str, str, object]] = []
        self.bound_contexts: list[DeviceContext] = []

    def for_context(self, context: DeviceContext) -> BoundAdb:
        self.bound_contexts.append(context)
        return BoundAdb(self, context)


class ContextDeviceManager:
    def __init__(self, settings: ProfileSettings, serial: str = "device-A") -> None:
        self.settings = settings
        self.active = DeviceInfo(
            serial=serial,
            model=f"Model {serial}",
            android_version="16",
            mode="ADB",
            state="device",
            transport_id=f"transport-{serial}",
        )
        self.current_generation = 1
        self.require_context_calls = 0

    def context(self) -> DeviceContext:
        return DeviceContext(
            serial=self.active.serial,
            mode=self.active.mode,
            transport_id=self.active.transport_id,
            profile_key=self.active.serial,
            profile_kind="Phone",
            profile_path=Path(self.settings.config_dir),
            backups_path=Path(self.settings.backups_folder),
            temp_path=Path(self.settings.temp_folder),
            logs_path=Path(self.settings.logs_folder),
            generation=self.current_generation,
        )

    def require_context(self, allowed_modes=None) -> DeviceContext:
        self.require_context_calls += 1
        context = self.context()
        if allowed_modes is not None and context.mode not in set(allowed_modes):
            raise RuntimeError("unsupported mode")
        return context

    def is_context_current(self, context: DeviceContext) -> bool:
        return context == self.context()

    def require_current(self, context: DeviceContext) -> DeviceContext:
        if not self.is_context_current(context):
            raise StaleDeviceContext("device changed")
        return context

    def switch(self, serial: str) -> None:
        self.current_generation += 1
        self.active = DeviceInfo(
            serial=serial,
            model=f"Model {serial}",
            android_version="16",
            mode="ADB",
            state="device",
            transport_id=f"transport-{serial}",
        )

    def reconnect(self, transport_id: str) -> None:
        self.current_generation += 1
        self.active = DeviceInfo(
            serial=self.active.serial,
            model=self.active.model,
            android_version=self.active.android_version,
            mode="ADB",
            state="device",
            transport_id=transport_id,
        )


class BackupOperationCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = ProfileSettings(self.root)
        self.manager = BackupManager(self.settings)  # type: ignore[arg-type]
        self.adb = RecordingAdb()
        self.devices = ContextDeviceManager(self.settings)
        self.coordinator = BackupOperationCoordinator(
            self.manager,
            self.adb,  # type: ignore[arg-type]
            self.devices,  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_backup(self, package: str = "com.example.demo") -> BackupInfo:
        backup_path = Path(self.settings.backups_folder) / package / "one"
        backup_path.mkdir(parents=True)
        (backup_path / "base.apk").write_bytes(b"apk")
        (backup_path / "metadata.json").write_text(
            json.dumps(
                {
                    "package_name": package,
                    "app_label": "Demo",
                    "apk_files": ["base.apk"],
                }
            ),
            encoding="utf-8",
        )
        return BackupInfo(
            path=backup_path,
            package_name=package,
            apk_files=["base.apk"],
            metadata_exists=True,
        )

    def test_local_scan_metadata_open_and_delete_need_no_device_context(self) -> None:
        backup = self.make_backup()
        profile = self.coordinator.capture_local_profile()

        scanned = self.coordinator.scan_backups(profile)
        metadata = self.coordinator.metadata_text(profile, backup)
        folder = self.coordinator.folder_to_open(profile, backup)
        deleted = self.coordinator.delete_local_backup(profile, backup)

        self.assertEqual([item.package_name for item in scanned], [backup.package_name])
        self.assertIn('"package_name": "com.example.demo"', metadata)
        self.assertEqual(folder, backup.path)
        self.assertTrue(deleted)
        self.assertFalse(backup.path.exists())
        self.assertEqual(self.devices.require_context_calls, 0)

    def test_cancelled_local_delete_keeps_complete_backup(self) -> None:
        backup = self.make_backup()
        cancel_event = threading.Event()
        cancel_event.set()

        deleted = self.coordinator.delete_local_backup(
            self.coordinator.capture_local_profile(),
            backup,
            cancel_event=cancel_event,
        )

        self.assertFalse(deleted)
        self.assertTrue(backup.path.exists())
        self.assertEqual(self.devices.require_context_calls, 0)

    def test_profile_change_blocks_local_delete_without_requiring_device(self) -> None:
        backup = self.make_backup()
        profile = self.coordinator.capture_local_profile()
        self.settings.backups_folder = self.root / "new-profile-backups"

        with self.assertRaises(StaleDeviceContext):
            self.coordinator.delete_local_backup(profile, backup)

        self.assertTrue(backup.path.exists())
        self.assertEqual(self.devices.require_context_calls, 0)

    def test_restore_uses_captured_serial_even_if_mutable_adb_changes(self) -> None:
        backup = self.make_backup()
        operation = self.coordinator.capture_device_operation()
        cancel_event = threading.Event()
        self.adb.serial = "device-B"

        result = self.coordinator.install_backup(
            operation,
            backup,
            cancel_event=cancel_event,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.device_serial, "device-A")
        self.assertEqual(self.adb.bound_contexts, [operation.context])
        self.assertEqual(self.adb.calls, [("install_apk", "device-A", cancel_event)])

    def test_same_serial_reconnect_rejects_old_transport_identity(self) -> None:
        backup = self.make_backup()
        old_operation = self.coordinator.capture_device_operation()
        self.devices.reconnect("transport-device-A-reconnected")

        with self.assertRaises(StaleDeviceContext):
            self.coordinator.restore_backup(old_operation, backup)

        new_operation = self.coordinator.capture_device_operation()
        result = self.coordinator.install_backup(new_operation, backup)
        self.assertTrue(result.success)
        self.assertEqual(old_operation.context.serial, new_operation.context.serial)
        self.assertNotEqual(
            old_operation.context.transport_id,
            new_operation.context.transport_id,
        )
        self.assertEqual(
            self.adb.bound_contexts,
            [old_operation.context, new_operation.context],
        )
        self.assertEqual([call[:2] for call in self.adb.calls], [("install_apk", "device-A")])

    def test_mutable_serial_only_adb_is_rejected(self) -> None:
        class MutableSerialAdb:
            serial = "device-A"

        coordinator = BackupOperationCoordinator(
            self.manager,
            MutableSerialAdb(),  # type: ignore[arg-type]
            self.devices,  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(DeviceContextUnavailable, "immutable device context"):
            coordinator.capture_device_operation()

    def test_mutable_active_device_is_not_synthesized_into_context(self) -> None:
        class LegacyDeviceManager:
            def __init__(self, settings) -> None:
                self.settings = settings
                self.active = DeviceInfo(serial="device-A", mode="ADB")

        coordinator = BackupOperationCoordinator(
            self.manager,
            self.adb,  # type: ignore[arg-type]
            LegacyDeviceManager(self.settings),  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(DeviceContextUnavailable, "immutable device context"):
            coordinator.capture_device_operation()

        self.assertEqual(self.adb.bound_contexts, [])

    def test_serial_only_for_context_binding_is_rejected(self) -> None:
        class SerialBoundAdb:
            def __init__(self, serial: str) -> None:
                self.serial = serial

        class SerialOnlyFactoryAdb:
            def for_context(self, context: DeviceContext) -> SerialBoundAdb:
                return SerialBoundAdb(context.serial)

        coordinator = BackupOperationCoordinator(
            self.manager,
            SerialOnlyFactoryAdb(),  # type: ignore[arg-type]
            self.devices,  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(DeviceContextUnavailable, "complete captured"):
            coordinator.capture_device_operation()

    def test_install_existing_uses_captured_serial(self) -> None:
        backup = self.make_backup()
        operation = self.coordinator.capture_device_operation()

        result = self.coordinator.install_existing(operation, backup)

        self.assertTrue(result.success)
        self.assertEqual(result.device_serial, "device-A")
        self.assertEqual(self.adb.calls[0][:2], ("install_existing", "device-A"))

    def test_context_invalidation_blocks_restore_before_adb(self) -> None:
        backup = self.make_backup()
        operation = self.coordinator.capture_device_operation()
        self.devices.switch("device-B")

        with self.assertRaises(StaleDeviceContext):
            self.coordinator.restore_backup(operation, backup)

        self.assertEqual(self.adb.calls, [])

    def test_context_change_during_capture_rejects_bound_operation(self) -> None:
        def switching_factory(_profile):
            self.devices.switch("device-B")
            return self.manager

        with self.assertRaises(StaleDeviceContext):
            self.coordinator.capture_device_operation(
                manager_factory=switching_factory
            )

        self.assertEqual(self.adb.calls, [])

    def test_cancelled_restore_returns_cancel_result_without_adb(self) -> None:
        backup = self.make_backup()
        operation = self.coordinator.capture_device_operation()
        cancel_event = threading.Event()
        cancel_event.set()
        self.devices.switch("device-B")

        result = self.coordinator.restore_backup(
            operation,
            backup,
            cancel_event=cancel_event,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "cancelled")
        self.assertEqual(self.adb.calls, [])

    def test_restore_preserves_unsafe_apk_path_protection(self) -> None:
        backup = self.make_backup()
        outside_apk = self.root / "outside.apk"
        outside_apk.write_bytes(b"outside")
        backup.apk_files = [str(outside_apk)]
        operation = self.coordinator.capture_device_operation()

        result = self.coordinator.restore_backup(operation, backup)

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "unsafe_backup_apk_path")
        self.assertEqual(self.adb.calls, [])

    def test_create_backup_uses_bound_context_and_matching_device_metadata(self) -> None:
        operation = self.coordinator.capture_device_operation()
        cancel_event = threading.Event()

        ok, backup, _message = self.coordinator.create_backup(
            operation,
            AppInfo(package_name="com.example.created"),
            DeviceInfo(serial="device-A", model="Model A", android_version="16"),
            "adb shell pm uninstall --user 0",
            cancel_event=cancel_event,
        )

        self.assertTrue(ok)
        self.assertIsNotNone(backup)
        self.assertTrue(backup.path.is_relative_to(Path(self.settings.backups_folder)))
        self.assertEqual(
            [call[:2] for call in self.adb.calls],
            [("get_package_path", "device-A"), ("pull", "device-A")],
        )
        self.assertTrue(all(call[2] is cancel_event for call in self.adb.calls))

    def test_create_rejects_metadata_from_another_device(self) -> None:
        operation = self.coordinator.capture_device_operation()

        with self.assertRaises(StaleDeviceContext):
            self.coordinator.create_backup(
                operation,
                AppInfo(package_name="com.example.wrong"),
                DeviceInfo(serial="device-B"),
                "backup only",
            )

        self.assertEqual(self.adb.calls, [])

    def test_restore_error_does_not_leave_coordinator_busy(self) -> None:
        backup = self.make_backup()

        class FailingManager:
            def restore_backup(self, *args, **kwargs):
                raise OSError("backup drive disconnected")

        operation = self.coordinator.capture_device_operation(
            manager_factory=lambda _profile: FailingManager()
        )
        with self.assertRaisesRegex(OSError, "disconnected"):
            self.coordinator.restore_backup(operation, backup)

        # The coordinator owns no implicit busy state: an error cannot poison
        # the next independently captured operation.
        next_operation = self.coordinator.capture_device_operation()
        result = self.coordinator.restore_backup(next_operation, backup)
        self.assertTrue(result.success)


if __name__ == "__main__":
    unittest.main()
