from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.device_context import DeviceContext, StaleDeviceContext
from openadb.core.icon_extractor import IconExtractor
from openadb.core.operations import OperationRegistry
from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.models.backup_info import BackupInfo
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.ui.apps_page import AppsPage
from openadb.ui.backups_page import BackupsPage


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


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


class ContextDeviceManager:
    def __init__(self, settings: SettingsManager, adb: "RecordingAdb", serial: str = "A") -> None:
        self.settings = settings
        self.adb = adb
        self.operations = OperationRegistry()
        self.active = DeviceInfo(
            serial=serial,
            model=f"Device {serial}",
            android_version="16",
            mode="ADB",
            state="device",
            transport_id=f"transport-{serial}",
        )
        self.current_generation = 1

    def _context(self) -> DeviceContext:
        profile = Path(self.settings.config_dir)
        return DeviceContext(
            serial=self.active.serial,
            mode=self.active.mode,
            transport_id=self.active.transport_id,
            profile_key=self.active.serial,
            profile_kind="Phone",
            profile_path=profile,
            backups_path=Path(self.settings.backups_folder),
            temp_path=Path(self.settings.temp_folder),
            logs_path=Path(self.settings.logs_folder),
            generation=self.current_generation,
        )

    def require_context(self, allowed_modes=None) -> DeviceContext:
        context = self._context()
        if allowed_modes is not None and context.mode not in set(allowed_modes):
            raise RuntimeError("Unsupported mode")
        return context

    def is_context_current(self, context: DeviceContext) -> bool:
        current = self._context()
        return (
            context.generation == current.generation
            and context.serial == current.serial
            and context.mode == current.mode
            and context.transport_id == current.transport_id
            and context.profile_path == current.profile_path
        )

    def require_current(self, context: DeviceContext) -> DeviceContext:
        if not self.is_context_current(context):
            raise StaleDeviceContext("device changed during operation")
        return context

    def switch_device(self, serial: str) -> None:
        self.current_generation += 1
        self.active = DeviceInfo(
            serial=serial,
            model=f"Device {serial}",
            android_version="16",
            mode="ADB",
            state="device",
            transport_id=f"transport-{serial}",
        )
        self.adb.serial = serial
        self.operations.cancel_stale(self.current_generation, "test device switch")


class BoundRecordingAdb:
    def __init__(self, source: "RecordingAdb", context: DeviceContext) -> None:
        self.source = source
        self.device_context = context
        self.serial = context.serial

    def list_packages(self, include_system: bool = True, load_details: bool = False, cancel_event=None):
        self.source.calls.append(("list_packages", self.serial))
        self.source.list_cancel_events.append(cancel_event)
        callback = self.source.on_list_packages
        self.source.on_list_packages = None
        if callback is not None:
            callback()
        if cancel_event is not None and cancel_event.is_set():
            return []
        return list(self.source.apps)

    def root_available(self, cancel_event=None) -> bool:
        self.source.calls.append(("root_available", self.serial))
        return False

    def get_package_path(self, package_name: str, cancel_event=None) -> list[str]:
        self.source.calls.append(("get_package_path", self.serial))
        callback = self.source.on_package_path
        self.source.on_package_path = None
        if callback is not None:
            callback()
        return [f"/data/app/{package_name}/base.apk"]

    def pull(
        self,
        remote: str,
        local: Path,
        timeout: int = 300,
        cancel_event=None,
    ) -> CommandResult:
        self.source.calls.append(("pull", self.serial))
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"demo-apk")
        return successful_result(["adb", "-s", self.serial, "pull", remote, str(local)], self.serial)

    def uninstall_package(
        self,
        package_name: str,
        system_app: bool = False,
        use_root: bool = False,
        cancel_event=None,
    ):
        self.source.calls.append(("uninstall", self.serial))
        return successful_result(["adb", "-s", self.serial, "uninstall", package_name], self.serial)

    def install_apk(self, apk_path: Path, cancel_event=None):
        self.source.calls.append(("install_apk", self.serial))
        return successful_result(["adb", "-s", self.serial, "install", str(apk_path)], self.serial)

    def install_multiple(self, apk_paths, cancel_event=None):
        self.source.calls.append(("install_multiple", self.serial))
        return successful_result(["adb", "-s", self.serial, "install-multiple"], self.serial)

    def restore_existing_package(self, package_name: str, cancel_event=None):
        self.source.calls.append(("install_existing", self.serial))
        return successful_result(["adb", "-s", self.serial, "install-existing", package_name], self.serial)


class RecordingAdb:
    def __init__(self) -> None:
        self.serial = "A"
        self.apps: list[AppInfo] = []
        self.calls: list[tuple[str, str]] = []
        self.on_package_path = None
        self.on_list_packages = None
        self.list_cancel_events: list[threading.Event | None] = []

    def for_context(self, context: DeviceContext) -> BoundRecordingAdb:
        return BoundRecordingAdb(self, context)


class AppsAndBackupsContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = IsolatedSettings(Path(self.temp.name))
        self.assertTrue(self.settings.activate_device_profile("A", "Device A", "Phone"))
        self.adb = RecordingAdb()
        self.devices = ContextDeviceManager(self.settings, self.adb)
        self.started_workers = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def capture_worker(self, _owner, _pool, worker, **_kwargs) -> bool:
        self.started_workers.append(worker)
        return True

    def switch_profile(self, serial: str) -> None:
        self.assertTrue(self.settings.activate_device_profile(serial, f"Device {serial}", "Phone"))
        self.devices.switch_device(serial)

    def make_apps_page(self) -> AppsPage:
        page = AppsPage(
            self.adb,
            BackupManager(self.settings),
            self.devices,
            IconExtractor(self.settings),
            self.settings,
        )
        page.show()
        self.qt_app.processEvents()
        return page

    def test_stale_apps_list_does_not_replace_new_profile_or_write_its_cache(self) -> None:
        page = self.make_apps_page()
        old = AppInfo(package_name="com.example.old", app_label="Old device app")
        new = AppInfo(package_name="com.example.new", app_label="New device app")
        with patch("openadb.ui.apps_page.start_worker", side_effect=self.capture_worker):
            page.refresh_apps()
        old_worker = self.started_workers.pop()

        self.switch_profile("B")
        page.refresh_storage_roots()
        page.reset_for_device_profile()
        page.apps = [new]
        page.table.set_apps_sorted([new], "name")
        old_worker.signals.result.emit([old])
        self.qt_app.processEvents()

        self.assertEqual([app.package_name for app in page.table.apps], ["com.example.new"])
        self.assertEqual(list((Path(self.settings.config_dir) / "app-cache").glob("*.json")), [])
        old_worker.signals.finished.emit()
        page.close()

    def test_cache_write_uses_captured_profile_cache_and_serial(self) -> None:
        page = self.make_apps_page()
        context = self.devices.require_context()
        services = page._profile_services(context, include_system=True)
        app = AppInfo(package_name="com.example.captured", app_label="Captured")
        page.apps = [app]
        page.table.set_apps_sorted([app], "name")
        decoy_cache = Path(self.temp.name) / "decoy-cache"
        decoy_cache.mkdir()
        page.app_cache.cache_dir = decoy_cache

        page._save_app_cache_from_table(
            context,
            services.app_cache,
            include_system=True,
        )

        cached, _saved_at = services.app_cache.load(context.serial, True)
        self.assertEqual([item.package_name for item in cached], [app.package_name])
        self.assertTrue((context.profile_path / "app-cache" / "A_all.json").is_file())
        self.assertEqual(list(decoy_cache.glob("*.json")), [])
        page.close()

    def test_stale_metadata_item_does_not_modify_new_device_table(self) -> None:
        page = self.make_apps_page()
        original = AppInfo(package_name="com.example.shared", app_label="Device A")
        with patch("openadb.ui.apps_page.start_worker", side_effect=self.capture_worker):
            page._load_metadata_background([original])
        worker = self.started_workers.pop()

        self.devices.switch_device("B")
        replacement = AppInfo(package_name="com.example.shared", app_label="Device B")
        page.apps = [replacement]
        page.table.set_apps_sorted([replacement], "name")
        worker.signals.item.emit(
            AppInfo(package_name="com.example.shared", app_label="Stale A metadata", metadata_checked=True)
        )
        self.qt_app.processEvents()

        self.assertEqual(page.table.apps[0].app_label, "Device B")
        worker.signals.finished.emit()
        page.close()

    def test_device_switch_before_registry_insert_rejects_stale_apps_token(self) -> None:
        page = self.make_apps_page()
        context = self.devices.require_context()
        original_register = self.devices.operations.register

        def register_after_switch(*args, **kwargs):
            self.switch_profile("B")
            return original_register(*args, **kwargs)

        with patch.object(
            self.devices.operations,
            "register",
            side_effect=register_after_switch,
        ):
            with self.assertRaises(StaleDeviceContext):
                page._register_operation(context, "metadata", "apps-metadata")

        self.assertEqual(self.devices.operations.active_count, 0)
        page.close()

    def test_profile_switch_during_cache_confirmation_does_not_clear_new_profile(self) -> None:
        page = self.make_apps_page()

        def switch_before_confirm(*_args, **_kwargs):
            self.switch_profile("B")
            return QMessageBox.Ok

        with (
            patch.object(QMessageBox, "warning", side_effect=switch_before_confirm),
            patch.object(page, "_clear_apps_cache_files") as clear_cache,
        ):
            page.clear_apps_cache()

        clear_cache.assert_not_called()
        self.assertIn("device profile", page.status_label.text().lower())
        page.close()

    def test_device_switch_during_path_lookup_cancels_backup_before_pull(self) -> None:
        page = self.make_apps_page()
        app = AppInfo(package_name="com.example.demo", app_label="Demo")
        page.apps = [app]
        page.table.set_apps_sorted([app], "name")
        page.update_device_state(self.devices.active)
        page.table.select_all_visible()
        self.adb.on_package_path = lambda: self.devices.switch_device("B")

        with patch("openadb.ui.apps_page.start_worker", side_effect=self.capture_worker):
            page.backup_selected()
        worker = self.started_workers.pop()
        messages = worker.fn()
        self.assertEqual(messages, [])
        self.assertNotIn(("pull", "A"), self.adb.calls)
        self.assertNotIn(("pull", "B"), self.adb.calls)
        with patch.object(QMessageBox, "information") as information:
            worker.signals.result.emit(messages)
            self.qt_app.processEvents()
            information.assert_not_called()
        worker.signals.finished.emit()
        page.close()

    def test_cancelled_backup_does_not_probe_root(self) -> None:
        self.settings.set("root_mode_enabled", True)
        page = self.make_apps_page()
        app = AppInfo(package_name="com.example.demo", app_label="Demo")
        page.apps = [app]
        page.table.set_apps_sorted([app], "name")
        page.update_device_state(self.devices.active)
        page.table.select_all_visible()

        with patch("openadb.ui.apps_page.start_worker", side_effect=self.capture_worker):
            page.backup_selected()
        worker = self.started_workers.pop()
        token = page._bulk_token
        self.assertIsNotNone(token)
        self.assertIn("device-package-workflow:A", token.conflict_groups)
        self.assertIn("device-exclusive:A", token.conflict_groups)

        self.devices.switch_device("B")
        self.assertEqual(worker.fn(), [])
        self.assertNotIn(("root_available", "A"), self.adb.calls)
        worker.signals.finished.emit()
        self.assertEqual(self.devices.operations.active_count, 0)
        page.close()

    def test_app_bulk_conflicts_with_device_exclusive_operation(self) -> None:
        page = self.make_apps_page()
        app = AppInfo(package_name="com.example.demo", app_label="Demo")
        page.apps = [app]
        page.table.set_apps_sorted([app], "name")
        page.update_device_state(self.devices.active)
        page.table.select_all_visible()
        context = self.devices.require_context()
        blocker = self.devices.operations.register(
            "file-manager.transfer",
            device_context=context,
            conflict_groups=(f"device-exclusive:{context.serial}",),
        )

        try:
            with (
                patch("openadb.ui.apps_page.start_worker") as start_worker,
                patch.object(QMessageBox, "information") as information,
            ):
                page.backup_selected()
            start_worker.assert_not_called()
            information.assert_called_once()
            self.assertIn("device-exclusive:A", information.call_args.args[2])
        finally:
            self.devices.operations.finish(blocker)
            page.close()

    def test_device_switch_after_backup_prevents_uninstall(self) -> None:
        page = self.make_apps_page()
        app = AppInfo(package_name="com.example.demo", app_label="Demo")
        page.apps = [app]
        page.table.set_apps_sorted([app], "name")
        page.update_device_state(self.devices.active)
        page.table.select_all_visible()
        self.adb.on_package_path = lambda: self.devices.switch_device("B")

        with (
            patch.object(page, "_confirm_apps", return_value=True),
            patch("openadb.ui.apps_page.start_worker", side_effect=self.capture_worker),
        ):
            page.uninstall_selected()
        worker = self.started_workers.pop()
        self.assertEqual(worker.fn(), [])
        self.assertNotIn(("pull", "A"), self.adb.calls)
        self.assertNotIn(("uninstall", "A"), self.adb.calls)
        self.assertNotIn(("uninstall", "B"), self.adb.calls)
        worker.signals.finished.emit()
        page.close()

    def test_cancelled_uninstall_does_not_probe_root(self) -> None:
        self.settings.set("root_mode_enabled", True)
        page = self.make_apps_page()
        app = AppInfo(package_name="com.example.demo", app_label="Demo")
        page.apps = [app]
        page.table.set_apps_sorted([app], "name")
        page.update_device_state(self.devices.active)
        page.table.select_all_visible()

        with (
            patch.object(page, "_confirm_apps", return_value=True),
            patch("openadb.ui.apps_page.start_worker", side_effect=self.capture_worker),
        ):
            page.uninstall_selected()
        worker = self.started_workers.pop()

        self.devices.switch_device("B")
        self.assertEqual(worker.fn(), [])
        self.assertNotIn(("root_available", "A"), self.adb.calls)
        self.assertNotIn(("uninstall", "A"), self.adb.calls)
        worker.signals.finished.emit()
        self.assertEqual(self.devices.operations.active_count, 0)
        page.close()

    def test_old_app_rows_cannot_start_a_bulk_operation_on_new_device(self) -> None:
        page = self.make_apps_page()
        app = AppInfo(package_name="com.example.old", app_label="Old device app")
        page.apps = [app]
        page.table.set_apps_sorted([app], "name")
        page.update_device_state(self.devices.active)
        page.table.select_all_visible()

        self.switch_profile("B")
        page.update_device_state(self.devices.active)
        with (
            patch("openadb.ui.apps_page.start_worker") as start_worker,
            patch.object(QMessageBox, "information") as information,
        ):
            page.backup_selected()

        start_worker.assert_not_called()
        information.assert_called_once()
        self.assertIn("another device", information.call_args.args[2].lower())
        self.assertEqual(self.adb.calls, [])
        page.close()

    def test_profile_switch_ignores_old_backup_scan_and_applies_new_scan(self) -> None:
        manager = BackupManager(self.settings)
        old_folder = Path(self.settings.backups_folder) / "com.example.old" / "one"
        old_folder.mkdir(parents=True)
        page = BackupsPage(manager, self.adb, self.devices)
        with patch("openadb.ui.backups_page.start_worker", side_effect=self.capture_worker):
            page.refresh()
            old_worker = self.started_workers.pop()
            self.switch_profile("B")
            old_results = old_worker.fn()
            self.assertEqual([backup.package_name for backup in old_results], ["com.example.old"])
            manager.refresh_root()
            page.refresh()
            new_worker = self.started_workers.pop()

        new_backup = BackupInfo(path=Path(self.temp.name) / "new", package_name="com.example.new")
        old_worker.signals.result.emit(old_results)
        new_worker.signals.result.emit([new_backup])
        self.qt_app.processEvents()
        self.assertEqual([backup.package_name for backup in page.backups], ["com.example.new"])
        old_worker.signals.finished.emit()
        self.assertTrue(page._loading)
        new_worker.signals.finished.emit()
        self.assertFalse(page._loading)
        page.close()

    def test_restore_uses_original_serial_and_suppresses_stale_success(self) -> None:
        manager = BackupManager(self.settings)
        backup_path = Path(self.settings.backups_folder) / "com.example.demo" / "one"
        backup_path.mkdir(parents=True)
        (backup_path / "base.apk").write_bytes(b"demo")
        backup = BackupInfo(
            path=backup_path,
            package_name="com.example.demo",
            apk_files=["base.apk"],
        )
        page = BackupsPage(manager, self.adb, self.devices)
        page._backups_loaded([backup])
        page.table.selectRow(0)

        with patch("openadb.ui.backups_page.start_worker", side_effect=self.capture_worker):
            page.restore_selected(force_apk=True)
        worker = self.started_workers.pop()
        token = page._action_token
        self.assertIsNotNone(token)
        self.assertIn("device-package-workflow:A", token.conflict_groups)
        self.assertIn("device-exclusive:A", token.conflict_groups)
        result = worker.fn()
        self.assertIn(("install_apk", "A"), self.adb.calls)
        self.devices.switch_device("B")
        with patch.object(QMessageBox, "information") as information:
            worker.signals.result.emit(result)
            self.qt_app.processEvents()
            information.assert_not_called()
        worker.signals.finished.emit()
        page.close()

    def test_old_profile_backup_cannot_restore_to_new_device(self) -> None:
        manager = BackupManager(self.settings)
        backup_path = Path(self.settings.backups_folder) / "com.example.old" / "one"
        backup_path.mkdir(parents=True)
        (backup_path / "base.apk").write_bytes(b"demo")
        backup = BackupInfo(
            path=backup_path,
            package_name="com.example.old",
            apk_files=["base.apk"],
        )
        page = BackupsPage(manager, self.adb, self.devices)
        page._backups_loaded([backup])
        page.table.selectRow(0)

        self.switch_profile("B")
        with (
            patch("openadb.ui.backups_page.start_worker") as start_worker,
            patch.object(QMessageBox, "information") as information,
        ):
            page.restore_selected(force_apk=True)

        start_worker.assert_not_called()
        information.assert_called_once()
        self.assertIn("another device profile", information.call_args.args[2].lower())
        self.assertEqual(self.adb.calls, [])
        page.close()

    def test_restore_setup_failure_does_not_leak_operation_or_busy_state(self) -> None:
        manager = BackupManager(self.settings)
        backup_path = Path(self.settings.backups_folder) / "com.example.demo" / "one"
        backup_path.mkdir(parents=True)
        (backup_path / "base.apk").write_bytes(b"demo")
        backup = BackupInfo(
            path=backup_path,
            package_name="com.example.demo",
            apk_files=["base.apk"],
        )
        page = BackupsPage(manager, self.adb, self.devices)
        page._backups_loaded([backup])
        page.table.selectRow(0)

        with (
            patch.object(page, "_manager_for_settings", side_effect=OSError("backup drive unavailable")),
            patch("openadb.ui.backups_page.start_worker") as start_worker,
            patch.object(QMessageBox, "information") as information,
        ):
            page.restore_selected(force_apk=True)

        start_worker.assert_not_called()
        information.assert_called_once()
        self.assertEqual(self.devices.operations.active_count, 0)
        self.assertIsNone(page._action_token)
        self.assertFalse(page._action_busy)
        page.close()

    def test_restore_rejects_apk_path_outside_selected_backup(self) -> None:
        manager = BackupManager(self.settings)
        backup_path = Path(self.settings.backups_folder) / "com.example.demo" / "one"
        backup_path.mkdir(parents=True)
        outside_apk = Path(self.temp.name) / "outside.apk"
        outside_apk.write_bytes(b"not-the-selected-backup")
        traversal = os.path.relpath(outside_apk, backup_path)
        backup = BackupInfo(
            path=backup_path,
            package_name="com.example.demo",
            apk_files=[traversal],
        )

        result = manager.restore_backup(
            backup,
            self.adb.for_context(self.devices.require_context()),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "unsafe_backup_apk_path")
        self.assertIn("unsafe", result.status.lower())
        self.assertEqual(self.adb.calls, [])

    def test_cancelling_during_second_split_removes_partial_backup(self) -> None:
        manager = BackupManager(self.settings)
        cancel_event = threading.Event()
        pulls: list[str] = []
        work_dirs: list[Path] = []

        class SplitBackupAdb:
            def get_package_path(self, _package_name: str, cancel_event=None) -> list[str]:
                return ["/data/app/base.apk", "/data/app/split_config.en.apk"]

            def pull(self, remote: str, local: Path, timeout=300, cancel_event=None):
                pulls.append(remote)
                work_dirs.append(local.parent)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(b"partial split backup")
                if len(pulls) == 2:
                    cancel_event.set()
                return successful_result(["adb", "pull", remote, str(local)], "A")

        ok, backup, message = manager.create_backup(
            AppInfo(package_name="com.example.split", app_label="Split app"),
            SplitBackupAdb(),
            self.devices.active,
            "pm uninstall",
            cancel_event=cancel_event,
        )

        self.assertFalse(ok)
        self.assertIsNone(backup)
        self.assertIn("cancelled", message.lower())
        self.assertEqual(pulls, ["/data/app/base.apk", "/data/app/split_config.en.apk"])
        self.assertTrue(work_dirs)
        self.assertTrue(all(not path.exists() for path in work_dirs))
        self.assertEqual(manager.scan_backups(), [])

        orphan = Path(self.settings.backups_folder) / "com.example.split" / ".partial-orphan"
        orphan.mkdir(parents=True)
        (orphan / "base.apk").write_bytes(b"orphan")
        self.assertEqual(manager.scan_backups(), [])

    def test_cancelled_backup_scan_stops_before_reading_second_entry(self) -> None:
        manager = BackupManager(self.settings)
        package_dir = Path(self.settings.backups_folder) / "com.example.scan"
        for name in ("one", "two"):
            backup_dir = package_dir / name
            backup_dir.mkdir(parents=True)
            (backup_dir / "base.apk").write_bytes(b"demo")
        cancel_event = threading.Event()
        reads: list[Path] = []
        original_read = manager._read_backup

        def read_then_cancel(path: Path):
            reads.append(path)
            result = original_read(path)
            cancel_event.set()
            return result

        with patch.object(manager, "_read_backup", side_effect=read_then_cancel):
            backups = manager.scan_backups(cancel_event=cancel_event)

        self.assertEqual(backups, [])
        self.assertEqual(len(reads), 1)

    def test_cancelled_package_list_stops_after_current_adb_command(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        cancel_event = threading.Event()
        commands: list[str] = []

        def run_shell(command: str, timeout=120, cancel_event=None):
            commands.append(command)
            result = successful_result(["adb", "shell", command], "A")
            result.stdout = "package:/data/app/com.example.demo/base.apk=com.example.demo"
            cancel_event.set()
            return result

        adb.run_shell = run_shell

        self.assertEqual(adb.list_packages(cancel_event=cancel_event), [])
        self.assertEqual(commands, ["pm list packages -f --show-versioncode"])

    def test_bound_adb_pins_captured_transport_when_same_serial_reconnects(self) -> None:
        class Tools:
            adb_path = Path("adb.exe")

        class Runner:
            def for_context(self, _context):
                return self

        adb = ADBClient.__new__(ADBClient)
        adb.platform_tools = Tools()
        adb.runner = Runner()
        adb._serial = "shared-serial"
        profile = Path(self.settings.config_dir)

        def context(transport_id: str, generation: int) -> DeviceContext:
            return DeviceContext(
                serial="shared-serial",
                mode="ADB",
                transport_id=transport_id,
                profile_key="shared-serial",
                profile_kind="Phone",
                profile_path=profile,
                backups_path=Path(self.settings.backups_folder),
                temp_path=Path(self.settings.temp_folder),
                logs_path=Path(self.settings.logs_folder),
                generation=generation,
            )

        first = adb.for_context(context("1", 1))
        second = adb.for_context(context("2", 2))

        self.assertEqual(first._base(), ["adb.exe", "-t", "1"])
        self.assertEqual(second._base(), ["adb.exe", "-t", "2"])
        self.assertEqual(first._base(), ["adb.exe", "-t", "1"])
        self.assertEqual(adb.for_serial("shared-serial")._base(), ["adb.exe", "-s", "shared-serial"])
        self.assertEqual(adb._base(serial=""), ["adb.exe"])
        with self.assertRaises(RuntimeError):
            first.run_raw(["devices"], use_serial=False)

    def test_cancelled_bulk_package_queries_do_not_start_next_chunk(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        cancel_event = threading.Event()
        commands: list[str] = []

        def run_shell(command: str, timeout=120, cancel_event=None):
            commands.append(command)
            result = successful_result(["adb", "shell", command], "A")
            cancel_event.set()
            return result

        adb.run_shell = run_shell
        packages = ["com.example.one", "com.example.two"]

        self.assertEqual(
            adb.get_package_paths_bulk(packages, chunk_size=1, cancel_event=cancel_event),
            {},
        )
        self.assertEqual(len(commands), 1)

        cancel_event.clear()
        commands.clear()
        self.assertEqual(
            adb.get_package_sizes_bulk(packages, chunk_size=1, cancel_event=cancel_event),
            {},
        )
        self.assertEqual(len(commands), 1)

    def test_cancelled_parallel_package_details_emit_no_partial_results(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        cancel_event = threading.Event()
        calls: list[str] = []
        progress: list[str] = []

        def get_details(package_name: str, cancel_event=None):
            calls.append(package_name)
            cancel_event.set()
            return {"versionName": "stale"}

        adb.get_package_details = get_details
        details = adb.get_package_details_many(
            ["com.example.one", "com.example.two", "com.example.three"],
            max_workers=1,
            progress_callback=lambda _done, _total, package, _details: progress.append(package),
            cancel_event=cancel_event,
        )

        self.assertEqual(details, {})
        self.assertEqual(calls, ["com.example.one"])
        self.assertEqual(progress, [])

    def test_cancelled_temp_pull_cleans_remote_and_local_staging(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        cancel_event = threading.Event()
        commands: list[str] = []
        with tempfile.TemporaryDirectory() as local_temp:
            target = Path(local_temp) / "base.apk"
            pre_existing = target.parent / f"0_{target.name}"
            pre_existing.write_bytes(b"user file")

            def run_shell(command: str, timeout=120, cancel_event=None):
                commands.append(command)
                return successful_result(["adb", "shell", command], "A")

            def pull(_remote: str, local: Path, timeout=120, cancel_event=None):
                (Path(local) / f"0_{target.name}").write_bytes(b"cancelled partial")
                cancel_event.set()
                return successful_result(["adb", "pull", str(local)], "A")

            adb.run_shell = run_shell
            adb.pull = pull
            results = adb.pull_files_via_temp(
                [("/data/app/com.example/base.apk", target)],
                cancel_event=cancel_event,
            )

            self.assertEqual(results, {target: False})
            self.assertEqual(len(commands), 3)
            self.assertIn("mkdir -p", commands[0])
            self.assertIn("cp ", commands[1])
            self.assertTrue(commands[2].startswith("rm -rf "))
            self.assertEqual(pre_existing.read_bytes(), b"user file")
            self.assertEqual(list(target.parent.glob(".openadb_bulk_*")), [])
            self.assertFalse(target.exists())

    def test_temp_pull_publish_failure_preserves_existing_destination(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        commands: list[str] = []
        pull_calls = 0
        with tempfile.TemporaryDirectory() as local_temp:
            target = Path(local_temp) / "base.apk"
            target.write_bytes(b"existing destination")

            def run_shell(command: str, timeout=120, cancel_event=None):
                commands.append(command)
                return successful_result(["adb", "shell", command], "A")

            def pull(_remote: str, local: Path, timeout=120, cancel_event=None):
                nonlocal pull_calls
                pull_calls += 1
                result = successful_result(["adb", "pull", str(local)], "A")
                if pull_calls == 1:
                    (Path(local) / f"0_{target.name}").write_bytes(b"new content")
                else:
                    result.success = False
                    result.exit_code = 1
                return result

            adb.run_shell = run_shell
            adb.pull = pull
            with patch.object(Path, "replace", side_effect=OSError("publish failed")):
                results = adb.pull_files_via_temp(
                    [("/data/app/com.example/base.apk", target)],
                )

            self.assertEqual(results, {target: False})
            self.assertEqual(target.read_bytes(), b"existing destination")
            self.assertEqual(pull_calls, 2)
            self.assertTrue(commands[-1].startswith("rm -rf "))
            self.assertEqual(list(target.parent.glob(".openadb_bulk_*")), [])

    def test_temp_pull_missing_staged_file_does_not_reuse_existing_destination(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        pull_calls = 0
        with tempfile.TemporaryDirectory() as local_temp:
            target = Path(local_temp) / "base.apk"
            target.write_bytes(b"existing destination")

            adb.run_shell = lambda command, timeout=120, cancel_event=None: successful_result(
                ["adb", "shell", command],
                "A",
            )

            def pull(_remote: str, local: Path, timeout=120, cancel_event=None):
                nonlocal pull_calls
                pull_calls += 1
                return successful_result(["adb", "pull", str(local)], "A")

            adb.pull = pull
            results = adb.pull_files_via_temp(
                [("/data/app/com.example/base.apk", target)],
            )

            self.assertEqual(results, {target: False})
            self.assertEqual(target.read_bytes(), b"existing destination")
            self.assertEqual(pull_calls, 2)
            self.assertEqual(list(target.parent.glob(".openadb_bulk_*")), [])
            self.assertEqual(list(target.parent.glob(".openadb_pull_*")), [])

    def test_temp_pull_atomically_replaces_existing_destination(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        pull_calls = 0
        with tempfile.TemporaryDirectory() as local_temp:
            target = Path(local_temp) / "base.apk"
            target.write_bytes(b"existing destination")

            adb.run_shell = lambda command, timeout=120, cancel_event=None: successful_result(
                ["adb", "shell", command],
                "A",
            )

            def pull(_remote: str, local: Path, timeout=120, cancel_event=None):
                nonlocal pull_calls
                pull_calls += 1
                (Path(local) / f"0_{target.name}").write_bytes(b"new content")
                return successful_result(["adb", "pull", str(local)], "A")

            adb.pull = pull
            results = adb.pull_files_via_temp(
                [("/data/app/com.example/base.apk", target)],
            )

            self.assertEqual(results, {target: True})
            self.assertEqual(target.read_bytes(), b"new content")
            self.assertEqual(pull_calls, 1)
            self.assertEqual(list(target.parent.glob(".openadb_bulk_*")), [])

    def test_apps_list_worker_passes_cancel_event_and_writes_no_stale_cache(self) -> None:
        page = self.make_apps_page()
        self.adb.apps = [AppInfo(package_name="com.example.stale", app_label="Stale")]
        self.adb.on_list_packages = lambda: self.switch_profile("B")
        with patch("openadb.ui.apps_page.start_worker", side_effect=self.capture_worker):
            page.refresh_apps()
        worker = self.started_workers.pop()
        token = page._apps_load_token

        apps = worker.fn()

        self.assertEqual(apps, [])
        self.assertIsNotNone(token)
        self.assertTrue(token.cancelled)
        self.assertEqual(self.adb.list_cancel_events, [token.cancel_event])
        worker.signals.result.emit(apps)
        self.qt_app.processEvents()
        self.assertEqual(page.apps, [])
        self.assertEqual(list(Path(self.settings.config_dir).rglob("*.json")), [self.settings.path])
        worker.signals.finished.emit()
        page.close()

    def test_cancelling_first_uninstall_attempt_prevents_destructive_fallbacks(self) -> None:
        adb = ADBClient.__new__(ADBClient)
        cancel_event = threading.Event()
        attempts: list[str] = []

        def run_shell(command: str, timeout=120, cancel_event=None):
            attempts.append(command)
            result = successful_result(["adb", "shell", command], "A")
            result.success = False
            result.exit_code = 1
            result.status = "Failure"
            cancel_event.set()
            return result

        adb.run_shell = run_shell
        result = adb.uninstall_package(
            "com.example.system",
            system_app=True,
            cancel_event=cancel_event,
        )

        self.assertEqual(len(attempts), 1)
        self.assertEqual(result.error_type, "cancelled")
        self.assertIn("no fallback", result.status.lower())

    def test_cancelled_restore_does_not_start_any_package_install(self) -> None:
        manager = BackupManager(self.settings)
        backup_path = Path(self.settings.backups_folder) / "com.example.cancelled" / "one"
        backup_path.mkdir(parents=True)
        (backup_path / "base.apk").write_bytes(b"demo")
        backup = BackupInfo(
            path=backup_path,
            package_name="com.example.cancelled",
            apk_files=["base.apk"],
        )
        cancel_event = threading.Event()
        cancel_event.set()

        result = manager.restore_backup(
            backup,
            self.adb.for_context(self.devices.require_context()),
            cancel_event=cancel_event,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "cancelled")
        self.assertEqual(self.adb.calls, [])


if __name__ == "__main__":
    unittest.main()
