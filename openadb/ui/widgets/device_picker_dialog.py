from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem, QVBoxLayout

from openadb.models.device_info import DeviceInfo


class DevicePickerDialog(QDialog):
    def __init__(self, devices: list[DeviceInfo], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose active device")
        self.resize(720, 320)
        self.devices = devices
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 6, self)
        self.table.setHorizontalHeaderLabels(["Serial", "Mode", "Model", "Manufacturer", "Android", "State"])
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
        self.table.setRowCount(len(self.devices))
        for row, device in enumerate(self.devices):
            values = [device.serial, device.mode, device.model, device.manufacturer, device.android_version, device.state]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, item)
        if self.devices:
            self.table.selectRow(0)
        self.table.resizeColumnsToContents()

    def selected_serial(self) -> str:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return ""
        return self.devices[rows[0].row()].serial
