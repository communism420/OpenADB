from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication

from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.fastboot import FastbootClient
from openadb.core.icon_extractor import IconExtractor
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.settings_manager import SettingsManager
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


if __name__ == "__main__":
    unittest.main()
