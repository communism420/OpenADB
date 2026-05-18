from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QVBoxLayout

from openadb.models.platform_tools_info import PlatformToolsInfo


class PlatformToolsPickerDialog(QDialog):
    def __init__(self, candidates: list[PlatformToolsInfo], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose Android Platform Tools")
        self.resize(900, 360)
        self.candidates = candidates
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["Status", "Folder", "ADB version", "Fastboot version", "Source"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._fill()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.candidates))
        for row, info in enumerate(self.candidates):
            values = [info.status, info.folder_text, info.adb_version, info.fastboot_version, info.source]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, item)
        if self.candidates:
            self.table.selectRow(0)
        self.table.resizeColumnsToContents()

    def selected_info(self) -> PlatformToolsInfo | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.candidates[rows[0].row()]
