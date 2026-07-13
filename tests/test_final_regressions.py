from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from openadb.core.command_runner import CommandRunner
from openadb.core.device_context import DeviceContext
from openadb.core.settings_manager import SettingsManager
from openadb.models.backup_info import BackupInfo
from openadb.ui.backups_page import BackupsPage
from openadb.ui.workers import Worker, start_worker


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class WorkerShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_worker_ignores_emits_after_qt_deletes_signal_source(self) -> None:
        worker = Worker(lambda progress_callback=None: progress_callback.emit("progress") or "done")
        shiboken6.delete(worker.signals)
        worker.run()  # Must not leak RuntimeError during application teardown.

    def test_owner_rejects_new_workers_after_shutdown_begins(self) -> None:
        owner = MagicMock()
        owner._workers_shutting_down = True
        pool = MagicMock(spec=QThreadPool)
        self.assertFalse(start_worker(owner, pool, Worker(lambda: None)))
        pool.start.assert_not_called()

    def test_command_runner_shutdown_terminates_owned_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = CommandRunner(Path(directory))
            result_holder = []
            thread = threading.Thread(
                target=lambda: result_holder.append(
                    runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
                )
            )
            thread.start()
            deadline = time.monotonic() + 5
            while runner.active_process_count() == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(runner.active_process_count(), 1)
            runner.shutdown()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(runner.active_process_count(), 0)
            self.assertEqual(len(result_holder), 1)

    def test_tracked_text_binary_and_timeout_results_preserve_runner_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner = CommandRunner(Path(directory))
            text_result = runner.run([sys.executable, "-c", "print('runner-ok')"])
            self.assertTrue(text_result.success)
            self.assertEqual(text_result.stdout.strip(), "runner-ok")

            binary_result, output = runner.run_binary_output(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abc\\x00def')"]
            )
            self.assertTrue(binary_result.success)
            self.assertEqual(output, b"abc\x00def")

            timeout_result = runner.run(
                [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.05
            )
            self.assertFalse(timeout_result.success)
            self.assertEqual(timeout_result.error_type, "timeout")
            self.assertEqual(runner.active_process_count(), 0)


class SettingsPersistenceRegressionTests(unittest.TestCase):
    def test_concurrent_saves_are_atomic_and_leave_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = IsolatedSettings(Path(directory))
            errors: list[Exception] = []

            def writer(worker: int) -> None:
                for value in range(30):
                    settings.set(f"test_worker_{worker}", value)

            threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(4)]
            for thread in threads:
                thread.start()
            while any(thread.is_alive() for thread in threads):
                try:
                    loaded = json.loads(settings.path.read_text(encoding="utf-8"))
                    self.assertIsInstance(loaded, dict)
                except PermissionError:
                    # Windows can briefly deny a reader while os.replace swaps the file.
                    continue
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(exc)
                time.sleep(0.002)
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            saved = json.loads(settings.path.read_text(encoding="utf-8"))
            for worker in range(4):
                self.assertEqual(saved[f"test_worker_{worker}"], 29)
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])


class BackupsPageRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_empty_list_metadata_open_restore_and_delete_cancel_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_path = root / "com.example.demo" / "2026-07-12_120000"
            backup_path.mkdir(parents=True)
            (backup_path / "metadata.json").write_text(
                json.dumps({"package": "com.example.demo"}), encoding="utf-8"
            )
            manager = MagicMock()
            manager.root = root
            manager.settings.logs_folder = root / "logs"
            context = DeviceContext(
                serial="device-a",
                mode="ADB",
                transport_id="transport-a",
                profile_key="device-a",
                profile_kind="Phone",
                profile_path=root.parent,
                backups_path=root,
                temp_path=root.parent / "temp",
                logs_path=root / "logs",
                generation=1,
            )
            device_manager = MagicMock()
            device_manager.require_context.return_value = context
            device_manager.is_context_current.return_value = True
            bound_adb = MagicMock()
            bound_adb.serial = context.serial
            bound_adb.device_context = context
            adb = MagicMock()
            adb.for_context.return_value = bound_adb
            page = BackupsPage(manager, adb, device_manager)
            self.assertEqual(page.empty_state.title_label.text(), "No backups")

            backup = BackupInfo(
                path=backup_path,
                package_name="com.example.demo",
                app_label="Demo app",
                backup_date="2026-07-12 12:00",
                apk_files=["base.apk"],
                metadata_exists=True,
            )
            page._backups_loaded([backup])
            page.table.selectRow(0)
            self.app.processEvents()
            self.assertTrue(page.restore_button.isEnabled())

            with patch("openadb.ui.backups_page.QDesktopServices.openUrl") as open_url:
                page.open_selected()
                open_url.assert_called_once()
            with patch.object(QDialog, "exec", return_value=QDialog.Rejected):
                page.show_metadata()
            with patch("openadb.ui.backups_page.start_worker") as queue_worker:
                page.restore_selected(force_apk=True)
                queue_worker.assert_called_once()
            with (
                patch.object(QMessageBox, "question", return_value=QMessageBox.No),
                patch("openadb.ui.backups_page.start_worker") as queue_delete,
            ):
                page.delete_selected()
                queue_delete.assert_not_called()
            page.close()


if __name__ == "__main__":
    unittest.main()
