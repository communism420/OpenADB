from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QMimeData, QObject, Qt, QUrl, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from openadb.core.adb import ADBClient
from openadb.core.settings_manager import SettingsManager
from openadb.core.acbridge_p2p import ADB_TRANSPORT, P2P_TRANSPORT
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.file_manager_controller import (
    FileActionItemResult,
    FileManagerAction,
    FileManagerActionResult,
    FileManagerSide,
    WindowsActionRequest,
)
from openadb.core.operations import OperationRegistry
from openadb.models.device_info import DeviceInfo
from openadb.models.file_item import FileItem
from openadb.ui.file_manager_page import FileManagerPage
from openadb.ui.style import apply_theme
from openadb.ui.widgets.file_panel import ANDROID_MIME, FileTable
from openadb.ui.widgets.windows_file_panel import WindowsFileTree


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class FakeAdb:
    def __init__(self) -> None:
        self.serial = "device-1"
        self.root_granted = False
        self.files: list[FileItem] = []

    def root_available(self, cancel_event=None) -> bool:
        return self.root_granted

    def for_context(self, context: DeviceContext):
        return FakeBoundAdb(self, context)

    def list_files(
        self,
        _path: str,
        use_root: bool = False,
        cancel_event=None,
    ) -> list[FileItem]:
        return list(self.files)

    def storage_info(
        self,
        _path: str,
        use_root: bool = False,
        cancel_event=None,
    ) -> dict:
        return {"free_bytes": 4096, "total_bytes": 8192}


class FakeBoundAdb:
    def __init__(self, source: FakeAdb, context: DeviceContext) -> None:
        self._source = source
        self.device_context = context
        self.serial = context.serial

    def root_available(self, cancel_event=None) -> bool:
        return self._source.root_granted

    def __getattr__(self, name: str):
        return getattr(self._source, name)


class FakeDeviceManager:
    def __init__(self, active: DeviceInfo, config_dir: Path) -> None:
        self.active = active
        self.operations = OperationRegistry()
        self._generation = 1
        self._config_dir = config_dir

    @property
    def current_generation(self) -> int:
        return self._generation

    def capture_context(self) -> DeviceContext:
        if not self.active.serial:
            raise DeviceContextUnavailable("No active Android device is available")
        profile = self._config_dir / self.active.serial
        return DeviceContext(
            serial=self.active.serial,
            mode=self.active.mode,
            transport_id=self.active.transport_id,
            profile_key=self.active.serial,
            profile_kind="Phone",
            profile_path=profile,
            backups_path=profile / "backups",
            temp_path=profile / "temp",
            logs_path=profile / "logs",
            generation=self._generation,
        )

    def require_context(self, allowed_modes=None) -> DeviceContext:
        context = self.capture_context()
        if allowed_modes is not None and context.mode not in set(allowed_modes):
            raise DeviceContextUnavailable(f"Current device mode is {context.mode}")
        return context

    def is_context_current(self, context: DeviceContext | None) -> bool:
        if context is None:
            return True
        try:
            current = self.capture_context()
        except DeviceContextUnavailable:
            return False
        return current == context

    def switch(self, active: DeviceInfo) -> None:
        self.active = active
        self._generation += 1
        self.operations.cancel_stale(self._generation, "test device changed")


class FakeDropEvent:
    def __init__(self, mime: QMimeData) -> None:
        self._mime = mime
        self.accepted = False
        self.ignored = False

    def mimeData(self) -> QMimeData:
        return self._mime

    def acceptProposedAction(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True


class FakeTransferDialog(QObject):
    cancel_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.updates: list[dict] = []
        self.shown = False

    def apply_update(self, update: dict) -> None:
        self.updates.append(dict(update))

    def show(self) -> None:
        self.shown = True


class FileManagerPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.windows_dir = self.config_dir / "windows"
        self.windows_dir.mkdir()
        self.settings = IsolatedSettings(self.config_dir)
        self.settings.set("auto_refresh_device", False)
        self.settings.set_global_values({"file_manager_windows_path": str(self.windows_dir)})
        self.adb = FakeAdb()
        self.device_manager = FakeDeviceManager(
            DeviceInfo(serial="device-1", model="Test device", mode="ADB", state="device"),
            self.config_dir,
        )
        self.native_patch = patch(
            "openadb.ui.file_manager_page.NativeExplorerPanel",
            side_effect=RuntimeError("Use deterministic Qt fallback in tests"),
        )
        self.native_patch.start()
        self.page = FileManagerPage(self.adb, self.device_manager, self.settings)
        self.page.resize(900, 620)
        self.page.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.page.save_ui_state()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.native_patch.stop()
        self.temp_dir.cleanup()

    def test_splitter_layout_groups_and_action_labels(self) -> None:
        self.assertEqual(self.page.file_splitter.count(), 3)
        self.assertFalse(self.page.file_splitter.childrenCollapsible())
        self.assertLessEqual(self.page.file_splitter.widget(1).maximumWidth(), 196)
        self.assertEqual(self.page.pull_button.text(), "Android → PC")
        self.assertEqual(self.page.push_button.text(), "PC → Android")
        self.assertEqual(self.page.copy_path_button.text(), "Copy path")
        self.assertEqual(self.page.open_explorer_button.text(), "Open in Explorer")
        self.assertEqual(self.page.properties_button.text(), "Properties")
        self.assertEqual(self.page.root_boost_button.text(), "Use root for transfers")
        self.assertTrue(self.page.delete_button.property("danger"))
        self.assertEqual(self.page.windows_back_button.icon().name(), "chevron_left")
        self.assertEqual(self.page.windows_forward_button.icon().name(), "chevron_right")
        self.assertFalse(self.page.windows_back_button.text())
        self.assertFalse(self.page.windows_forward_button.text())
        titles = [
            label.text()
            for label in self.page.findChildren(QLabel, "fileManagerActionGroupTitle")
        ]
        self.assertEqual(titles, ["Transfer", "File operations", "Advanced"])

    def test_new_android_folder_never_prompts_for_a_stale_device_view(self) -> None:
        self.page._android_view_context = self.device_manager.capture_context()
        self.page._android_view_path = self.page.android_path
        self.device_manager.switch(
            DeviceInfo(serial="device-2", model="Second", mode="ADB", state="device")
        )

        with (
            patch("openadb.ui.file_manager_page.QMessageBox.warning"),
            patch("openadb.ui.file_manager_actions.QInputDialog.getText") as get_text,
        ):
            self.page.new_folder("android")

        get_text.assert_not_called()

    def test_legacy_page_symbol_patch_paths_remain_available(self) -> None:
        with patch(
            "openadb.ui.file_manager_page.QInputDialog.getText",
            return_value=("", False),
        ) as get_text:
            self.page.new_folder("windows")

        get_text.assert_called_once()

    def test_local_action_shutdown_cancels_the_coordinator_request(self) -> None:
        captured: list[object] = []

        def capture_worker(worker) -> bool:
            captured.append(worker)
            return True

        request = WindowsActionRequest.properties(self.windows_dir)
        with patch.object(self.page, "_start_local_worker", side_effect=capture_worker):
            self.page.file_actions._start_windows(request, title="Properties")
        self.page.cancel_active_transfers()

        result = captured[0].fn()

        self.assertTrue(result.cancelled)

    def test_action_failure_dialog_redacts_secret_and_prioritizes_failure(self) -> None:
        secret = "b4" * 32
        items = tuple(
            FileActionItemResult(f"/sdcard/{index}", True, f"item {index}: deleted")
            for index in range(90)
        ) + (
            FileActionItemResult(
                "/sdcard/failure",
                False,
                f"permission denied; session_key={secret}",
            ),
        )
        result = FileManagerActionResult(
            FileManagerAction.DELETE,
            FileManagerSide.ANDROID,
            items,
        )

        with patch("openadb.ui.file_manager_page.QMessageBox.warning") as warning:
            self.page.file_actions._present_result("Delete", result, None)

        dialog_text = " ".join(str(value) for value in warning.call_args.args)
        self.assertNotIn(secret, dialog_text)
        self.assertIn("Permission denied", dialog_text)

    def test_splitter_and_paths_persist_in_the_intended_scope(self) -> None:
        self.page.file_splitter.setSizes([260, 176, 410])
        self.app.processEvents()
        self.page._save_splitter_state()
        saved_sizes = self.settings.get_global("file_manager_splitter_sizes")
        self.assertEqual(len(saved_sizes), 3)
        self.assertLessEqual(saved_sizes[1], 196)

        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "Phone"))
        self.page.reload_from_settings()
        with patch.object(self.page, "refresh_android"):
            self.page.navigate_android("/storage/emulated/0/Documents")
        self.assertEqual(self.settings.get("file_manager_android_path"), "/storage/emulated/0/Documents")

        second_windows_dir = self.config_dir / "windows-two"
        second_windows_dir.mkdir()
        self.page.navigate_windows(str(second_windows_dir))
        self.assertEqual(self.settings.get_global("file_manager_windows_path"), str(second_windows_dir))

        self.settings.set("file_manager_root_transfer", True)
        self.assertTrue(self.settings.activate_device_profile("device-b", "Device B", "Phone"))
        self.page.reload_from_settings()
        self.assertEqual(self.page.android_path, "/sdcard/")
        self.assertFalse(self.page.root_boost_button.isChecked())
        self.assertEqual(self.settings.get_global("file_manager_splitter_sizes"), saved_sizes)

    def test_root_toggle_is_explicit_checked_and_profile_local(self) -> None:
        self.settings.set("root_mode_enabled", True)
        self.page.reload_from_settings()
        self.assertFalse(self.page.root_boost_button.isChecked())
        self.assertEqual(self.page.root_status_label.text(), "Root: not checked")

        with patch("openadb.ui.file_manager_page.start_worker") as start_worker:
            self.page.root_boost_button.setChecked(True)
        self.assertTrue(self.settings.get("file_manager_root_transfer"))
        self.assertEqual(self.page.root_status_label.text(), "Root: checking")
        self.assertFalse(self.page.root_boost_button.isEnabled())
        self.assertFalse(self.page.pull_button.isEnabled())
        self.assertFalse(self.page.push_button.isEnabled())
        start_worker.assert_called_once()

        first_token = self.page._root_check_token
        self.assertIsNotNone(first_token)
        self.page._root_check_result(first_token, True)
        self.page._root_check_finished(first_token)
        self.assertEqual(self.page.root_status_label.text(), "Root: granted")
        self.assertTrue(self.page.root_boost_button.isEnabled())
        self.assertTrue(self.page.pull_button.isEnabled())

        self.page.root_boost_button.setChecked(False)
        self.assertFalse(self.settings.get("file_manager_root_transfer"))
        self.assertEqual(self.page.root_status_label.text(), "Root: not checked")

        with patch("openadb.ui.file_manager_page.start_worker"):
            self.page.root_boost_button.setChecked(True)
        second_token = self.page._root_check_token
        self.assertIsNotNone(second_token)
        self.page._root_check_result(second_token, False)
        self.page._root_check_finished(second_token)
        self.assertEqual(self.page.root_status_label.text(), "Root: denied")
        self.assertTrue(self.page.root_boost_button.isChecked())
        self.assertIn("normal ADB", self.page.status_label.text())
        self.page.root_boost_button.setChecked(False)

        self.device_manager.active = DeviceInfo(mode="No device", state="none")
        with patch("openadb.ui.file_manager_page.start_worker") as start_worker:
            self.page.root_boost_button.setChecked(True)
        start_worker.assert_not_called()
        self.assertFalse(self.page.root_boost_button.isChecked())
        self.assertEqual(self.page.root_status_label.text(), "Root: unavailable")
        self.assertIn("connect", self.page.status_label.text().lower())

    def test_upload_transport_is_explicit_and_profile_local(self) -> None:
        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "TV"))
        self.page.reload_from_settings()
        self.assertEqual(self.page.transfer_transport_combo.currentData(), ADB_TRANSPORT)
        self.assertTrue(self.page.root_boost_button.isEnabled())
        self.assertTrue(self.page.p2p_parallelism_row.isHidden())

        self.page.transfer_transport_combo.setCurrentIndex(
            self.page.transfer_transport_combo.findData(P2P_TRANSPORT)
        )
        self.assertEqual(self.settings.get("file_manager_transfer_transport"), P2P_TRANSPORT)
        self.assertFalse(self.page.root_boost_button.isEnabled())
        self.assertEqual(self.page.root_status_label.text(), "Root: not used by P2P")
        self.assertIn("SAF", self.page.push_button.toolTip())
        self.assertFalse(self.page.p2p_parallelism_row.isHidden())
        self.page.p2p_parallelism_combo.setCurrentIndex(self.page.p2p_parallelism_combo.findData(4))
        self.assertEqual(self.settings.get("file_manager_p2p_parallelism"), 4)

        self.assertTrue(self.settings.activate_device_profile("device-b", "Device B", "TV"))
        self.page.reload_from_settings()
        self.assertEqual(self.page.transfer_transport_combo.currentData(), ADB_TRANSPORT)
        self.assertEqual(self.page._selected_p2p_parallelism(), 1)

        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "TV"))
        self.page.reload_from_settings()
        self.assertEqual(self.page.transfer_transport_combo.currentData(), P2P_TRANSPORT)
        self.assertEqual(self.page._selected_p2p_parallelism(), 4)

    def test_transfer_directions_worker_guard_and_cancel_state(self) -> None:
        self.page._android_view_context = self.device_manager.capture_context()
        self.page._android_view_path = self.page.android_path
        pull_dialog = FakeTransferDialog()
        with (
            patch.object(self.page, "_create_transfer_dialog", return_value=pull_dialog) as create_dialog,
            patch.object(self.page, "_run_pull_transfer", return_value={"success": True}) as run_pull,
            patch("openadb.ui.file_manager_page.start_worker") as start_worker,
        ):
            self.page.pull_paths(["/sdcard/example.txt"])
            create_dialog.assert_called_once_with("Android → PC")
            self.assertTrue(self.page._transfer_running)
            self.assertEqual(len(self.page._transfer_cancel_events), 1)
            self.assertFalse(self.page.pull_button.isEnabled())
            worker = start_worker.call_args.args[2]
            callback = object()
            worker.fn(item_callback=callback)
            run_pull.assert_called_once()
            self.page.push_paths([str(self.windows_dir / "queued.txt")])
            self.assertEqual(start_worker.call_count, 1)
            worker.signals.finished.emit()
            self.assertFalse(self.page._transfer_running)
            self.assertEqual(self.page._transfer_cancel_events, set())

        local_file = self.windows_dir / "file.txt"
        local_file.write_text("safe mock", encoding="utf-8")
        self.page.transfer_transport_combo.setCurrentIndex(
            self.page.transfer_transport_combo.findData(P2P_TRANSPORT)
        )
        self.page.p2p_parallelism_combo.setCurrentIndex(self.page.p2p_parallelism_combo.findData(4))
        push_dialog = FakeTransferDialog()
        with (
            patch.object(self.page, "_create_transfer_dialog", return_value=push_dialog) as create_dialog,
            patch.object(self.page, "_offer_install_single_apk", return_value=False),
            patch.object(self.page, "_warn_android_write", return_value=True),
            patch.object(self.page, "_run_push_transfer", return_value={"success": True}) as run_push,
            patch("openadb.ui.file_manager_page.start_worker") as start_worker,
        ):
            self.page.push_paths([str(local_file)])
            create_dialog.assert_called_once_with("PC → Android")
            worker = start_worker.call_args.args[2]
            self.assertEqual(len(self.page._transfer_cancel_events), 1)
            worker.fn(item_callback=object())
            run_push.assert_called_once()
            self.assertEqual(run_push.call_args.kwargs["transport"], P2P_TRANSPORT)
            self.assertEqual(run_push.call_args.kwargs["p2p_parallelism"], 4)
            worker.signals.finished.emit()
            self.assertEqual(self.page._transfer_cancel_events, set())

        cancel_event = threading.Event()
        cancel_token = self.device_manager.operations.register(
            "test.transfer-cancel",
            device_context=self.device_manager.capture_context(),
            cancel_event=cancel_event,
        )
        self.page._cancel_transfer(push_dialog, cancel_token)
        self.assertTrue(cancel_event.is_set())
        self.assertEqual(push_dialog.updates[-1]["type"], "cancelled")
        self.assertIn("cancellation", self.page.status_label.text().lower())
        self.device_manager.operations.finish(cancel_token)

    def test_failed_and_cancelled_transfers_never_report_success(self) -> None:
        dialog = FakeTransferDialog()
        refresh = MagicMock()
        token = self.device_manager.operations.register(
            "test.transfer-result",
            device_context=self.device_manager.capture_context(),
        )
        self.page._transfer_done(
            token,
            dialog,
            {"success": False, "summary": "adb: write failed: No space left on device"},
            refresh,
        )
        self.assertFalse(dialog.updates[-1]["success"])
        self.assertIn("Insufficient space", dialog.updates[-1]["message"])
        self.assertNotIn("successfully", self.page.status_label.text().lower())
        refresh.assert_called_once()

        self.page._transfer_done(
            token,
            dialog,
            {"success": False, "summary": "Transfer cancelled by user."},
            refresh,
        )
        self.assertIn("cancelled", dialog.updates[-1]["message"].lower())
        self.assertFalse(dialog.updates[-1]["success"])
        self.device_manager.operations.finish(token)

        cases = {
            "permission denied": "Permission denied",
            "read-only file system": "protected or read-only",
            "device offline": "disconnected",
            "su: not found": "Root access",
            "storage unavailable": "storage or path",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertIn(expected, self.page._friendly_error("Operation", raw))

    def test_android_transfer_stats_stop_before_du_when_cancelled_during_find(self) -> None:
        cancel_event = threading.Event()
        adb = MagicMock()
        commands = []

        def run_shell(command, *, timeout, cancel_event=None):
            commands.append(command)
            self.assertIsNotNone(cancel_event)
            if command.startswith("if [ -d"):
                return SimpleNamespace(stdout="dir\n")
            if command.startswith("find "):
                cancel_event.set()
                return SimpleNamespace(stdout="4\n")
            self.fail(f"Unexpected command after cancellation: {command}")

        adb.run_shell.side_effect = run_shell

        stats = self.page._android_transfer_stats_with_kind(
            adb,
            "/sdcard/Large",
            cancel_event=cancel_event,
        )

        self.assertEqual(stats, (0, 0, True))
        self.assertTrue(cancel_event.is_set())
        self.assertEqual(len(commands), 2)
        self.assertFalse(any(command.startswith("du ") for command in commands))

    def test_cancelled_entry_skips_final_remote_observation_and_repair(self) -> None:
        source = self.windows_dir / "cancelled.bin"
        source.write_bytes(b"payload")
        cancel_event = threading.Event()
        result = SimpleNamespace(
            success=False,
            status="Cancelled by user",
            error_type="cancelled",
            stdout="",
            stderr="",
        )
        adb = MagicMock()

        def cancel_during_push(*_args, **_kwargs):
            cancel_event.set()
            return result

        adb.push_streaming.side_effect = cancel_during_push
        with (
            patch.object(
                self.page,
                "_transfer_observation_baseline",
                return_value=(0, 0),
            ) as baseline,
            patch.object(self.page, "_observed_transfer_stats") as observation,
            patch.object(self.page, "_repair_standard_push_missing_files") as repair,
        ):
            state = self.page._run_entry_command_with_progress(
                adb=adb,
                source=source,
                destination="/sdcard/",
                is_pull=False,
                transfer_source=source,
                transfer_destination="/sdcard/",
                root_mode=False,
                timeout=None,
                cancel_event=cancel_event,
                output_callback=None,
                item_callback=None,
                entry_size=source.stat().st_size,
                done_bytes=0,
                total_bytes=source.stat().st_size,
                total_files=1,
                done_files=0,
                started=0.0,
                entry_count=1,
                file_markers=[(source.stat().st_size, source.name)],
            )

        self.assertIs(state["result"], result)
        baseline.assert_called_once()
        self.assertIs(baseline.call_args.kwargs["cancel_event"], cancel_event)
        observation.assert_not_called()
        repair.assert_not_called()

    def test_cancel_after_stream_skips_remote_finalize_and_runs_bounded_cleanup(self) -> None:
        source = self.windows_dir / "cancel-before-finalize.bin"
        source.write_bytes(b"payload")
        cancel_event = threading.Event()
        streamed = SimpleNamespace(
            success=True,
            status="Success",
            error_type="",
            stdout="",
            stderr="",
        )
        cleanup_result = SimpleNamespace(success=True, status="Success", error_type="")
        adb = MagicMock()

        def finish_stream(*_args, **_kwargs):
            cancel_event.set()
            return streamed

        adb.run_raw_with_input_stream.side_effect = finish_stream
        adb.run_shell.return_value = cleanup_result

        result, _sent = self.page._stream_push_file_to_android_target(
            adb=adb,
            source=source,
            target="/sdcard/cancel-before-finalize.bin",
            cancel_event=cancel_event,
            output_callback=None,
            item_callback=None,
            base_done_bytes=0,
            base_done_files=0,
            total_bytes=source.stat().st_size,
            total_files=1,
            started=0.0,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "cancelled")
        adb.run_shell.assert_called_once()
        cleanup_command = adb.run_shell.call_args.args[0]
        self.assertIn("rm -f", cleanup_command)
        self.assertNotIn("mv -f", cleanup_command)
        self.assertEqual(adb.run_shell.call_args.kwargs["timeout"], 5)

    def test_mutating_adb_helpers_forward_cancel_and_skip_delete_fallback(self) -> None:
        client = object.__new__(ADBClient)
        cancel_event = threading.Event()
        success = SimpleNamespace(success=True, status="Success", error_type="", stdout="", stderr="")
        client.run_shell = MagicMock(return_value=success)
        client.run_root_shell = MagicMock(return_value=success)

        client.mkdir("/sdcard/New", cancel_event=cancel_event)
        client.delete("/sdcard/Old", recursive=True, cancel_event=cancel_event)
        client.rename("/sdcard/Old", "/sdcard/New", cancel_event=cancel_event)

        for call in client.run_shell.call_args_list:
            self.assertIs(call.kwargs["cancel_event"], cancel_event)

        cancelled = SimpleNamespace(
            success=False,
            status="Cancelled before execution",
            error_type="cancelled",
            stdout="",
            stderr="",
        )

        def cancel_shell(*_args, **_kwargs):
            cancel_event.set()
            return cancelled

        cancel_event.clear()
        client.run_shell.reset_mock(side_effect=True, return_value=True)
        client.run_shell.side_effect = cancel_shell
        client._delete_public_storage_via_mediastore = MagicMock(return_value=cancelled)

        result = client.delete(
            "/storage/ABCD-1234/Old",
            recursive=True,
            cancel_event=cancel_event,
        )

        self.assertIs(result, cancelled)
        self.assertTrue(cancel_event.is_set())
        client._delete_public_storage_via_mediastore.assert_not_called()
        self.assertGreaterEqual(client.run_shell.call_count, 2)
        for call in client.run_shell.call_args_list:
            self.assertIs(call.kwargs["cancel_event"], cancel_event)

    def test_recursive_planning_and_observation_stop_without_eager_rglob(self) -> None:
        root = self.windows_dir / "recursive"
        root.mkdir()
        first = root / "first.bin"
        first.write_bytes(b"1234")

        class ExplodingPath:
            def __str__(self) -> str:
                raise AssertionError("cancelled recursive traversal consumed another path")

            def is_file(self) -> bool:
                raise AssertionError("cancelled recursive traversal inspected another path")

        def cancelling_rglob(cancel_event: threading.Event):
            yield first
            cancel_event.set()
            yield ExplodingPath()

        tar_cancel = threading.Event()
        with patch.object(
            Path,
            "rglob",
            autospec=True,
            side_effect=lambda _path, _pattern: cancelling_rglob(tar_cancel),
        ):
            directories, files = self.page._tar_stream_items(
                root,
                cancel_event=tar_cancel,
            )
        self.assertEqual(len(directories), 1)
        self.assertEqual([item[0] for item in files], [first])

        observation_cancel = threading.Event()
        with patch.object(
            Path,
            "rglob",
            autospec=True,
            side_effect=lambda _path, _pattern: cancelling_rglob(observation_cancel),
        ):
            size, count, newest = self.page._local_transfer_observation(
                root,
                started_wall=0.0,
                cancel_event=observation_cancel,
            )
        self.assertEqual((size, count), (4, 1))
        self.assertEqual(newest, str(first))

        repair_cancel = threading.Event()
        failed_result = SimpleNamespace(
            stdout="",
            stderr=f"adb: error: cannot lstat '{first}'",
        )
        with patch.object(
            Path,
            "rglob",
            autospec=True,
            side_effect=lambda _path, _pattern: cancelling_rglob(repair_cancel),
        ):
            failed = self.page._standard_push_failed_local_paths(
                failed_result,
                root,
                cancel_event=repair_cancel,
            )
        self.assertEqual(failed, [])

    def test_device_switch_inside_delete_confirmation_prevents_destructive_worker(self) -> None:
        context = self.device_manager.capture_context()
        self.page._android_view_context = context
        self.page._android_view_path = self.page.android_path
        self.page.android_panel.set_items(
            [FileItem("old.txt", "/sdcard/old.txt", False, size=7)]
        )
        self.page.android_panel.table.selectRow(0)

        def switch_device(*_args, **_kwargs):
            self.device_manager.switch(
                DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
            )
            return QMessageBox.Ok

        with (
            patch.object(QMessageBox, "warning", side_effect=switch_device),
            patch.object(self.page, "_warn_android_write", return_value=True),
            patch("openadb.ui.file_manager_page.start_worker") as start_worker,
        ):
            self.page.delete_selected("android")

        start_worker.assert_not_called()
        self.assertEqual(self.device_manager.operations.active_count, 0)
        self.assertIn("changed", self.page.status_label.text().lower())

    def test_device_switch_between_pull_view_check_and_capture_blocks_worker(self) -> None:
        old_context = self.device_manager.capture_context()
        self.page._android_view_context = old_context
        self.page._android_view_path = self.page.android_path

        def switch_before_capture(_allowed_modes=None):
            self.device_manager.switch(
                DeviceInfo(
                    serial="device-2",
                    model="Second device",
                    mode="ADB",
                    state="device",
                )
            )
            return self.device_manager.capture_context()

        with (
            patch.object(
                self.device_manager,
                "require_context",
                side_effect=switch_before_capture,
            ),
            patch("openadb.ui.file_manager_page.start_worker") as start_worker,
        ):
            self.page.pull_paths(["/sdcard/old-device.txt"])

        start_worker.assert_not_called()
        self.assertFalse(self.page._transfer_running)
        self.assertEqual(self.device_manager.operations.active_count, 0)
        self.assertIn("another device", self.page.status_label.text().lower())

    def test_device_switch_inside_apk_choice_prevents_install_and_copy_workers(self) -> None:
        apk_path = self.windows_dir / "demo.apk"
        apk_path.write_bytes(b"mock apk")

        for choice in ("install", "copy"):
            with self.subTest(choice=choice):
                self.device_manager.switch(
                    DeviceInfo(serial="device-1", model="Test device", mode="ADB", state="device")
                )
                self.page._android_view_context = self.device_manager.capture_context()
                self.page._android_view_path = self.page.android_path
                box = MagicMock()
                install_button = object()
                copy_button = object()
                box.addButton.side_effect = [install_button, copy_button, object()]
                box.clickedButton.return_value = (
                    install_button if choice == "install" else copy_button
                )
                box.exec.side_effect = lambda: self.device_manager.switch(
                    DeviceInfo(
                        serial="device-2",
                        model="Second device",
                        mode="ADB",
                        state="device",
                    )
                )

                with (
                    patch(
                        "openadb.ui.file_manager_page.QMessageBox",
                        return_value=box,
                    ),
                    patch.object(self.page, "_warn_android_write", return_value=True),
                    patch("openadb.ui.file_manager_page.start_worker") as start_worker,
                ):
                    self.page.push_paths([str(apk_path)])

                box.exec.assert_called_once_with()
                start_worker.assert_not_called()
                self.assertFalse(self.page._transfer_running)
                self.assertEqual(self.device_manager.operations.active_count, 0)

    def test_device_switch_during_listing_ignores_old_items_error_and_runs_pending_refresh(self) -> None:
        with (
            patch("openadb.ui.file_manager_page.start_worker") as start_worker,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.refresh_android()
            self.assertEqual(start_worker.call_count, 1)
            old_worker = start_worker.call_args.args[2]
            old_token = self.page._android_refresh_token
            self.assertIsNotNone(old_token)

            self.device_manager.switch(
                DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
            )
            self.page.status_label.setText("Device 2 is current")
            self.page.refresh_android()
            self.assertTrue(self.page._android_refresh_pending)

            old_worker.signals.result.emit(
                (
                    "/sdcard/",
                    [FileItem("old.txt", "/sdcard/old.txt", False, size=3)],
                    {"free_bytes": 1},
                    False,
                )
            )
            old_worker.signals.error.emit("device offline", "trace")
            self.assertEqual(self.page.android_panel.table.rowCount(), 0)
            self.assertEqual(self.page.status_label.text(), "Device 2 is current")
            warning.assert_not_called()

            old_worker.signals.finished.emit()
            self.assertEqual(start_worker.call_count, 2)
            new_token = self.page._android_refresh_token
            self.assertIsNotNone(new_token)
            self.assertEqual(new_token.device_context.serial, "device-2")
            self.page._android_refresh_finished(new_token)

    def test_device_switch_clears_old_rows_before_new_actions_can_start(self) -> None:
        old_context = self.device_manager.capture_context()
        self.page._android_view_context = old_context
        self.page._android_view_path = self.page.android_path
        self.page.android_panel.set_items(
            [FileItem("old.txt", "/sdcard/old.txt", False, size=7)]
        )
        self.page.android_panel.table.selectRow(0)
        self.assertEqual(self.page.android_panel.selected_paths(), ["/sdcard/old.txt"])

        self.device_manager.switch(
            DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
        )
        with (
            patch.object(self.page, "_capture_device_operation") as capture,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.delete_selected("android")

        capture.assert_not_called()
        warning.assert_called_once()
        self.assertEqual(self.page.android_panel.table.rowCount(), 0)
        self.assertEqual(self.page.android_panel.selected_paths(), [])

    def test_path_change_clears_rows_from_previous_folder(self) -> None:
        self.page._android_view_context = self.device_manager.capture_context()
        self.page._android_view_path = "/sdcard/"
        self.page.android_panel.set_items(
            [FileItem("old.txt", "/sdcard/old.txt", False, size=7)]
        )

        with patch.object(self.page, "refresh_android"):
            self.page.navigate_android("/sdcard/Download/")

        self.assertEqual(self.page.android_panel.table.rowCount(), 0)
        self.assertIsNone(self.page._android_view_context)

    def test_current_listing_result_applies_before_registry_cleanup(self) -> None:
        self.adb.files = [FileItem("current.txt", "/sdcard/current.txt", False, size=7)]
        self.page.refresh_android()
        for _attempt in range(100):
            self.app.processEvents()
            if not self.page._android_loading:
                break
            QTest.qWait(10)

        self.assertFalse(self.page._android_loading)
        self.assertEqual(self.page.android_panel.table.rowCount(), 1)
        self.assertEqual(self.page.android_panel.table.item(0, 0).text(), "current.txt")
        self.assertEqual(self.device_manager.operations.active_count, 0)

    def test_device_switch_during_storage_refresh_does_not_replace_new_volumes(self) -> None:
        with patch("openadb.ui.file_manager_page.start_worker") as start_worker:
            self.page.refresh_android_storage_roots()
            old_worker = start_worker.call_args.args[2]
            old_token = self.page._android_storage_token
            self.assertIsNotNone(old_token)

            self.device_manager.switch(
                DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
            )
            current_volume = SimpleNamespace(
                path="/storage/NEW",
                label="Device 2 storage",
                free_bytes=2048,
                state="mounted",
            )
            self.page._set_android_storage_combo([current_volume])
            self.page.refresh_android_storage_roots()
            old_worker.signals.result.emit(
                [
                    SimpleNamespace(
                        path="/storage/OLD",
                        label="Old device storage",
                        free_bytes=1024,
                        state="mounted",
                    )
                ]
            )
            self.assertIn("Device 2 storage", self.page.android_storage_combo.itemText(0))

            old_worker.signals.finished.emit()
            self.assertEqual(start_worker.call_count, 2)
            new_token = self.page._android_storage_token
            self.assertIsNotNone(new_token)
            self.assertEqual(new_token.device_context.serial, "device-2")
            self.page._android_storage_refresh_finished(new_token)

    def test_device_switch_before_p2p_worker_prevents_transfer_and_suppresses_success(self) -> None:
        local_file = self.windows_dir / "movie.bin"
        local_file.write_bytes(b"safe mock")
        dialog = FakeTransferDialog()
        refresh = MagicMock()
        self.page._android_view_context = self.device_manager.capture_context()
        self.page._android_view_path = self.page.android_path

        with (
            patch.object(self.page, "_create_transfer_dialog", return_value=dialog),
            patch.object(self.page, "_offer_install_single_apk", return_value=False),
            patch.object(self.page, "_warn_android_write", return_value=True),
            patch.object(self.page, "_run_push_transfer", return_value={"success": True, "summary": "done"}) as run_push,
            patch.object(self.page, "refresh_android", refresh),
            patch("openadb.ui.file_manager_page.start_worker") as start_worker,
        ):
            self.page.transfer_transport_combo.setCurrentIndex(
                self.page.transfer_transport_combo.findData(P2P_TRANSPORT)
            )
            self.page.push_paths([str(local_file)])
            worker = start_worker.call_args.args[2]
            token = self.page._transfer_token
            self.assertIsNotNone(token)
            self.assertIn("file-manager.transfer", token.conflict_groups)
            self.assertIn("device-exclusive:device-1", token.conflict_groups)

            self.page.android_path = "/storage/CHANGED"
            self.device_manager.switch(
                DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
            )
            self.assertTrue(token.cancel_event.is_set())
            with self.assertRaises(DeviceContextUnavailable):
                worker.fn(item_callback=object())
            run_push.assert_not_called()

            worker.signals.result.emit({"success": True, "summary": "done"})
            self.assertFalse(dialog.updates[-1]["success"])
            self.assertIn("device", dialog.updates[-1]["message"].lower())
            self.assertNotIn("success", self.page.status_label.text().lower())
            refresh.assert_not_called()
            worker.signals.finished.emit()
            self.assertFalse(self.page._transfer_running)

    def test_stale_transfer_progress_is_replaced_once_and_never_reaches_dialog(self) -> None:
        dialog = FakeTransferDialog()
        token = self.device_manager.operations.register(
            "test.stale-progress",
            device_context=self.device_manager.capture_context(),
            cancel_event=threading.Event(),
        )
        self.device_manager.switch(
            DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
        )

        stale_update = {
            "type": "progress",
            "done_bytes": 999,
            "message": "late progress from device 1",
        }
        self.page._transfer_progress(token, dialog, stale_update)
        self.page._transfer_progress(token, dialog, stale_update)

        self.assertEqual(len(dialog.updates), 1)
        self.assertEqual(dialog.updates[0]["type"], "done")
        self.assertFalse(dialog.updates[0]["success"])
        self.assertNotIn("done_bytes", dialog.updates[0])
        self.device_manager.operations.finish(token)

    def test_stale_transfer_ignores_progress_and_result_queued_after_worker_finished(self) -> None:
        dialog = FakeTransferDialog()
        refresh = MagicMock()
        token = self.device_manager.operations.register(
            "test.stale-finished",
            device_context=self.device_manager.capture_context(),
            cancel_event=threading.Event(),
        )
        self.device_manager.switch(
            DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
        )

        self.page._transfer_worker_finished(token, dialog)
        self.assertEqual(len(dialog.updates), 1)
        terminal_update = dict(dialog.updates[0])

        self.page._transfer_progress(
            token,
            dialog,
            {"type": "progress", "done_bytes": 999, "message": "late progress"},
        )
        self.page._transfer_done(
            token,
            dialog,
            {"success": True, "summary": "late success"},
            refresh,
        )
        self.page._transfer_failed(token, dialog, "Transfer", "late failure")

        self.assertEqual(dialog.updates, [terminal_update])
        self.assertFalse(dialog.updates[0]["success"])
        refresh.assert_not_called()
        self.assertIn(token.operation_id, self.page._stale_transfer_notifications)

        self.page._forget_transfer_dialog(dialog)
        self.assertNotIn(token.operation_id, self.page._stale_transfer_notifications)

    def test_stale_root_result_does_not_mark_new_device_as_granted(self) -> None:
        with patch("openadb.ui.file_manager_page.start_worker") as start_worker:
            self.page.root_boost_button.setChecked(True)
            worker = start_worker.call_args.args[2]
            token = self.page._root_check_token
            self.assertIsNotNone(token)
            self.device_manager.switch(
                DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
            )
            self.page._set_root_status("not checked")
            worker.signals.result.emit(True)
            self.assertEqual(self.page.root_status_label.text(), "Root: not checked")
            worker.signals.finished.emit()

    def test_stale_mutation_result_does_not_refresh_or_show_a_modal(self) -> None:
        token = self.device_manager.operations.register(
            "test.mutation",
            device_context=self.device_manager.capture_context(),
        )
        self.device_manager.switch(
            DeviceInfo(serial="device-2", model="Second device", mode="ADB", state="device")
        )
        result = SimpleNamespace(success=True, status="deleted", stderr="", stdout="")
        refresh = MagicMock()
        with (
            patch.object(QMessageBox, "information") as information,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page._device_command_done(token, "Delete", result, refresh)
            self.page._device_operation_failed(token, "Delete", "late failure")

        refresh.assert_not_called()
        information.assert_not_called()
        warning.assert_not_called()
        self.device_manager.operations.finish(token)

    def test_android_unavailable_and_protected_path_are_explicit(self) -> None:
        self.device_manager.active = DeviceInfo(mode="No device", state="none")
        with (
            patch("openadb.ui.file_manager_page.start_worker") as start_worker,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page.pull_paths(["/sdcard/stale.txt"])
        start_worker.assert_not_called()
        warning.assert_called_once()
        self.assertIn("disconnected", self.page.status_label.text().lower())

        with patch.object(QMessageBox, "warning", return_value=QMessageBox.Cancel) as warning:
            allowed = self.page._warn_android_write("/system/build.prop")
        self.assertFalse(allowed)
        self.assertIn("not guaranteed", warning.call_args.args[2])

        result = SimpleNamespace(success=False, status="permission denied", stderr="", stdout="")
        refresh = MagicMock()
        with (
            patch.object(QMessageBox, "information") as information,
            patch.object(QMessageBox, "warning") as warning,
        ):
            self.page._command_done("Rename", result, refresh)
        information.assert_not_called()
        warning.assert_called_once()
        refresh.assert_called_once()

    def test_shortcuts_are_safe_and_target_the_active_panel(self) -> None:
        self.assertEqual(self.page.refresh_shortcut.key(), QKeySequence("F5"))
        table = FileTable("android")
        emitted: list[str] = []
        table.rename_requested.connect(lambda: emitted.append("rename"))
        table.delete_requested.connect(lambda: emitted.append("delete"))
        table.open_current_requested.connect(lambda: emitted.append("open"))
        table.up_requested.connect(lambda: emitted.append("up"))
        table.refresh_requested.connect(lambda: emitted.append("refresh"))
        for key in [Qt.Key_F2, Qt.Key_Delete, Qt.Key_Return, Qt.Key_Backspace, Qt.Key_F5]:
            table.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
        self.assertEqual(emitted, ["rename", "delete", "open", "up", "refresh"])

        windows_tree = WindowsFileTree()
        windows_emitted: list[str] = []
        windows_tree.rename_requested.connect(lambda: windows_emitted.append("rename"))
        windows_tree.delete_requested.connect(lambda: windows_emitted.append("delete"))
        windows_tree.open_current_requested.connect(lambda: windows_emitted.append("open"))
        windows_tree.up_requested.connect(lambda: windows_emitted.append("up"))
        for key in [Qt.Key_F2, Qt.Key_Delete, Qt.Key_Return, Qt.Key_Backspace]:
            windows_tree.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
        self.assertEqual(windows_emitted, ["rename", "delete", "open", "up"])

        self.page.android_path_edit.setText("abc")
        self.page.android_path_edit.setCursorPosition(3)
        self.page.android_path_edit.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.page.android_path_edit, Qt.Key_Backspace)
        self.assertEqual(self.page.android_path_edit.text(), "ab")

    def test_storage_selector_and_drag_drop_directions_are_preserved(self) -> None:
        volumes = [
            SimpleNamespace(path="/sdcard/", label="Internal", free_bytes=1024, state="mounted"),
            SimpleNamespace(path="/storage/USB1", label="USB", free_bytes=2048, state="mounted"),
        ]
        self.page._set_android_storage_combo(volumes)
        self.assertEqual(self.page.android_storage_combo.count(), 2)
        self.assertIn("USB", self.page.android_storage_combo.itemText(1))
        with patch.object(self.page, "navigate_android") as navigate:
            self.page._android_storage_selected(1)
        navigate.assert_called_once_with("/storage/USB1")

        local_file = self.windows_dir / "drop.txt"
        local_file.write_text("mock", encoding="utf-8")
        android_table = FileTable("android")
        android_drops: list[list[str]] = []
        android_table.dropped.connect(android_drops.append)
        local_mime = QMimeData()
        local_mime.setUrls([QUrl.fromLocalFile(str(local_file))])
        local_event = FakeDropEvent(local_mime)
        android_table.dropEvent(local_event)
        self.assertTrue(local_event.accepted)
        self.assertEqual(
            os.path.normcase(os.path.normpath(android_drops[0][0])),
            os.path.normcase(os.path.normpath(str(local_file))),
        )

        windows_tree = WindowsFileTree()
        android_drops_on_pc: list[list[str]] = []
        windows_tree.dropped.connect(android_drops_on_pc.append)
        android_mime = QMimeData()
        android_mime.setData(ANDROID_MIME, b"/sdcard/a.txt\n/sdcard/b.txt")
        android_event = FakeDropEvent(android_mime)
        windows_tree.dropEvent(android_event)
        self.assertTrue(android_event.accepted)
        self.assertEqual(android_drops_on_pc, [["/sdcard/a.txt", "/sdcard/b.txt"]])

    def test_narrow_layout_and_splitter_render_in_all_themes(self) -> None:
        for theme in ("System", "Light", "Dark"):
            with self.subTest(theme=theme):
                apply_theme(self.app, theme)
                self.page.setMinimumWidth(0)
                self.page.resize(630, 520)
                self.page.file_splitter.setSizes([220, 156, 220])
                self.app.processEvents()
                self.assertEqual(self.page.width(), 630)
                self.assertFalse(self.page.grab().isNull())
                self.assertLessEqual(self.page.file_splitter.widget(1).width(), 196)
                self.assertGreater(self.page.file_splitter.sizes()[0], 0)
                self.assertGreater(self.page.file_splitter.sizes()[2], 0)


if __name__ == "__main__":
    unittest.main()
