from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.device import DeviceManager
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable, StaleDeviceContext
from openadb.core.operations import OperationConflictError, OperationRegistry, OperationToken
from openadb.core.path_utils import safe_filename
from openadb.models.backup_info import BackupInfo
from openadb.ui.design_system import configure_dialog, configure_page_layout, set_button_role
from openadb.ui.dialogs import show_error_dialog
from openadb.ui.performance import optimize_table
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.workers import Worker, start_worker


@dataclass(frozen=True, slots=True)
class _CapturedBackupSettings:
    config_dir: Path
    backups_folder: Path
    temp_folder: Path
    logs_folder: Path


class BackupsPage(QWidget):
    def __init__(self, backup_manager: BackupManager, adb: ADBClient, device_manager: DeviceManager, parent=None) -> None:
        super().__init__(parent)
        self.backup_manager = backup_manager
        self.adb = adb
        self.device_manager = device_manager
        operations = getattr(device_manager, "operations", None)
        self.operations = operations if isinstance(operations, OperationRegistry) else OperationRegistry()
        self.backups: list[BackupInfo] = []
        self.pool = QThreadPool.globalInstance()
        self._loading = False
        self._action_busy = False
        self._refresh_token: OperationToken | None = None
        self._action_token: OperationToken | None = None
        self._refresh_root: Path | None = None
        self._action_root: Path | None = None
        self._refresh_after_action = False
        layout = QVBoxLayout(self)
        configure_page_layout(layout)
        title = QLabel("Backups")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Restore, inspect, or remove APK backups created by OpenADB.")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        toolbar = QFrame()
        toolbar.setObjectName("toolbarCard")
        buttons = QGridLayout()
        toolbar.setLayout(buttons)
        self.refresh_button = QPushButton("Refresh backups")
        self.restore_button = QPushButton("Restore selected")
        self.delete_button = QPushButton("Delete selected backup")
        self.open_button = QPushButton("Open backup folder")
        self.metadata_button = QPushButton("Show metadata")
        self.install_button = QPushButton("Install APK from backup")
        set_button_role(self.refresh_button, "primary")
        set_button_role(self.delete_button, "danger")
        self.delete_button.setProperty("danger", True)
        action_buttons = [
            self.refresh_button,
            self.restore_button,
            self.delete_button,
            self.open_button,
            self.metadata_button,
            self.install_button,
        ]
        for index, button in enumerate(action_buttons):
            buttons.addWidget(button, index // 2, index % 2)
        for column in range(2):
            buttons.setColumnStretch(column, 1)
        layout.addWidget(toolbar)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["App label", "Package name", "Date", "Device", "Android", "APK count", "Backup path", "Metadata"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        optimize_table(self.table)
        self.empty_state = EmptyState(
            "No backups",
            "No APK backups are available for the current device profile.",
            "Refresh backups",
        )
        self.content = QStackedWidget()
        self.content.addWidget(self.table)
        self.content.addWidget(self.empty_state)
        self.content.setCurrentWidget(self.empty_state)
        layout.addWidget(self.content, 1)

        self.refresh_button.clicked.connect(self.refresh)
        self.restore_button.clicked.connect(self.restore_selected)
        self.install_button.clicked.connect(lambda: self.restore_selected(force_apk=True))
        self.delete_button.clicked.connect(self.delete_selected)
        self.open_button.clicked.connect(self.open_selected)
        self.metadata_button.clicked.connect(self.show_metadata)
        self.empty_state.action_requested.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._update_action_states)
        self._update_action_states()

    def _current_profile_settings(self, root: Path | None = None) -> _CapturedBackupSettings:
        settings = getattr(self.backup_manager, "settings", None) or getattr(
            self.device_manager,
            "settings",
            None,
        )
        configured_root = getattr(settings, "backups_folder", None)
        if root is None:
            if isinstance(configured_root, (str, Path)):
                root = Path(configured_root)
            else:
                root = Path(getattr(self.backup_manager, "root", Path.cwd() / "backups"))
        profile_path = getattr(settings, "config_dir", root.parent)
        if not isinstance(profile_path, (str, Path)):
            profile_path = root.parent

        def configured_path(name: str, fallback: Path) -> Path:
            value = getattr(settings, name, None)
            return Path(value) if isinstance(value, (str, Path)) else fallback

        profile_path = Path(profile_path)
        return _CapturedBackupSettings(
            config_dir=profile_path,
            backups_folder=Path(root),
            temp_folder=configured_path("temp_folder", profile_path / "temp"),
            logs_folder=configured_path("logs_folder", profile_path / "logs"),
        )

    def _current_backup_root(self) -> Path:
        return self._current_profile_settings().backups_folder

    @staticmethod
    def _settings_for_context(context: DeviceContext) -> _CapturedBackupSettings:
        return _CapturedBackupSettings(
            config_dir=context.profile_path,
            backups_folder=context.backups_path,
            temp_folder=context.temp_path,
            logs_folder=context.logs_path,
        )

    @staticmethod
    def _path_identity(path: Path) -> str:
        try:
            return str(path.resolve(strict=False)).casefold()
        except OSError:
            return str(path.absolute()).casefold()

    @staticmethod
    def _backup_belongs_to_root(backup: BackupInfo, root: Path) -> bool:
        try:
            backup.path.resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except (OSError, ValueError):
            return False

    def _profile_is_current(self, root: Path) -> bool:
        return self._path_identity(self._current_backup_root()) == self._path_identity(root)

    def _manager_for_settings(self, settings: _CapturedBackupSettings):
        if isinstance(self.backup_manager, BackupManager):
            return BackupManager(settings)  # type: ignore[arg-type]
        return self.backup_manager

    def _require_restore_context(self) -> DeviceContext:
        require_context = getattr(self.device_manager, "require_context", None)
        if callable(require_context):
            context = require_context({"ADB", "Recovery"})
            if isinstance(context, DeviceContext):
                return context
        active = getattr(self.device_manager, "active", None)
        raw_serial = getattr(active, "serial", "")
        raw_mode = getattr(active, "mode", "No device")
        serial = raw_serial if isinstance(raw_serial, str) else ""
        mode = raw_mode if isinstance(raw_mode, str) else "No device"
        if not serial and not isinstance(self.device_manager, DeviceManager):
            # Legacy tests/integrations historically supplied only mutable ADB.
            # Production always reaches the strict require_context path above.
            serial = str(getattr(self.adb, "serial", "") or "legacy-device")
            mode = "ADB"
        if not serial or mode not in {"ADB", "Recovery"}:
            raise DeviceContextUnavailable("An authorized ADB or Recovery device is required")
        paths = self._current_profile_settings()
        return DeviceContext(
            serial=serial,
            mode=mode,
            transport_id=str(getattr(active, "transport_id", "") or ""),
            profile_key=safe_filename(serial),
            profile_kind=str(getattr(paths, "profile_kind", "") or "Phone"),
            profile_path=paths.config_dir,
            backups_path=paths.backups_folder,
            temp_path=paths.temp_folder,
            logs_path=paths.logs_folder,
            generation=int(getattr(self.device_manager, "current_generation", 0) or 0),
        )

    def _bound_adb_for_context(self, context: DeviceContext):
        for_context = getattr(self.adb, "for_context", None)
        if callable(for_context):
            return for_context(context)
        if str(getattr(self.adb, "serial", "") or "") == context.serial:
            return self.adb
        raise DeviceContextUnavailable("ADB client cannot be safely bound to the active device")

    def _is_context_current(self, context: DeviceContext) -> bool:
        is_current = getattr(self.device_manager, "is_context_current", None)
        if callable(is_current):
            return bool(is_current(context))
        active = getattr(self.device_manager, "active", None)
        return (
            str(getattr(active, "serial", "") or "") == context.serial
            and str(getattr(active, "mode", "") or "") == context.mode
            and self._profile_is_current(context.backups_path)
        )

    def _require_current_context(self, context: DeviceContext) -> None:
        require_current = getattr(self.device_manager, "require_current", None)
        if callable(require_current):
            require_current(context)
            return
        if not self._is_context_current(context):
            raise StaleDeviceContext("The active device or profile changed")

    def _can_apply_device_operation(self, token: OperationToken, context: DeviceContext) -> bool:
        return (
            self.operations.contains(token)
            and not token.cancelled
            and self._is_context_current(context)
        )

    def _can_apply_local_operation(self, token: OperationToken, root: Path) -> bool:
        return self.operations.contains(token) and not token.cancelled and self._profile_is_current(root)

    def reset_for_device_profile(self) -> None:
        self.operations.cancel_owner("backups.scan", "backup profile changed")
        self.operations.cancel_owner("backups.action", "backup profile changed")
        self._refresh_token = None
        self._action_token = None
        self._action_root = None
        self._loading = False
        self._action_busy = False
        self._refresh_after_action = False
        self.backups = []
        self.table.clearSelection()
        self.table.setRowCount(0)
        self.empty_state.set_content(
            "No backups",
            "No APK backups are available for the current device profile.",
            "Refresh backups",
        )
        self.content.setCurrentWidget(self.empty_state)
        self._update_action_states()

    def refresh(self) -> None:
        root = self._current_backup_root()
        if (
            self._action_token is not None
            and self._action_root is not None
            and self._path_identity(self._action_root) != self._path_identity(root)
        ):
            self._action_token.cancel("backup profile changed")
            self._action_token = None
            self._action_root = None
            self._action_busy = False
            self._refresh_after_action = False
        if self._loading:
            token = self._refresh_token
            if token is not None and not token.cancelled and self._refresh_root == root:
                return
            if token is not None:
                token.cancel("backup profile changed")
            self._refresh_token = None
            self._loading = False
        if self._refresh_root is not None and self._refresh_root != root:
            self.backups = []
            self.table.clearSelection()
            self.table.setRowCount(0)
        settings = self._current_profile_settings(root)
        manager = self._manager_for_settings(settings)
        try:
            token = self.operations.register(
                "backups.scan",
                conflict_group=f"backups-scan:{self._path_identity(root)}",
            )
        except (OperationConflictError, RuntimeError):
            return
        self._refresh_token = token
        self._refresh_root = root
        self._loading = True
        if not self.backups:
            self.empty_state.set_content("Loading backups", "OpenADB is scanning the active backup folder.")
            self.content.setCurrentWidget(self.empty_state)
        self._update_action_states()
        worker = Worker(lambda: manager.scan_backups(cancel_event=token.cancel_event))
        worker.signals.result.connect(
            lambda backups: self._backups_loaded_for_operation(token, root, backups)
        )
        worker.signals.error.connect(
            lambda message, trace: self._backups_load_failed_for_operation(
                token,
                root,
                settings.logs_folder,
                message,
                trace,
            )
        )
        worker.signals.finished.connect(lambda: self._refresh_finished(token))
        if not start_worker(
            self,
            self.pool,
            worker,
            operation_registry=self.operations,
            operation_token=token,
        ):
            self._refresh_finished(token)

    def _refresh_finished(self, token: OperationToken) -> None:
        self.operations.finish(token)
        if self._refresh_token is not token:
            return
        self._refresh_token = None
        self._loading = False
        self._update_action_states()

    def _backups_loaded_for_operation(
        self,
        token: OperationToken,
        root: Path,
        backups: list[BackupInfo],
    ) -> None:
        if self._can_apply_local_operation(token, root):
            self._backups_loaded(backups)

    def _backups_loaded(self, backups: list[BackupInfo]) -> None:
        self.table.setUpdatesEnabled(False)
        self.backups = backups
        self.table.setRowCount(len(self.backups))
        for row, backup in enumerate(self.backups):
            values = [
                backup.display_name,
                backup.package_name,
                backup.backup_date,
                backup.device_model or backup.device_serial,
                backup.android_version,
                str(backup.apk_count),
                str(backup.path),
                "Yes" if backup.metadata_exists else "No",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, item)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self.table.resizeColumnToContents(2)
        self.table.resizeColumnToContents(5)
        self.table.resizeColumnToContents(7)
        self.table.setUpdatesEnabled(True)
        if self.backups:
            self.content.setCurrentWidget(self.table)
        else:
            self.empty_state.set_content(
                "No backups",
                "No APK backups are available for the current device profile.",
                "Refresh backups",
            )
            self.content.setCurrentWidget(self.empty_state)
        self._update_action_states()

    def _backups_load_failed(self, message: str, _trace: str) -> None:
        self.empty_state.set_content(
            "Backups could not be loaded",
            "Review the error, then try scanning the backup folder again.",
            "Retry",
            kind="warning",
        )
        self.content.setCurrentWidget(self.empty_state)
        show_error_dialog(self, "Backups could not be loaded", message, self.backup_manager.settings.logs_folder)

    def _backups_load_failed_for_operation(
        self,
        token: OperationToken,
        root: Path,
        logs_path: Path,
        message: str,
        _trace: str,
    ) -> None:
        if not self._can_apply_local_operation(token, root):
            return
        self.empty_state.set_content(
            "Backups could not be loaded",
            "Review the error, then try scanning the backup folder again.",
            "Retry",
            kind="warning",
        )
        self.content.setCurrentWidget(self.empty_state)
        show_error_dialog(self, "Backups could not be loaded", message, logs_path)

    def _update_action_states(self) -> None:
        selected = self.selected_backup() is not None
        mode = getattr(getattr(self.device_manager, "active", None), "mode", None)
        device_ready = not isinstance(mode, str) or mode in {"ADB", "Recovery"}
        idle = not self._loading and not self._action_busy
        self.refresh_button.setEnabled(not self._loading and not self._action_busy)
        self.restore_button.setEnabled(selected and idle and device_ready)
        self.install_button.setEnabled(selected and idle and device_ready)
        for button in [self.delete_button, self.metadata_button]:
            button.setEnabled(selected and idle)

    def selected_backup(self) -> BackupInfo | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.backups[rows[0].row()]

    def restore_selected(self, force_apk: bool = False) -> None:
        backup = self.selected_backup()
        if not backup:
            QMessageBox.information(self, "Restore backup", "Select a backup first.")
            return
        prefer_existing = False
        if not force_apk and backup.uninstall_method and "--user 0" in backup.uninstall_method:
            box = QMessageBox(self)
            box.setWindowTitle("Restore system app")
            box.setText("This backup was created before removing a system app for user 0.")
            install_existing = box.addButton("Use install-existing", QMessageBox.AcceptRole)
            install_apk = box.addButton("Install APK", QMessageBox.DestructiveRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is install_existing:
                prefer_existing = True
            elif box.clickedButton() is install_apk:
                prefer_existing = False
            else:
                return
        try:
            context = self._require_restore_context()
            if not self._backup_belongs_to_root(backup, context.backups_path):
                raise DeviceContextUnavailable(
                    "The selected backup belongs to another device profile. Refresh backups before restoring it."
                )
            bound_adb = self._bound_adb_for_context(context)
            manager = self._manager_for_settings(self._settings_for_context(context))
            token = self.operations.register(
                "backups.action",
                device_context=context,
                conflict_group=f"device-package-workflow:{context.serial}",
                conflict_groups=(f"device-exclusive:{context.serial}",),
            )
        except (DeviceContextUnavailable, OperationConflictError, OSError, RuntimeError) as exc:
            QMessageBox.information(self, "Restore backup", str(exc))
            return
        self._action_token = token
        self._action_root = context.backups_path
        self._action_busy = True
        self._refresh_after_action = False
        self._update_action_states()

        def restore_backup():
            if token.cancelled:
                raise StaleDeviceContext("Backup restore was cancelled before it started")
            self._require_current_context(context)
            return manager.restore_backup(
                backup,
                bound_adb,
                prefer_existing,
                cancel_event=token.cancel_event,
            )

        worker = Worker(restore_backup)
        worker.signals.result.connect(
            lambda result: self._restore_finished_result(token, context, result.status)
        )
        worker.signals.error.connect(
            lambda message, _trace: self._device_action_failed(
                token,
                context,
                "Backup could not be restored",
                message,
            )
        )
        self._start_action_worker(token, worker, context=context)

    def delete_selected(self) -> None:
        backup = self.selected_backup()
        if not backup:
            return
        answer = QMessageBox.question(self, "Delete backup", f"Delete backup folder?\n{backup.path}")
        if answer != QMessageBox.Yes:
            return
        root = self._current_backup_root()
        settings = self._current_profile_settings(root)
        manager = self._manager_for_settings(settings)
        try:
            token = self.operations.register(
                "backups.action",
                conflict_group=f"backup-write:{self._path_identity(root)}",
            )
        except (OperationConflictError, RuntimeError) as exc:
            QMessageBox.information(self, "Delete backup", str(exc))
            return
        self._action_token = token
        self._action_root = root
        self._action_busy = True
        self._refresh_after_action = False
        self._update_action_states()
        def delete_backup():
            if token.cancelled:
                return None
            return manager.delete_backup(backup)

        worker = Worker(delete_backup)
        worker.signals.result.connect(lambda _result: self._delete_finished_result(token, root))
        worker.signals.error.connect(
            lambda message, _trace: self._local_action_failed(token, root, "Delete backup", message)
        )
        self._start_action_worker(token, worker, root=root)

    def _start_action_worker(
        self,
        token: OperationToken,
        worker: Worker,
        *,
        context: DeviceContext | None = None,
        root: Path | None = None,
    ) -> None:
        worker.signals.finished.connect(
            lambda: self._action_finished(token, context=context, root=root)
        )
        if not start_worker(
            self,
            self.pool,
            worker,
            operation_registry=self.operations,
            operation_token=token,
        ):
            self._action_finished(token, context=context, root=root)

    def _restore_finished_result(
        self,
        token: OperationToken,
        context: DeviceContext,
        status: str,
    ) -> None:
        if self._can_apply_device_operation(token, context):
            QMessageBox.information(self, "Restore backup", status)

    def _delete_finished_result(self, token: OperationToken, root: Path) -> None:
        if self._can_apply_local_operation(token, root):
            self._refresh_after_action = True

    def _device_action_failed(
        self,
        token: OperationToken,
        context: DeviceContext,
        title: str,
        message: str,
    ) -> None:
        if self._can_apply_device_operation(token, context):
            show_error_dialog(self, title, message, context.logs_path)

    def _local_action_failed(
        self,
        token: OperationToken,
        root: Path,
        title: str,
        message: str,
    ) -> None:
        if self._can_apply_local_operation(token, root):
            QMessageBox.warning(self, title, message)

    def _action_finished(
        self,
        token: OperationToken,
        *,
        context: DeviceContext | None = None,
        root: Path | None = None,
    ) -> None:
        self.operations.finish(token)
        if self._action_token is not token:
            return
        refresh = self._refresh_after_action
        self._refresh_after_action = False
        self._action_token = None
        self._action_root = None
        self._action_busy = False
        self._update_action_states()
        current = (
            self._is_context_current(context)
            if context is not None
            else root is not None and self._profile_is_current(root)
        )
        if refresh and current and not token.cancelled:
            self.refresh()

    def open_selected(self) -> None:
        backup = self.selected_backup()
        path = backup.path if backup else self.backup_manager.root
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_metadata(self) -> None:
        backup = self.selected_backup()
        if not backup:
            return
        metadata_path = backup.path / "metadata.json"
        if not metadata_path.exists():
            QMessageBox.information(self, "Metadata", "metadata.json does not exist for this backup.")
            return
        try:
            text = json.dumps(json.loads(metadata_path.read_text(encoding="utf-8")), indent=2, ensure_ascii=False)
        except Exception:
            text = metadata_path.read_text(encoding="utf-8", errors="replace")
        dialog = QDialog(self)
        dialog.setWindowTitle("Backup metadata")
        configure_dialog(dialog, "Backup metadata")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        edit = QTextEdit()
        edit.setReadOnly(True)
        edit.setPlainText(text)
        layout.addWidget(edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
