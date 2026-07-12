from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.design_system import configure_dialog


class PlatformToolsPickerDialog(QDialog):
    def __init__(self, candidates: list[PlatformToolsInfo], parent=None) -> None:
        super().__init__(parent)
        configure_dialog(self, "Choose Android Platform Tools")
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
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        self.buttons.button(QDialogButtonBox.Ok).setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self.accept())
        self._fill()
        self.table.setFocus()

    def _fill(self) -> None:
        self.table.setRowCount(len(self.candidates))
        for row, info in enumerate(self.candidates):
            values = [info.status, info.folder_text, info.adb_version, info.fastboot_version, info.source]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        if self.candidates:
            self.table.selectRow(0)
        self.table.resizeColumnsToContents()
        for column in range(self.table.columnCount()):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column), 320))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self._selection_changed()

    def _selection_changed(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def selected_info(self) -> PlatformToolsInfo | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.candidates[rows[0].row()]
