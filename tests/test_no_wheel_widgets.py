from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from openadb.ui.style import apply_theme
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox


class NoWheelComboBoxAdaptiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_long_current_value_is_bounded_and_preserved_in_tooltip(self) -> None:
        combo = NoWheelComboBox()
        original = "A deliberately long storage volume label with free space and /storage/path"
        updated = "A newly discovered Android storage volume with another long path"
        combo.addItem(original)
        combo.setToolTip("Choose the active Android storage volume.")
        combo.resize(120, 30)

        for theme in ("Light", "Dark", "System"):
            with self.subTest(theme=theme):
                apply_theme(self.app, theme)
                combo.show()
                self.app.processEvents()
                self.assertFalse(combo.grab().isNull())
                self.assertEqual(combo.currentText(), original)
                self.assertIn(original, combo.toolTip())
                self.assertIn("Choose the active", combo.toolTip())
                self.assertLess(combo.width(), combo.fontMetrics().horizontalAdvance(original))

        combo.setItemText(0, updated)
        self.app.processEvents()
        self.assertEqual(combo.currentText(), updated)
        self.assertIn(updated, combo.toolTip())
        self.assertNotIn(original, combo.toolTip())
        self.assertIn("Choose the active", combo.toolTip())


if __name__ == "__main__":
    unittest.main()
