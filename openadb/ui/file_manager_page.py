from __future__ import annotations

from bisect import bisect_right
import os
import re
import shutil
import tarfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from PySide6.QtCore import QThreadPool, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.adb import ADBClient
from openadb.core.device import DeviceManager
from openadb.core.path_utils import (
    format_bytes,
    is_probably_writable_android_path,
    join_android_path,
    parent_android_path,
    safe_filename,
    shell_quote,
)
from openadb.core.settings_manager import SettingsManager
from openadb.ui.widgets.file_panel import FilePanel
from openadb.ui.widgets.native_explorer_panel import NativeExplorerPanel
from openadb.ui.widgets.progress_dialog import TransferProgressDialog
from openadb.ui.widgets.windows_file_panel import WindowsFilePanel
from openadb.ui.workers import Worker, start_worker


PERCENT_PATTERN = re.compile(r"(\d{1,3})\s*%")
ADB_LSTAT_FAILED_PATTERN = re.compile(r"cannot lstat '([^']+)'", re.IGNORECASE)
FAST_TAR_MIN_FILES = 256
FAST_TAR_MAX_AVERAGE_FILE_SIZE = 2 * 1024 * 1024
FAST_TAR_MAX_LARGE_FILE_RATIO = 0.05
FAST_TAR_LARGE_FILE_SIZE = 16 * 1024 * 1024
FAST_TAR_COPY_BUFFER_SIZE = 4 * 1024 * 1024
FAST_TAR_PULL_MIN_FILES = 8
ADB_PUSH_LARGE_AVERAGE_FILE_SIZE = 16 * 1024 * 1024
ADB_PUSH_LARGE_TOTAL_SIZE = 8 * 1024 * 1024 * 1024
ADB_PUSH_LARGE_OBSERVATION_INTERVAL = 4.0
ADB_PUSH_DEFAULT_OBSERVATION_INTERVAL = 2.0
ADB_PUSH_FIRST_OBSERVATION_DELAY = 0.8
ADB_PUSH_PROGRESS_INTERPOLATION_CAP = 0.985
ADB_TRANSFER_DISABLE_COMPRESSION_SIZE = 256 * 1024 * 1024
ADB_TRANSFER_DISABLE_COMPRESSION_AVERAGE = 8 * 1024 * 1024
SINGLE_FILE_STREAM_BUFFER_SIZE = 4 * 1024 * 1024
SINGLE_FILE_STREAM_PROGRESS_INTERVAL = 0.2
FILE_MANAGER_ACTION_PANEL_WIDTH = 168


class _ProgressFile:
    def __init__(self, fileobj: BinaryIO, on_read, cancel_event: threading.Event) -> None:
        self._fileobj = fileobj
        self._on_read = on_read
        self._cancel_event = cancel_event

    def read(self, size: int = -1) -> bytes:
        if self._cancel_event.is_set():
            raise OSError("Transfer cancelled by user")
        data = self._fileobj.read(size)
        if data:
            self._on_read(len(data))
        return data


class FileManagerPage(QWidget):
    def __init__(self, adb: ADBClient, device_manager: DeviceManager, settings: SettingsManager, parent=None) -> None:
        super().__init__(parent)
        self.adb = adb
        self.device_manager = device_manager
        self.settings = settings
        self.pool = QThreadPool.globalInstance()
        self.android_path = "/sdcard/"
        self.windows_path = str(Path.home())
        self._active_side = "android"
        self._windows_history: list[str] = []
        self._windows_history_index = -1
        self._syncing_windows_history = False
        self._android_loading = False
        self._android_refresh_pending = False
        self._transfer_dialogs: list[TransferProgressDialog] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(5)

        top = QHBoxLayout()
        top.setSpacing(6)

        android_top = QHBoxLayout()
        android_top.setContentsMargins(0, 0, 0, 0)
        android_top.setSpacing(5)
        self.android_path_edit = QLineEdit()
        self.android_path_edit.setObjectName("fileManagerPathEdit")
        self.android_path_edit.returnPressed.connect(lambda: self.navigate_android(self.android_path_edit.text()))
        self.android_up_button = QToolButton()
        self.android_up_button.setText("Up")
        self.android_up_button.setObjectName("fileManagerNavButton")
        self.android_up_button.setToolTip("Go up one Android folder")
        self.android_up_button.clicked.connect(lambda: self.navigate_android(self._android_parent_path(self.android_path)))
        android_top.addWidget(self.android_path_edit, 1)
        android_top.addWidget(self.android_up_button)

        windows_top = QHBoxLayout()
        windows_top.setContentsMargins(0, 0, 0, 0)
        windows_top.setSpacing(5)
        self.windows_back_button = QToolButton()
        self.windows_back_button.setText("<")
        self.windows_back_button.setObjectName("fileManagerNavButton")
        self.windows_back_button.setToolTip("Back")
        self.windows_back_button.clicked.connect(self.windows_back)
        self.windows_forward_button = QToolButton()
        self.windows_forward_button.setText(">")
        self.windows_forward_button.setObjectName("fileManagerNavButton")
        self.windows_forward_button.setToolTip("Forward")
        self.windows_forward_button.clicked.connect(self.windows_forward)
        self.windows_path_edit = QLineEdit()
        self.windows_path_edit.setObjectName("fileManagerPathEdit")
        self.windows_path_edit.returnPressed.connect(lambda: self.navigate_windows(self.windows_path_edit.text()))
        windows_top.addWidget(self.windows_back_button)
        windows_top.addWidget(self.windows_forward_button)
        windows_top.addWidget(self.windows_path_edit, 1)

        top.addLayout(android_top, 1)
        top.addSpacing(FILE_MANAGER_ACTION_PANEL_WIDTH)
        top.addLayout(windows_top, 1)
        layout.addLayout(top)

        content = QHBoxLayout()
        content.setSpacing(6)
        self.android_panel = FilePanel("Android", "android", show_path_bar=False, show_button_row=False)
        self.android_panel.table.setObjectName("fileManagerAndroidTable")
        self.windows_panel = self._create_windows_panel()

        android_side = QWidget()
        android_side_layout = QVBoxLayout(android_side)
        android_side_layout.setContentsMargins(0, 0, 0, 0)
        android_side_layout.setSpacing(4)
        android_side_layout.addWidget(self.android_panel, 1)
        self.android_space_label = QLabel("Free space: -")
        self.android_space_label.setObjectName("fileManagerAndroidSpaceLabel")
        android_side_layout.addWidget(self.android_space_label)
        content.addWidget(android_side, 1)

        center = QFrame()
        center.setObjectName("fileManagerCenterPanel")
        center.setFixedWidth(FILE_MANAGER_ACTION_PANEL_WIDTH)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(5, 5, 5, 5)
        center_layout.setSpacing(5)

        self.pull_button = QPushButton(">")
        self.pull_button.setObjectName("fileManagerArrowButton")
        self.pull_button.setToolTip("Copy selected Android files to the current Windows folder")
        self.push_button = QPushButton("<")
        self.push_button.setObjectName("fileManagerArrowButton")
        self.push_button.setToolTip("Copy selected Windows files to the current Android folder")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("fileManagerCompactButton")
        self.refresh_button.setToolTip("Refresh both panels")
        self.mkdir_button = QPushButton("New folder")
        self.mkdir_button.setObjectName("fileManagerCompactButton")
        self.mkdir_button.setToolTip("Create a folder on the active side")
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("fileManagerCompactButton")
        self.rename_button = QPushButton("Rename")
        self.rename_button.setObjectName("fileManagerCompactButton")
        self.copy_path_button = QPushButton("Copy")
        self.copy_path_button.setObjectName("fileManagerCompactButton")
        self.copy_path_button.setToolTip("Copy selected path")
        self.properties_button = QPushButton("Info")
        self.properties_button.setObjectName("fileManagerCompactButton")
        self.open_explorer_button = QPushButton("Open")
        self.open_explorer_button.setObjectName("fileManagerCompactButton")
        self.open_explorer_button.setToolTip("Open current Windows folder in Explorer")
        self.root_boost_button = QPushButton("Root boost")
        self.root_boost_button.setObjectName("fileManagerCompactButton")
        self.root_boost_button.setCheckable(True)
        self.root_boost_button.setChecked(
            bool(self.settings.get("file_manager_root_transfer", False))
            or bool(self.settings.get("root_mode_enabled", False))
        )
        self.root_boost_button.setToolTip(
            "Use su/root for fast ADB transfer streams when the connected device grants root access"
        )
        self.root_boost_button.toggled.connect(lambda checked: self.settings.set("file_manager_root_transfer", checked))
        for button in [self.pull_button, self.push_button]:
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(FILE_MANAGER_ACTION_PANEL_WIDTH - 12)
            button.setMinimumHeight(44)
            center_layout.addWidget(button)
        center_layout.addWidget(self._center_separator())
        for button in [
            self.refresh_button,
            self.mkdir_button,
        ]:
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(FILE_MANAGER_ACTION_PANEL_WIDTH - 12)
            center_layout.addWidget(button)
        center_layout.addWidget(self._center_separator())
        for button in [
            self.delete_button,
            self.rename_button,
            self.copy_path_button,
            self.properties_button,
            self.open_explorer_button,
            self.root_boost_button,
        ]:
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setMinimumWidth(FILE_MANAGER_ACTION_PANEL_WIDTH - 12)
            center_layout.addWidget(button)
        center_layout.addStretch()
        content.addWidget(center)
        content.addWidget(self.windows_panel, 1)
        layout.addLayout(content, 1)

        self.status_label = QLabel("Select files on one side and use the middle buttons to copy through ADB.")
        self.status_label.setObjectName("fileManagerStatusLabel")
        layout.addWidget(self.status_label)

        self.android_panel.navigate_requested.connect(self.navigate_android)
        self.android_panel.up_requested.connect(lambda: self.navigate_android(self._android_parent_path(self.android_path)))
        self.android_panel.refresh_requested.connect(self.refresh_android)
        self.android_panel.new_folder_requested.connect(lambda: self.new_folder("android"))
        self.android_panel.delete_requested.connect(lambda: self.delete_selected("android"))
        self.android_panel.rename_requested.connect(lambda: self.rename_selected("android"))
        self.android_panel.transfer_requested.connect(self.pull_selected)
        self.android_panel.copy_path_requested.connect(lambda: self.copy_path("android"))
        self.android_panel.properties_requested.connect(lambda: self.properties("android"))
        self.android_panel.dropped.connect(self.push_paths)
        self.android_panel.table.focused.connect(lambda: self._set_active_side("android"))

        self.windows_panel.navigate_requested.connect(self.navigate_windows)
        self.windows_panel.up_requested.connect(lambda: self.navigate_windows(str(Path(self.windows_path).parent)))
        self.windows_panel.refresh_requested.connect(self.refresh_windows)
        self.windows_panel.new_folder_requested.connect(lambda: self.new_folder("windows"))
        self.windows_panel.delete_requested.connect(lambda: self.delete_selected("windows"))
        self.windows_panel.rename_requested.connect(lambda: self.rename_selected("windows"))
        self.windows_panel.transfer_requested.connect(self.push_selected)
        self.windows_panel.copy_path_requested.connect(lambda: self.copy_path("windows"))
        self.windows_panel.properties_requested.connect(lambda: self.properties("windows"))
        self.windows_panel.open_external_requested.connect(self.open_explorer)
        self.windows_panel.dropped.connect(self.pull_paths)
        if hasattr(self.windows_panel, "path_changed"):
            self.windows_panel.path_changed.connect(self._windows_path_changed)
        if hasattr(self.windows_panel, "tree"):
            self.windows_panel.tree.focused.connect(lambda: self._set_active_side("windows"))
        if hasattr(self.windows_panel, "focused"):
            self.windows_panel.focused.connect(lambda: self._set_active_side("windows"))

        self.refresh_button.clicked.connect(self.refresh_all)
        self.mkdir_button.clicked.connect(lambda: self.new_folder(self._active_side))
        self.pull_button.clicked.connect(self.pull_selected)
        self.push_button.clicked.connect(self.push_selected)
        self.delete_button.clicked.connect(lambda: self.delete_selected(self._active_side))
        self.rename_button.clicked.connect(lambda: self.rename_selected(self._active_side))
        self.copy_path_button.clicked.connect(lambda: self.copy_path(self._active_side))
        self.properties_button.clicked.connect(lambda: self.properties(self._active_side))
        self.open_explorer_button.clicked.connect(self.open_explorer)

        self.refresh_shortcut = QShortcut(QKeySequence("F5"), self)
        self.refresh_shortcut.activated.connect(self.refresh_all)

        self.android_panel.set_path(self.android_path)
        self.android_path_edit.setText(self.android_path)
        self.navigate_windows(self.windows_path)

    def reload_from_settings(self) -> None:
        self.root_boost_button.blockSignals(True)
        self.root_boost_button.setChecked(
            bool(self.settings.get("file_manager_root_transfer", False))
            or bool(self.settings.get("root_mode_enabled", False))
        )
        self.root_boost_button.blockSignals(False)

    def _center_separator(self) -> QFrame:
        separator = QFrame()
        separator.setObjectName("fileManagerCenterSeparator")
        separator.setFrameShape(QFrame.HLine)
        separator.setFixedHeight(1)
        return separator

    def _create_windows_panel(self) -> QWidget:
        try:
            return NativeExplorerPanel(self.windows_path)
        except Exception:
            return WindowsFilePanel(self.windows_path, show_path_bar=False, show_button_row=False)

    def _set_active_side(self, side: str) -> None:
        self._active_side = "windows" if side == "windows" else "android"

    def _active_up(self) -> None:
        if self._active_side == "windows":
            self.navigate_windows(str(Path(self.windows_path).parent))
        else:
            self.navigate_android(self._android_parent_path(self.android_path))

    def refresh_all(self) -> None:
        self.refresh_windows()
        self.refresh_android()

    def refresh_android(self) -> None:
        if self._android_loading:
            self._android_refresh_pending = True
            return
        if self.device_manager.active.mode not in {"ADB", "Recovery"}:
            self.android_panel.set_path(self.android_path)
            self.android_path_edit.setText(self.android_path)
            self.android_panel.set_items([])
            self.android_space_label.setText("Free space: -")
            self.status_label.setText("Connect an authorized ADB device to browse Android files.")
            return
        path = self.android_path
        self._android_loading = True
        self.android_panel.set_path(self.android_path)
        self.android_path_edit.setText(self.android_path)
        self.android_space_label.setText("Free space: checking...")
        self.status_label.setText(f"Loading Android files: {self.android_path}")
        use_root_requested = self._file_manager_root_requested()
        worker = Worker(lambda: self._load_android_files(path, use_root_requested))
        worker.signals.result.connect(self._android_items_loaded)
        worker.signals.error.connect(lambda message, _trace: QMessageBox.warning(self, "Android files", message))
        worker.signals.finished.connect(self._android_refresh_finished)
        start_worker(self, self.pool, worker)

    def _android_refresh_finished(self) -> None:
        self._android_loading = False
        if self._android_refresh_pending:
            self._android_refresh_pending = False
            self.refresh_android()

    def _load_android_files(self, path: str, use_root_requested: bool) -> tuple[str, list, dict, bool]:
        use_root = self._root_available_for_worker(use_root_requested)
        return path, self.adb.list_files(path, use_root=use_root), self.adb.storage_info(path, use_root=use_root), use_root

    def _android_items_loaded(self, result: tuple[str, list, dict] | tuple[str, list, dict, bool]) -> None:
        path, items, storage = result[:3]
        use_root = bool(result[3]) if len(result) > 3 else False
        if path == self.android_path:
            self.android_panel.set_items(items)
            storage_text = self._android_storage_text(storage)
            self.android_space_label.setText(storage_text)
            prefix = "Android root" if use_root else "Android"
            self.status_label.setText(f"{prefix}: {path} - {len(items)} item(s) - {storage_text}")

    def _android_storage_text(self, storage: dict) -> str:
        free = storage.get("free_bytes")
        total = storage.get("total_bytes")
        used = storage.get("used_bytes")
        used_percent = storage.get("used_percent")
        if not isinstance(free, int) or free < 0:
            return "Free space: Unknown"
        parts = [f"Free space: {format_bytes(free)}"]
        if isinstance(total, int) and total >= 0:
            parts.append(f"Total: {format_bytes(total)}")
        if isinstance(used, int) and used >= 0:
            parts.append(f"Used: {format_bytes(used)}")
        if isinstance(used_percent, int) and used_percent >= 0:
            parts.append(f"{used_percent}% used")
        return " | ".join(parts)

    def navigate_android(self, path: str) -> None:
        normalized = self._normalize_android_path(path)
        self.android_path = normalized
        self.android_path_edit.setText(self.android_path)
        self.refresh_android()

    def _android_parent_path(self, path: str) -> str:
        normalized = self._normalize_android_path(path)
        clean = normalized.rstrip("/") or "/"
        return self._normalize_android_path(parent_android_path(clean))

    def _normalize_android_path(self, path: str) -> str:
        normalized = (path or "").strip().replace("\\", "/") or "/sdcard/"
        normalized = re.sub(r"/+", "/", normalized)
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        if normalized != "/":
            normalized = normalized.rstrip("/")
        if normalized in {"/sdcard", "/storage/emulated/0"}:
            return normalized + "/"
        return normalized

    def refresh_windows(self) -> None:
        if hasattr(self.windows_panel, "refresh"):
            self.windows_panel.refresh()
        else:
            self.windows_panel.set_path(self.windows_path)
        self.windows_path_edit.setText(self.windows_path)

    def navigate_windows(self, path: str, record_history: bool = True) -> None:
        if not path:
            return
        target = Path(path).expanduser()
        if target.exists() and target.is_dir():
            resolved = str(target)
            self.windows_path = resolved
            self.windows_path_edit.setText(resolved)
            self.windows_panel.set_path(resolved)
            if record_history and not self._syncing_windows_history:
                self._push_windows_history(resolved)
            self._sync_windows_history_buttons()
            self.status_label.setText(f"Windows: {resolved}")
        else:
            QMessageBox.warning(self, "Windows path", f"Folder does not exist:\n{path}")

    def _windows_path_changed(self, path: str) -> None:
        if path:
            if os.path.normcase(path) != os.path.normcase(self.windows_path):
                self.windows_path = path
                self.windows_path_edit.setText(path)
                if not self._syncing_windows_history:
                    self._push_windows_history(path)
            self._sync_windows_history_buttons()

    def _push_windows_history(self, path: str) -> None:
        if self._windows_history and 0 <= self._windows_history_index < len(self._windows_history):
            if self._windows_history[self._windows_history_index] == path:
                return
        if self._windows_history_index < len(self._windows_history) - 1:
            self._windows_history = self._windows_history[: self._windows_history_index + 1]
        self._windows_history.append(path)
        self._windows_history_index = len(self._windows_history) - 1

    def _sync_windows_history_buttons(self) -> None:
        self.windows_back_button.setEnabled(self._windows_history_index > 0)
        self.windows_forward_button.setEnabled(0 <= self._windows_history_index < len(self._windows_history) - 1)

    def windows_back(self) -> None:
        if self._windows_history_index <= 0:
            return
        self._windows_history_index -= 1
        self._syncing_windows_history = True
        try:
            self.navigate_windows(self._windows_history[self._windows_history_index], record_history=False)
        finally:
            self._syncing_windows_history = False
        self._sync_windows_history_buttons()

    def windows_forward(self) -> None:
        if self._windows_history_index >= len(self._windows_history) - 1:
            return
        self._windows_history_index += 1
        self._syncing_windows_history = True
        try:
            self.navigate_windows(self._windows_history[self._windows_history_index], record_history=False)
        finally:
            self._syncing_windows_history = False
        self._sync_windows_history_buttons()

    def new_folder(self, kind: str) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok or not name.strip():
            return
        if kind == "android":
            target = join_android_path(self.android_path, name.strip())
            if not self._warn_android_write(target):
                return
            use_root_requested = self._file_manager_root_requested()
            worker = Worker(lambda: self.adb.mkdir(target, use_root=self._root_available_for_worker(use_root_requested)))
            worker.signals.result.connect(lambda result: self._command_done("New folder", result.status, self.refresh_android))
            start_worker(self, self.pool, worker)
        else:
            try:
                (Path(self.windows_path) / safe_filename(name)).mkdir(parents=True, exist_ok=False)
                self.refresh_windows()
            except OSError as exc:
                QMessageBox.warning(self, "New folder", str(exc))

    def delete_selected(self, kind: str) -> None:
        panel = self.android_panel if kind == "android" else self.windows_panel
        paths = panel.selected_paths()
        if not paths:
            return
        answer = QMessageBox.warning(self, "Delete", "Delete selected item(s)?", QMessageBox.Ok | QMessageBox.Cancel)
        if answer != QMessageBox.Ok:
            return
        if kind == "android":
            if any(not self._warn_android_write(path) for path in paths):
                return
            use_root_requested = self._file_manager_root_requested()

            def run() -> list[str]:
                messages: list[str] = []
                use_root = self._root_available_for_worker(use_root_requested)
                for path in paths:
                    result = self.adb.delete(path, recursive=True, use_root=use_root)
                    messages.append(f"{path}: {result.status}")
                return messages

            worker = Worker(run)
            worker.signals.result.connect(lambda messages: self._messages_done("Delete", messages, self.refresh_android))
            start_worker(self, self.pool, worker)
        else:
            def run_delete() -> list[str]:
                messages: list[str] = []
                for path in paths:
                    try:
                        p = Path(path)
                        if p.is_dir():
                            shutil.rmtree(p)
                        else:
                            p.unlink()
                        messages.append(f"{path}: deleted")
                    except OSError as exc:
                        messages.append(f"{path}: {exc}")
                return messages

            worker = Worker(run_delete)
            worker.signals.result.connect(lambda messages: self._messages_done("Delete", messages, self.refresh_windows))
            start_worker(self, self.pool, worker)

    def rename_selected(self, kind: str) -> None:
        panel = self.android_panel if kind == "android" else self.windows_panel
        path = panel.selected_path()
        if not path:
            return
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=Path(path).name if kind == "windows" else path.rstrip("/").split("/")[-1])
        if not ok or not new_name.strip():
            return
        if kind == "android":
            target = join_android_path(parent_android_path(path), new_name.strip())
            if not self._warn_android_write(path):
                return
            use_root_requested = self._file_manager_root_requested()
            worker = Worker(
                lambda: self.adb.rename(path, target, use_root=self._root_available_for_worker(use_root_requested))
            )
            worker.signals.result.connect(lambda result: self._command_done("Rename", result.status, self.refresh_android))
            start_worker(self, self.pool, worker)
        else:
            try:
                Path(path).rename(Path(path).with_name(new_name.strip()))
                self.refresh_windows()
            except OSError as exc:
                QMessageBox.warning(self, "Rename", str(exc))

    def pull_selected(self) -> None:
        self.pull_paths(self.android_panel.selected_paths())

    def pull_paths(self, android_paths: list[str]) -> None:
        if not android_paths:
            return
        destination = Path(self.windows_path)
        cancel_event = threading.Event()
        use_root = self._file_manager_root_requested()
        dialog = self._create_transfer_dialog("ADB Pull")
        dialog.cancel_requested.connect(lambda: self._cancel_transfer(dialog, cancel_event))

        def run(item_callback=None) -> dict:
            return self._run_pull_transfer(android_paths, destination, cancel_event, item_callback, use_root)

        worker = Worker(run)
        worker.signals.item.connect(dialog.apply_update)
        worker.signals.result.connect(lambda result: self._transfer_done(dialog, result, self.refresh_windows))
        worker.signals.error.connect(lambda message, _trace: self._transfer_failed(dialog, "Pull to PC", message))
        start_worker(self, self.pool, worker)
        dialog.show()

    def push_selected(self) -> None:
        self.push_paths(self.windows_panel.selected_paths())

    def push_paths(self, local_paths: list[str]) -> None:
        if not local_paths:
            return
        if not self._warn_android_write(self.android_path):
            return
        cancel_event = threading.Event()
        use_root = self._file_manager_root_requested()
        dialog = self._create_transfer_dialog("ADB Push")
        dialog.cancel_requested.connect(lambda: self._cancel_transfer(dialog, cancel_event))

        def run(item_callback=None) -> dict:
            return self._run_push_transfer(local_paths, self.android_path, cancel_event, item_callback, use_root)

        worker = Worker(run)
        worker.signals.item.connect(dialog.apply_update)
        worker.signals.result.connect(lambda result: self._transfer_done(dialog, result, self.refresh_android))
        worker.signals.error.connect(lambda message, _trace: self._transfer_failed(dialog, "Push to device", message))
        start_worker(self, self.pool, worker)
        dialog.show()

    def copy_path(self, kind: str) -> None:
        panel = self.android_panel if kind == "android" else self.windows_panel
        path = panel.selected_path() or panel.current_path
        QGuiApplication.clipboard().setText(path)

    def properties(self, kind: str) -> None:
        panel = self.android_panel if kind == "android" else self.windows_panel
        path = panel.selected_path() or panel.current_path
        if kind == "android":
            use_root_requested = self._file_manager_root_requested()
            worker = Worker(lambda: self.adb.stat(path, use_root=self._root_available_for_worker(use_root_requested)))
            worker.signals.result.connect(lambda result: QMessageBox.information(self, "Properties", result.stdout or result.stderr or result.status))
            start_worker(self, self.pool, worker)
        else:
            try:
                stat = Path(path).stat()
                text = f"Path: {path}\nSize: {stat.st_size} bytes\nModified: {stat.st_mtime}"
                QMessageBox.information(self, "Properties", text)
            except OSError as exc:
                QMessageBox.warning(self, "Properties", str(exc))

    def open_explorer(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.windows_path))

    def _warn_android_write(self, path: str) -> bool:
        if is_probably_writable_android_path(path):
            return True
        answer = QMessageBox.warning(
            self,
            "Android path warning",
            "This path may be read-only or protected without root. Continue?",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        return answer == QMessageBox.Ok

    def _file_manager_root_requested(self) -> bool:
        return bool(self.root_boost_button.isChecked() or self.settings.get("root_mode_enabled", False))

    def _root_available_for_worker(self, requested: bool) -> bool:
        return bool(requested and self.adb.root_available())

    def _command_done(self, title: str, message: str, refresh) -> None:
        QMessageBox.information(self, title, message)
        refresh()

    def _messages_done(self, title: str, messages: list[str], refresh) -> None:
        QMessageBox.information(self, title, "\n".join(messages[:80]))
        refresh()

    def _create_transfer_dialog(self, title: str) -> TransferProgressDialog:
        dialog = TransferProgressDialog(title, self)
        self._transfer_dialogs.append(dialog)
        dialog.finished.connect(lambda _code, dlg=dialog: self._forget_transfer_dialog(dlg))
        return dialog

    def _forget_transfer_dialog(self, dialog: TransferProgressDialog) -> None:
        if dialog in self._transfer_dialogs:
            self._transfer_dialogs.remove(dialog)

    def _cancel_transfer(self, dialog: TransferProgressDialog, cancel_event: threading.Event) -> None:
        cancel_event.set()
        dialog.apply_update({"type": "cancelled"})

    def _transfer_done(self, dialog: TransferProgressDialog, result: dict, refresh) -> None:
        dialog.apply_update(
            {
                "type": "done",
                "success": result.get("success", False),
                "message": result.get("summary", "Transfer finished."),
            }
        )
        refresh()

    def _transfer_failed(self, dialog: TransferProgressDialog, title: str, message: str) -> None:
        dialog.apply_update({"type": "done", "success": False, "message": f"{title} failed: {message}"})

    def _run_pull_transfer(
        self,
        android_paths: list[str],
        destination: Path,
        cancel_event: threading.Event,
        item_callback,
        use_root_requested: bool,
    ) -> dict:
        entries = []
        use_root = self._root_available_for_worker(use_root_requested)
        for path in android_paths:
            size, count, is_dir = self._android_transfer_stats_with_kind(path, use_root=use_root)
            entries.append({"source": path, "destination": destination, "size": size, "count": count, "is_dir": is_dir})
        return self._run_transfer_entries(
            "Android -> Windows", entries, cancel_event, item_callback, is_pull=True, use_root_requested=use_root
        )

    def _run_push_transfer(
        self,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback,
        use_root_requested: bool,
    ) -> dict:
        entries = []
        for path in local_paths:
            source = Path(path)
            size, count, file_markers = self._local_transfer_stats_with_markers(source)
            entries.append(
                {
                    "source": source,
                    "destination": android_destination,
                    "size": size,
                    "count": count,
                    "file_markers": file_markers,
                }
            )
        return self._run_transfer_entries(
            "Windows -> Android", entries, cancel_event, item_callback, is_pull=False, use_root_requested=use_root_requested
        )

    def _run_transfer_entries(
        self,
        direction: str,
        entries: list[dict],
        cancel_event: threading.Event,
        item_callback,
        is_pull: bool,
        use_root_requested: bool = False,
    ) -> dict:
        started = time.monotonic()
        total_bytes = sum(entry["size"] for entry in entries if isinstance(entry["size"], int) and entry["size"] > 0)
        total_files = sum(entry["count"] for entry in entries if isinstance(entry["count"], int) and entry["count"] > 0) or len(entries)
        done_bytes = 0
        done_files = 0
        messages: list[str] = []
        tar_command = self.adb.detect_tar_command()
        root_available = False
        root_message = ""
        if use_root_requested:
            root_available = self.adb.root_available()
            if root_available:
                root_message = "Root boost is active. OpenADB will use su/root streaming where it is safer or faster."
            else:
                root_message = "Root boost was requested, but root access was not granted. Using normal ADB transfer."
        self._emit_transfer(
            item_callback,
            {
                "type": "plan",
                "title": "ADB transfer started",
                "direction": direction,
                "total_files": total_files,
                "total_bytes": total_bytes,
                "source": "\n".join(str(entry["source"]) for entry in entries),
                "destination": str(entries[0]["destination"]) if entries else "",
                "message": (
                    f"Prepared {len(entries)} selected item(s), estimated files: {total_files}, "
                    f"estimated bytes: {self._format_bytes(total_bytes)}."
                    + (f"\n{root_message}" if root_message else "")
                ),
            },
        )

        success = True
        for index, entry in enumerate(entries, start=1):
            if cancel_event.is_set():
                success = False
                messages.append("Transfer cancelled by user.")
                break
            source = entry["source"]
            destination = entry["destination"]
            entry_size = entry["size"] if isinstance(entry["size"], int) and entry["size"] > 0 else 0
            entry_count = entry["count"] if isinstance(entry["count"], int) and entry["count"] > 0 else 1
            file_markers = entry.get("file_markers") if isinstance(entry.get("file_markers"), list) else []
            root_mode = root_available and use_root_requested
            fast_push = self._should_use_fast_tar_push(
                source,
                entry_size,
                entry_count,
                file_markers,
                tar_command,
                is_pull,
                root_mode,
                str(destination),
            )
            fast_pull = self._should_use_fast_tar_pull(
                source,
                entry_size,
                entry_count,
                tar_command,
                is_pull,
                bool(entry.get("is_dir")),
                root_mode,
            )
            stream_file = self._should_use_single_file_stream(source, is_pull, entry_count, bool(entry.get("is_dir")))
            transfer_source = source
            transfer_destination = destination
            if root_mode and is_pull and (fast_pull or stream_file):
                transfer_source = self._root_accel_android_path(str(source), preserve_root_name=True)
            elif root_mode and not is_pull and (fast_push or stream_file):
                transfer_destination = self._root_accel_android_path(str(destination))
            disable_adb_compression = self._should_disable_adb_compression(
                source,
                entry_size,
                entry_count,
                file_markers,
                fast_push=fast_push,
                fast_pull=fast_pull,
                stream_file=stream_file,
            )
            command = self._transfer_command_text(
                source,
                destination,
                is_pull,
                fast_push=fast_push,
                fast_pull=fast_pull,
                tar_command=tar_command,
                stream_file=stream_file,
                root_mode=root_mode,
                transfer_source=transfer_source,
                transfer_destination=transfer_destination,
                disable_compression=disable_adb_compression,
            )
            start_message = f"Starting: {command}"
            if fast_pull:
                start_message = f"Starting {'root ' if root_mode else ''}fast TAR pull mode: {command}"
            elif fast_push:
                start_message = f"Starting {'root ' if root_mode else ''}fast TAR push mode: {command}"
            elif stream_file:
                start_message = f"Starting {'root ' if root_mode else ''}live single-file stream: {command}"
            elif disable_adb_compression:
                start_message = f"Starting: {command}\nUsing native ADB transfer with compression disabled for large/already-compressed files."
            elif is_pull and bool(entry.get("is_dir")) and not tar_command:
                start_message = f"Starting: {command}\nFast TAR pull mode is unavailable because Android tar was not found."
            elif not is_pull and Path(source).is_dir() and tar_command:
                start_message = f"Starting: {command}\nUsing standard adb push because this folder is better suited for native ADB transfer."
            elif not is_pull and Path(source).is_dir() and not tar_command:
                start_message = f"Starting: {command}\nFast TAR push mode is unavailable because Android tar was not found."
            self._emit_transfer(
                item_callback,
                {
                    "type": "file_start",
                    "current_file": self._current_transfer_file_label(source, 0, file_markers),
                    "command": command,
                    "message": start_message,
                },
            )

            last_percent = 0

            def on_output(channel: str, text: str) -> None:
                nonlocal last_percent
                percent = self._extract_percent(text)
                if percent is not None:
                    last_percent = max(last_percent, percent)
                current_entry_bytes = int(entry_size * last_percent / 100) if entry_size else 0
                current_entry_files = (
                    0
                    if is_pull
                    else self._estimate_observed_files(entry_count, entry_size, current_entry_bytes, file_markers)
                )
                current_bytes = done_bytes + current_entry_bytes
                current_files = done_files + current_entry_files
                current_file = (
                    ""
                    if is_pull
                    else self._current_transfer_file_label(source, current_entry_bytes, file_markers)
                )
                update = {
                    "type": "progress",
                    "done_bytes": current_bytes,
                    "total_bytes": total_bytes,
                    "done_files": current_files,
                    "total_files": total_files,
                    "speed": self._speed_text(current_bytes, started),
                    "output": f"[{channel}] {text.strip()}",
                }
                if current_file:
                    update["current_file"] = current_file
                self._emit_transfer(item_callback, update)

            transfer_state = self._run_entry_command_with_progress(
                source=source,
                destination=destination,
                is_pull=is_pull,
                transfer_source=transfer_source,
                transfer_destination=transfer_destination,
                root_mode=root_mode,
                timeout=None,
                cancel_event=cancel_event,
                output_callback=on_output,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                entry_count=entry_count,
                file_markers=file_markers,
                fast_push=fast_push,
                fast_pull=fast_pull,
                tar_command=tar_command,
                stream_file=stream_file,
                entry_is_dir=bool(entry.get("is_dir")),
                disable_compression=disable_adb_compression,
            )
            result = transfer_state.get("result")
            observed_bytes = int(transfer_state.get("observed_bytes") or 0)
            observed_files = int(transfer_state.get("observed_files") or 0)
            if result is None:
                success = False
                done_bytes += observed_bytes
                done_files += observed_files
                message = f"{source} -> {destination}: transfer process did not return a result"
                messages.append(message)
                self._emit_transfer(
                    item_callback,
                    {
                        "type": "file_done",
                        "done_files": done_files,
                        "total_files": max(total_files, done_files),
                        "done_bytes": done_bytes,
                        "total_bytes": max(total_bytes, done_bytes),
                        "speed": self._speed_text(done_bytes, started),
                        "message": message,
                    },
                )
                continue
            if result.success:
                done_bytes += max(entry_size, observed_bytes)
                done_files += max(entry_count, observed_files)
            else:
                success = False
                done_bytes += observed_bytes
                done_files += observed_files
            message = f"{source} -> {destination}: {result.status}"
            messages.append(message)
            self._emit_transfer(
                item_callback,
                {
                    "type": "file_done",
                    "done_files": done_files,
                    "total_files": max(total_files, done_files),
                    "done_bytes": done_bytes,
                    "total_bytes": max(total_bytes, done_bytes),
                    "speed": self._speed_text(done_bytes, started),
                    "message": message,
                },
            )
        elapsed = time.monotonic() - started
        reported_total_files = max(total_files, done_files)
        summary = (
            f"Transfer {'completed' if success else 'finished with errors'}: "
            f"{done_files}/{reported_total_files} files, {self._format_bytes(done_bytes)} in {elapsed:.1f}s."
        )
        if messages:
            summary += "\n" + "\n".join(messages[-10:])
        return {"success": success, "summary": summary, "messages": messages}

    def _emit_transfer(self, item_callback, update: dict) -> None:
        if item_callback:
            item_callback.emit(update)

    def _run_entry_command_with_progress(
        self,
        source,
        destination,
        is_pull: bool,
        transfer_source,
        transfer_destination,
        root_mode: bool,
        timeout: int | float | None,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        entry_count: int,
        file_markers: list[tuple[int, str]],
        fast_push: bool = False,
        fast_pull: bool = False,
        tar_command: str = "",
        stream_file: bool = False,
        entry_is_dir: bool = False,
        disable_compression: bool = False,
    ) -> dict:
        if fast_pull:
            return self._run_fast_tar_pull_with_progress(
                source=str(transfer_source),
                destination=Path(destination),
                tar_command=tar_command,
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                entry_count=entry_count,
                use_root=root_mode,
            )
        if fast_push:
            return self._run_fast_tar_push_with_progress(
                source=Path(source),
                destination=str(transfer_destination),
                tar_command=tar_command,
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                entry_count=entry_count,
                use_root=root_mode,
            )
        if stream_file and is_pull and not entry_is_dir:
            return self._run_single_file_pull_with_progress(
                source=str(transfer_source),
                display_source=str(source),
                destination=Path(destination),
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                use_root=root_mode,
            )
        if stream_file and not is_pull and isinstance(source, Path) and source.is_file():
            return self._run_single_file_push_with_progress(
                source=source,
                destination=str(transfer_destination),
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                entry_size=entry_size,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
                total_files=total_files,
                done_files=done_files,
                started=started,
                use_root=root_mode,
            )

        result_holder = {}
        command_done = threading.Event()
        entry_started_wall = time.time()
        entry_started_monotonic = time.monotonic()
        baseline = self._transfer_observation_baseline(source, destination, is_pull)
        latest_bytes = 0
        latest_files = 0
        latest_file = self._current_transfer_file_label(source, 0, file_markers)
        observed_speed = 0.0
        previous_observation_bytes = 0
        previous_observation_time = entry_started_monotonic
        observation_interval = (
            1.0
            if is_pull
            else self._push_observation_interval(entry_size, entry_count, file_markers)
        )
        next_observation = (
            0.0
            if is_pull
            else entry_started_monotonic + min(ADB_PUSH_FIRST_OBSERVATION_DELAY, observation_interval)
        )

        def run_command() -> None:
            try:
                if is_pull:
                    result_holder["result"] = self.adb.pull_streaming(
                        str(source),
                        destination,
                        timeout=timeout,
                        output_callback=output_callback,
                        cancel_event=cancel_event,
                        disable_compression=disable_compression,
                    )
                else:
                    result_holder["result"] = self.adb.push_streaming(
                        source,
                        str(destination),
                        timeout=timeout,
                        output_callback=output_callback,
                        cancel_event=cancel_event,
                        disable_compression=disable_compression,
                    )
            finally:
                command_done.set()

        thread = threading.Thread(target=run_command, daemon=True)
        thread.start()

        while not command_done.wait(0.5):
            if cancel_event.is_set():
                break
            now = time.monotonic()
            if now >= next_observation:
                latest_bytes, latest_files, latest_file = self._observed_transfer_stats(
                    source,
                    destination,
                    is_pull,
                    entry_size,
                    entry_started_wall,
                    baseline,
                    entry_count,
                    file_markers,
                )
                if latest_bytes >= previous_observation_bytes:
                    delta_bytes = latest_bytes - previous_observation_bytes
                    delta_seconds = max(0.1, now - previous_observation_time)
                    if delta_bytes > 0:
                        observed_speed = delta_bytes / delta_seconds
                    previous_observation_bytes = latest_bytes
                    previous_observation_time = now
                next_observation = now + observation_interval
            current_entry_bytes = max(0, latest_bytes)
            current_entry_files = max(0, latest_files)
            current_file = latest_file
            if not is_pull and entry_size > current_entry_bytes and observed_speed > 0:
                estimated_bytes = int(latest_bytes + observed_speed * max(0.0, now - previous_observation_time))
                interpolation_cap = max(current_entry_bytes, int(entry_size * ADB_PUSH_PROGRESS_INTERPOLATION_CAP))
                estimated_bytes = min(max(current_entry_bytes, estimated_bytes), interpolation_cap)
                if estimated_bytes > current_entry_bytes:
                    current_entry_bytes = estimated_bytes
                    current_entry_files = max(
                        current_entry_files,
                        self._estimate_observed_files(entry_count, entry_size, estimated_bytes, file_markers),
                    )
                    current_file = self._current_transfer_file_label(source, estimated_bytes, file_markers)
            current_bytes = done_bytes + current_entry_bytes
            current_files = done_files + current_entry_files
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": current_file,
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "ADB transfer is running",
                },
            )

        thread.join(timeout=3)
        result = result_holder.get("result")
        if not is_pull and result is not None and isinstance(source, Path) and source.is_dir():
            missing_files = self._standard_push_failed_local_paths(result, source)
            if missing_files:
                fixed_files, failed_files = self._repair_standard_push_missing_files(
                    missing_files=missing_files,
                    source_root=source,
                    destination=str(destination),
                    cancel_event=cancel_event,
                    output_callback=output_callback,
                    item_callback=item_callback,
                    entry_size=entry_size,
                    entry_count=entry_count,
                    done_bytes=done_bytes,
                    done_files=done_files,
                    total_bytes=total_bytes,
                    total_files=total_files,
                    started=started,
                    use_root=root_mode,
                )
                if failed_files:
                    result.success = False
                    result.status = (
                        f"Partial transfer: repaired {fixed_files}/{len(missing_files)} long-path file(s); "
                        f"{len(failed_files)} file(s) still failed."
                    )
                    failed_text = "\n".join(str(path) for path in failed_files[:10])
                    result.stderr = (result.stderr + "\n" if result.stderr else "") + failed_text
                elif fixed_files:
                    result.status = f"Success; repaired {fixed_files} long-path file(s) through OpenADB streaming fallback."
        if not is_pull and result is not None and result.success:
            return {
                "result": result,
                "observed_bytes": entry_size,
                "observed_files": entry_count,
            }
        latest_bytes, latest_files, latest_file = self._observed_transfer_stats(
            source,
            destination,
            is_pull,
            entry_size,
            entry_started_wall,
            baseline,
            entry_count,
            file_markers,
        )
        return {
            "result": result_holder.get("result"),
            "observed_bytes": max(0, latest_bytes),
            "observed_files": max(0, latest_files),
        }

    def _standard_push_failed_local_paths(self, result, source_root: Path) -> list[Path]:
        text = "\n".join(part for part in [getattr(result, "stdout", ""), getattr(result, "stderr", "")] if part)
        if not text:
            return []
        candidates = []
        for match in ADB_LSTAT_FAILED_PATTERN.finditer(text):
            raw_path = match.group(1).strip()
            if raw_path:
                candidates.append(raw_path)
        if not candidates:
            return []
        known_files = {}
        try:
            for path in source_root.rglob("*"):
                if path.is_file():
                    known_files[os.path.normcase(str(path))] = path
        except OSError:
            known_files = {}
        failed: list[Path] = []
        seen: set[str] = set()
        for raw_path in candidates:
            path = Path(raw_path)
            if not path.exists():
                path = known_files.get(os.path.normcase(raw_path), path)
            if not path.exists() or not path.is_file():
                continue
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            failed.append(path)
        return failed

    def _repair_standard_push_missing_files(
        self,
        missing_files: list[Path],
        source_root: Path,
        destination: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        entry_count: int,
        done_bytes: int,
        done_files: int,
        total_bytes: int,
        total_files: int,
        started: float,
        use_root: bool,
    ) -> tuple[int, list[Path]]:
        missing_sizes = []
        for path in missing_files:
            try:
                missing_sizes.append(path.stat().st_size)
            except OSError:
                missing_sizes.append(0)
        missing_total = sum(missing_sizes)
        base_bytes = max(0, entry_size - missing_total)
        base_files = max(0, entry_count - len(missing_files))
        repaired_bytes = 0
        repaired_files = 0
        failed_files: list[Path] = []

        for path, size in zip(missing_files, missing_sizes):
            if cancel_event.is_set():
                failed_files.append(path)
                continue
            try:
                relative = path.relative_to(source_root).as_posix()
            except ValueError:
                relative = path.name
            remote_target = join_android_path(join_android_path(destination, source_root.name), relative)
            target_use_root = bool(use_root and not is_probably_writable_android_path(remote_target))
            result, sent = self._stream_push_file_to_android_target(
                source=path,
                target=remote_target,
                cancel_event=cancel_event,
                output_callback=output_callback,
                item_callback=item_callback,
                base_done_bytes=done_bytes + base_bytes + repaired_bytes,
                base_done_files=done_files + base_files + repaired_files,
                total_bytes=total_bytes,
                total_files=total_files,
                started=started,
                use_root=target_use_root,
                activity="Long Windows path fallback push is running",
            )
            repaired_bytes += sent if result.success else 0
            if result.success:
                repaired_files += 1
            else:
                failed_files.append(path)
        return repaired_files, failed_files

    def _stream_push_file_to_android_target(
        self,
        source: Path,
        target: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        base_done_bytes: int,
        base_done_files: int,
        total_bytes: int,
        total_files: int,
        started: float,
        use_root: bool = False,
        activity: str = "ADB single-file push is running",
    ) -> tuple[object, int]:
        temp_target = self._android_temp_sibling_path(target)
        sent_bytes = 0
        last_emit = 0.0

        def emit_progress(force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < SINGLE_FILE_STREAM_PROGRESS_INTERVAL:
                return
            last_emit = now
            current_bytes = base_done_bytes + max(0, sent_bytes)
            current_files = base_done_files + (1 if source.exists() and sent_bytes >= source.stat().st_size else 0)
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": str(source),
                    "speed": self._speed_text(current_bytes, started),
                    "activity": activity,
                },
            )

        def input_writer(stream: BinaryIO) -> None:
            nonlocal sent_bytes
            with source.open("rb") as fileobj:
                while True:
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    chunk = fileobj.read(SINGLE_FILE_STREAM_BUFFER_SIZE)
                    if not chunk:
                        break
                    stream.write(chunk)
                    sent_bytes += len(chunk)
                    emit_progress()
            emit_progress(force=True)

        script = (
            f"target={shell_quote(target)}; tmp={shell_quote(temp_target)}; "
            'parent=${target%/*}; [ "$parent" = "$target" ] && parent=.; '
            'mkdir -p "$parent" && cat > "$tmp"'
        )
        if use_root:
            script = self.adb.root_shell_script(script)
        result = self.adb.run_raw_with_input_stream(
            ["exec-in", "sh", "-c", script],
            input_writer=input_writer,
            timeout=None,
            output_callback=output_callback,
            cancel_event=cancel_event,
        )
        if result.success:
            finalize_script = (
                f"target={shell_quote(target)}; tmp={shell_quote(temp_target)}; "
                'parent=${target%/*}; [ "$parent" = "$target" ] && parent=.; '
                'owner=$(stat -c "%u:%g" "$parent" 2>/dev/null || true); '
                'mv -f "$tmp" "$target"; rc=$?; '
                'if [ $rc -eq 0 ] && [ -n "$owner" ]; then '
                'chown "$owner" "$target" 2>/dev/null || true; '
                'restorecon "$target" 2>/dev/null || true; '
                'fi; exit $rc'
            )
            finalize_result = (
                self.adb.run_root_shell(finalize_script, timeout=30)
                if use_root
                else self.adb.run_shell(finalize_script, timeout=30)
            )
            if not finalize_result.success:
                result.success = False
                result.status = f"Remote file finalize failed: {finalize_result.status}"
                result.error_type = finalize_result.error_type or "remote_finalize_failed"
                detail = finalize_result.stderr or finalize_result.stdout or finalize_result.status
                result.stderr = (result.stderr + "\n" if result.stderr else "") + detail
        if not result.success:
            cleanup_script = f"rm -f {shell_quote(temp_target)}"
            if use_root:
                self.adb.run_root_shell(cleanup_script, timeout=15)
            else:
                self.adb.run_shell(cleanup_script, timeout=15)
        return result, sent_bytes

    def _run_single_file_push_with_progress(
        self,
        source: Path,
        destination: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        use_root: bool = False,
    ) -> dict:
        target = self._android_push_target(source, destination)
        result, sent_bytes = self._stream_push_file_to_android_target(
            source=source,
            target=target,
            cancel_event=cancel_event,
            output_callback=output_callback,
            item_callback=item_callback,
            base_done_bytes=done_bytes,
            base_done_files=done_files,
            total_bytes=total_bytes,
            total_files=total_files,
            started=started,
            use_root=use_root,
            activity="Root single-file push is running" if use_root else "ADB single-file push is running",
        )
        observed_bytes = entry_size if result.success else sent_bytes
        observed_files = 1 if result.success else (1 if entry_size > 0 and sent_bytes >= entry_size else 0)
        return {"result": result, "observed_bytes": observed_bytes, "observed_files": observed_files}

    def _android_temp_sibling_path(self, target: str) -> str:
        parent = parent_android_path(target)
        stamp = int(time.time() * 1000)
        suffix = abs(hash((target, stamp, threading.get_ident()))) & 0xFFFFFF
        return join_android_path(parent, f".openadb-part-{stamp}-{suffix:06x}")

    def _local_temp_sibling_path(self, target: Path) -> Path:
        stamp = int(time.time() * 1000)
        suffix = abs(hash((str(target), stamp, threading.get_ident()))) & 0xFFFFFF
        return target.with_name(f".openadb-part-{stamp}-{suffix:06x}")

    def _run_single_file_pull_with_progress(
        self,
        source: str,
        display_source: str,
        destination: Path,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        use_root: bool = False,
    ) -> dict:
        target = self._local_pull_target(display_source, destination)
        temp_target = self._local_temp_sibling_path(target)
        received_bytes = 0
        last_emit = 0.0

        def emit_progress(force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < SINGLE_FILE_STREAM_PROGRESS_INTERVAL:
                return
            last_emit = now
            current_bytes = done_bytes + max(0, received_bytes)
            current_files = done_files + (1 if entry_size > 0 and received_bytes >= entry_size else 0)
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": display_source,
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "Root single-file pull is running" if use_root else "ADB single-file pull is running",
                },
            )

        def on_progress(total_written: int) -> None:
            nonlocal received_bytes
            received_bytes = max(received_bytes, int(total_written))
            emit_progress()

        result = self.adb.pull_file_streaming_to_file(
            source,
            temp_target,
            timeout=None,
            output_callback=output_callback,
            progress_callback=on_progress,
            cancel_event=cancel_event,
            use_root=use_root,
        )
        emit_progress(force=True)
        if result.success:
            try:
                if target.exists() and target.is_dir():
                    raise OSError(f"Cannot overwrite directory: {target}")
                os.replace(temp_target, target)
            except OSError as exc:
                result.success = False
                result.status = f"Local file rename failed: {exc}"
                result.error_type = "local_rename_failed"
                result.stderr = (result.stderr + "\n" if result.stderr else "") + str(exc)
        if not result.success:
            try:
                temp_target.unlink(missing_ok=True)
            except OSError:
                pass
        observed_bytes = entry_size if result.success else received_bytes
        observed_files = 1 if result.success else (1 if entry_size > 0 and received_bytes >= entry_size else 0)
        return {"result": result, "observed_bytes": observed_bytes, "observed_files": observed_files}

    def _run_fast_tar_pull_with_progress(
        self,
        source: str,
        destination: Path,
        tar_command: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        entry_count: int,
        use_root: bool = False,
    ) -> dict:
        received_bytes = 0
        received_files = 0
        current_file = source
        last_emit = 0.0
        destination_root = destination.resolve()

        def emit_progress(force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < 0.25:
                return
            last_emit = now
            current_bytes = done_bytes + max(0, received_bytes)
            current_files = done_files + max(0, received_files)
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": current_file,
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "Root fast TAR pull is running" if use_root else "Fast TAR pull is running",
                },
            )

        def safe_target(member_name: str) -> Path | None:
            clean_name = str(PurePosixPath(member_name.replace("\\", "/"))).lstrip("/")
            parts = PurePosixPath(clean_name).parts
            if not parts or any(part in {"", ".", ".."} for part in parts):
                return None
            target = destination.joinpath(*parts).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError:
                return None
            return target

        def output_writer(stream: BinaryIO) -> None:
            nonlocal received_bytes, received_files, current_file
            destination.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=stream, mode="r|*") as archive:
                for member in archive:
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    target = safe_target(member.name)
                    if target is None:
                        continue
                    current_file = str(target)
                    emit_progress(force=True)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source_file = archive.extractfile(member)
                    if source_file is None:
                        continue
                    with source_file, target.open("wb") as fileobj:
                        while True:
                            if cancel_event.is_set():
                                raise OSError("Transfer cancelled by user")
                            chunk = source_file.read(1024 * 1024)
                            if not chunk:
                                break
                            fileobj.write(chunk)
                            received_bytes += len(chunk)
                            emit_progress()
                    if member.mtime:
                        try:
                            os.utime(target, (member.mtime, member.mtime))
                        except OSError:
                            pass
                    received_files += 1
                    emit_progress(force=True)

        result = self.adb.pull_tar_streaming(
            source=source,
            tar_command=tar_command,
            output_writer=output_writer,
            timeout=None,
            output_callback=output_callback,
            cancel_event=cancel_event,
            use_root=use_root,
        )
        observed_bytes = entry_size if result.success else received_bytes
        observed_files = entry_count if result.success else received_files
        return {"result": result, "observed_bytes": observed_bytes, "observed_files": observed_files}

    def _run_fast_tar_push_with_progress(
        self,
        source: Path,
        destination: str,
        tar_command: str,
        cancel_event: threading.Event,
        output_callback,
        item_callback,
        entry_size: int,
        done_bytes: int,
        total_bytes: int,
        total_files: int,
        done_files: int,
        started: float,
        entry_count: int,
        use_root: bool = False,
    ) -> dict:
        directories, files = self._tar_stream_items(source)
        sent_bytes = 0
        sent_files = 0
        current_file = files[0][1] if files else str(source)
        last_emit = 0.0

        def emit_progress(force: bool = False) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if not force and now - last_emit < 0.25:
                return
            last_emit = now
            current_bytes = done_bytes + max(0, sent_bytes)
            current_files = done_files + max(0, sent_files)
            self._emit_transfer(
                item_callback,
                {
                    "type": "heartbeat",
                    "done_bytes": current_bytes,
                    "total_bytes": max(total_bytes, current_bytes),
                    "done_files": current_files,
                    "total_files": max(total_files, current_files),
                    "current_file": current_file,
                    "speed": self._speed_text(current_bytes, started),
                    "activity": "Root fast TAR push is running" if use_root else "Fast TAR push is running",
                },
            )

        def input_writer(stream: BinaryIO) -> None:
            nonlocal sent_bytes, sent_files, current_file
            with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT, dereference=True) as archive:
                archive.copybufsize = FAST_TAR_COPY_BUFFER_SIZE
                for directory, arcname in directories:
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    info = archive.gettarinfo(str(directory), arcname=arcname)
                    archive.addfile(info)
                for file_path, arcname, _file_size in files:
                    if cancel_event.is_set():
                        raise OSError("Transfer cancelled by user")
                    current_file = arcname
                    emit_progress(force=True)

                    def on_read(chunk_size: int) -> None:
                        nonlocal sent_bytes
                        sent_bytes += chunk_size
                        emit_progress()

                    info = archive.gettarinfo(str(file_path), arcname=arcname)
                    with file_path.open("rb") as fileobj:
                        archive.addfile(info, _ProgressFile(fileobj, on_read, cancel_event))
                    sent_files += 1
                    emit_progress(force=True)

        result = self.adb.push_tar_streaming(
            destination=destination,
            tar_command=tar_command,
            input_writer=input_writer,
            timeout=None,
            output_callback=output_callback,
            cancel_event=cancel_event,
            use_root=use_root,
            target_name=source.name,
        )
        observed_bytes = entry_size if result.success else sent_bytes
        observed_files = entry_count if result.success else sent_files
        return {"result": result, "observed_bytes": observed_bytes, "observed_files": observed_files}

    def _push_observation_interval(
        self,
        entry_size: int,
        entry_count: int,
        file_markers: list[tuple[int, str]],
    ) -> float:
        if entry_size <= 0 or entry_count <= 0:
            return ADB_PUSH_DEFAULT_OBSERVATION_INTERVAL
        average_size = entry_size / max(1, entry_count)
        if average_size >= ADB_PUSH_LARGE_AVERAGE_FILE_SIZE or entry_size >= ADB_PUSH_LARGE_TOTAL_SIZE:
            return ADB_PUSH_LARGE_OBSERVATION_INTERVAL
        if any(size >= ADB_PUSH_LARGE_AVERAGE_FILE_SIZE for size in self._file_sizes_from_markers(file_markers)):
            return ADB_PUSH_LARGE_OBSERVATION_INTERVAL
        return ADB_PUSH_DEFAULT_OBSERVATION_INTERVAL

    def _transfer_observation_baseline(self, source, destination, is_pull: bool) -> tuple[int, int]:
        if is_pull:
            target = self._local_pull_target(str(source), Path(destination))
            return self._local_transfer_stats(target) if target.exists() else (0, 0)
        return self._android_transfer_observation(self._android_push_target(source, destination))

    def _observed_transfer_stats(
        self,
        source,
        destination,
        is_pull: bool,
        entry_size: int,
        entry_started_wall: float,
        baseline: tuple[int, int],
        entry_count: int,
        file_markers: list[tuple[int, str]],
    ) -> tuple[int, int, str]:
        if is_pull:
            target = self._local_pull_target(str(source), Path(destination))
            if not target.exists():
                return (0, 0, str(source))
            size, count, current_file = self._local_transfer_observation(target, entry_started_wall)
            return (max(0, size - baseline[0]), max(0, count - baseline[1]), current_file or str(source))
        target = self._android_push_target(source, destination)
        size, count = self._android_transfer_observation(target)
        observed_bytes = max(0, size - baseline[0])
        observed_files = max(0, count - baseline[1])
        if observed_files <= 0 and observed_bytes > 0:
            observed_files = self._estimate_observed_files(entry_count, entry_size, observed_bytes, file_markers)
        current_file = self._current_transfer_file_label(source, observed_bytes, file_markers)
        return (observed_bytes, observed_files, current_file)

    def _android_push_target(self, source, destination) -> str:
        name = Path(source).name
        destination_text = str(destination).replace("\\", "/").strip() or "/sdcard/"
        return join_android_path(destination_text, name)

    def _android_transfer_observation(self, android_path: str, use_root: bool = False) -> tuple[int, int]:
        quoted_path = shell_quote(android_path)
        script = (
            f"p={quoted_path}; "
            'if [ -d "$p" ]; then '
            'size=$(du -s -k "$p" 2>/dev/null | sed -n "1s/[[:space:]].*$//p"); '
            'count=$(find "$p" -type f 2>/dev/null | wc -l); '
            'echo OPENADB_SIZE_KB:${size:-0}; '
            'echo OPENADB_FILES:${count:-0}; '
            'elif [ -e "$p" ]; then '
            'size=$(stat -c %s "$p" 2>/dev/null); '
            'echo OPENADB_SIZE_BYTES:${size:-0}; '
            'echo OPENADB_FILES:1; '
            'else '
            'echo OPENADB_SIZE_BYTES:0; '
            'echo OPENADB_FILES:0; '
            "fi"
        )
        result = self.adb.run_root_shell(script, timeout=12) if use_root else self.adb.run_shell(script, timeout=12)
        if not result.stdout:
            return (0, 0)
        size_bytes = 0
        file_count = 0
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("OPENADB_SIZE_BYTES:"):
                size_bytes = self._first_int(line.split(":", 1)[1]) or 0
            elif line.startswith("OPENADB_SIZE_KB:"):
                size_bytes = (self._first_int(line.split(":", 1)[1]) or 0) * 1024
            elif line.startswith("OPENADB_FILES:"):
                file_count = self._first_int(line.split(":", 1)[1]) or 0
        return (size_bytes, file_count)

    def _local_pull_target(self, source: str, destination: Path) -> Path:
        source_name = PurePosixPath(source.rstrip("/")).name or Path(source).name
        if destination.exists() and destination.is_dir():
            return destination / source_name
        return destination

    def _transfer_command_text(
        self,
        source,
        destination,
        is_pull: bool,
        fast_push: bool = False,
        fast_pull: bool = False,
        tar_command: str = "",
        stream_file: bool = False,
        root_mode: bool = False,
        transfer_source=None,
        transfer_destination=None,
        disable_compression: bool = False,
    ) -> str:
        effective_source = str(transfer_source if transfer_source is not None else source)
        effective_destination = str(transfer_destination if transfer_destination is not None else destination)
        if fast_pull:
            clean_source = effective_source.rstrip("/") or "/"
            script = (
                f"src={shell_quote(clean_source)}; "
                'parent=${src%/*}; name=${src##*/}; '
                '[ -z "$parent" ] && parent=/; [ "$parent" = "$src" ] && parent=/; '
                '[ -z "$name" ] && name=.; '
                f"cd \"$parent\" && {tar_command} -cf - \"$name\""
            )
            if root_mode:
                script = self.adb.root_shell_script(script)
            args = ["exec-out", "sh", "-c", script]
        elif fast_push:
            quoted_destination = shell_quote(effective_destination)
            if root_mode:
                quoted_target_name = shell_quote(Path(source).name)
                script = (
                    f"dest={quoted_destination}; target_name={quoted_target_name}; "
                    'mkdir -p "$dest" || exit $?; '
                    'owner=$(stat -c "%u:%g" "$dest" 2>/dev/null || true); '
                    f'cd "$dest" && {tar_command} -xf -; rc=$?; '
                    'if [ $rc -eq 0 ] && [ -n "$owner" ] && [ -n "$target_name" ]; then '
                    'target="$dest/$target_name"; chown -R "$owner" "$target" 2>/dev/null || true; '
                    'restorecon -R "$target" 2>/dev/null || true; fi; exit $rc'
                )
                script = self.adb.root_shell_script(script)
            else:
                script = f"mkdir -p {quoted_destination} && cd {quoted_destination} && {tar_command} -xf -"
            args = ["exec-in", "sh", "-c", script]
        elif stream_file and is_pull:
            script = f"cat {shell_quote(effective_source)}"
            if root_mode:
                script = self.adb.root_shell_script(script)
            args = ["exec-out", "sh", "-c", script]
        elif stream_file:
            target = self._android_push_target(source, effective_destination)
            script = f"cat > {shell_quote(target)}"
            if root_mode:
                script = self.adb.root_shell_script(script)
            args = ["exec-in", "sh", "-c", script]
        else:
            if is_pull:
                args = ["pull"]
                if disable_compression:
                    args.append("-Z")
                args.extend([str(source), str(destination)])
            else:
                args = ["push"]
                if disable_compression:
                    args.append("-Z")
                args.extend([str(source), str(destination)])
        return self.adb.runner.command_text([*self.adb._base(), *args])

    def _should_use_single_file_stream(self, source, is_pull: bool, entry_count: int, entry_is_dir: bool) -> bool:
        if entry_count != 1 or entry_is_dir:
            return False
        if is_pull:
            return True
        return isinstance(source, Path) and source.is_file()

    def _should_use_fast_tar_push(
        self,
        source,
        entry_size: int,
        entry_count: int,
        file_markers: list[tuple[int, str]],
        tar_command: str,
        is_pull: bool,
        root_mode: bool = False,
        destination: str = "",
    ) -> bool:
        if is_pull or not tar_command or not isinstance(source, Path) or not source.is_dir():
            return False
        if root_mode and destination and not is_probably_writable_android_path(destination):
            return entry_count > 0
        if entry_count < FAST_TAR_MIN_FILES or entry_size <= 0:
            return False

        average_size = entry_size / max(1, entry_count)
        if average_size > FAST_TAR_MAX_AVERAGE_FILE_SIZE:
            return False

        file_sizes = self._file_sizes_from_markers(file_markers)
        if file_sizes:
            large_files = sum(1 for size in file_sizes if size >= FAST_TAR_LARGE_FILE_SIZE)
            if large_files / len(file_sizes) > FAST_TAR_MAX_LARGE_FILE_RATIO:
                return False
        return True

    def _should_disable_adb_compression(
        self,
        source,
        entry_size: int,
        entry_count: int,
        file_markers: list[tuple[int, str]],
        fast_push: bool = False,
        fast_pull: bool = False,
        stream_file: bool = False,
    ) -> bool:
        if fast_push or fast_pull or stream_file:
            return False
        if entry_size >= ADB_TRANSFER_DISABLE_COMPRESSION_SIZE:
            return True
        average_size = entry_size / max(1, entry_count)
        if average_size >= ADB_TRANSFER_DISABLE_COMPRESSION_AVERAGE:
            return True
        compressed_extensions = {
            ".7z",
            ".avi",
            ".flac",
            ".gz",
            ".jpg",
            ".jpeg",
            ".m4a",
            ".mkv",
            ".mov",
            ".mp3",
            ".mp4",
            ".ogg",
            ".png",
            ".rar",
            ".ts",
            ".webm",
            ".zip",
        }
        paths = [label for _size, label in file_markers[:32]]
        if isinstance(source, Path) and source.is_file():
            paths.append(str(source))
        return any(Path(path).suffix.lower() in compressed_extensions for path in paths)

    def _should_use_fast_tar_pull(
        self,
        source,
        entry_size: int,
        entry_count: int,
        tar_command: str,
        is_pull: bool,
        entry_is_dir: bool,
        root_mode: bool = False,
    ) -> bool:
        if not is_pull or not tar_command or not entry_is_dir:
            return False
        if root_mode and not is_probably_writable_android_path(str(source)):
            return bool(str(source).strip())
        if entry_count < FAST_TAR_PULL_MIN_FILES or entry_size <= 0:
            return False
        return bool(str(source).strip())

    def _root_accel_android_path(self, path: str, preserve_root_name: bool = False) -> str:
        normalized = (path or "").replace("\\", "/").strip() or "/"
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        sdcard = "/sdcard"
        emulated = "/storage/emulated/0"
        direct = "/data/media/0"
        if normalized == sdcard:
            return normalized if preserve_root_name else direct
        if normalized.startswith(sdcard + "/"):
            return direct + normalized[len(sdcard) :]
        if normalized == emulated:
            return normalized if preserve_root_name else direct
        if normalized.startswith(emulated + "/"):
            return direct + normalized[len(emulated) :]
        return normalized

    def _file_sizes_from_markers(self, file_markers: list[tuple[int, str]]) -> list[int]:
        sizes: list[int] = []
        previous = 0
        for cumulative, _label in file_markers:
            size = max(0, int(cumulative) - previous)
            previous = int(cumulative)
            sizes.append(size)
        return sizes

    def _extract_percent(self, text: str) -> int | None:
        match = PERCENT_PATTERN.search(text)
        if not match:
            return None
        value = max(0, min(100, int(match.group(1))))
        return value

    def _local_transfer_stats(self, path: Path) -> tuple[int, int]:
        size, count, _markers = self._local_transfer_stats_with_markers(path)
        return size, count

    def _local_transfer_stats_with_markers(self, path: Path) -> tuple[int, int, list[tuple[int, str]]]:
        try:
            if path.is_file():
                size = path.stat().st_size
                return size, 1, [(size, str(path))]
            total = 0
            count = 0
            markers: list[tuple[int, str]] = []
            for child in sorted(path.rglob("*"), key=lambda item: str(item).lower()):
                try:
                    if child.is_file():
                        total += child.stat().st_size
                        count += 1
                        try:
                            label = str(Path(path.name) / child.relative_to(path))
                        except Exception:
                            label = str(child)
                        markers.append((total, label))
                except OSError:
                    continue
            return total, count, markers
        except OSError:
            return 0, 0, []

    def _tar_stream_items(self, source: Path) -> tuple[list[tuple[Path, str]], list[tuple[Path, str, int]]]:
        directories: list[tuple[Path, str]] = []
        files: list[tuple[Path, str, int]] = []
        try:
            if source.is_file():
                return [], [(source, source.name, source.stat().st_size)]
            root_name = source.name
            directories.append((source, root_name))
            for child in sorted(source.rglob("*"), key=lambda item: str(item).lower()):
                try:
                    arcname = str(Path(root_name) / child.relative_to(source)).replace("\\", "/")
                    if child.is_dir():
                        directories.append((child, arcname))
                    elif child.is_file():
                        files.append((child, arcname, child.stat().st_size))
                except OSError:
                    continue
        except OSError:
            return directories, files
        return directories, files

    def _estimate_observed_files(
        self,
        entry_count: int,
        entry_size: int,
        observed_bytes: int,
        file_markers: list[tuple[int, str]],
    ) -> int:
        if entry_count <= 0 or observed_bytes <= 0:
            return 0
        if entry_size > 0 and observed_bytes >= entry_size:
            return entry_count
        marker_estimate = bisect_right([marker[0] for marker in file_markers], observed_bytes) if file_markers else 0
        ratio_estimate = int(entry_count * observed_bytes / entry_size) if entry_size > 0 else 0
        return min(entry_count, max(1, marker_estimate, ratio_estimate))

    def _current_transfer_file_label(self, source, observed_bytes: int, file_markers: list[tuple[int, str]]) -> str:
        if not file_markers:
            return str(source)
        if observed_bytes <= 0:
            return file_markers[0][1]
        sizes = [marker[0] for marker in file_markers]
        index = bisect_right(sizes, observed_bytes)
        if index >= len(file_markers):
            index = len(file_markers) - 1
        return file_markers[index][1]

    def _local_transfer_observation(self, path: Path, started_wall: float) -> tuple[int, int, str]:
        try:
            if path.is_file():
                return path.stat().st_size, 1, str(path)
            total = 0
            count = 0
            newest_file = ""
            newest_mtime = 0.0
            for child in path.rglob("*"):
                try:
                    if not child.is_file():
                        continue
                    stat = child.stat()
                    total += stat.st_size
                    count += 1
                    if stat.st_mtime >= started_wall - 2 and stat.st_mtime >= newest_mtime:
                        newest_mtime = stat.st_mtime
                        newest_file = str(child)
                except OSError:
                    continue
            return total, count, newest_file
        except OSError:
            return 0, 0, ""

    def _android_transfer_stats(self, path: str, use_root: bool = False) -> tuple[int, int]:
        size, count, _is_dir = self._android_transfer_stats_with_kind(path, use_root=use_root)
        return size, count

    def _android_transfer_stats_with_kind(self, path: str, use_root: bool = False) -> tuple[int, int, bool]:
        quoted = shell_quote(path)
        kind_command = f"if [ -d {quoted} ]; then echo dir; else echo file; fi"
        kind_result = self.adb.run_root_shell(kind_command, timeout=15) if use_root else self.adb.run_shell(kind_command, timeout=15)
        kind = (kind_result.stdout or "").strip()
        if kind == "dir":
            count_command = f"find {quoted} -type f 2>/dev/null | wc -l"
            size_command = f"du -s -k {quoted} 2>/dev/null"
            count_result = self.adb.run_root_shell(count_command, timeout=60) if use_root else self.adb.run_shell(count_command, timeout=60)
            size_result = self.adb.run_root_shell(size_command, timeout=60) if use_root else self.adb.run_shell(size_command, timeout=60)
            count = self._first_int(count_result.stdout) or 1
            size_kb = self._first_int(size_result.stdout) or 0
            return size_kb * 1024, count, True
        size_command = f"stat -c %s {quoted} 2>/dev/null"
        size_result = self.adb.run_root_shell(size_command, timeout=15) if use_root else self.adb.run_shell(size_command, timeout=15)
        return self._first_int(size_result.stdout) or 0, 1, False

    def _first_int(self, text: str) -> int | None:
        match = re.search(r"\d+", text or "")
        return int(match.group(0)) if match else None

    def _speed_text(self, bytes_done: int, started: float) -> str:
        elapsed = max(0.1, time.monotonic() - started)
        return f"{self._format_bytes(bytes_done / elapsed)}/s"

    def _format_bytes(self, size: int | float | None) -> str:
        if size is None:
            return "Unknown"
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return str(size)
