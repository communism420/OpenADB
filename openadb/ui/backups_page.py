from __future__ import annotations

import json

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
        self.backups: list[BackupInfo] = []
        self.pool = QThreadPool.globalInstance()
        self._loading = False
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

    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        if not self.backups:
            self.empty_state.set_content("Loading backups", "OpenADB is scanning the active backup folder.")
            self.content.setCurrentWidget(self.empty_state)
        self._update_action_states()
        worker = Worker(self.backup_manager.scan_backups)
        worker.signals.result.connect(self._backups_loaded)
        worker.signals.error.connect(self._backups_load_failed)
        worker.signals.finished.connect(self._refresh_finished)
        start_worker(self, self.pool, worker)

    def _refresh_finished(self) -> None:
        self._loading = False
        self._update_action_states()

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

    def _update_action_states(self) -> None:
        selected = self.selected_backup() is not None
        self.refresh_button.setEnabled(not self._loading)
        for button in [self.restore_button, self.delete_button, self.metadata_button, self.install_button]:
            button.setEnabled(selected and not self._loading)

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
        worker = Worker(lambda: self.backup_manager.restore_backup(backup, self.adb, prefer_existing))
        worker.signals.result.connect(lambda result: QMessageBox.information(self, "Restore backup", result.status))
        worker.signals.error.connect(
            lambda message, _trace: show_error_dialog(
                self, "Backup could not be restored", message, self.backup_manager.settings.logs_folder
            )
        )
        start_worker(self, self.pool, worker)

    def delete_selected(self) -> None:
        backup = self.selected_backup()
        if not backup:
            return
        answer = QMessageBox.question(self, "Delete backup", f"Delete backup folder?\n{backup.path}")
        if answer != QMessageBox.Yes:
            return
        worker = Worker(lambda: self.backup_manager.delete_backup(backup))
        worker.signals.result.connect(lambda _result: self.refresh())
        worker.signals.error.connect(lambda message, _trace: QMessageBox.warning(self, "Delete backup", message))
        start_worker(self, self.pool, worker)

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
