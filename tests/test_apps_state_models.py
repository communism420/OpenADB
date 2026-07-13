from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from openadb.models.app_info import AppInfo
from openadb.ui.app_selection_model import AppSelectionModel, VisibleSelectionState
from openadb.ui.apps_filter_controller import AppsFilterController, AppsViewState
from openadb.ui.widgets.app_list_widget import AppFilterState


class ProfileSettings:
    def __init__(self) -> None:
        self.active_profile_serial = "device-a"
        self.active_profile_kind = "Phone"
        self.path = Path("profiles/device-a/settings.json")
        self.profiles: dict[str, dict[str, object]] = {"device-a": {}}
        self.save_counts: dict[str, int] = {"device-a": 0}

    def switch(self, serial: str) -> None:
        self.active_profile_serial = serial
        self.path = Path(f"profiles/{serial}/settings.json")
        self.profiles.setdefault(serial, {})
        self.save_counts.setdefault(serial, 0)

    def get(self, key: str, default=None):
        return self.profiles[self.active_profile_serial].get(key, default)

    def set(self, key: str, value, save: bool = True) -> None:
        self.profiles[self.active_profile_serial][key] = value
        if save:
            self.save()

    def save(self) -> None:
        serial = self.active_profile_serial
        self.save_counts[serial] += 1


def sample_apps() -> list[AppInfo]:
    return [
        AppInfo(
            package_name="com.example.system.helper",
            app_label="System Helper",
            app_type="system",
            state="enabled",
            bloatware_removal="Recommended",
        ),
        AppInfo(
            package_name="com.example.user.notes",
            app_label="Notes Plus",
            app_type="user",
            state="disabled",
            bloatware_removal="Unsafe",
        ),
    ]


class AppsFilterControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = ProfileSettings()
        self.controller = AppsFilterController(self.settings)

    def test_defaults_and_invalid_values_are_normalized(self) -> None:
        self.assertEqual(self.controller.state, AppsViewState())

        self.settings.profiles["device-a"].update(
            {
                "apps_filter_type": "invalid",
                "apps_filter_state": "broken",
                "apps_filter_uad": "other",
                "apps_filter_search": None,
                "apps_sort_mode": "random",
            }
        )

        self.assertEqual(self.controller.reload(), AppsViewState())

    def test_combined_filters_and_search_delegate_to_app_filter_state(self) -> None:
        system_app, user_app = sample_apps()
        state = self.controller.update_filters(
            search_text="helper",
            app_type="SYSTEM",
            app_state="ENABLED",
            uad_category="RECOMMENDED",
            persist=False,
        )

        self.assertEqual(
            state.filters,
            AppFilterState.from_values(
                search_text="helper",
                app_type="system",
                app_state="enabled",
                uad_category="recommended",
            ),
        )
        self.assertTrue(self.controller.matches(system_app, "Recommended"))
        self.assertFalse(self.controller.matches(user_app, "Unsafe"))

        self.controller.update_filters(
            search_text="system.helper",
            app_type="all",
            app_state="any",
            uad_category="any",
            persist=False,
        )
        self.assertTrue(self.controller.matches(system_app, "Recommended"))
        self.assertFalse(self.controller.matches(user_app, "Unsafe"))

        with patch.object(AppFilterState, "matches", return_value=True) as matches:
            self.assertTrue(self.controller.matches(system_app, "Recommended"))
        matches.assert_called_once_with(system_app, "Recommended")

    def test_summary_and_reset_preserve_sort_until_full_reset(self) -> None:
        self.controller.set_sort_mode("size_desc")
        self.controller.update_filters(
            search_text="helper",
            app_type="system",
            app_state="enabled",
            uad_category="recommended",
        )

        summary = self.controller.summary()
        self.assertEqual(summary.menu_filter_count, 3)
        self.assertEqual(summary.filter_button_text, "Filters (3)")
        self.assertEqual(
            summary.active_parts,
            ("System", "Enabled", "Recommended", 'Search: "helper"'),
        )
        self.assertTrue(summary.has_active_filters)

        reset = self.controller.reset_filters()
        self.assertEqual(reset.filters, AppFilterState())
        self.assertEqual(reset.sort_mode, "size_desc")
        self.assertEqual(self.controller.summary().active_text, "No active filters")
        self.assertEqual(self.settings.profiles["device-a"]["apps_sort_mode"], "size_desc")

        self.assertEqual(self.controller.reset_view(), AppsViewState())
        self.assertEqual(self.settings.profiles["device-a"]["apps_sort_mode"], "name")

    def test_profile_switch_loads_and_persists_independent_state(self) -> None:
        state_a = self.controller.update_filters(
            search_text="alpha",
            app_type="system",
            app_state="enabled",
            uad_category="recommended",
        )
        self.controller.set_sort_mode("size_desc")

        self.settings.switch("device-b")
        self.assertEqual(self.controller.state, AppsViewState())
        state_b = self.controller.update_filters(
            search_text="beta",
            app_type="user",
            app_state="disabled",
            uad_category="unsafe",
        )
        self.controller.set_sort_mode("size_asc")

        self.settings.switch("device-a")
        self.assertEqual(self.controller.state.filters, state_a.filters)
        self.assertEqual(self.controller.state.sort_mode, "size_desc")
        self.assertEqual(self.settings.profiles["device-a"]["apps_filter_search"], "alpha")

        self.settings.switch("device-b")
        self.assertEqual(self.controller.state.filters, state_b.filters)
        self.assertEqual(self.controller.state.sort_mode, "size_asc")
        self.assertEqual(self.settings.profiles["device-b"]["apps_filter_search"], "beta")


class AppSelectionModelTests(unittest.TestCase):
    def test_hidden_selection_survives_visible_select_and_unselect(self) -> None:
        model = AppSelectionModel(["hidden.package"])
        visible = ["visible.one", "visible.two"]

        initial = model.summary(visible)
        self.assertEqual(initial.total_selected, 1)
        self.assertEqual(initial.visible_selected, 0)
        self.assertEqual(initial.hidden_selected, 1)
        self.assertEqual(initial.visible_state, VisibleSelectionState.UNCHECKED)
        self.assertEqual(initial.text, "1 selected · 1 hidden by filters")

        self.assertTrue(model.select_visible(visible))
        selected = model.summary(visible)
        self.assertEqual(selected.visible_state, VisibleSelectionState.CHECKED)
        self.assertEqual(selected.hidden_selected, 1)
        self.assertEqual(
            model.selected_packages,
            frozenset({"hidden.package", "visible.one", "visible.two"}),
        )

        self.assertTrue(model.unselect_visible(visible))
        self.assertEqual(model.selected_packages, frozenset({"hidden.package"}))
        self.assertTrue(model.clear())
        self.assertEqual(model.selected_packages, frozenset())
        self.assertFalse(model.clear())

    def test_mixed_state_is_based_only_on_visible_packages(self) -> None:
        model = AppSelectionModel(["visible.one", "hidden.package"])
        visible = ["visible.one", "visible.two", "visible.three"]

        summary = model.summary(visible)
        self.assertEqual(summary.visible_state, VisibleSelectionState.PARTIALLY_CHECKED)
        self.assertEqual(summary.visible_selected, 1)
        self.assertEqual(summary.hidden_selected, 1)

        model.set_selected("visible.two")
        self.assertEqual(model.visible_state(visible), VisibleSelectionState.PARTIALLY_CHECKED)
        model.set_selected("visible.three")
        self.assertEqual(model.visible_state(visible), VisibleSelectionState.CHECKED)
        self.assertEqual(model.visible_state([]), VisibleSelectionState.UNCHECKED)

    def test_package_keys_are_normalized_and_can_be_retained_after_reload(self) -> None:
        model = AppSelectionModel([" package.one ", "", "package.two", "package.two"])
        self.assertEqual(model.selected_packages, frozenset({"package.one", "package.two"}))
        self.assertIn("package.one", model)
        self.assertFalse(model.set_selected(" "))

        self.assertTrue(model.retain(["package.two", "package.three"]))
        self.assertEqual(model.selected_packages, frozenset({"package.two"}))
        self.assertFalse(model.retain(["package.two"]))
        self.assertTrue(model.toggle("package.two"))
        self.assertFalse(model.is_selected("package.two"))


if __name__ == "__main__":
    unittest.main()
