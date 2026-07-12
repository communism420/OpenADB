from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.fastboot import FastbootClient
from openadb.core.icon_extractor import IconExtractor
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.settings_manager import SettingsManager
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.main_window import MainWindow
from openadb.ui.style import apply_theme


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class AdaptiveMainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.windows: list[MainWindow] = []
        self.single_shot_patch = patch("openadb.ui.main_window.QTimer.singleShot")
        self.native_panel_patch = patch(
            "openadb.ui.file_manager_page.NativeExplorerPanel",
            side_effect=RuntimeError("Use deterministic Qt fallback in tests"),
        )
        self.single_shot_patch.start()
        self.native_panel_patch.start()

    def tearDown(self) -> None:
        for window in reversed(self.windows):
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.native_panel_patch.stop()
        self.single_shot_patch.stop()
        self.temp_dir.cleanup()

    def _settings(self) -> IsolatedSettings:
        settings = IsolatedSettings(self.config_dir)
        settings.set("auto_refresh_device", False)
        return settings

    def _window(self, settings: IsolatedSettings | None = None) -> MainWindow:
        settings = settings or self._settings()
        platform_tools = PlatformToolsManager(settings)
        runner = CommandRunner(settings.logs_folder)
        adb = ADBClient(platform_tools, runner)
        fastboot = FastbootClient(platform_tools, runner)
        device_manager = DeviceManager(adb, fastboot, settings)
        window = MainWindow(
            settings=settings,
            platform_tools=platform_tools,
            runner=runner,
            adb=adb,
            fastboot=fastboot,
            device_manager=device_manager,
            backup_manager=BackupManager(settings),
            icon_extractor=IconExtractor(settings),
        )
        self.windows.append(window)
        return window

    def test_navigation_icons_accessibility_and_collapsed_state_round_trip(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        self.assertFalse(window.navigation_collapsed)
        for row, name in enumerate(window.pages):
            item = window.nav.item(row)
            self.assertFalse(item.icon().isNull())
            self.assertEqual(item.text(), name)
            self.assertEqual(item.toolTip(), name)
            self.assertEqual(item.data(Qt.AccessibleTextRole), name)

        window.toggle_navigation()
        self.assertTrue(window.navigation_collapsed)
        self.assertTrue(all(not window.nav.item(row).text() for row in range(window.nav.count())))
        self.assertEqual(window.nav_toggle.accessibleName(), "Expand navigation")
        self.assertTrue(settings.get_global("navigation_collapsed"))

        window._save_window_state()
        restored = self._window(IsolatedSettings(self.config_dir))
        self.assertTrue(restored.navigation_collapsed)
        restored.toggle_navigation()
        self.assertFalse(restored.navigation_collapsed)
        self.assertEqual(restored.nav_toggle.accessibleName(), "Collapse navigation")

    def test_window_geometry_round_trip_uses_global_settings(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        window.show()
        window.setGeometry(30, 40, 740, 600)
        self.app.processEvents()
        window._save_window_state()

        settings.activate_device_profile("device-one", "Test device", "Phone")
        settings.set_global_values({"navigation_collapsed": True})
        self.assertTrue(settings.get_global("navigation_collapsed"))
        global_json = json.loads(settings.global_path.read_text(encoding="utf-8"))
        self.assertEqual(global_json["window_width"], 740)
        self.assertEqual(global_json["window_height"], 600)

        restored = self._window(IsolatedSettings(self.config_dir))
        restored.show()
        self.app.processEvents()
        self.assertEqual(restored.geometry(), QRect(30, 40, 740, 600))
        self.assertTrue(restored.navigation_collapsed)

    def test_disconnected_monitor_and_oversized_geometry_are_recovered(self) -> None:
        primary = QRect(0, 0, 1920, 1040)
        disconnected = QRect(4000, 300, 1100, 800)
        recovered = MainWindow._bounded_window_geometry(disconnected, [primary])
        self.assertTrue(primary.contains(recovered))
        self.assertEqual(recovered.size(), disconnected.size())

        second = QRect(1920, 0, 1920, 1040)
        valid_second_screen = QRect(2100, 100, 1000, 700)
        self.assertEqual(
            MainWindow._bounded_window_geometry(valid_second_screen, [primary, second]),
            valid_second_screen,
        )

        oversized = MainWindow._bounded_window_geometry(QRect(-500, -500, 5000, 3000), [primary])
        self.assertEqual(oversized, primary)

    def test_narrow_standard_and_maximized_layout_in_all_themes(self) -> None:
        window = self._window()
        window.show()
        window._set_navigation_collapsed(True, persist=False)
        for width, height in [(760, 520), (960, 640)]:
            window.showNormal()
            window.resize(width, height)
            self.app.processEvents()
            self.assertEqual(window.size().width(), width)
            self.assertGreaterEqual(window.stack.width(), width - 110)
            for row in range(window.nav.count()):
                window.nav.setCurrentRow(row)
                self.app.processEvents()
                self.assertEqual(window.stack.currentIndex(), row)

        for theme in ("System", "Light", "Dark"):
            apply_theme(self.app, theme)
            self.app.processEvents()
            self.assertFalse(window.grab().isNull())

        window.showMaximized()
        self.app.processEvents()
        self.assertTrue(window.isMaximized())
        window._save_window_state()
        self.assertTrue(window.settings.get_global("window_maximized"))
        restored = self._window(IsolatedSettings(self.config_dir))
        restored.show()
        self.app.processEvents()
        self.assertTrue(restored.isMaximized())

    def test_legacy_settings_receive_safe_ui_defaults(self) -> None:
        (self.config_dir / "settings.json").write_text(
            json.dumps({"theme": "Dark", "auto_refresh_device": False}),
            encoding="utf-8",
        )
        settings = IsolatedSettings(self.config_dir)
        self.assertEqual(settings.get_global("window_width"), 1280)
        self.assertEqual(settings.get_global("window_height"), 820)
        self.assertFalse(settings.get_global("window_maximized"))
        self.assertFalse(settings.get_global("navigation_collapsed"))

    def test_selected_platform_tools_verification_does_not_run_discovery(self) -> None:
        settings = self._settings()
        window = self._window(settings)
        selected = self.config_dir / "selected-platform-tools"
        selected.mkdir()
        info = window.platform_tools.choose_folder(selected)
        window._update_tools(info)
        verified = PlatformToolsInfo(folder=selected, source="Manual")

        with (
            patch.object(window.platform_tools, "inspect_folder", return_value=verified) as inspect,
            patch.object(window.platform_tools, "detect") as detect,
            patch("openadb.ui.main_window.start_worker") as start,
        ):
            window.verify_selected_platform_tools()
            worker = start.call_args.args[2]
            worker.run()

        inspect.assert_called_once_with(selected, "Manual")
        detect.assert_not_called()
        self.assertIn("Verification result", window.settings_page.last_verification.text())
        self.assertFalse(window._verifying_platform_tools)

    def test_platform_tools_find_cancel_keeps_previous_selection(self) -> None:
        window = self._window()
        previous = PlatformToolsInfo(folder=self.config_dir / "previous", source="Saved settings")
        window.platform_tools.active = previous
        candidates = [
            PlatformToolsInfo(folder=self.config_dir / "candidate-one", source="PATH"),
            PlatformToolsInfo(folder=self.config_dir / "candidate-two", source="Android SDK"),
        ]

        with patch("openadb.ui.main_window.PlatformToolsPickerDialog") as picker:
            picker.return_value.exec.return_value = 0
            window._platform_tools_detected(candidates, interactive=True)

        self.assertIs(window.platform_tools.active, previous)
        self.assertIn("cancelled", window.settings_page.last_verification.text())

    def test_platform_tools_find_can_open_manual_choice_before_worker_finished(self) -> None:
        window = self._window()
        window._detecting_platform_tools = True
        with (
            patch.object(QMessageBox, "warning", return_value=QMessageBox.Yes),
            patch.object(window, "_choose_platform_tools_folder") as choose,
        ):
            window._platform_tools_detected([], interactive=True)
        choose.assert_called_once_with()

    def test_icon_cache_action_calls_cache_manager(self) -> None:
        window = self._window()
        with (
            patch.object(window.icon_extractor, "clear_cache") as clear,
            patch.object(QMessageBox, "information"),
        ):
            window._clear_icon_cache()
        clear.assert_called_once_with()

    def test_full_reset_cancel_preserves_settings_and_profile(self) -> None:
        settings = self._settings()
        settings.set("theme", "Dark")
        settings.activate_device_profile("keep-device", "Keep phone", "Phone")
        profile_path = settings.path
        window = self._window(settings)

        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Cancel):
            window._reset_all_settings_and_caches()

        self.assertEqual(settings.get("theme"), "Dark")
        self.assertTrue(profile_path.exists())
        self.assertIn("cancelled", window.statusBar().currentMessage().lower())

    def test_ui_reset_applies_to_current_pages_without_removing_operational_settings(self) -> None:
        settings = self._settings()
        settings.set("theme", "Dark")
        settings.set("apps_filter_type", "system")
        settings.set("platform_tools_path", "C:/keep/platform-tools")
        settings.set_global_values({"navigation_collapsed": True, "window_width": 760, "window_height": 520})
        window = self._window(settings)
        window._set_navigation_collapsed(True, persist=False)

        with (
            patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok),
            patch.object(QMessageBox, "information"),
        ):
            window._reset_ui_settings()

        self.assertFalse(window.navigation_collapsed)
        self.assertEqual(window.settings_page.theme.currentText(), "System")
        self.assertEqual(window.apps_page._filter_values["type"], "all")
        self.assertEqual(settings.get("platform_tools_path"), "C:/keep/platform-tools")
        self.assertEqual(settings.get_global("window_width"), MainWindow.DEFAULT_WINDOW_SIZE.width())
        self.assertEqual(settings.get_global("window_height"), MainWindow.DEFAULT_WINDOW_SIZE.height())

    def test_identical_device_refresh_does_not_reload_open_file_manager_repeatedly(self) -> None:
        window = self._window()
        window.stack.setCurrentWidget(window.file_manager_page)
        device = window.device_manager.active
        device.serial = "stable-device"
        device.mode = "ADB"
        device.state = "device"
        with (
            patch.object(window, "_activate_device_profile", side_effect=[True, False, False]),
            patch.object(window.file_manager_page, "refresh_all") as refresh_all,
        ):
            window._on_device_refreshed(device)
            window._on_device_refreshed(device)
            window._on_device_refreshed(device)
        refresh_all.assert_called_once_with()

    def test_close_cancels_operations_and_stops_owned_processes(self) -> None:
        window = self._window()
        with (
            patch.object(window.commands_page, "cancel_running_command") as cancel_command,
            patch.object(window.file_manager_page, "cancel_active_transfers") as cancel_transfers,
            patch.object(window.device_bar, "stop_device_monitor") as stop_monitor,
            patch.object(window.runner, "shutdown") as shutdown_runner,
        ):
            window.close()
        cancel_command.assert_called_once_with()
        cancel_transfers.assert_called_once_with()
        stop_monitor.assert_called_once_with()
        shutdown_runner.assert_called_once_with()
        self.assertTrue(window._closing)
        for owner in (
            window,
            window.device_bar,
            window.apps_page,
            window.backups_page,
            window.file_manager_page,
            window.commands_page,
        ):
            self.assertTrue(owner._workers_shutting_down)

    def test_second_qr_pair_request_reuses_existing_dialog(self) -> None:
        window = self._window()
        dialog = MagicMock()
        window._wireless_qr_dialog = dialog
        with patch("openadb.ui.main_window.generate_wireless_qr_payload") as generate_payload:
            window.pair_wireless_adb_qr()
        generate_payload.assert_not_called()
        dialog.show.assert_called_once_with()
        dialog.raise_.assert_called_once_with()
        dialog.activateWindow.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
