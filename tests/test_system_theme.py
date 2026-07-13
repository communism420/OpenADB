from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest

from openadb.ui.style import apply_theme
from openadb.ui.system_theme import SystemThemeController


class _FakeSystemThemeProvider:
    def __init__(self, theme: str) -> None:
        self.theme = theme
        self.read_count = 0

    def current_theme(self) -> str:
        self.read_count += 1
        return self.theme


class _RefreshProbe(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.icon_refreshes = 0
        self.semantic_refreshes = 0

    def refresh_material_icons(self) -> None:
        self.icon_refreshes += 1

    def refresh_semantic_colors(self) -> None:
        self.semantic_refreshes += 1


class SystemThemeControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        apply_theme(self.app, "Light")
        self.provider = _FakeSystemThemeProvider("Light")
        self.controller = SystemThemeController(
            self.app,
            provider=self.provider,
            poll_interval_ms=60_000,
        )

    def tearDown(self) -> None:
        self.controller.stop()

    def test_system_light_to_dark_and_dark_to_light_apply_once_each(self) -> None:
        probe = _RefreshProbe()
        self.controller.start("System")
        probe.icon_refreshes = 0
        probe.semantic_refreshes = 0

        self.provider.theme = "Dark"
        self.assertTrue(self.controller.poll_now())
        self.assertEqual(self.app.property("openadbResolvedTheme"), "Dark")
        self.assertEqual(probe.icon_refreshes, 1)
        self.assertEqual(probe.semantic_refreshes, 1)
        self.assertFalse(self.controller.poll_now())
        self.assertEqual(probe.icon_refreshes, 1)

        self.provider.theme = "Light"
        self.assertTrue(self.controller.poll_now())
        self.assertEqual(self.app.property("openadbResolvedTheme"), "Light")
        self.assertEqual(probe.icon_refreshes, 2)
        self.assertEqual(probe.semantic_refreshes, 2)
        probe.deleteLater()

    def test_explicit_light_ignores_system_changes_and_does_not_poll(self) -> None:
        self.controller.start("Light")
        reads_before = self.provider.read_count
        self.provider.theme = "Dark"

        self.assertFalse(self.controller.is_listening)
        self.assertFalse(self.controller.poll_now())
        self.assertEqual(self.provider.read_count, reads_before)
        self.assertEqual(self.app.property("openadbResolvedTheme"), "Light")

    def test_explicit_dark_ignores_system_changes_and_does_not_poll(self) -> None:
        self.controller.start("Dark")
        reads_before = self.provider.read_count
        self.provider.theme = "Light"

        self.assertFalse(self.controller.is_listening)
        self.assertFalse(self.controller.poll_now())
        self.assertEqual(self.provider.read_count, reads_before)
        self.assertEqual(self.app.property("openadbResolvedTheme"), "Dark")

    def test_stop_disables_listener_and_future_refreshes(self) -> None:
        applied: list[str] = []
        controller = SystemThemeController(
            self.app,
            provider=self.provider,
            theme_applier=lambda _app, theme: applied.append(theme),
            poll_interval_ms=60_000,
        )
        controller.start("System")
        controller.stop()
        self.provider.theme = "Dark"

        self.assertFalse(controller.is_listening)
        self.assertFalse(controller.poll_now())
        self.assertEqual(applied, [])

    def test_system_timer_observes_a_live_change_without_manual_poll(self) -> None:
        controller = SystemThemeController(
            self.app,
            provider=self.provider,
            poll_interval_ms=250,
        )
        self.addCleanup(controller.stop)
        controller.start("System")
        self.provider.theme = "Dark"

        QTest.qWait(350)

        self.assertEqual(self.app.property("openadbResolvedTheme"), "Dark")
        controller.stop()

    def test_invalid_mode_and_provider_value_fall_back_safely(self) -> None:
        self.provider.theme = "unexpected"
        self.controller.start("broken-setting")

        self.assertEqual(self.controller.theme_mode, "System")
        self.assertTrue(self.controller.is_listening)
        self.assertEqual(self.app.property("openadbResolvedTheme"), "Light")


if __name__ == "__main__":
    unittest.main()
