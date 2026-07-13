from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.ui.apps_page import AppsPage
from openadb.ui.widgets.app_list_widget import PACKAGE_ROLE, AppFilterState, AppTable


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


def mock_apps() -> list[AppInfo]:
    return [
        AppInfo(
            package_name="com.example.system.recommended",
            app_label="System Helper",
            app_type="system",
            state="enabled",
            size="20 MB",
            bloatware_removal="Recommended",
        ),
        AppInfo(
            package_name="com.example.system.disabled",
            app_label="Disabled System Tool",
            app_type="system",
            state="disabled",
            size="8 MB",
            bloatware_removal="Recommended",
        ),
        AppInfo(
            package_name="com.example.user.advanced",
            app_label="Notes Plus",
            app_type="user",
            state="enabled",
            size="5 MB",
            bloatware_removal="Advanced",
        ),
        AppInfo(
            package_name="org.example.user.unsafe",
            app_label="Risky Utility",
            app_type="user",
            state="disabled",
            size="12 MB",
            bloatware_removal="Unsafe",
        ),
        AppInfo(
            package_name="com.example.system.expert",
            app_label="Expert Service",
            app_type="system",
            state="enabled",
            size="2 MB",
            bloatware_removal="Expert",
        ),
        AppInfo(
            package_name="net.example.user.unlisted",
            app_label="Private Camera",
            app_type="user",
            state="disabled",
            size="30 MB",
        ),
        AppInfo(
            package_name="android.example.system.unlisted",
            app_label="Core Component",
            app_type="system",
            state="disabled",
            size="Unknown",
        ),
    ]


class AppTableFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.table = AppTable()
        self.table.set_apps_sorted(mock_apps(), "name")
        self.table.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.table.close()
        self.table.deleteLater()
        self.app.processEvents()

    def test_required_combinable_filter_matrix(self) -> None:
        cases = [
            (
                AppFilterState.from_values(app_type="all", app_state="any", uad_category="any"),
                {
                    "com.example.system.recommended",
                    "com.example.system.disabled",
                    "com.example.user.advanced",
                    "org.example.user.unsafe",
                    "com.example.system.expert",
                    "net.example.user.unlisted",
                    "android.example.system.unlisted",
                },
            ),
            (
                AppFilterState.from_values(
                    app_type="system",
                    app_state="enabled",
                    uad_category="recommended",
                ),
                {"com.example.system.recommended"},
            ),
            (
                AppFilterState.from_values(app_type="user", app_state="disabled"),
                {"org.example.user.unsafe", "net.example.user.unlisted"},
            ),
            (
                AppFilterState.from_values(uad_category="unsafe"),
                {"org.example.user.unsafe"},
            ),
            (
                AppFilterState.from_values(uad_category="advanced"),
                {"com.example.user.advanced"},
            ),
            (
                AppFilterState.from_values(uad_category="expert"),
                {"com.example.system.expert"},
            ),
            (
                AppFilterState.from_values(uad_category="not listed"),
                {"net.example.user.unlisted", "android.example.system.unlisted"},
            ),
        ]
        for filters, expected in cases:
            with self.subTest(filters=filters):
                self.table.apply_filters(filters)
                self.assertEqual(self._visible_packages(), expected)

    def test_search_matches_application_name_and_package_name(self) -> None:
        self.table.apply_filters(AppFilterState.from_values(search_text="Notes Plus"))
        self.assertEqual(self._visible_packages(), {"com.example.user.advanced"})
        self.table.apply_filters(AppFilterState.from_values(search_text="system.recommended"))
        self.assertEqual(self._visible_packages(), {"com.example.system.recommended"})

    def test_hidden_checkbox_selection_survives_filters_and_sorts(self) -> None:
        selected_package = "com.example.system.recommended"
        self._check_package(selected_package)
        self.table.apply_filters(AppFilterState.from_values(app_type="user", app_state="disabled"))
        self.assertNotIn(selected_package, self._visible_packages())
        self.assertEqual(self.table.checked_package_names(), {selected_package})

        self.table.apply_sort("size_desc")
        self.table.apply_filters(AppFilterState.from_values(app_type="user", app_state="disabled"))
        self.assertEqual(self.table.checked_package_names(), {selected_package})
        self.table.apply_sort("name")
        self.table.apply_filters(AppFilterState())
        self.assertEqual(self.table.checked_package_names(), {selected_package})
        self.assertEqual(self._checked_state(selected_package), Qt.Checked)

    def test_size_sort_order_is_unchanged_by_filter_and_search(self) -> None:
        self.table.apply_sort("size_desc")
        expected_order = [
            "net.example.user.unlisted",
            "com.example.system.recommended",
            "org.example.user.unsafe",
            "com.example.system.disabled",
            "com.example.user.advanced",
            "com.example.system.expert",
            "android.example.system.unlisted",
        ]
        self.assertEqual(self._all_packages_in_row_order(), expected_order)
        self.table.apply_filters(AppFilterState.from_values(app_type="system"))
        self.table.apply_filters(AppFilterState.from_values(search_text="example"))
        self.table.apply_filters(AppFilterState())
        self.assertEqual(self._all_packages_in_row_order(), expected_order)

        self.table.apply_sort("size_asc")
        self.assertEqual(
            self._all_packages_in_row_order(),
            [
                "com.example.system.expert",
                "com.example.user.advanced",
                "com.example.system.disabled",
                "org.example.user.unsafe",
                "com.example.system.recommended",
                "net.example.user.unlisted",
                "android.example.system.unlisted",
            ],
        )

        self.table.apply_sort("name")
        self.assertEqual(
            self._all_packages_in_row_order(),
            [
                "android.example.system.unlisted",
                "com.example.system.disabled",
                "com.example.system.expert",
                "com.example.user.advanced",
                "net.example.user.unlisted",
                "org.example.user.unsafe",
                "com.example.system.recommended",
            ],
        )

    def test_invalid_filter_values_fall_back_to_safe_defaults(self) -> None:
        filters = AppFilterState.from_values(app_type="invalid", app_state="broken", uad_category="other")
        self.assertEqual(filters, AppFilterState())
        self.assertEqual(self.table.apply_filters(filters), len(mock_apps()))

    def test_metadata_updates_do_not_emit_checkbox_selection_changes(self) -> None:
        selection_events: list[None] = []
        self.table.selection_changed.connect(lambda: selection_events.append(None))
        package_name = "com.example.system.recommended"

        self.table.update_app_details(
            AppInfo(
                package_name=package_name,
                app_label="Updated helper",
                size="21 MB",
                metadata_checked=True,
            )
        )

        self.assertEqual(selection_events, [])
        self.assertTrue(self.table._resize_columns_pending)
        self._check_package(package_name)
        self.assertEqual(len(selection_events), 1)

    def _visible_packages(self) -> set[str]:
        return {
            str(self.table.item(row, 0).data(PACKAGE_ROLE))
            for row in range(self.table.rowCount())
            if not self.table.isRowHidden(row)
        }

    def _all_packages_in_row_order(self) -> list[str]:
        return [str(self.table.item(row, 0).data(PACKAGE_ROLE)) for row in range(self.table.rowCount())]

    def _check_package(self, package_name: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.data(PACKAGE_ROLE) == package_name:
                item.setCheckState(Qt.Checked)
                return
        self.fail(f"Package was not found: {package_name}")

    def _checked_state(self, package_name: str) -> Qt.CheckState:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.data(PACKAGE_ROLE) == package_name:
                return item.checkState()
        self.fail(f"Package was not found: {package_name}")


class AppsPageFilterUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = IsolatedSettings(Path(self.temp_dir.name))
        self.page = AppsPage(None, None, None, None, self.settings)
        self.page.apps = mock_apps()
        self.page.table.set_apps_sorted(self.page.apps, "name")
        self.page.apply_filter(save_state=False)
        self.page.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_reset_filters_restores_defaults_without_resetting_sort(self) -> None:
        with patch("openadb.ui.apps_page.start_worker") as start_worker:
            self._select_filter("type", "system")
            self._select_filter("state", "enabled")
            self._select_filter("uad", "recommended")
            self.page.search.setText("Helper")
            self.page._set_sort_mode("size_desc")
            self.page.apply_filter()
            self.assertEqual(self.page.total_label.text(), "Showing 1 of 7 applications")
            self.assertIn("System · Enabled · Recommended", self.page.active_filters_label.full_text())
            self.assertEqual(self.page.filters_button.text(), "Filters (3)")
            self.assertTrue(self.page._filter_actions["type"]["system"].isChecked())
            self.assertTrue(self.page._filter_actions["state"]["enabled"].isChecked())
            self.assertTrue(self.page._filter_actions["uad"]["recommended"].isChecked())

            self.page.reset_filters_button.click()
            self.assertEqual(self.page._filter_values, {"type": "all", "state": "any", "uad": "any"})
            self.assertTrue(self.page._filter_actions["type"]["all"].isChecked())
            self.assertTrue(self.page._filter_actions["state"]["any"].isChecked())
            self.assertTrue(self.page._filter_actions["uad"]["any"].isChecked())
            self.assertEqual(self.page.search.text(), "")
            self.assertEqual(self.page._sort_mode, "size_desc")
            self.assertEqual(self.page.total_label.text(), "Showing 7 of 7 applications")
            self.assertEqual(self.page.active_filters_label.full_text(), "No active filters")
            self.assertEqual(self.page.filters_button.text(), "Filters")
            self.assertFalse(self.page.reset_filters_button.isEnabled())
            start_worker.assert_not_called()

    def test_count_includes_checked_application_hidden_by_filter(self) -> None:
        target = "com.example.system.recommended"
        for row in range(self.page.table.rowCount()):
            item = self.page.table.item(row, 0)
            if item.data(PACKAGE_ROLE) == target:
                item.setCheckState(Qt.Checked)
                break
        self._select_filter("type", "user")
        self._select_filter("state", "disabled")
        self.page.apply_filter()
        self.assertEqual(self.page.total_label.text(), "Showing 2 of 7 applications")
        self.assertEqual(self.page.selection_summary_label.full_text(), "1 selected · 1 hidden by filters")
        self.assertEqual(self.page.table.checked_package_names(), {target})
        self.assertEqual([app.package_name for app in self.page.selected_apps()], [target])

    def test_profile_switch_restores_separate_filter_search_and_sort_state(self) -> None:
        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "Phone"))
        self.page.reset_for_device_profile()
        self.page.apps = mock_apps()
        self.page.table.set_apps_sorted(self.page.apps, self.page._sort_mode)
        self._select_filter("type", "system")
        self._select_filter("state", "enabled")
        self._select_filter("uad", "recommended")
        self.page.search.setText("Helper")
        self.page._set_sort_mode("size_desc")
        self.page.apply_filter()

        self.assertTrue(self.settings.activate_device_profile("device-b", "Device B", "Phone"))
        self.page.reset_for_device_profile()
        self.assertEqual(self.page._filter_values, {"type": "all", "state": "any", "uad": "any"})
        self.assertEqual(self.page.search.text(), "")
        self.assertEqual(self.page._sort_mode, "name")
        self._select_filter("type", "user")
        self._select_filter("state", "disabled")
        self.page.apply_filter()

        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "Phone"))
        self.page.reset_for_device_profile()
        self.assertEqual(
            self.page._filter_values,
            {"type": "system", "state": "enabled", "uad": "recommended"},
        )
        self.assertEqual(self.page.search.text(), "Helper")
        self.assertEqual(self.page._sort_mode, "size_desc")

    def _select_filter(self, kind: str, value: str) -> None:
        action = self.page._filter_actions[kind].get(value)
        self.assertIsNotNone(action)
        action.trigger()
        self.app.processEvents()
