from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from openadb.core.device_context import DeviceContext, StaleDeviceContext
from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.models.device_info import DeviceInfo
from openadb.ui.apps_page import AppsPage
from openadb.ui.widgets.app_list_widget import PACKAGE_ROLE, AppFilterState


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class FakeAdb:
    serial = "device-1"

    def for_context(self, context: DeviceContext):
        if context.serial != self.serial:
            raise RuntimeError("wrong test device")
        return SimpleNamespace(
            serial=context.serial,
            device_context=context,
        )


class FakeDeviceManager:
    def __init__(self, settings: SettingsManager, mode: str = "ADB") -> None:
        self.settings = settings
        self.current_generation = 1
        self.active = DeviceInfo(serial="device-1", model="Test device", mode=mode, state="device")

    def _context(self) -> DeviceContext:
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
        context = self._context()
        if allowed_modes is not None and context.mode not in set(allowed_modes):
            raise RuntimeError("unsupported test device mode")
        return context

    def is_context_current(self, context: DeviceContext) -> bool:
        return context == self._context()

    def require_current(self, context: DeviceContext) -> None:
        if not self.is_context_current(context):
            raise StaleDeviceContext("test device changed")

    def active_snapshot(self):
        return self.active, self.current_generation


def action_apps() -> list[AppInfo]:
    return [
        AppInfo(
            package_name="com.example.enabled",
            app_label="Enabled user app",
            app_type="user",
            state="enabled",
        ),
        AppInfo(
            package_name="com.example.disabled",
            app_label="Disabled user app",
            app_type="user",
            state="disabled",
        ),
        AppInfo(
            package_name="com.example.second",
            app_label="Second enabled app",
            app_type="user",
            state="enabled",
        ),
        AppInfo(
            package_name="com.android.systemui",
            app_label="System UI",
            app_type="system",
            state="enabled",
        ),
    ]


class AppsPageActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = IsolatedSettings(Path(self.temp_dir.name))
        self.device_manager = FakeDeviceManager(self.settings)
        self.page = AppsPage(FakeAdb(), object(), self.device_manager, object(), self.settings)
        self.page.apps = action_apps()
        self.page.table.set_apps_sorted(self.page.apps, "name")
        self.page.apply_filter(save_state=False)
        self.page.update_device_state(self.device_manager.active)
        self.page.resize(820, 760)
        self.page.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        if shiboken6.isValid(self.page):
            self.page.close()
            self.page.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_compact_action_layout_and_dynamic_refresh_label(self) -> None:
        self.assertEqual(self.page.refresh_button.text(), "Refresh applications")
        self.assertIsNotNone(self.page.bulk_action_bar)
        self.assertTrue(self.page.bulk_action_bar.isHidden())
        self.assertIsNone(self.page.findChild(type(self.page.bulk_action_bar), "appsActionPanel"))
        self.assertEqual(
            [action.text() for action in self.page.more_menu.actions() if not action.isSeparator()],
            ["Install existing", "Export package list", "Clear apps cache…"],
        )
        self.assertEqual(
            [action.text() for action in self.page.page_actions_menu.actions()],
            ["Export package list", "Clear apps cache…"],
        )
        self.page.apps = []
        self.page.table.set_apps_sorted([], "name")
        self.page.apply_filter(save_state=False)
        self.assertEqual(self.page.refresh_button.text(), "Load applications")

    def test_contextual_bar_uses_table_space_and_stays_open_for_hidden_selection(self) -> None:
        page_size = self.page.size()
        table_height_without_selection = self.page.table.height()

        self.page.table.setFocus(Qt.OtherFocusReason)
        self.app.processEvents()
        self._check_package("com.android.systemui")
        self.app.processEvents()

        self.assertTrue(self.page.bulk_action_bar.isVisible())
        self.assertIs(self.app.focusWidget(), self.page.table)
        self.assertEqual(
            [
                self.page.backup_button.text(),
                self.page.enable_button.text(),
                self.page.disable_button.text(),
                self.page.uninstall_button.text(),
                self.page.more_button.text(),
            ],
            ["Backup", "Enable", "Disable", "Uninstall", "More"],
        )
        self.assertTrue(self.page.uninstall_button.property("danger"))
        self.assertLess(self.page.table.height(), table_height_without_selection)
        self.assertEqual(self.page.size(), page_size)

        self.page.table.apply_filters(AppFilterState.from_values(app_type="user"))
        self.page._update_app_count()
        self.app.processEvents()

        self.assertTrue(self.page.bulk_action_bar.isVisible())
        self.assertEqual(self.page.selection_summary_label.full_text(), "1 selected · 1 hidden by filters")
        self.assertEqual(self.page.size(), page_size)

    def test_clearing_contextual_selection_restores_focus_without_resizing(self) -> None:
        self._check_package("com.example.enabled")
        self.page.clear_selection_button.setFocus(Qt.OtherFocusReason)
        self.app.processEvents()
        self.assertIs(self.app.focusWidget(), self.page.clear_selection_button)
        page_size = self.page.size()

        self.page.clear_selection_button.click()
        self.app.processEvents()

        self.assertTrue(self.page.bulk_action_bar.isHidden())
        self.assertEqual(self.page.table.checked_package_names(), set())
        self.assertEqual(self.page.size(), page_size)
        self.assertIn(self.app.focusWidget(), {self.page.table, self.page.search})

    def test_pending_focus_restore_is_cancelled_when_page_is_destroyed(self) -> None:
        self._check_package("com.example.enabled")
        self.page.clear_selection_button.setFocus(Qt.OtherFocusReason)
        self.app.processEvents()

        self.page.clear_selection_button.click()
        focus_timer = self.page._focus_restore_timer
        self.assertTrue(focus_timer.isActive())

        shiboken6.delete(self.page)
        self.app.processEvents()

        self.assertFalse(shiboken6.isValid(focus_timer))

    def test_contextual_state_explanation_covers_uniform_and_mixed_selection(self) -> None:
        self._check_package("com.example.enabled")
        self.assertEqual(
            self.page.selection_state_label.full_text(),
            "Enabled apps selected; Disable is available.",
        )

        self._check_package("com.example.disabled")
        explanation = self.page.selection_state_label.full_text()
        self.assertIn("mixes enabled and disabled states", explanation)
        self.assertTrue(self.page.selection_state_label.isVisible())
        self.assertFalse(self.page.enable_button.isEnabled())
        self.assertFalse(self.page.disable_button.isEnabled())
        self.assertEqual(self.page.enable_button.toolTip(), explanation)
        self.assertEqual(self.page.disable_button.toolTip(), explanation)

    def test_visible_selection_checkbox_is_tristate_and_clear_selection_is_global(self) -> None:
        self.assertEqual(self.page.select_all_check.checkState(), Qt.Unchecked)
        self._check_package("com.example.enabled")
        self.assertEqual(self.page.select_all_check.checkState(), Qt.PartiallyChecked)
        self.assertEqual(self.page.selection_summary_label.full_text(), "1 selected")

        self.page.select_all_check.click()
        self.app.processEvents()
        self.assertEqual(len(self.page.table.checked_package_names()), 4)
        self.assertEqual(self.page.select_all_check.checkState(), Qt.Checked)

        self.page.table.unselect_all()
        self._check_package("com.android.systemui")
        self.page.table.apply_filters(AppFilterState.from_values(app_type="user"))
        self.page._update_app_count()
        self.assertEqual(self.page.selection_summary_label.full_text(), "1 selected · 1 hidden by filters")
        self.assertEqual(self.page.select_all_check.checkState(), Qt.Unchecked)

        self.page.select_all_check.click()
        self.app.processEvents()
        self.assertEqual(len(self.page.table.checked_package_names()), 4)
        self.page.select_all_check.click()
        self.app.processEvents()
        self.assertEqual(self.page.table.checked_package_names(), {"com.android.systemui"})
        self.assertTrue(self.page.clear_selection_button.isEnabled())
        self.page.clear_selection_button.click()
        self.assertEqual(self.page.table.checked_package_names(), set())

    def test_action_availability_tracks_selection_device_state_and_danger(self) -> None:
        for button in [
            self.page.backup_button,
            self.page.uninstall_button,
            self.page.enable_button,
            self.page.disable_button,
        ]:
            self.assertFalse(button.isEnabled())
            self.assertIn("Select", button.toolTip())

        self._check_package("com.example.enabled")
        self.assertTrue(self.page.backup_button.isEnabled())
        self.assertTrue(self.page.uninstall_button.isEnabled())
        self.assertFalse(self.page.enable_button.isEnabled())
        self.assertTrue(self.page.disable_button.isEnabled())

        self.page.table.unselect_all()
        self._check_package("com.example.disabled")
        self.assertTrue(self.page.enable_button.isEnabled())
        self.assertFalse(self.page.disable_button.isEnabled())

        self._check_package("com.example.enabled")
        self.assertFalse(self.page.enable_button.isEnabled())
        self.assertFalse(self.page.disable_button.isEnabled())
        self.assertIn("mixes", self.page.enable_button.toolTip())

        with (
            patch("openadb.ui.apps_page.start_worker") as start_worker,
            patch.object(QMessageBox, "information"),
        ):
            self.page.set_enabled_selected(True)
        start_worker.assert_not_called()

        self.page.table.unselect_all()
        self._check_package("com.android.systemui")
        self.assertTrue(self.page.uninstall_button.isEnabled())
        self.assertTrue(self.page.uninstall_button.property("danger"))
        self.assertIn("system or protected", self.page.disable_button.toolTip())

        self.page.update_device_state(DeviceInfo(mode="Offline", state="offline"))
        self.assertFalse(self.page.refresh_button.isEnabled())
        self.assertFalse(self.page.backup_button.isEnabled())
        self.assertIn("Offline", self.page.backup_button.toolTip())

    def test_background_and_bulk_busy_states_disable_conflicting_actions(self) -> None:
        self._check_package("com.example.enabled")
        self.page._assets_loading = True
        self.page._update_action_states()
        self.assertFalse(self.page.refresh_button.isEnabled())
        self.assertFalse(self.page.backup_button.isEnabled())
        self.assertFalse(self.page.install_existing_action.isEnabled())
        self.assertFalse(self.page.clear_cache_action.isEnabled())

        self.page._assets_loading = False
        self.page._set_bulk_operation_busy(True, "backup")
        self.assertFalse(self.page.backup_button.isEnabled())
        self.assertFalse(self.page.export_action.isEnabled())
        self.assertIn("backup", self.page.backup_button.toolTip())
        self.page._finish_bulk_operation()

    def test_second_bulk_operation_does_not_start_another_worker(self) -> None:
        self._check_package("com.example.enabled")
        with (
            patch("openadb.ui.apps_page.start_worker") as start_worker,
            patch.object(QMessageBox, "information"),
        ):
            self.page.backup_selected()
            self.page.backup_selected()
            self.assertEqual(start_worker.call_count, 1)
            self.assertTrue(self.page._bulk_operation_busy)
            self.page._finish_bulk_operation()
            self.assertEqual(self.page.operations.active_count, 0)

            self.page.backup_selected()
            self.assertEqual(start_worker.call_count, 2)
            self.page._finish_bulk_operation()
            self.assertEqual(self.page.operations.active_count, 0)

    def test_refresh_after_bulk_result_starts_only_after_worker_finishes(self) -> None:
        self.page._set_bulk_operation_busy(True, "disable")
        with (
            patch.object(QMessageBox, "information"),
            patch.object(self.page, "refresh_apps") as refresh_apps,
        ):
            self.page._operation_done("Disable selected", ["OK"], refresh=True)
            refresh_apps.assert_not_called()
            self.page._finish_bulk_operation()
            refresh_apps.assert_called_once_with()

    def test_bulk_refresh_survives_modal_nested_event_loop(self) -> None:
        self.page._set_bulk_operation_busy(True, "disable")
        with patch.object(self.page, "refresh_apps") as refresh_apps:
            def finish_while_modal_is_open(*_args) -> None:
                self.page._finish_bulk_operation()

            with patch.object(QMessageBox, "information", side_effect=finish_while_modal_is_open):
                self.page._operation_done("Disable selected", ["OK"], refresh=True)

        refresh_apps.assert_called_once_with()
        self.assertFalse(self.page._refresh_after_bulk)
        self.assertFalse(self.page._bulk_operation_busy)

    def _check_package(self, package_name: str) -> None:
        for row in range(self.page.table.rowCount()):
            item = self.page.table.item(row, 0)
            if item.data(PACKAGE_ROLE) == package_name:
                item.setCheckState(Qt.Checked)
                self.app.processEvents()
                return
        self.fail(f"Package was not found: {package_name}")
