from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from openadb.core.backup_manager import BackupManager
from openadb.models.backup_info import BackupInfo
from openadb.ui.backups_page import BackupsPage
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
            patch.object(QMessageBox, "warning") as warning,
        ):
            page.restore_selected(force_apk=True)

        warning.assert_called_once()
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
            patch.object(QMessageBox, "information") as information,
        ):
            page.restore_selected(force_apk=True)

        start_worker.assert_not_called()
        information.assert_called_once()
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
