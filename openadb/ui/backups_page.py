from __future__ import annotations

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
from openadb.core.backup_operation_coordinator import (
    BackupOperationCoordinator,
    BackupProfileContext,
)
from openadb.core.device import DeviceManager
from openadb.core.device_context import (
    DeviceContext,
    DeviceContextUnavailable,
    StaleDeviceContext,
)
from openadb.core.operations import OperationConflictError, OperationRegistry, OperationToken
from openadb.models.backup_info import BackupInfo
from openadb.ui.design_system import configure_dialog, configure_page_layout, set_button_role
from openadb.ui.dialogs import show_error_dialog
from openadb.ui.performance import optimize_table
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.workers import Worker, start_worker


class BackupsPage(QWidget):
    def __init__(self, backup_manager: BackupManager, adb: ADBClient, device_manager: DeviceManager, parent=None) -> None:
        super().__init__(parent)
        self.backup_manager = backup_manager
        self.adb = adb
        self.device_manager = device_manager
        self.coordinator = BackupOperationCoordinator(backup_manager, adb, device_manager)
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

    def _manager_for_settings(self, settings: BackupProfileContext):
        """Compatibility seam for integrations that provide a custom manager."""

        return self.coordinator.manager_for_profile(settings)

    def _can_apply_device_operation(self, token: OperationToken, context: DeviceContext) -> bool:
        return (
            self.operations.contains(token)
            and not token.cancelled
            and self.coordinator.is_context_current(context)
        )

    def _can_apply_local_operation(self, token: OperationToken, root: Path) -> bool:
        profile = self.coordinator.capture_local_profile(root)
        return (
            self.operations.contains(token)
            and not token.cancelled
            and self.coordinator.is_profile_current(profile)
        )

    def _register_device_action(self, context: DeviceContext) -> OperationToken:
        token = self.operations.register(
            "backups.action",
            device_context=context,
            conflict_group=f"device-package-workflow:{context.serial}",
            conflict_groups=(f"device-exclusive:{context.serial}",),
        )
        if not self.coordinator.is_context_current(context):
            token.cancel("device context changed during backup operation registration")
            self.operations.finish(token)
            raise StaleDeviceContext(
                "The active device changed before the backup operation could start"
            )
        return token

    def _register_local_operation(
        self,
        owner: str,
        profile: BackupProfileContext,
        conflict_group: str,
    ) -> OperationToken:
        token = self.operations.register(owner, conflict_group=conflict_group)
        if not self.coordinator.is_profile_current(profile):
            token.cancel("backup profile changed during operation registration")
            self.operations.finish(token)
            raise StaleDeviceContext(
                "The backup profile changed before the local operation could start"
            )
        return token

    def reset_for_device_profile(self) -> None:
        self.operations.cancel_owner("backups.scan", "backup profile changed")
        self.operations.cancel_owner("backups.action", "backup profile changed")
        self._refresh_token = None
        self._action_token = None
        self._refresh_root = None
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
        profile = self.coordinator.capture_local_profile()
        root = profile.backups_folder
        if (
            self._action_token is not None
            and self._action_root is not None
            and self.coordinator.path_identity(self._action_root)
            != self.coordinator.path_identity(root)
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
        try:
            token = self._register_local_operation(
                "backups.scan",
                profile,
                f"backups-scan:{self.coordinator.path_identity(root)}",
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
        worker = Worker(
            lambda: self.coordinator.scan_backups(
                profile,
                cancel_event=token.cancel_event,
                manager_factory=self._manager_for_settings,
            )
        )
        worker.signals.result.connect(
            lambda backups: self._backups_loaded_for_operation(token, root, backups)
        )
        worker.signals.error.connect(
            lambda message, trace: self._backups_load_failed_for_operation(
                token,
                root,
                profile.logs_folder,
                message,
                trace,
            )
        )
        worker.signals.finished.connect(lambda: self._refresh_finished(token))
        try:
            started = start_worker(
                self,
                self.pool,
                worker,
                operation_registry=self.operations,
                operation_token=token,
            )
        except Exception as exc:
            self._refresh_finished(token)
            self.empty_state.set_content(
                "Backup scan could not start",
                str(exc) or "The background worker could not be started.",
                "Retry",
                kind="warning",
            )
            self.content.setCurrentWidget(self.empty_state)
            return
        if not started:
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
            operation = self.coordinator.capture_device_operation(
                manager_factory=self._manager_for_settings
            )
            context = operation.context
            if not self.coordinator.backup_belongs_to_profile(backup, operation.profile):
                raise DeviceContextUnavailable(
                    "The selected backup belongs to another device profile. Refresh backups before restoring it."
                )
            token = self._register_device_action(context)
        except (DeviceContextUnavailable, OperationConflictError, OSError, RuntimeError) as exc:
            QMessageBox.information(self, "Restore backup", str(exc))
            return
        self._action_token = token
        self._action_root = context.backups_path
        self._action_busy = True
        self._refresh_after_action = False
        self._update_action_states()

        def restore_backup():
            return self.coordinator.restore_backup(
                operation,
                backup,
                prefer_install_existing=prefer_existing,
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
        profile = self.coordinator.capture_local_profile()
        root = profile.backups_folder
        try:
            token = self._register_local_operation(
                "backups.action",
                profile,
                f"backup-write:{self.coordinator.path_identity(root)}",
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
            return self.coordinator.delete_local_backup(
                profile,
                backup,
                cancel_event=token.cancel_event,
                manager_factory=self._manager_for_settings,
            )

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
        try:
            started = start_worker(
                self,
                self.pool,
                worker,
                operation_registry=self.operations,
                operation_token=token,
            )
        except Exception as exc:
            self._action_finished(token, context=context, root=root)
            if not getattr(self, "_workers_shutting_down", False):
                QMessageBox.warning(
                    self,
                    "Backup operation could not start",
                    str(exc) or "The background worker could not be started.",
                )
            return
        if not started:
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
            self.coordinator.is_context_current(context)
            if context is not None
            else root is not None
            and self.coordinator.is_profile_current(
                self.coordinator.capture_local_profile(root)
            )
        )
        if refresh and current and not token.cancelled:
            self.refresh()

    def open_selected(self) -> None:
        backup = self.selected_backup()
        profile = self.coordinator.capture_local_profile()
        try:
            path = self.coordinator.folder_to_open(profile, backup)
        except DeviceContextUnavailable as exc:
            QMessageBox.warning(self, "Open backup folder", str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_metadata(self) -> None:
        backup = self.selected_backup()
        if not backup:
            return
        profile = self.coordinator.capture_local_profile()
        metadata_path = backup.path / "metadata.json"
        if not metadata_path.exists():
            QMessageBox.information(self, "Metadata", "metadata.json does not exist for this backup.")
            return
        try:
            text = self.coordinator.metadata_text(profile, backup)
        except (DeviceContextUnavailable, OSError) as exc:
            QMessageBox.warning(self, "Metadata", str(exc))
            return
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
