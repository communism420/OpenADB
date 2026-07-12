from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle

from openadb.models.app_info import AppInfo
from openadb.models.file_item import FileItem
from openadb.ui.material_icons import material_icon, material_icon_names
from openadb.ui.style import apply_theme
from openadb.ui.widgets.app_list_widget import AppTable
from openadb.ui.widgets.collapsible_card import CollapsibleCard
from openadb.ui.widgets.file_panel import FilePanel


def first_opaque_color(icon: QIcon, size: int = 24) -> str:
    image = icon.pixmap(size, size).toImage()
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() > 0:
                return color.name()
    return ""


class MaterialIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_all_material_symbols_render_as_nonempty_vectors(self) -> None:
        expected = {
            "apps",
            "backup",
            "dashboard",
            "description",
            "draft",
            "folder",
            "info",
            "settings",
            "terminal",
        }
        self.assertTrue(expected.issubset(material_icon_names()))
        for name in material_icon_names():
            with self.subTest(name=name):
                icon = material_icon(name)
                self.assertFalse(icon.isNull())
                self.assertEqual(icon.name(), name)
                self.assertTrue(first_opaque_color(icon))

    def test_icons_follow_theme_modes_and_high_dpi(self) -> None:
        apply_theme(self.app, "Light")
        icon = material_icon("settings")
        light = first_opaque_color(icon)
        disabled = first_opaque_color(QIcon(icon.pixmap(24, 24, QIcon.Disabled)))
        apply_theme(self.app, "Dark")
        dark = first_opaque_color(icon)

        self.assertNotEqual(light, dark)
        self.assertNotEqual(light, disabled)
        high_dpi = icon.pixmap(QSize(24, 24), 2.0)
        self.assertEqual(high_dpi.size(), QSize(48, 48))
        self.assertEqual(high_dpi.devicePixelRatio(), 2.0)
        self.assertEqual(high_dpi.deviceIndependentSize(), QSize(24, 24))

    def test_proxy_style_replaces_stock_dialog_and_file_icons(self) -> None:
        apply_theme(self.app, "Light")
        expected = {
            QStyle.SP_MessageBoxInformation: "info",
            QStyle.SP_MessageBoxWarning: "warning",
            QStyle.SP_MessageBoxCritical: "error",
            QStyle.SP_MessageBoxQuestion: "help",
            QStyle.SP_DirIcon: "folder",
            QStyle.SP_FileIcon: "draft",
        }
        for standard_icon, name in expected.items():
            with self.subTest(name=name):
                icon = self.app.style().standardIcon(standard_icon)
                self.assertEqual(icon.name(), name)
                self.assertTrue(first_opaque_color(icon))

    def test_widgets_use_material_icons_instead_of_platform_glyphs(self) -> None:
        apply_theme(self.app, "Light")
        card = CollapsibleCard("Details", expanded=False)
        self.assertEqual(card.toggle_button.icon().name(), "chevron_right")
        card.set_expanded(True)
        self.assertEqual(card.toggle_button.icon().name(), "expand_more")

        panel = FilePanel("Android", "android")
        panel.set_items(
            [
                FileItem(name="Folder", path="/sdcard/Folder", is_dir=True),
                FileItem(name="file.txt", path="/sdcard/file.txt", is_dir=False),
            ]
        )
        self.assertEqual(panel.table.item(0, 0).icon().name(), "folder")
        self.assertEqual(panel.table.item(1, 0).icon().name(), "draft")

        table = AppTable()
        fallback = table._fallback_icon(AppInfo("com.example", "Example"))
        self.assertEqual(fallback.name(), "apps")


if __name__ == "__main__":
    unittest.main()
