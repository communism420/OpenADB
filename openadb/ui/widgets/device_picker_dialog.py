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

from openadb.models.device_info import DeviceInfo
from openadb.ui.design_system import configure_dialog


class DevicePickerDialog(QDialog):
    def __init__(self, devices: list[DeviceInfo], parent=None, active_serial: str = "") -> None:
        super().__init__(parent)
        configure_dialog(self, "Choose active device")
        self.setWindowTitle("Choose active device")
        self.resize(720, 320)
        self.devices = devices
        self.active_serial = str(active_serial or "")
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 7, self)
        self.table.setHorizontalHeaderLabels(
            ["Active", "Model", "Mode", "Serial", "Manufacturer", "Android", "State"]
        )
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
        self.table.setRowCount(len(self.devices))
        selected_row = -1
        for row, device in enumerate(self.devices):
            is_active = bool(self.active_serial and device.serial == self.active_serial)
            values = [
                "Current" if is_active else "",
                device.model or device.product or "Unknown device",
                device.mode,
                device.serial,
                device.manufacturer,
                device.android_version,
                device.state,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
            if is_active:
                selected_row = row
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        else:
            self.table.clearSelection()
        self.table.resizeColumnsToContents()
        for column in range(self.table.columnCount()):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column), 240))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self._selection_changed()

    def _selection_changed(self) -> None:
        ok_button = self.buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def selected_serial(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return ""
        return self.devices[rows[0].row()].serial
