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

from openadb.core.settings_manager import SettingsManager
from openadb.core.acbridge_p2p import ADB_TRANSPORT, P2P_TRANSPORT
from openadb.models.device_info import DeviceInfo
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

    def root_available(self) -> bool:
        return self.root_granted


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
        self.device_manager = SimpleNamespace(
            active=DeviceInfo(serial="device-1", model="Test device", mode="ADB", state="device")
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

        self.page._root_check_result(True)
        self.page._root_check_finished()
        self.assertEqual(self.page.root_status_label.text(), "Root: granted")
        self.assertTrue(self.page.root_boost_button.isEnabled())
        self.assertTrue(self.page.pull_button.isEnabled())

        self.page.root_boost_button.setChecked(False)
        self.assertFalse(self.settings.get("file_manager_root_transfer"))
        self.assertEqual(self.page.root_status_label.text(), "Root: not checked")

        with patch("openadb.ui.file_manager_page.start_worker"):
            self.page.root_boost_button.setChecked(True)
        self.page._root_check_result(False)
        self.page._root_check_finished()
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

        self.page.transfer_transport_combo.setCurrentIndex(
            self.page.transfer_transport_combo.findData(P2P_TRANSPORT)
        )
        self.assertEqual(self.settings.get("file_manager_transfer_transport"), P2P_TRANSPORT)
        self.assertFalse(self.page.root_boost_button.isEnabled())
        self.assertEqual(self.page.root_status_label.text(), "Root: not used by P2P")
        self.assertIn("SAF", self.page.push_button.toolTip())

        self.assertTrue(self.settings.activate_device_profile("device-b", "Device B", "TV"))
        self.page.reload_from_settings()
        self.assertEqual(self.page.transfer_transport_combo.currentData(), ADB_TRANSPORT)

        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "TV"))
        self.page.reload_from_settings()
        self.assertEqual(self.page.transfer_transport_combo.currentData(), P2P_TRANSPORT)

    def test_transfer_directions_worker_guard_and_cancel_state(self) -> None:
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
            worker.signals.finished.emit()
            self.assertEqual(self.page._transfer_cancel_events, set())

        cancel_event = threading.Event()
        self.page._cancel_transfer(push_dialog, cancel_event)
        self.assertTrue(cancel_event.is_set())
        self.assertEqual(push_dialog.updates[-1]["type"], "cancelled")
        self.assertIn("cancellation", self.page.status_label.text().lower())

    def test_failed_and_cancelled_transfers_never_report_success(self) -> None:
        dialog = FakeTransferDialog()
        refresh = MagicMock()
        self.page._transfer_done(
            dialog,
            {"success": False, "summary": "adb: write failed: No space left on device"},
            refresh,
        )
        self.assertFalse(dialog.updates[-1]["success"])
        self.assertIn("Insufficient space", dialog.updates[-1]["message"])
        self.assertNotIn("successfully", self.page.status_label.text().lower())
        refresh.assert_called_once()

        self.page._transfer_done(
            dialog,
            {"success": False, "summary": "Transfer cancelled by user."},
            refresh,
        )
        self.assertIn("cancelled", dialog.updates[-1]["message"].lower())
        self.assertFalse(dialog.updates[-1]["success"])

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
