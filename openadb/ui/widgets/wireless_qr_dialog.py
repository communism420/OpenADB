from __future__ import annotations

from io import BytesIO

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from openadb.core.wireless_qr import WirelessQrPayload
from openadb.ui.design_system import (
    configure_dialog,
    fit_dialog_to_available_screen,
    set_button_role,
)
from openadb.ui.widgets.elided_label import ElidedLabel

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
        target_size = fit_dialog_to_available_screen(
            self,
            preferred=QSize(460, 520),
            minimum=QSize(280, 360),
        )
        layout = QVBoxLayout(self)

        title = QLabel("Scan this QR code on the phone")
        title.setObjectName("dialogTitle")
        title.setTextFormat(Qt.PlainText)
        title.setWordWrap(True)
        title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        title.setMinimumWidth(0)
        title.setAccessibleDescription(title.text())
        layout.addWidget(title)

        hint = QLabel(
            "Open Developer options -> Wireless debugging -> Pair device with QR code, "
            "then scan this code. OpenADB will pair and connect automatically."
        )
        hint.setObjectName("hintLabel")
        hint.setTextFormat(Qt.PlainText)
        hint.setWordWrap(True)
        hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hint.setMinimumWidth(0)
        hint.setAccessibleDescription(hint.text())
        layout.addWidget(hint)

        qr = QLabel()
        qr.setAlignment(Qt.AlignCenter)
        qr.setObjectName("qrCodeImage")
        qr.setAccessibleName("Wireless ADB pairing QR code")
        # A 300-DIP QR is ideal at normal sizes, but on a small logical screen
        # (including a high-DPI display) it used to defeat the dialog's screen
        # cap. Preserve a comfortably scannable code while leaving room for
        # the wrapped instructions, live status, and buttons.
        qr_size = max(
            160,
            min(300, target_size.width() - 40, target_size.height() - 180),
        )
        qr.setPixmap(_make_qr_pixmap(payload.qr_text, qr_size))
        self.qr_image = qr
        layout.addWidget(qr, alignment=Qt.AlignCenter)

        self.status = ElidedLabel("Waiting for QR scan...", elide_mode=Qt.ElideRight)
        self.status.setObjectName("hintLabel")
        self.status.setAccessibleName("Wireless ADB QR pairing status")
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
