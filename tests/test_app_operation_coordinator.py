from __future__ import annotations

import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from openadb.core.app_operation_coordinator import AppOperationCoordinator
from openadb.core.device_context import DeviceContext, StaleDeviceContext
from openadb.models.app_info import AppInfo
from openadb.models.device_info import DeviceInfo


def device_context() -> DeviceContext:
    root = Path("profiles") / "device-a"
    return DeviceContext(
        serial="device-a",
        mode="ADB",
        transport_id="transport-7",
        profile_key="device-a",
        profile_kind="Phone",
        profile_path=root,
        backups_path=root / "backups",
        temp_path=root / "temp",
        logs_path=root / "logs",
        generation=11,
    )


class RecordingADB:
    def __init__(self, context: DeviceContext) -> None:
        self.context = context
        self.calls: list[tuple] = []
        self.cancel_after: str = ""

    def root_available(self, *, cancel_event):
        self.calls.append(("root", self.context))
        return False

    def uninstall_package(
        self,
        package_name,
        *,
        system_app,
        use_root,
        cancel_event,
    ):
        self.calls.append(
            ("uninstall", self.context, package_name, system_app, use_root)
        )
        if self.cancel_after == package_name:
            cancel_event.set()
        return SimpleNamespace(status="Uninstalled")

    def enable_package(self, package_name, *, cancel_event):
        self.calls.append(("enable", self.context, package_name))
        if self.cancel_after == package_name:
            cancel_event.set()
        return SimpleNamespace(status="Enabled")

    def disable_package(self, package_name, *, cancel_event):
        self.calls.append(("disable", self.context, package_name))
        return SimpleNamespace(status="Disabled")

    def restore_existing_package(self, package_name, *, cancel_event):
        self.calls.append(("install-existing", self.context, package_name))
        return SimpleNamespace(status="Installed")


class RecordingBackups:
    def __init__(self, context: DeviceContext) -> None:
        self.context = context
        self.calls: list[tuple] = []
        self.after_backup = None
        self.ok = True

    def create_backup(
        self,
        app,
        adb,
        device,
        uninstall_method,
        icon_path,
        *,
        use_root,
        cancel_event,
    ):
        self.calls.append(
            (
                "backup",
                self.context,
                adb.context,
                device.serial,
                app.package_name,
                uninstall_method,
            )
        )
        if self.after_backup is not None:
            self.after_backup()
        return self.ok, None, "created" if self.ok else "failed"


class AppOperationCoordinatorTests(unittest.TestCase):
    def make_coordinator(self):
        context = device_context()
        current = {"value": True}
        guarded: list[DeviceContext] = []

        def require_current(captured: DeviceContext) -> None:
            guarded.append(captured)
            if not current["value"]:
                raise StaleDeviceContext("device switched")

        adb = RecordingADB(context)
        backups = RecordingBackups(context)
        cancel_event = threading.Event()
        coordinator = AppOperationCoordinator(
            context=context,
            adb=adb,
            backup_manager=backups,
            device=DeviceInfo(
                serial=context.serial,
                mode=context.mode,
                transport_id=context.transport_id,
            ),
            cancel_event=cancel_event,
            require_current=require_current,
        )
        return coordinator, adb, backups, cancel_event, current, guarded

    def test_backup_and_uninstall_use_one_captured_context(self) -> None:
        coordinator, adb, backups, _cancel, _current, guarded = self.make_coordinator()
        app = AppInfo(package_name="com.example.demo", app_type="user")

        messages = coordinator.uninstall([app], require_backup=True)

        self.assertEqual(messages, ["com.example.demo: Uninstalled"])
        self.assertEqual(backups.calls[0][1:4], (coordinator.context, coordinator.context, "device-a"))
        self.assertEqual(adb.calls[0][0:3], ("uninstall", coordinator.context, "com.example.demo"))
        self.assertTrue(all(item is coordinator.context for item in guarded))

    def test_context_invalidation_after_backup_blocks_uninstall(self) -> None:
        coordinator, adb, backups, _cancel, current, _guarded = self.make_coordinator()
        backups.after_backup = lambda: current.update(value=False)

        with self.assertRaises(StaleDeviceContext):
            coordinator.uninstall(
                [AppInfo(package_name="com.example.demo")],
                require_backup=True,
            )

        self.assertEqual([call[0] for call in adb.calls], [])

    def test_failed_required_backup_skips_only_that_uninstall(self) -> None:
        coordinator, adb, backups, _cancel, _current, _guarded = self.make_coordinator()
        backups.ok = False

        messages = coordinator.uninstall(
            [AppInfo(package_name="com.example.demo")],
            require_backup=True,
        )

        self.assertIn("skipped, backup failed", messages[0])
        self.assertEqual(adb.calls, [])

    def test_cancelled_bulk_operation_stops_before_next_package(self) -> None:
        coordinator, adb, _backups, cancel, _current, _guarded = self.make_coordinator()
        adb.cancel_after = "com.example.first"

        messages = coordinator.set_enabled(
            [
                AppInfo(package_name="com.example.first"),
                AppInfo(package_name="com.example.second"),
            ],
            enabled=True,
        )

        self.assertTrue(cancel.is_set())
        self.assertEqual(messages, [])
        self.assertEqual(
            [call[2] for call in adb.calls],
            ["com.example.first"],
        )

    def test_install_existing_and_backup_are_gui_independent(self) -> None:
        coordinator, adb, backups, _cancel, _current, _guarded = self.make_coordinator()
        app = AppInfo(package_name="com.example.system", app_type="system")

        backup_messages = coordinator.backup([app])
        install_messages = coordinator.install_existing([app])

        self.assertEqual(backup_messages, ["com.example.system: OK - created"])
        self.assertEqual(install_messages, ["com.example.system: Installed"])
        self.assertEqual(backups.calls[0][-1], "pm uninstall --user 0")
        self.assertEqual(adb.calls[-1][0], "install-existing")


if __name__ == "__main__":
    unittest.main()
