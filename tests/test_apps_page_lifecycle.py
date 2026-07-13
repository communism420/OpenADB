from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from openadb.core.app_operation_coordinator import AppOperationCoordinator
from openadb.core.backup_manager import BackupManager
from openadb.core.device_context import StaleDeviceContext
from openadb.core.icon_extractor import IconExtractor
from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.ui.apps_page import AppsPage
from tests.test_apps_device_context import ContextDeviceManager, RecordingAdb


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class AppsPageLifecycleTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_page(self) -> AppsPage:
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

    def test_rejected_and_raising_worker_start_release_every_loader_token(self) -> None:
        for outcome in (False, RuntimeError("thread pool unavailable")):
            with self.subTest(outcome=type(outcome).__name__):
                page = self.make_page()
                side_effect = outcome if isinstance(outcome, Exception) else None
                return_value = False if side_effect is None else None
                with patch(
                    "openadb.ui.apps_page.start_worker",
                    return_value=return_value,
                    side_effect=side_effect,
                ):
                    page.refresh_apps()
                    self.assertFalse(page._apps_loading)
                    self.assertIsNone(page._apps_load_token)
                    self.assertEqual(page.operations.active_count, 0)

                    context = self.devices.require_context()
                    services = page._profile_services(context, include_system=True)
                    app = AppInfo(package_name="com.example.lifecycle")
                    page._load_metadata_background([app], context, services)
                    self.assertIsNone(page._metadata_token)
                    self.assertEqual(page.operations.active_count, 0)

                    page._load_apk_assets_background(
                        context,
                        services,
                        [app],
                        [app],
                        [],
                    )
                    self.assertFalse(page._assets_loading)
                    self.assertIsNone(page._assets_token)
                    self.assertEqual(page.operations.active_count, 0)
                page.close()

    def test_second_refresh_does_not_create_a_duplicate_worker(self) -> None:
        page = self.make_page()
        workers = []

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        with patch("openadb.ui.apps_page.start_worker", side_effect=capture_worker):
            page.refresh_apps()
            page.refresh_apps()

        self.assertEqual(len(workers), 1)
        self.assertTrue(page._apps_loading)
        self.assertEqual(page.operations.active_count, 1)
        workers[0].signals.finished.emit()
        self.assertFalse(page._apps_loading)
        self.assertEqual(page.operations.active_count, 0)
        page.close()

    def test_loader_setup_context_error_registers_no_operation(self) -> None:
        page = self.make_page()
        context = self.devices.require_context()
        services = page._profile_services(context, include_system=True)
        app = AppInfo(package_name="com.example.setup")

        with patch.object(
            page,
            "_bound_adb_for_context",
            side_effect=StaleDeviceContext("device changed during setup"),
        ):
            page._load_metadata_background([app], context, services)
            page._load_apk_assets_background(
                context,
                services,
                [app],
                [app],
                [],
            )

        self.assertIsNone(page._metadata_token)
        self.assertIsNone(page._assets_token)
        self.assertFalse(page._assets_loading)
        self.assertEqual(page.operations.active_count, 0)
        page.close()

    def test_rejected_and_raising_bulk_start_reset_busy_state(self) -> None:
        for outcome in (False, RuntimeError("thread pool unavailable")):
            with self.subTest(outcome=type(outcome).__name__):
                page = self.make_page()
                context = self.devices.require_context()
                app = AppInfo(package_name="com.example.bulk", app_label="Bulk")
                page.apps = [app]
                page._set_table_apps([app])
                page._set_apps_view_identity(context.serial, context)
                page.update_device_state(self.devices.active)
                page.table.select_all_visible()
                side_effect = outcome if isinstance(outcome, Exception) else None
                return_value = False if side_effect is None else None

                with patch(
                    "openadb.ui.apps_page.start_worker",
                    return_value=return_value,
                    side_effect=side_effect,
                ):
                    page.backup_selected()

                self.assertFalse(page._bulk_operation_busy)
                self.assertIsNone(page._bulk_token)
                self.assertEqual(page.operations.active_count, 0)
                page.close()

    def test_bulk_worker_error_still_runs_final_cleanup(self) -> None:
        page = self.make_page()
        context = self.devices.require_context()
        app = AppInfo(package_name="com.example.error", app_label="Error")
        page.apps = [app]
        page._set_table_apps([app])
        page._set_apps_view_identity(context.serial, context)
        page.update_device_state(self.devices.active)
        page.table.select_all_visible()
        workers = []

        def capture_worker(_owner, _pool, worker, **_kwargs) -> bool:
            workers.append(worker)
            return True

        with (
            patch("openadb.ui.apps_page.start_worker", side_effect=capture_worker),
            patch.object(
                AppOperationCoordinator,
                "backup",
                side_effect=RuntimeError("coordinator failed"),
            ),
            patch("openadb.ui.apps_action_workflow.show_error_dialog"),
        ):
            page.backup_selected()
            self.assertTrue(page._bulk_operation_busy)
            self.assertEqual(len(workers), 1)
            workers[0].run()
            self.qt_app.processEvents()

        self.assertFalse(page._bulk_operation_busy)
        self.assertIsNone(page._bulk_token)
        self.assertEqual(page.operations.active_count, 0)
        page.close()


if __name__ == "__main__":
    unittest.main()
