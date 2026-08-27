from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import Qt, QThreadPool, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHeaderView,
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
from openadb.core.operations import (
    OperationConflictError,
    OperationRegistry,
    OperationToken,
)
from openadb.core.privilege import PrivilegeBackend
from openadb.models.backup_info import BackupInfo
from openadb.ui.design_system import (
    configure_dialog,
    configure_page_layout,
    set_button_role,
)
from openadb.ui.dialogs import show_error_dialog
from openadb.ui.performance import optimize_table
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.workers import Worker, start_worker


class BackupsPage(QWidget):
    TABLE_HEADERS: ClassVar[tuple[str, ...]] = (
        "App label",
        "Package name",
        "Date",
        "Device",
        "Android",
        "APK count",
        "Backup path",
        "Metadata",
    )
    COLUMN_MIN_WIDTHS: ClassVar[dict[int, int]] = {
        0: 160,
        1: 180,
        2: 110,
        3: 120,
        4: 90,
        5: 150,
        6: 200,
        7: 140,
    }
    COLUMN_MAX_WIDTHS: ClassVar[dict[int, int]] = {
        0: 300,
        1: 360,
        2: 180,
        3: 260,
        4: 180,
        5: 180,
        6: 420,
        7: 180,
    }

    def __init__(
        self,
        backup_manager: BackupManager,
        adb: ADBClient,
        device_manager: DeviceManager,
        parent=None,
        *,
        privilege_manager=None,
    ) -> None:
        super().__init__(parent)
        self.backup_manager = backup_manager
        self.adb = adb
        self.device_manager = device_manager
        self.privilege_manager = privilege_manager
        self.coordinator = BackupOperationCoordinator(
            backup_manager,
            adb,
            device_manager,
            privilege_manager=privilege_manager,
        )
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
        self.table.setHorizontalHeaderLabels(self.TABLE_HEADERS)
        for column, header_text in enumerate(self.TABLE_HEADERS):
            header_item = self.table.horizontalHeaderItem(column)
            if header_item is not None:
                header_item.setToolTip(header_text)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        optimize_table(self.table)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(64)
        header.setSectionResizeMode(QHeaderView.Interactive)
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

    def _register_device_action(
        self,
        context: DeviceContext,
        *,
        privilege_lease=None,
    ) -> OperationToken:
        token = self.operations.register(
            "backups.action",
            device_context=context,
            conflict_group=f"device-package-workflow:{context.serial}",
            conflict_groups=(
                f"device-exclusive:{context.serial}",
                f"acbridge-maintenance:{context.serial}",
            ),
        )
        if not self.coordinator.is_context_current(context):
            token.cancel("device context changed during backup operation registration")
            self.operations.finish(token)
            raise StaleDeviceContext(
                "The active device changed before the backup operation could start"
            )
        token.privilege_lease = privilege_lease
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
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        self._resize_backup_columns()
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

    def _resize_backup_columns(self) -> None:
        metrics = self.table.fontMetrics()
        for column, header_text in enumerate(self.TABLE_HEADERS):
            content_width = metrics.horizontalAdvance(header_text) + 34
            for row in range(self.table.rowCount()):
                item = self.table.item(row, column)
                if item is not None:
                    content_width = max(
                        content_width,
                        metrics.horizontalAdvance(item.text()) + 28,
                    )
            minimum = self.COLUMN_MIN_WIDTHS[column]
            maximum = self.COLUMN_MAX_WIDTHS[column]
            self.table.setColumnWidth(
                column,
                min(max(content_width, minimum), maximum),
            )

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
        shell_backend_ready = self._privilege_shell_backend_available()
        idle = not self._loading and not self._action_busy
        self.refresh_button.setEnabled(not self._loading and not self._action_busy)
        self.restore_button.setEnabled(
            selected and idle and device_ready and shell_backend_ready
        )
        self.install_button.setEnabled(selected and idle and device_ready)
        for button in [self.delete_button, self.metadata_button]:
            button.setEnabled(selected and idle)

    def update_privilege_status(self, _status=None) -> None:
        """Apply a global access result to device-side restore availability."""

        self._update_action_states()

    def _privilege_shell_backend_available(self) -> bool:
        """Gate install-existing without blocking mode-independent APK installs."""

        manager = self.privilege_manager
        if manager is None:
            # Standalone/test embeddings do not have a global privilege backend.
            # Device-mode readiness is still enforced by ``_update_action_states``.
            return True
        raw_mode = getattr(
            getattr(self.device_manager, "active", None), "mode", None
        )
        if not isinstance(raw_mode, str):
            return True
        mode = raw_mode or "No device"
        if mode not in {"ADB", "Recovery"}:
            return False
        backend = PrivilegeBackend.normalize(manager.selected_backend)
        if backend is not PrivilegeBackend.SHIZUKU:
            return True
        if mode != "ADB":
            return False
        cached_status = getattr(manager, "cached_status", None)
        if not callable(cached_status):
            return True
        status = cached_status()
        return bool(
            status is not None
            and status.backend is PrivilegeBackend.SHIZUKU
            and status.available
        )

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
        if prefer_existing and not self._privilege_shell_backend_available():
            QMessageBox.information(
                self,
                "Restore backup",
                (
                    "The selected access mode cannot run install-existing right now. "
                    "Verify Shizuku access in ADB mode, or use Install APK."
                ),
            )
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
            token = self._register_device_action(
                context,
                privilege_lease=(
                    operation.privilege_lease if prefer_existing else None
                ),
            )
        except (DeviceContextUnavailable, OperationConflictError, OSError, RuntimeError) as exc:
            self._show_details_message(
                "Restore backup",
                "The restore could not start. Open Details for complete information.",
                str(exc),
                icon=QMessageBox.Warning,
            )
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
        answer = self._delete_confirmation_box(backup).exec()
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
            self._show_details_message(
                "Delete backup",
                "The backup could not be deleted. Open Details for complete information.",
                str(exc),
                icon=QMessageBox.Warning,
            )
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

    def _delete_confirmation_box(self, backup: BackupInfo) -> QMessageBox:
        box = QMessageBox(self)
        configure_dialog(box, "Delete backup")
        box.setWindowTitle("Delete backup")
        box.setIcon(QMessageBox.Warning)
        box.setText("Permanently delete the selected backup folder?")
        box.setInformativeText(
            "This cannot be undone. Open Details to review the complete folder path."
        )
        box.setDetailedText(str(backup.path))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        return box

    def _details_message_box(
        self,
        title: str,
        summary: str,
        details: str,
        *,
        icon=QMessageBox.Information,
    ) -> QMessageBox:
        """Build a compact message whose complete variable text stays scrollable."""

        box = QMessageBox(self)
        configure_dialog(box, title)
        box.setWindowTitle(title)
        box.setIcon(icon)
        box.setText(summary)
        box.setDetailedText(str(details or "No additional information.").strip())
        box.setStandardButtons(QMessageBox.Ok)
        box.setDefaultButton(QMessageBox.Ok)
        return box

    def _show_details_message(
        self,
        title: str,
        summary: str,
        details: str,
        *,
        icon=QMessageBox.Information,
    ) -> None:
        self._details_message_box(title, summary, details, icon=icon).exec()

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
                self._show_details_message(
                    "Backup operation could not start",
                    "The background backup operation could not start. Open Details for complete information.",
                    str(exc) or "The background worker could not be started.",
                    icon=QMessageBox.Warning,
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
            self._show_details_message(
                "Restore backup",
                "The restore operation finished. Open Details to review the result.",
                status,
            )

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
            self._show_details_message(
                title,
                "The backup operation failed. Open Details for complete information.",
                message,
                icon=QMessageBox.Warning,
            )

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
            self._show_details_message(
                "Open backup folder",
                "The backup folder could not be opened. Open Details for complete information.",
                str(exc),
                icon=QMessageBox.Warning,
            )
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
            self._show_details_message(
                "Metadata",
                "The backup metadata could not be opened. Open Details for complete information.",
                str(exc),
                icon=QMessageBox.Warning,
            )
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
