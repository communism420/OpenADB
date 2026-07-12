from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from openadb.ui.design_system import configure_dialog


def show_error_dialog(
    parent: QWidget | None,
    title: str,
    message: str,
    logs_folder: str | Path | None = None,
) -> None:
    """Show a user-facing error without a traceback and optionally link to logs."""
    box = QMessageBox(parent)
    configure_dialog(box, title)
    box.setObjectName("errorDialog")
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle(title)
    box.setText(message.strip() or "The operation failed.")
    box.setInformativeText("Technical details are available in OpenADB logs." if logs_folder else "")
    close_button = box.addButton("Close", QMessageBox.RejectRole)
    close_button.setAccessibleName("Close error message")
    logs_button = box.addButton("Open Logs", QMessageBox.ActionRole) if logs_folder else None
    if logs_button is not None:
        logs_button.setAccessibleName("Open OpenADB logs folder")
    box.setDefaultButton(close_button)
    box.exec()
    if logs_button is not None and box.clickedButton() is logs_button:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(logs_folder).expanduser())))
