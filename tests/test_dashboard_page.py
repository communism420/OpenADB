from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QMessageBox, QWidget

from openadb.core.settings_manager import DEFAULT_SETTINGS, SettingsManager
from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.dashboard_page import (
    DashboardPage,
    WIRELESS_LEGACY_PORT,
    WIRELESS_SCENARIO_LEGACY,
    WIRELESS_SCENARIO_MODERN,
    WIRELESS_SCENARIO_TV,
)
from openadb.ui.main_window import MainWindow
from openadb.ui.style import apply_theme


class MemorySettings:
    def __init__(self, **overrides) -> None:
        self.data = dict(DEFAULT_SETTINGS)
        self.data.update(overrides)
        self.save_count = 0

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value, save: bool = True) -> None:
        self.data[key] = value
        if save:
            self.save()

    def save(self) -> None:
        self.save_count += 1


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class AcceptedPairingDialog:
    def __init__(self, host: str, pairing_port: int | None, parent=None) -> None:
        self.initial_host = host
        self.initial_port = pairing_port

    def exec(self) -> int:
        return QDialog.Accepted

    def values(self) -> tuple[str, int, str]:
        return "192.168.1.40", 37123, "123456"


class DashboardPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        tools_dir = Path(self.temp_dir.name) / "platform-tools"
        tools_dir.mkdir()
        adb_path = tools_dir / "adb.exe"
        fastboot_path = tools_dir / "fastboot.exe"
        adb_path.touch()
        fastboot_path.touch()
        self.tools = PlatformToolsInfo(
            folder=tools_dir,
            adb_path=adb_path,
            fastboot_path=fastboot_path,
            adb_version="Android Debug Bridge version 1.0.41",
            fastboot_version="fastboot version 37.0.0",
            adb_works=True,
            fastboot_works=True,
        )
        self.settings = MemorySettings()
        self.page = DashboardPage(self.settings)
        self.page.resize(720, 720)
        self.page.show()
        self.page.update_tools(self.tools)
        self.app.processEvents()

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def test_connection_states_are_textual_and_actionable(self) -> None:
        cases = [
            (DeviceInfo(mode="No device", state="none"), "NO DEVICE", "No Android device detected", "Connection help"),
            (
                DeviceInfo(serial="device-1", model="Pixel", mode="Unauthorized", state="unauthorized"),
                "AUTHORIZATION REQUIRED",
                "USB debugging authorization required",
                "Show authorization steps",
            ),
            (
                DeviceInfo(serial="device-1", model="Pixel", mode="Offline", state="offline"),
                "OFFLINE",
                "Device is offline",
                "Reconnect",
            ),
            (
                DeviceInfo(
                    serial="device-1",
                    model="Pixel 10",
                    android_version="16",
                    form_factor="Phone",
                    mode="ADB",
                    state="device",
                ),
                "CONNECTED",
                "Connected via ADB",
                "Open applications",
            ),
            (
                DeviceInfo(serial="device-1", model="Pixel", mode="Recovery", state="recovery"),
                "RECOVERY",
                "Connected in Recovery mode",
                "Open applications",
            ),
            (
                DeviceInfo(serial="device-1", model="Pixel", mode="Fastboot", state="fastboot"),
                "FASTBOOT",
                "Device is in Fastboot mode",
                "Open Fastboot commands",
            ),
        ]
        for device, badge, status, action in cases:
            with self.subTest(mode=device.mode):
                self.page.update_device(device)
                self.assertEqual(self.page.status_badge.text(), badge)
                self.assertEqual(self.page.connection_status_title.text(), status)
                self.assertEqual(self.page.mode_value.text(), device.mode)
                self.assertEqual(self.page.primary_action_button.text(), action)

    def test_primary_actions_emit_the_state_specific_safe_requests(self) -> None:
        requested: list[tuple[str, str]] = []
        self.page.reconnect_device_requested.connect(lambda: requested.append(("reconnect", "")))
        self.page.verify_tools_requested.connect(lambda: requested.append(("verify", "")))
        self.page.open_commands_requested.connect(
            lambda category: requested.append(("commands", category))
        )
        self.page.open_page_requested.connect(lambda page: requested.append(("page", page)))

        self.page.update_device(DeviceInfo(serial="offline-1", mode="Offline", state="offline"))
        self.page.primary_action_button.click()
        self.page.update_device(DeviceInfo(serial="fastboot-1", mode="Fastboot", state="fastboot"))
        self.page.primary_action_button.click()
        self.page.update_device(DeviceInfo(serial="adb-1", mode="ADB", state="device"))
        self.page.primary_action_button.click()

        partial_dir = Path(self.temp_dir.name) / "partial-tools"
        partial_dir.mkdir()
        partial_adb = partial_dir / "adb.exe"
        partial_adb.touch()
        self.page.update_tools(
            PlatformToolsInfo(folder=partial_dir, adb_path=partial_adb, adb_works=True)
        )
        self.assertEqual(self.page.primary_action_button.text(), "Verify Platform Tools")
        self.page.primary_action_button.click()

        self.assertEqual(
            requested,
            [
                ("reconnect", ""),
                ("commands", "Fastboot"),
                ("page", "Apps"),
                ("verify", ""),
            ],
        )

    def test_help_actions_cover_the_checklists_and_can_refresh(self) -> None:
        with patch.object(self.page, "_show_help_dialog") as show_dialog:
            self.page.update_device(DeviceInfo(mode="No device", state="none"))
            self.page.primary_action_button.click()
            title, _text, checklist = show_dialog.call_args.args
            self.assertEqual(title, "Connection help")
            for expected in ("USB debugging", "RSA", "cable", "driver", "Platform Tools", "Refresh"):
                self.assertIn(expected, checklist)

            show_dialog.reset_mock()
            self.page.update_device(
                DeviceInfo(serial="device-1", mode="Unauthorized", state="unauthorized")
            )
            self.page.primary_action_button.click()
            title, _text, checklist = show_dialog.call_args.args
            self.assertEqual(title, "USB debugging authorization")
            self.assertIn("RSA fingerprint", checklist)
            self.assertIn("Refresh", checklist)

        refreshed: list[bool] = []
        self.page.refresh_device_requested.connect(lambda: refreshed.append(True))
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.Retry):
            self.page._show_connection_help()
        self.assertEqual(refreshed, [True])

    def test_each_dashboard_state_has_one_visible_action_per_function(self) -> None:
        full_tools = self.tools
        partial_dir = Path(self.temp_dir.name) / "uniqueness-partial"
        partial_dir.mkdir()
        partial_adb = partial_dir / "adb.exe"
        partial_adb.touch()
        partial_tools = PlatformToolsInfo(folder=partial_dir, adb_path=partial_adb)
        states = [
            (full_tools, DeviceInfo(mode="No device", state="none")),
            (full_tools, DeviceInfo(serial="u", mode="Unauthorized", state="unauthorized")),
            (full_tools, DeviceInfo(serial="o", mode="Offline", state="offline")),
            (full_tools, DeviceInfo(serial="a", mode="ADB", state="device")),
            (full_tools, DeviceInfo(serial="r", mode="Recovery", state="recovery")),
            (full_tools, DeviceInfo(serial="f", mode="Fastboot", state="fastboot")),
            (full_tools, DeviceInfo(mode="Checking", state="checking")),
            (PlatformToolsInfo(), DeviceInfo(serial="a", mode="ADB", state="device")),
            (partial_tools, DeviceInfo(serial="a", mode="ADB", state="device")),
        ]

        for tools, device in states:
            with self.subTest(tools=tools.status, mode=device.mode):
                self.page.update_tools(tools)
                self.page.update_device(device)
                self.app.processEvents()
                primary_text = self.page.primary_action_button.text().strip().casefold()
                quick_text = self.page.refresh_button.text().strip().casefold()
                if self.page._recommended_action == "refresh":
                    self.assertTrue(self.page.refresh_button.isHidden())
                else:
                    self.assertTrue(self.page.refresh_button.isVisible())
                    self.assertNotEqual(primary_text, quick_text)

    def test_missing_tools_override_the_recommended_action(self) -> None:
        self.page.update_device(DeviceInfo(serial="device-1", model="Pixel", mode="ADB", state="device"))
        self.page.update_tools(PlatformToolsInfo())
        requested: list[str] = []
        self.page.detect_tools_requested.connect(lambda: requested.append("detect"))
        self.assertEqual(self.page.primary_action_button.text(), "Set up Platform Tools")
        self.page.primary_action_button.click()
        self.assertEqual(requested, ["detect"])

    def test_fastboot_navigation_selects_the_matching_commands_category(self) -> None:
        category_filter = QComboBox()
        category_filter.addItems(["All categories", "Common", "ADB", "Fastboot"])
        window = SimpleNamespace(
            open_page=MagicMock(),
            commands_page=SimpleNamespace(category_filter=category_filter),
        )

        try:
            MainWindow.open_dashboard_commands(window, "Fastboot")

            window.open_page.assert_called_once_with("Commands")
            self.assertEqual(category_filter.currentText(), "Fastboot")
        finally:
            category_filter.deleteLater()

    def test_quick_action_menus_preserve_previous_commands(self) -> None:
        self.page.update_device(DeviceInfo(serial="device-1", model="Pixel", mode="ADB", state="device"))
        commands: list[str] = []
        pages: list[str] = []
        self.page.command_requested.connect(commands.append)
        self.page.open_page_requested.connect(pages.append)
        for key in [
            "adb_reboot",
            "adb_reboot_recovery",
            "adb_reboot_bootloader",
            "adb_reboot_sideload",
        ]:
            self.page.reboot_actions[key].trigger()
        self.page.more_actions["adb_devices"].trigger()
        self.page.more_actions["fastboot_devices"].trigger()
        self.page.more_actions["commands"].trigger()
        self.page.more_actions["logs"].trigger()
        self.page.more_actions["settings"].trigger()
        self.assertEqual(
            commands,
            [
                "adb_reboot",
                "adb_reboot_recovery",
                "adb_reboot_bootloader",
                "adb_reboot_sideload",
                "adb_devices",
                "fastboot_devices",
            ],
        )
        self.assertEqual(pages, ["Commands", "Logs", "Settings"])

    def test_details_state_and_long_values_are_preserved(self) -> None:
        long_serial = "SERIAL-" * 40
        long_path = "C:/" + "/".join(["very-long-platform-tools-folder"] * 25)
        long_tools = PlatformToolsInfo(
            folder=Path(long_path),
            adb_version=self.tools.adb_version,
            fastboot_version=self.tools.fastboot_version,
        )
        self.page.update_device(
            DeviceInfo(
                serial=long_serial,
                model="Very Long Android Device Name " * 15,
                manufacturer="Long Manufacturer " * 10,
                mode="ADB",
                state="device",
            )
        )
        self.page.update_tools(long_tools)
        self.page.details_card.set_expanded(True)
        self.page.resize(660, 720)
        self.app.processEvents()
        self.assertTrue(self.settings.get("dashboard_details_expanded"))
        self.assertEqual(self.page.detail_labels["Serial number"].toolTip(), long_serial)
        normalized_path = str(Path(long_path))
        self.assertEqual(self.page.detail_labels["Active path"].toolTip(), normalized_path)
        self.assertEqual(self.page.detail_labels["Active path"].full_text(), normalized_path)
        self.assertNotEqual(self.page.detail_labels["Active path"].text(), normalized_path)
        self.page.details_card.set_expanded(False)
        self.assertFalse(self.settings.get("dashboard_details_expanded"))

    def test_wireless_scenarios_show_only_relevant_controls_and_save_values(self) -> None:
        self.page.wireless_card.set_expanded(True)

        self._select_scenario(WIRELESS_SCENARIO_MODERN)
        self.page.wireless_host.setText("192.168.1.20")
        self.page.wireless_port.setValue(41000)
        self.page._save_wireless_settings()
        self.assertTrue(self.page.wireless_port.isVisible())
        self.assertEqual(self.page.wireless_actions_stack.currentIndex(), 0)
        self.assertTrue(self.page.wireless_pair.isVisible())
        self.assertFalse(self.page.wireless_enable_tcpip.isVisible())
        self.assertEqual(self.settings.get("wireless_modern_host"), "192.168.1.20")
        self.assertEqual(self.settings.get("wireless_modern_port"), 41000)

        self._select_scenario(WIRELESS_SCENARIO_LEGACY)
        self.page.wireless_host.setText("192.168.1.21")
        self.page._save_wireless_settings()
        self.assertFalse(self.page.wireless_port.isVisible())
        self.assertEqual(self.page.wireless_actions_stack.currentIndex(), 1)
        self.assertTrue(self.page.wireless_enable_tcpip.isVisible())
        self.assertFalse(self.page.wireless_pair.isVisible())
        self.assertEqual(self.settings.get("wireless_legacy_host"), "192.168.1.21")
        self.assertEqual(self.settings.get("wireless_adb_port"), WIRELESS_LEGACY_PORT)

        self._select_scenario(WIRELESS_SCENARIO_TV)
        self.page.wireless_host.setText("living-room-tv.local")
        self.page.wireless_port.setValue(42000)
        self.page._save_wireless_settings()
        self.assertTrue(self.page.wireless_port.isVisible())
        self.assertEqual(self.page.wireless_actions_stack.currentIndex(), 2)
        self.assertTrue(self.page.wireless_scan.isVisible())
        self.assertFalse(self.page.wireless_enable_tcpip.isVisible())
        self.assertEqual(self.settings.get("wireless_tv_host"), "living-room-tv.local")
        self.assertEqual(self.settings.get("wireless_tv_port"), 42000)

        self.page.set_wireless_busy(True)
        self.assertFalse(self.page.wireless_scenario.isEnabled())
        self.assertFalse(self.page.wireless_connect.isEnabled())
        self.page.set_wireless_busy(False)
        self.assertTrue(self.page.wireless_scenario.isEnabled())
        self.assertTrue(self.page.wireless_connect.isEnabled())

    def test_pairing_dialog_values_emit_without_saving_pairing_code(self) -> None:
        self._select_scenario(WIRELESS_SCENARIO_MODERN)
        self.page._pairing_dialog_factory = AcceptedPairingDialog
        emitted: list[tuple[str, int, str]] = []
        self.page.wireless_pair_requested.connect(lambda host, port, code: emitted.append((host, port, code)))
        self.page._request_wireless_pair()
        self.assertEqual(emitted, [("192.168.1.40", 37123, "123456")])
        self.assertEqual(self.settings.get("wireless_modern_pair_port"), "37123")
        self.assertNotIn("123456", [str(value) for value in self.settings.data.values()])

    def test_mdns_wireless_disconnect_does_not_append_form_port(self) -> None:
        self._select_scenario(WIRELESS_SCENARIO_MODERN)
        serial = "adb-3A131FDJG000SZ-example._adb-tls-connect._tcp"
        self.page.wireless_host.setText(serial)
        self.page.wireless_port.setValue(40765)
        emitted: list[tuple[str, object]] = []
        self.page.wireless_disconnect_requested.connect(lambda host, port: emitted.append((host, port)))

        self.page._request_wireless_disconnect()

        self.assertEqual(emitted, [(serial, None)])

    def test_legacy_settings_select_legacy_scenario(self) -> None:
        legacy_settings = MemorySettings(
            wireless_dashboard_scenario="",
            wireless_connection_mode="legacy",
            wireless_adb_mode="legacy",
        )
        page = DashboardPage(legacy_settings)
        try:
            self.assertEqual(page.wireless_scenario.currentData(), WIRELESS_SCENARIO_LEGACY)
        finally:
            page.close()
            page.deleteLater()

    def test_page_is_responsive_and_renders_in_all_themes(self) -> None:
        self.page.update_device(
            DeviceInfo(
                serial="device-1",
                model="Responsive test device",
                android_version="16",
                form_factor="Phone",
                mode="ADB",
                state="device",
            )
        )
        self.page.resize(680, 620)
        for theme in ("System", "Light", "Dark"):
            with self.subTest(theme=theme):
                apply_theme(self.app, theme)
                self.app.processEvents()
                pixmap = self.page.grab()
                self.assertFalse(pixmap.isNull())
                self.assertFalse(self.page.horizontalScrollBar().isVisible())
        self.assertLessEqual(self.page.root.minimumSizeHint().width(), 680)

    def test_collapsed_cards_remain_compact_after_resize(self) -> None:
        self.page.resize(900, 1000)
        self.page.details_card.set_expanded(True)
        self.app.processEvents()
        self.page.details_card.set_expanded(False)
        self.page.wireless_card.set_expanded(True)
        self.page.resize(720, 1000)
        self.app.processEvents()
        self.assertLessEqual(self.page.details_card.height(), 80)
        self.assertFalse(self.page.details_card.content_widget.isVisible())

    def test_expanded_cards_fit_the_real_narrow_main_window_viewport(self) -> None:
        """Expanded content must fit beside compact nav at the 720 px minimum."""

        self.page.details_card.set_expanded(True)
        self.page.wireless_card.set_expanded(True)
        self.page.update_device(
            DeviceInfo(
                serial="serial-with-a-very-long-identifier-0123456789",
                model="A very long Android television model name",
                android_version="16 vendor build with a long description",
                form_factor="Android television",
                mode="ADB",
                state="device",
            )
        )

        # A 720 px MainWindow leaves roughly 620 px for the page after the
        # compact navigation panel; the scroll-area viewport is narrower still.
        for theme in ("Light", "Dark"):
            for page_width in (620, 800, 1180):
                with self.subTest(theme=theme, page_width=page_width):
                    apply_theme(self.app, theme)
                    self.page.resize(page_width, 700)
                    self.app.processEvents()

                    viewport_width = self.page.viewport().width()
                    self.assertLess(viewport_width, page_width)
                    self.assertLessEqual(
                        self.page.root.minimumSizeHint().width(),
                        viewport_width,
                    )
                    self.assertEqual(self.page.root.width(), viewport_width)
                    self.assertFalse(self.page.horizontalScrollBar().isVisible())

    def _select_scenario(self, scenario: str) -> None:
        index = self.page.wireless_scenario.findData(scenario)
        self.assertGreaterEqual(index, 0)
        self.page.wireless_scenario.setCurrentIndex(index)
        self.app.processEvents()


class SettingsCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_old_settings_json_loads_with_new_dashboard_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "theme": "Dark",
                        "wireless_connection_mode": "legacy",
                        "wireless_adb_mode": "legacy",
                    }
                ),
                encoding="utf-8",
            )
            settings = IsolatedSettings(config_dir)
            self.assertEqual(settings.get("theme"), "Dark")
            self.assertEqual(settings.get("wireless_dashboard_scenario"), "")
            self.assertFalse(settings.get("dashboard_details_expanded"))
            self.assertFalse(settings.get("dashboard_wireless_expanded"))
            self.assertEqual(settings.get("wireless_connection_mode"), "legacy")

    def test_dashboard_state_round_trips_through_settings_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            settings = IsolatedSettings(config_dir)
            page = DashboardPage(settings)
            try:
                page.details_card.set_expanded(True)
                page.wireless_card.set_expanded(True)
                page.wireless_scenario.setCurrentIndex(page.wireless_scenario.findData(WIRELESS_SCENARIO_TV))
                page.wireless_host.setText("living-room-tv.local")
                page.wireless_port.setValue(42123)
                page._save_wireless_settings()
            finally:
                page.close()
                page.deleteLater()
                self.app.processEvents()

            reloaded = IsolatedSettings(config_dir)
            restored_page = DashboardPage(reloaded)
            try:
                self.assertTrue(restored_page.details_card.is_expanded())
                self.assertTrue(restored_page.wireless_card.is_expanded())
                self.assertEqual(restored_page.wireless_scenario.currentData(), WIRELESS_SCENARIO_TV)
                self.assertEqual(restored_page.wireless_host.text(), "living-room-tv.local")
                self.assertEqual(restored_page.wireless_port.value(), 42123)
            finally:
                restored_page.close()
                restored_page.deleteLater()
                self.app.processEvents()


class DashboardCommandSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_sideload_reboot_defaults_to_cancel_without_starting_worker(self) -> None:
        parent = QWidget()
        try:
            with (
                patch.object(QMessageBox, "warning", return_value=QMessageBox.Cancel),
                patch("openadb.ui.main_window.start_worker") as start_worker,
            ):
                MainWindow.run_dashboard_command(parent, "adb_reboot_sideload")
            start_worker.assert_not_called()
        finally:
            parent.close()
            parent.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
