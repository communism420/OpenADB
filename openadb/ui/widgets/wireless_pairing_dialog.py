from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from openadb.ui.design_system import configure_dialog, fit_dialog_to_available_screen


class WirelessPairingDialog(QDialog):
    """Collect pairing-only values without crowding the connection form."""

    def __init__(
        self,
        host: str = "",
        pairing_port: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        configure_dialog(self, "Pair Wireless ADB device")
        self.setWindowTitle("Pair Wireless ADB device")
        fit_dialog_to_available_screen(
            self,
            preferred=QSize(500, 300),
            minimum=QSize(360, 260),
        )

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Enter the pairing port and code currently shown in Android Wireless debugging. "
            "The pairing code is used once and is not saved."
        )
        hint.setObjectName("hintLabel")
        hint.setTextFormat(Qt.PlainText)
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hint.setMinimumWidth(0)
        hint.setAccessibleDescription(hint.text())
        layout.addWidget(hint)

        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.host = QLineEdit(host)
        self.host.setPlaceholderText("Device IP address or hostname")
        self.pairing_port = QLineEdit(str(pairing_port) if pairing_port else "")
        self.pairing_port.setPlaceholderText("Port shown by Android")
        self.pairing_port.setValidator(QIntValidator(1, 65535, self.pairing_port))
        self.pairing_code = QLineEdit()
        self.pairing_code.setPlaceholderText("Pairing code")
        self.pairing_code.setMaxLength(32)
        for field, accessible_name in (
            (self.host, "Wireless ADB device IP address or hostname"),
            (self.pairing_port, "Wireless ADB pairing port"),
            (self.pairing_code, "Wireless ADB pairing code"),
        ):
            field.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            field.setMinimumWidth(0)
            field.setAccessibleName(accessible_name)
            field.setAccessibleDescription(field.placeholderText())
            field.setToolTip(field.placeholderText())
        form.addRow("Device IP / host", self.host)
        form.addRow("Pairing port", self.pairing_port)
        form.addRow("Pairing code", self.pairing_code)
        layout.addLayout(form)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setDefault(True)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.host.setFocus()

    def values(self) -> tuple[str, int, str]:
        port_text = self.pairing_port.text().strip()
        port = int(port_text) if port_text.isdigit() else 0
        return self.host.text().strip(), port, self.pairing_code.text().strip()

    def accept(self) -> None:
        host, port, code = self.values()
        if not host:
            QMessageBox.warning(self, "Wireless ADB pair", "Enter the device IP address or hostname.")
            return
        if not code:
            QMessageBox.warning(self, "Wireless ADB pair", "Enter the pairing code shown on the device.")
            return
        if not 1 <= port <= 65535:
            QMessageBox.warning(self, "Wireless ADB pair", "Enter a pairing port from 1 to 65535.")
            return
        super().accept()
