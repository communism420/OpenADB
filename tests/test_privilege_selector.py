from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from openadb.core.privilege import PrivilegeBackend
from openadb.ui.style import apply_theme
from openadb.ui.widgets.privilege_selector import PrivilegeModeSelector


class PrivilegeModeSelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.widgets: list[PrivilegeModeSelector] = []

    def tearDown(self) -> None:
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def _selector(self, *, compact: bool = False) -> PrivilegeModeSelector:
        selector = PrivilegeModeSelector(compact=compact)
        self.widgets.append(selector)
        return selector

    def test_exposes_exactly_three_explicit_backends(self) -> None:
        selector = self._selector()

        self.assertEqual(selector.count(), 3)
        self.assertEqual(
            [selector.itemData(index) for index in range(selector.count())],
            ["standard", "root", "shizuku"],
        )
        self.assertEqual(selector.backend(), PrivilegeBackend.STANDARD)
        self.assertIn("no requested Root/Shizuku", selector.currentText())

    def test_programmatic_sync_does_not_emit_but_user_choice_does(self) -> None:
        selector = self._selector()
        emitted: list[str] = []
        selector.backend_changed.connect(emitted.append)

        selector.set_backend("shizuku")

        self.assertEqual(selector.backend(), PrivilegeBackend.SHIZUKU)
        self.assertEqual(emitted, [])

        selector.setCurrentIndex(selector.findData("root"))

        self.assertEqual(emitted, ["root"])

    def test_empty_offline_state_can_queue_standard_directly(self) -> None:
        selector = self._selector()
        emitted: list[str] = []
        selector.backend_changed.connect(emitted.append)

        selector.set_profile_available(False)
        selector.set_pending_backend("")

        self.assertFalse(selector.has_backend())
        self.assertEqual(selector.currentIndex(), -1)
        self.assertIn("No access-mode override is queued", selector.toolTip())

        selector.setCurrentIndex(selector.findData("standard"))

        self.assertTrue(selector.has_backend())
        self.assertEqual(emitted, ["standard"])

    def test_accessibility_and_runtime_status_are_textual(self) -> None:
        selector = self._selector(compact=True)
        selector.set_backend(PrivilegeBackend.SHIZUKU)
        selector.set_runtime_status("Permission required on Android")

        self.assertEqual(selector.currentText(), "Shizuku")
        self.assertEqual(selector.accessibleName(), "Privilege mode")
        self.assertIn("Permission required on Android", selector.toolTip())
        self.assertIn(
            "Permission required on Android",
            selector.accessibleDescription(),
        )

    def test_compact_selector_renders_in_all_themes(self) -> None:
        selector = self._selector(compact=True)
        selector.resize(140, 32)
        selector.show()

        for theme in ("System", "Light", "Dark"):
            with self.subTest(theme=theme):
                apply_theme(self.app, theme)
                self.app.processEvents()
                self.assertFalse(selector.grab().isNull())


if __name__ == "__main__":
    unittest.main()
