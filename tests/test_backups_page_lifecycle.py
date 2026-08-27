from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QTextEdit

from openadb.core.backup_manager import BackupManager
from openadb.models.backup_info import BackupInfo
from openadb.ui.backups_page import BackupsPage
from openadb.ui.style import apply_theme
from tests.test_backup_operation_coordinator import (
    ContextDeviceManager,
    ProfileSettings,
    RecordingAdb,
)


class BackupsPageLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = ProfileSettings(self.root)
        self.manager = BackupManager(self.settings)  # type: ignore[arg-type]
        self.adb = RecordingAdb()
        self.devices = ContextDeviceManager(self.settings)
        self.backup_path = (
            Path(self.settings.backups_folder) / "com.example.demo" / "one"
        )
        self.backup_path.mkdir(parents=True)
        (self.backup_path / "base.apk").write_bytes(b"apk")
        self.backup = BackupInfo(
            path=self.backup_path,
            package_name="com.example.demo",
            apk_files=["base.apk"],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_page(self) -> BackupsPage:
        page = BackupsPage(
            self.manager,
            self.adb,  # type: ignore[arg-type]
            self.devices,  # type: ignore[arg-type]
        )
        page._backups_loaded([self.backup])
        page.table.selectRow(0)
        self.app.processEvents()
        return page

    def test_long_backup_values_are_bounded_elided_and_preserved_in_tooltips(self) -> None:
        page = self.make_page()
        long_path = Path(
            "C:/" + "/".join(["very-long-backup-path-segment"] * 40)
        )
        backup = BackupInfo(
            path=long_path,
            package_name="com.example." + "verylongpackage." * 20,
            app_label="Very long backup application label " * 15,
            backup_date="2026-08-26 14:32:10 +03:00",
            device_model="Extremely long custom Android device model " * 10,
            android_version="Android 16 vendor build " + "X" * 80,
            apk_files=["base.apk"],
            metadata_exists=True,
        )
        page._backups_loaded([backup])

        for theme in ("System", "Light", "Dark"):
            for width in (660, 900, 1280):
                with self.subTest(theme=theme, width=width):
                    apply_theme(self.app, theme)
                    page.resize(width, 650)
                    page._resize_backup_columns()
                    self.app.processEvents()
                    self.assertEqual(page.table.textElideMode(), Qt.ElideRight)
                    for column in range(page.table.columnCount()):
                        header_item = page.table.horizontalHeaderItem(column)
                        header_text = page.TABLE_HEADERS[column]
                        self.assertEqual(header_item.toolTip(), header_text)
                        self.assertLessEqual(
                            page.table.horizontalHeader().fontMetrics().horizontalAdvance(
                                header_text
                            )
                            + 24,
                            page.table.columnWidth(column),
                        )
                        item = page.table.item(0, column)
                        self.assertEqual(item.toolTip(), item.text())
                        self.assertGreaterEqual(
                            page.table.columnWidth(column),
                            page.COLUMN_MIN_WIDTHS[column],
                        )
                        self.assertLessEqual(
                            page.table.columnWidth(column),
                            page.COLUMN_MAX_WIDTHS[column],
                        )

        self.assertEqual(page.table.item(0, 6).toolTip(), str(long_path))
        self.assertEqual(page.table.item(0, 3).toolTip(), backup.device_model)
        page.close()

    def test_long_delete_path_uses_bounded_scrollable_details(self) -> None:
        page = self.make_page()
        long_path = Path(
            "C:/" + "/".join(["very-long-backup-path-segment"] * 120)
        )
        backup = BackupInfo(path=long_path, package_name="com.example.long")
        box = page._delete_confirmation_box(backup)

        self.assertNotIn(str(long_path), box.text())
        self.assertEqual(box.detailedText(), str(long_path))
        self.assertEqual(box.defaultButton(), box.button(QMessageBox.No))
        box.show()
        self.app.processEvents()
        self.assertLessEqual(box.height(), box.screen().availableGeometry().height())
        details_buttons = [
            button
            for button in box.findChildren(QPushButton)
            if "Details" in button.text()
        ]
        self.assertEqual(len(details_buttons), 1)
        details_buttons[0].click()
        self.app.processEvents()
        self.assertLessEqual(box.height(), box.screen().availableGeometry().height())
        details = box.findChildren(QTextEdit)
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].toPlainText(), str(long_path))
        box.close()
        page.close()

    def test_long_variable_message_uses_bounded_scrollable_details(self) -> None:
        page = self.make_page()
        details = "C:/" + "/".join(["unbroken-backup-path-segment"] * 160)
        box = page._details_message_box(
            "Restore backup",
            "The restore could not start. Open Details for complete information.",
            details,
            icon=QMessageBox.Warning,
        )

        self.assertNotIn(details, box.text())
        self.assertEqual(box.detailedText(), details)
        box.show()
        self.app.processEvents()
        self.assertLessEqual(box.width(), box.screen().availableGeometry().width())
        self.assertLessEqual(box.height(), box.screen().availableGeometry().height())
        box.close()
        page.close()

    def test_restore_error_finished_path_releases_token_and_busy_state(self) -> None:
        page = self.make_page()
        workers = []
        with patch(
            "openadb.ui.backups_page.start_worker",
            side_effect=lambda _owner, _pool, worker, **_kwargs: workers.append(worker) or True,
        ):
            page.restore_selected(force_apk=True)

        self.assertEqual(page.operations.active_count, 1)
        self.assertTrue(page._action_busy)
        with patch("openadb.ui.backups_page.show_error_dialog") as show_error:
            workers[0].signals.error.emit("backup drive disconnected", "trace")
            workers[0].signals.finished.emit()
            self.app.processEvents()

        show_error.assert_called_once()
        self.assertEqual(page.operations.active_count, 0)
        self.assertFalse(page._action_busy)
        self.assertIsNone(page._action_token)
        page.close()

    def test_cancelled_restore_finally_suppresses_late_result_and_releases_token(self) -> None:
        page = self.make_page()
        workers = []
        with patch(
            "openadb.ui.backups_page.start_worker",
            side_effect=lambda _owner, _pool, worker, **_kwargs: workers.append(worker) or True,
        ):
            page.restore_selected(force_apk=True)

        token = page._action_token
        self.assertIsNotNone(token)
        page.reset_for_device_profile()
        self.assertTrue(token.cancelled)
        with patch.object(QMessageBox, "information") as information:
            workers[0].signals.result.emit(
                self.manager.restore_backup(
                    self.backup,
                    self.adb.for_context(self.devices.context()),
                    cancel_event=token.cancel_event,
                )
            )
            workers[0].signals.finished.emit()
            self.app.processEvents()

        information.assert_not_called()
        self.assertEqual(page.operations.active_count, 0)
        self.assertFalse(page._action_busy)
        page.close()

    def test_rejected_worker_start_runs_final_cleanup(self) -> None:
        page = self.make_page()
        with patch("openadb.ui.backups_page.start_worker", return_value=False):
            page.restore_selected(force_apk=True)

        self.assertEqual(page.operations.active_count, 0)
        self.assertFalse(page._action_busy)
        self.assertIsNone(page._action_token)
        page.close()

    def test_worker_start_exception_runs_final_cleanup(self) -> None:
        page = self.make_page()
        with (
            patch(
                "openadb.ui.backups_page.start_worker",
                side_effect=RuntimeError("thread pool unavailable"),
            ),
            patch.object(page, "_show_details_message") as show_details,
        ):
            page.restore_selected(force_apk=True)

        show_details.assert_called_once()
        self.assertIn("thread pool unavailable", show_details.call_args.args[2])
        self.assertEqual(page.operations.active_count, 0)
        self.assertFalse(page._action_busy)
        self.assertIsNone(page._action_token)
        page.close()

    def test_device_change_during_registration_starts_no_restore_worker(self) -> None:
        page = self.make_page()
        original_register = page.operations.register

        def register_then_switch(*args, **kwargs):
            token = original_register(*args, **kwargs)
            self.devices.switch("device-B")
            return token

        with (
            patch.object(page.operations, "register", side_effect=register_then_switch),
            patch("openadb.ui.backups_page.start_worker") as start_worker,
            patch.object(page, "_show_details_message") as show_details,
        ):
            page.restore_selected(force_apk=True)

        start_worker.assert_not_called()
        show_details.assert_called_once()
        self.assertEqual(page.operations.active_count, 0)
        self.assertFalse(page._action_busy)
        self.assertEqual(self.adb.calls, [])
        page.close()

    def test_profile_change_during_registration_starts_no_local_scan(self) -> None:
        page = self.make_page()
        original_register = page.operations.register

        def register_then_switch_profile(*args, **kwargs):
            token = original_register(*args, **kwargs)
            self.settings.backups_folder = self.root / "other-backups"
            return token

        with (
            patch.object(
                page.operations,
                "register",
                side_effect=register_then_switch_profile,
            ),
            patch("openadb.ui.backups_page.start_worker") as start_worker,
        ):
            page.refresh()

        start_worker.assert_not_called()
        self.assertEqual(page.operations.active_count, 0)
        self.assertFalse(page._loading)
        page.close()


if __name__ == "__main__":
    unittest.main()
