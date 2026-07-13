from __future__ import annotations

from io import BytesIO

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from openadb.core.wireless_qr import WirelessQrPayload
from openadb.ui.design_system import configure_dialog, set_button_role

try:
    import qrcode
except ImportError:  # pragma: no cover - exercised only on incomplete installs
    qrcode = None


class WirelessQrDialog(QDialog):
    cancel_requested = Signal()

    def __init__(self, payload: WirelessQrPayload, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        configure_dialog(self, "Wireless ADB QR pairing")
        self._finished = False
        self.setWindowTitle("Wireless ADB QR pairing")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)

        title = QLabel("Scan this QR code on the phone")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        hint = QLabel(
            "Open Developer options -> Wireless debugging -> Pair device with QR code, "
            "then scan this code. OpenADB will pair and connect automatically."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        qr = QLabel()
        qr.setAlignment(Qt.AlignCenter)
        qr.setObjectName("qrCodeImage")
        qr.setPixmap(_make_qr_pixmap(payload.qr_text, 300))
        layout.addWidget(qr, alignment=Qt.AlignCenter)

        self.status = QLabel("Waiting for QR scan...")
        self.status.setObjectName("hintLabel")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(False)
        set_button_role(self.close_button, "primary")
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.cancel_button.clicked.connect(self._cancel)
        self.close_button.clicked.connect(self.accept)
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()

    def set_status(self, message: str) -> None:
        self.status.setText(message)

    def mark_finished(self, success: bool) -> None:
        self._finished = True
        self.cancel_button.setEnabled(False)
        self.close_button.setEnabled(True)
        if success:
            self.close_button.setDefault(True)

    def closeEvent(self, event) -> None:
        if not self._finished:
            self.cancel_requested.emit()
        super().closeEvent(event)

    def _cancel(self) -> None:
        self.cancel_requested.emit()
        self.cancel_button.setEnabled(False)
        self.status.setText("Cancelling QR pairing...")


def _make_qr_pixmap(text: str, size: int) -> QPixmap:
    if qrcode is None:
        raise RuntimeError("The qrcode package is required for Wireless ADB QR pairing. Run pip install -r requirements.txt.")

    qr = qrcode.QRCode(border=2, box_size=8)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    image = image.resize((size, size))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return pixmap
