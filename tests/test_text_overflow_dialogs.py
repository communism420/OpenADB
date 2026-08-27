from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QTextEdit, QWidget

from openadb.core.operations import OperationRegistry
from openadb.core.wireless_qr import WirelessQrPayload
from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.design_system import fit_dialog_to_available_screen
from openadb.ui.device_status_bar import DeviceDetailsDialog, DeviceStatusBar
from openadb.ui.dialogs import ErrorDialog, build_bounded_message_box
from openadb.ui.style import apply_theme
from openadb.ui.widgets.device_picker_dialog import DevicePickerDialog
from openadb.ui.widgets.elided_label import ElidedLabel
from openadb.ui.widgets.platform_tools_picker_dialog import PlatformToolsPickerDialog
from openadb.ui.widgets.progress_dialog import ActivityDialog, TransferProgressDialog
from openadb.ui.widgets.wireless_pairing_dialog import WirelessPairingDialog
from openadb.ui.widgets.wireless_qr_dialog import WirelessQrDialog
from tests.qt_text_overflow_harness import find_text_overflow


LONG_TOKEN = "C:/" + "nested-directory-without-breaks_" * 45 + "payload.bin"


class TextOverflowDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        windows_font = Path("C:/Windows/Fonts/segoeui.ttf")
        if windows_font.exists():
            QFontDatabase.addApplicationFont(str(windows_font))
            cls.app.setFont(QFont("Segoe UI", 9))

    def tearDown(self) -> None:
        for widget in QApplication.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def _show(self, widget, width: int | None = None, height: int | None = None) -> None:
        if width is not None and height is not None:
            widget.resize(width, height)
        widget.setAttribute(Qt.WA_DontShowOnScreen, True)
        widget.show()
        self.app.processEvents()

    def assert_elided_text_is_recoverable(self, label: ElidedLabel, expected: str) -> None:
        self.assertEqual(label.full_text(), expected)
        self.assertEqual(label.toolTip(), expected)
        self.assertEqual(label.accessibleDescription(), expected)
        self.assertLessEqual(
            label.fontMetrics().horizontalAdvance(label.text()),
            label.contentsRect().width() + 1,
        )

    def assert_wrapped_labels_fit(self, root) -> None:
        for label in root.findChildren(QLabel):
            if not label.isVisibleTo(root) or not label.wordWrap() or label.width() <= 0:
                continue
            required_height = label.heightForWidth(label.width())
            with self.subTest(widget=type(root).__name__, text=label.text()[:60]):
                self.assertLessEqual(required_height, label.height() + 2)

    def assert_direct_children_do_not_overlap(self, root) -> None:
        children = [
            child
            for child in root.findChildren(
                QWidget,
                options=Qt.FindDirectChildrenOnly,
            )
            if child.isVisibleTo(root)
        ]
        children.sort(key=lambda child: child.geometry().left())
        for left, right in zip(children, children[1:]):
            with self.subTest(left=left.objectName(), right=right.objectName()):
                self.assertLess(left.geometry().right(), right.geometry().left())

    def test_elided_label_preserves_full_text_and_accessibility(self) -> None:
        label = ElidedLabel(LONG_TOKEN, elide_mode=Qt.ElideMiddle)
        self._show(label, 180, 30)

        self.assert_elided_text_is_recoverable(label, LONG_TOKEN)
        self.assertIn("…", label.text())

    def test_progress_dialogs_bound_long_dynamic_values(self) -> None:
        activity = ActivityDialog("Working", LONG_TOKEN)
        self._show(activity, 420, 140)
        self.assert_elided_text_is_recoverable(activity.status_label, LONG_TOKEN)

        transfer = TransferProgressDialog("PC → Android")
        transfer.apply_update(
            {
                "type": "plan",
                "title": "Preparing a transfer with a long but wrappable status message",
                "direction": "PC → Android",
                "total_bytes": 1024,
                "total_files": 1,
                "source": LONG_TOKEN,
                "destination": "/sdcard/" + "destination_" * 80,
            }
        )
        transfer.apply_update(
            {
                "type": "file_start",
                "current_file": LONG_TOKEN,
                "command": "adb exec-in sh -c " + LONG_TOKEN,
            }
        )
        transfer.append_detail(LONG_TOKEN)
        self._show(transfer, 520, 440)

        for label, value in (
            (transfer.source, LONG_TOKEN),
            (transfer.current_file, LONG_TOKEN),
            (transfer.command, "adb exec-in sh -c " + LONG_TOKEN),
        ):
            self.assert_elided_text_is_recoverable(label, value)
        self.assertEqual(transfer.details.horizontalScrollBar().maximum(), 0)
        self.assert_wrapped_labels_fit(transfer)

    def test_error_dialog_wraps_unbroken_details_without_growing_off_screen(self) -> None:
        message = "Permission denied: " + LONG_TOKEN
        dialog = ErrorDialog(None, "Transfer failed", message, Path("C:/OpenADB/logs"))
        self._show(dialog)

        available = dialog.screen().availableGeometry()
        self.assertLessEqual(dialog.width(), available.width())
        self.assertLessEqual(dialog.height(), available.height())
        self.assertEqual(dialog.message_view.toPlainText(), message)
        self.assertEqual(dialog.message_view.accessibleDescription(), message)
        self.assertEqual(dialog.message_view.horizontalScrollBar().maximum(), 0)
        self.assertTrue(dialog.close_button.isDefault())

    def test_bounded_confirmation_keeps_paths_in_wrapped_details(self) -> None:
        details = "Configured path:\n" + LONG_TOKEN
        box = build_bounded_message_box(
            None,
            "Confirm cleanup",
            "This operation is permanent. Open Details to review the path.",
            icon=QMessageBox.Critical,
            buttons=QMessageBox.Yes | QMessageBox.Cancel,
            default_button=QMessageBox.Cancel,
            detailed_text=details,
        )
        self._show(box)

        available = box.screen().availableGeometry()
        self.assertLessEqual(box.width(), available.width())
        self.assertLessEqual(box.height(), available.height())
        detail_views = box.findChildren(QTextEdit)
        self.assertTrue(detail_views)
        self.assertEqual(detail_views[0].toPlainText(), details)
        self.assertEqual(detail_views[0].accessibleDescription(), details)
        self.assertEqual(detail_views[0].horizontalScrollBar().maximum(), 0)

    def test_picker_tables_elide_long_values_and_keep_full_tooltips(self) -> None:
        serial = "adb-" + "serial_" * 80
        devices = DevicePickerDialog(
            [DeviceInfo(serial=serial, model="Model " + "X" * 200, mode="ADB")]
        )
        tools_path = Path("C:/") / ("platform-tools-path-" * 55)
        tools = PlatformToolsPickerDialog(
            [PlatformToolsInfo(folder=tools_path, source="Manual " + "source_" * 50)]
        )
        for dialog in (devices, tools):
            self._show(dialog)
            available = dialog.screen().availableGeometry()
            self.assertLessEqual(dialog.width(), available.width())
            self.assertEqual(dialog.table.textElideMode(), Qt.ElideMiddle)
        self.assertEqual(devices.table.item(0, 3).toolTip(), serial)
        self.assertEqual(tools.table.item(0, 1).toolTip(), str(tools_path))

    def test_wireless_dialogs_fit_and_expose_full_status_and_placeholders(self) -> None:
        pairing = WirelessPairingDialog("192.0.2.25", 37891)
        payload = WirelessQrPayload(
            "studio-test",
            "temporary-password",
            "WIFI:T:ADB;S:studio-test;P:temporary-password;;",
        )
        qr = WirelessQrDialog(payload)
        qr.set_status(LONG_TOKEN)
        for theme in ("Light", "Dark", "System"):
            apply_theme(self.app, theme)
            for dialog in (pairing, qr):
                self._show(dialog, max(dialog.minimumWidth(), 380), dialog.height())
                available = dialog.screen().availableGeometry()
                self.assertLessEqual(dialog.width(), available.width())
                self.assert_wrapped_labels_fit(dialog)

        self.assert_elided_text_is_recoverable(qr.status, LONG_TOKEN)
        for field in (pairing.host, pairing.pairing_port, pairing.pairing_code):
            self.assertTrue(field.accessibleName())
            self.assertEqual(field.toolTip(), field.placeholderText())
            self.assertEqual(field.accessibleDescription(), field.placeholderText())

    def test_reusable_dialogs_have_no_detectable_text_overflow_at_compact_widths(self) -> None:
        payload = WirelessQrPayload(
            "studio-test",
            "temporary-password",
            "WIFI:T:ADB;S:studio-test;P:temporary-password;;",
        )
        for theme in ("Light", "Dark", "System"):
            apply_theme(self.app, theme)
            dialogs = (
                WirelessPairingDialog("192.0.2.25", 37891),
                WirelessQrDialog(payload),
                ActivityDialog("Working", LONG_TOKEN),
                TransferProgressDialog("Transfer"),
                ErrorDialog(None, "Error", LONG_TOKEN),
            )
            for dialog in dialogs:
                self._show(dialog, max(dialog.minimumWidth(), 420), max(dialog.minimumHeight(), 360))
                issues = find_text_overflow(dialog)
                with self.subTest(theme=theme, dialog=type(dialog).__name__):
                    self.assertEqual([], issues, "\n".join(map(str, issues)))

    def test_dialog_fit_uses_small_screen_available_geometry_in_logical_pixels(self) -> None:
        dialog = MagicMock()
        screen = MagicMock()
        screen.availableGeometry.return_value = QRect(0, 0, 360, 480)
        dialog.screen.return_value = screen

        target = fit_dialog_to_available_screen(
            dialog,
            preferred=QSize(900, 600),
            minimum=QSize(460, 360),
        )

        self.assertEqual(target, QSize(312, 432))
        dialog.setMinimumSize.assert_called_once_with(312, 360)
        dialog.resize.assert_called_once_with(QSize(312, 432))

    def test_device_details_and_status_bar_keep_all_dynamic_text_recoverable(self) -> None:
        device = DeviceInfo(
            serial=LONG_TOKEN,
            model="Pixel " + "model-name-" * 40,
            manufacturer="Manufacturer " + "name-" * 40,
            mode="ADB",
            state="device",
        )
        details = DeviceDetailsDialog(device)
        self._show(details, 420, 360)
        self.assertEqual(details.fields["serial"].toolTip(), LONG_TOKEN)
        self.assertEqual(details.fields["serial"].accessibleDescription(), LONG_TOKEN)

        settings = MagicMock()
        settings.get.side_effect = lambda key, default=None: (
            False if key == "auto_refresh_device" else default
        )
        manager = MagicMock()
        manager.operations = OperationRegistry()
        manager.devices = [device]
        manager.active = device
        bar = DeviceStatusBar(manager, settings)
        bar.set_device(device)
        for width in (720, 960, 1920):
            self._show(bar, width, 54)
            with self.subTest(width=width):
                if width < bar.COMPACT_TEXT_BREAKPOINT:
                    self.assertFalse(bar.summary.isVisibleTo(bar))
                    self.assertFalse(bar.mode_label.isVisibleTo(bar))
                else:
                    self.assertTrue(bar.summary.isVisibleTo(bar))
                    self.assertTrue(bar.mode_label.isVisibleTo(bar))
                    self.assertGreater(bar.summary.width(), 0)
                    self.assertGreater(bar.mode_label.width(), 0)
                    self.assertGreaterEqual(bar.summary.width(), bar.summary.minimumWidth())
                    self.assertGreaterEqual(bar.mode_label.width(), bar.mode_label.minimumWidth())
                self.assertIn("connection mode: ADB", bar.dot.toolTip())
                self.assert_direct_children_do_not_overlap(bar)
                self.assert_elided_text_is_recoverable(bar.device_name, device.model)


if __name__ == "__main__":
    unittest.main()
