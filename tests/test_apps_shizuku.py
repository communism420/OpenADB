from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from openadb.core.app_operation_coordinator import AppOperationCoordinator
from openadb.core.backup_manager import BackupManager
from openadb.core.device_context import StaleDeviceContext
from openadb.core.icon_extractor import IconExtractor
from openadb.core.privilege import PrivilegeBackend
from openadb.models.app_info import AppInfo
from openadb.ui.apps_page import AppsPage
from tests.test_apps_device_context import (
    BoundRecordingAdb,
    ContextDeviceManager,
    IsolatedSettings,
    RecordingAdb,
)


class RecordingPrivilegeManager:
    def __init__(
        self,
        facade,
        selected_backend: PrivilegeBackend = PrivilegeBackend.SHIZUKU,
    ) -> None:
        self.facade = facade
        self.selected_backend = selected_backend
        self.calls: list[tuple[object, object, int]] = []

    def prepare_adb(self, context, *, cancel_event=None):
        self.calls.append((context, cancel_event, threading.get_ident()))
        return self.facade


class AppsShizukuWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.settings = IsolatedSettings(Path(self.temp.name))
        self.assertTrue(
            self.settings.activate_device_profile("A", "Device A", "Phone")
        )
        self.adb = RecordingAdb()
        self.devices = ContextDeviceManager(self.settings, self.adb)
        self.facade = MagicMock()
        self.facade.effective_privilege_backend = PrivilegeBackend.SHIZUKU
        self.facade.list_packages.return_value = []
        self.privileges = RecordingPrivilegeManager(self.facade)
        self.page = AppsPage(
            self.adb,
            BackupManager(self.settings),
            self.devices,
            IconExtractor(self.settings),
            self.settings,
            privilege_manager=self.privileges,
        )

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        self.qt_app.processEvents()
        self.temp.cleanup()

    def test_constructor_keeps_parent_compatibility_and_stores_optional_manager(self) -> None:
        self.assertIs(self.page.privilege_manager, self.privileges)

        legacy = AppsPage(
            self.adb,
            BackupManager(self.settings),
            self.devices,
            IconExtractor(self.settings),
            self.settings,
        )
        self.assertIsNone(legacy.privilege_manager)
        legacy.close()

    def test_bulk_workflow_receives_the_page_privilege_manager(self) -> None:
        context = self.devices.require_context()
        self.page._set_apps_view_identity(context.serial, context)

        prepared = self.page._prepare_bulk_operation(
            "Test package action",
            "test",
        )

        self.assertIsNotNone(prepared)
        _context, coordinator, token = prepared
        self.assertIs(coordinator.privilege_manager, self.privileges)
        self.page._finish_bulk_operation(token, context)

    def test_package_refresh_prepares_one_facade_inside_worker(self) -> None:
        workers = []

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        with patch(
            "openadb.ui.apps_page.start_worker",
            side_effect=capture_worker,
        ):
            self.page.refresh_apps()

        self.assertEqual(len(workers), 1)
        self.assertEqual(self.privileges.calls, [])
        self.facade.list_packages.assert_not_called()
        expected_cancel_event = self.page._apps_load_token.cancel_event

        worker_thread = threading.get_ident()
        workers[0].run()
        self.qt_app.processEvents()

        self.assertEqual(len(self.privileges.calls), 1)
        context, cancel_event, prepared_thread = self.privileges.calls[0]
        self.assertEqual(context.serial, "A")
        self.assertIs(cancel_event, expected_cancel_event)
        self.assertEqual(prepared_thread, worker_thread)
        self.facade.list_packages.assert_called_once_with(
            include_system=True,
            load_details=False,
            cancel_event=cancel_event,
        )
        self.assertFalse(self.page._apps_loading)
        self.assertEqual(self.page.operations.active_count, 0)

    def test_package_refresh_hands_maintenance_lease_to_asset_worker(self) -> None:
        workers = []
        app = AppInfo(package_name="com.example.assets", app_label="")
        self.facade.list_packages.return_value = [app]

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        with (
            patch(
                "openadb.ui.apps_page.start_worker",
                side_effect=capture_worker,
            ),
            patch("openadb.ui.apps_data_workflow.AppAssetLoader") as loader_type,
        ):
            loader_type.return_value.load.return_value = [app]
            self.page.refresh_apps()
            list_token = self.page._apps_load_token
            self.assertIsNotNone(list_token)
            self.assertEqual(len(workers), 1)

            result = workers[0].fn()
            workers[0].signals.result.emit(result)
            self.qt_app.processEvents()

            self.assertEqual(len(workers), 1)
            self.assertTrue(self.page.operations.contains(list_token))
            self.assertIsNotNone(self.page._pending_app_asset_refresh)

            workers[0].signals.finished.emit()
            self.qt_app.processEvents()

            self.assertEqual(len(workers), 2)
            self.assertFalse(self.page.operations.contains(list_token))
            self.assertIsNone(self.page._pending_app_asset_refresh)
            self.assertIsNotNone(self.page._assets_token)
            self.assertEqual(self.page._assets_token.owner_key, "apps.assets")
            self.assertEqual(self.page.operations.active_count, 1)

            workers[1].run()
            self.qt_app.processEvents()

        loader_type.return_value.load.assert_called_once()
        self.assertFalse(self.page._apps_loading)
        self.assertFalse(self.page._assets_loading)
        self.assertEqual(self.page.operations.active_count, 0)

    def test_cancelled_package_refresh_discards_pending_asset_worker(self) -> None:
        workers = []
        app = AppInfo(package_name="com.example.cancelled", app_label="")
        self.facade.list_packages.return_value = [app]

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        with patch(
            "openadb.ui.apps_page.start_worker",
            side_effect=capture_worker,
        ):
            self.page.refresh_apps()
            token = self.page._apps_load_token
            self.assertIsNotNone(token)

            result = workers[0].fn()
            workers[0].signals.result.emit(result)
            self.assertIsNotNone(self.page._pending_app_asset_refresh)

            token.cancel("device changed")
            workers[0].signals.finished.emit()
            self.qt_app.processEvents()

        self.assertEqual(len(workers), 1)
        self.assertIsNone(self.page._pending_app_asset_refresh)
        self.assertFalse(self.page._apps_loading)
        self.assertEqual(self.page.operations.active_count, 0)

    def test_metadata_prepares_one_facade_inside_worker_and_passes_it_to_loader(self) -> None:
        workers = []
        context = self.devices.require_context()
        services = self.page._profile_services(context, include_system=True)
        app = AppInfo(package_name="com.example.metadata")

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        with (
            patch(
                "openadb.ui.apps_page.start_worker",
                side_effect=capture_worker,
            ),
            patch("openadb.ui.apps_data_workflow.AppMetadataLoader") as loader_type,
        ):
            loader_type.return_value.load.return_value = [app]
            self.page._load_metadata_background([app], context, services)
            self.assertEqual(self.privileges.calls, [])
            loader_type.assert_not_called()

            workers[0].run()
            self.qt_app.processEvents()

        self.assertEqual(len(self.privileges.calls), 1)
        loader_type.assert_called_once_with(
            self.facade,
            self.settings.get("apps_metadata_parallelism", 6),
        )
        loader_type.return_value.load.assert_called_once()
        self.assertIsNone(self.page._metadata_token)
        self.assertEqual(self.page.operations.active_count, 0)

    def test_cancel_during_facade_preparation_stops_before_package_read(self) -> None:
        workers = []

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        def cancel_while_preparing(_context, *, cancel_event=None):
            cancel_event.set()
            return self.facade

        self.privileges.prepare_adb = MagicMock(side_effect=cancel_while_preparing)
        with patch(
            "openadb.ui.apps_page.start_worker",
            side_effect=capture_worker,
        ):
            self.page.refresh_apps()

        workers[0].run()
        self.qt_app.processEvents()

        self.privileges.prepare_adb.assert_called_once()
        self.facade.list_packages.assert_not_called()
        self.assertFalse(self.page._apps_loading)
        self.assertEqual(self.page.operations.active_count, 0)

    def test_asset_loader_keeps_direct_control_plane_and_prepared_shell_facade(self) -> None:
        workers = []
        context = self.devices.require_context()
        services = self.page._profile_services(context, include_system=True)
        app = AppInfo(package_name="com.example.assets")

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        with (
            patch(
                "openadb.ui.apps_page.start_worker",
                side_effect=capture_worker,
            ),
            patch("openadb.ui.apps_data_workflow.AppAssetLoader") as loader_type,
        ):
            self.page._load_apk_assets_background(
                context,
                services,
                [app],
                [app],
                [],
            )
            self.assertEqual(self.privileges.calls, [])
            self.assertEqual(len(workers), 1)
            loader_type.return_value.load.return_value = []
            workers[0].run()
            self.qt_app.processEvents()

            direct_adb = loader_type.call_args.args[0]
            self.assertIsInstance(direct_adb, BoundRecordingAdb)
            self.assertEqual(direct_adb.device_context, context)
            self.assertIs(
                loader_type.call_args.kwargs["operation_adb"],
                self.facade,
            )
            root_resolver = loader_type.call_args.kwargs["root_available"]
            self.assertFalse(root_resolver(threading.Event()))

        self.assertEqual(len(self.privileges.calls), 1)

    def test_asset_root_resolver_uses_only_an_effective_root_facade(self) -> None:
        context = self.devices.require_context()
        services = self.page._profile_services(context, include_system=True)
        app = AppInfo(package_name="com.example.root-assets")
        self.privileges.selected_backend = PrivilegeBackend.ROOT
        self.facade.effective_privilege_backend = PrivilegeBackend.ROOT

        workers = []

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        with (
            patch("openadb.ui.apps_page.start_worker", side_effect=capture_worker),
            patch("openadb.ui.apps_data_workflow.AppAssetLoader") as loader_type,
        ):
            self.page._load_apk_assets_background(
                context,
                services,
                [app],
                [app],
                [],
            )
            loader_type.return_value.load.return_value = []
            workers[0].run()
            self.qt_app.processEvents()
            root_resolver = loader_type.call_args.kwargs["root_available"]
            self.assertTrue(root_resolver(threading.Event()))

        self.assertEqual(len(self.privileges.calls), 1)


class AppOperationCoordinatorShizukuTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = SimpleNamespace(serial="A", generation=3)
        self.cancel_event = threading.Event()
        self.direct_adb = MagicMock()
        self.facade = MagicMock()
        self.facade.enable_package.return_value = SimpleNamespace(status="enabled")
        self.facade.disable_package.return_value = SimpleNamespace(status="disabled")
        self.facade.restore_existing_package.return_value = SimpleNamespace(
            status="restored"
        )
        self.facade.effective_privilege_backend = PrivilegeBackend.SHIZUKU
        self.facade.uninstall_package.return_value = SimpleNamespace(
            status="uninstalled"
        )
        self.facade.root_available.return_value = False
        self.backups = MagicMock()
        self.backups.create_backup.return_value = (True, object(), "backup created")
        self.require_current = MagicMock()
        self.privileges = RecordingPrivilegeManager(self.facade)
        self.apps = [
            AppInfo(package_name="com.example.one"),
            AppInfo(package_name="com.example.two"),
        ]

    def coordinator(self, **kwargs) -> AppOperationCoordinator:
        return AppOperationCoordinator(
            context=self.context,
            adb=self.direct_adb,
            backup_manager=self.backups,
            device=SimpleNamespace(serial="A"),
            cancel_event=self.cancel_event,
            require_current=self.require_current,
            privilege_manager=self.privileges,
            **kwargs,
        )

    def test_one_prepared_facade_serves_all_package_actions_in_workflow(self) -> None:
        coordinator = self.coordinator()

        messages = coordinator.set_enabled(self.apps, enabled=True)
        restored = coordinator.install_existing(self.apps[:1])

        self.assertEqual(messages, [
            "com.example.one: enabled",
            "com.example.two: enabled",
        ])
        self.assertEqual(restored, ["com.example.one: restored"])
        self.assertEqual(len(self.privileges.calls), 1)
        self.assertEqual(self.facade.enable_package.call_count, 2)
        self.facade.restore_existing_package.assert_called_once()
        self.direct_adb.enable_package.assert_not_called()
        self.direct_adb.restore_existing_package.assert_not_called()

    def test_backup_and_uninstall_share_facade_including_binary_backup_api(self) -> None:
        coordinator = self.coordinator()

        messages = coordinator.uninstall(self.apps[:1], require_backup=True)

        self.assertEqual(messages, ["com.example.one: uninstalled"])
        self.assertEqual(len(self.privileges.calls), 1)
        self.backups.create_backup.assert_called_once()
        self.assertIs(self.backups.create_backup.call_args.args[1], self.facade)
        self.facade.uninstall_package.assert_called_once_with(
            "com.example.one",
            system_app=False,
            use_root=False,
            cancel_event=self.cancel_event,
        )
        self.direct_adb.uninstall_package.assert_not_called()

    def test_prepare_failure_never_falls_back_to_direct_adb(self) -> None:
        self.privileges.facade = None
        coordinator = self.coordinator()

        with self.assertRaisesRegex(RuntimeError, "backend was not prepared"):
            coordinator.set_enabled(self.apps[:1], enabled=False)

        self.direct_adb.disable_package.assert_not_called()
        self.facade.disable_package.assert_not_called()

    def test_context_change_during_prepare_prevents_first_mutation(self) -> None:
        self.require_current.side_effect = [None, None, StaleDeviceContext("changed")]
        coordinator = self.coordinator()

        with self.assertRaises(StaleDeviceContext):
            coordinator.set_enabled(self.apps[:1], enabled=False)

        self.facade.disable_package.assert_not_called()
        self.direct_adb.disable_package.assert_not_called()

    def test_cancel_during_prepare_prevents_first_mutation(self) -> None:
        def cancel_while_preparing(_context, *, cancel_event=None):
            cancel_event.set()
            return self.facade

        self.privileges.prepare_adb = MagicMock(side_effect=cancel_while_preparing)
        coordinator = self.coordinator()

        messages = coordinator.set_enabled(self.apps[:1], enabled=False)

        self.assertEqual(messages, [])
        self.facade.disable_package.assert_not_called()
        self.direct_adb.disable_package.assert_not_called()

    def test_shizuku_uid_zero_never_enables_direct_root_streaming(self) -> None:
        self.facade.verified_uid = 0
        self.facade.root_available.return_value = True
        coordinator = self.coordinator(root_enabled=True)

        coordinator.uninstall(self.apps[:1], require_backup=True)

        self.facade.root_available.assert_not_called()
        self.assertFalse(self.backups.create_backup.call_args.kwargs["use_root"])
        self.assertFalse(self.facade.uninstall_package.call_args.kwargs["use_root"])

    def test_effective_root_facade_enables_root_workflow_without_legacy_flag(self) -> None:
        self.facade.effective_privilege_backend = PrivilegeBackend.ROOT
        self.facade.root_available.return_value = True
        coordinator = self.coordinator(root_enabled=False)

        coordinator.uninstall(self.apps[:1], require_backup=True)

        self.facade.root_available.assert_called_once_with(
            cancel_event=self.cancel_event
        )
        self.assertTrue(self.backups.create_backup.call_args.kwargs["use_root"])
        self.assertTrue(self.facade.uninstall_package.call_args.kwargs["use_root"])


if __name__ == "__main__":
    unittest.main()
