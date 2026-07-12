from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.core.wireless_qr import WirelessQrPayload
from openadb.ui.backups_page import BackupsPage
from openadb.ui.design_system import DARK_COLORS, LAYOUT, LIGHT_COLORS, TYPOGRAPHY
from openadb.ui.device_status_bar import DeviceDetailsDialog
from openadb.ui.logs_page import LogsPage
from openadb.ui.style import DARK, LIGHT, apply_theme
from openadb.ui.widgets.device_picker_dialog import DevicePickerDialog
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.widgets.file_panel import FilePanel
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox, NoWheelSpinBox
from openadb.ui.widgets.platform_tools_picker_dialog import PlatformToolsPickerDialog
from openadb.ui.widgets.progress_dialog import ActivityDialog, TransferProgressDialog
from openadb.ui.widgets.wireless_pairing_dialog import WirelessPairingDialog
from openadb.ui.widgets.wireless_qr_dialog import WirelessQrDialog


def _relative_luminance(color: str) -> float:
    values = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    converted = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
    return 0.2126 * converted[0] + 0.7152 * converted[1] + 0.0722 * converted[2]


def _contrast(foreground: str, background: str) -> float:
    high, low = sorted((_relative_luminance(foreground), _relative_luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class DesignSystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self) -> None:
        for widget in QApplication.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def test_shared_tokens_and_theme_component_rules_are_complete(self) -> None:
        self.assertEqual(LAYOUT.page_margins, (16, 12, 16, 12))
        self.assertEqual(LAYOUT.button_height, 32)
        self.assertEqual(LAYOUT.compact_button_height, 28)
        self.assertEqual(LAYOUT.input_height, 30)
        self.assertEqual(TYPOGRAPHY.page_title_pt, 22)
        for stylesheet in (LIGHT, DARK):
            for selector in (
                "QLabel#pageTitle",
                "QFrame#emptyState",
                'QPushButton[uiRole="danger"]',
                "QPushButton:focus",
                "QToolTip",
                "QDialog#appDialog",
            ):
                self.assertIn(selector, stylesheet)

    def test_light_and_dark_semantic_colors_meet_contrast_targets(self) -> None:
        pairs = (
            ("text", "canvas"),
            ("text_secondary", "canvas"),
            ("disabled_text", "disabled_surface"),
            ("danger", "danger_surface"),
            ("warning", "warning_surface"),
            ("link", "canvas"),
            ("selection_text", "selection"),
            ("tooltip_text", "tooltip_surface"),
        )
        for theme in (LIGHT_COLORS, DARK_COLORS):
            for foreground, background in pairs:
                with self.subTest(theme=theme.canvas, pair=(foreground, background)):
                    self.assertGreaterEqual(_contrast(getattr(theme, foreground), getattr(theme, background)), 4.0)

    def test_empty_state_has_one_accessible_keyboard_action(self) -> None:
        state = EmptyState("No logs", "Run a command to create a log entry.", "Open logs folder")
        triggered = MagicMock()
        state.action_requested.connect(triggered)
        state.show()
        self.app.processEvents()
        state.action_button.setFocus()
        QTest.keyClick(state.action_button, Qt.Key_Return)
        self.assertEqual(state.accessibleName(), "No logs")
        self.assertEqual(state.action_button.text(), "Open logs folder")
        triggered.assert_called_once_with()

    def test_backups_logs_and_folders_use_the_same_actionable_empty_state(self) -> None:
        backup_manager = MagicMock()
        backup_manager.settings.logs_folder = Path("C:/OpenADB/logs")
        pages = [
            (BackupsPage(backup_manager, MagicMock(), MagicMock()).empty_state, "No backups"),
            (LogsPage(Path("C:/OpenADB/logs")).empty_state, "No logs"),
            (FilePanel("Android", "android").empty_state, "Empty folder"),
        ]
        for state, title in pages:
            with self.subTest(title=title):
                self.assertIsInstance(state, EmptyState)
                self.assertEqual(state.title_label.text(), title)
                self.assertTrue(state.description_label.text())
                self.assertTrue(state.action_button.text())

    def test_picker_dialogs_expose_long_values_defaults_focus_and_escape(self) -> None:
        serial = "device-" + "x" * 180
        device_dialog = DevicePickerDialog([DeviceInfo(serial=serial, model="Long device", mode="ADB")])
        self.assertEqual(device_dialog.table.item(0, 3).toolTip(), serial)
        self.assertTrue(device_dialog.buttons.button(QDialogButtonBox.Ok).isDefault())
        device_dialog.show()
        QTest.keyClick(device_dialog, Qt.Key_Escape)
        self.assertEqual(device_dialog.result(), QDialog.Rejected)

        long_folder = Path("C:/") / ("very-long-platform-tools-folder-" * 8)
        tools_dialog = PlatformToolsPickerDialog([PlatformToolsInfo(folder=long_folder, source="Manual")])
        self.assertEqual(tools_dialog.table.item(0, 1).toolTip(), str(long_folder))
        self.assertTrue(tools_dialog.buttons.button(QDialogButtonBox.Ok).isDefault())
        self.assertTrue(tools_dialog.accessibleName())

    def test_keyboard_order_enter_escape_themes_and_no_wheel_contract(self) -> None:
        for theme in ("Light", "Dark", "System"):
            apply_theme(self.app, theme)
            dialog = WirelessPairingDialog("192.168.1.2", 37001)
            dialog.pairing_code.setText("123456")
            dialog.resize(460, 280)
            dialog.show()
            self.app.processEvents()
            dialog.host.setFocus()
            first = self.app.focusWidget()
            QTest.keyClick(dialog, Qt.Key_Tab)
            self.assertIsNot(self.app.focusWidget(), first)
            QTest.keyClick(dialog, Qt.Key_Backtab)
            self.assertIs(self.app.focusWidget(), first)
            QTest.keyClick(dialog, Qt.Key_Return)
            self.assertEqual(dialog.result(), QDialog.Accepted)

        event = MagicMock()
        NoWheelComboBox().wheelEvent(event)
        NoWheelSpinBox().wheelEvent(event)
        self.assertEqual(event.ignore.call_count, 2)

    def test_all_custom_dialogs_render_in_both_themes_at_compact_size(self) -> None:
        device = DeviceInfo(serial="device-1", model="Test device", mode="ADB", state="device")
        tools = PlatformToolsInfo(folder=Path("C:/platform-tools"), source="Manual")
        payload = WirelessQrPayload("studio-test", "password1234", "WIFI:T:ADB;S:studio-test;P:password1234;;")
        for theme in ("Light", "Dark"):
            apply_theme(self.app, theme)
            dialogs = [
                DeviceDetailsDialog(device),
                DevicePickerDialog([device]),
                PlatformToolsPickerDialog([tools]),
                WirelessPairingDialog("192.168.1.2", 37001),
                WirelessQrDialog(payload),
                ActivityDialog("Working", "Checking local state"),
                TransferProgressDialog("Transfer"),
            ]
            for dialog in dialogs:
                with self.subTest(theme=theme, dialog=type(dialog).__name__):
                    dialog.resize(620, 420)
                    dialog.show()
                    self.app.processEvents()
                    self.assertEqual(dialog.objectName(), "appDialog")
                    self.assertFalse(dialog.grab().isNull())
                    dialog.close()


if __name__ == "__main__":
    unittest.main()
