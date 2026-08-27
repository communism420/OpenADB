from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QTextOption
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from openadb.ui.design_system import configure_dialog, fit_dialog_to_available_screen


class ErrorDialog(QDialog):
    """A bounded, copyable error dialog that also handles unbroken paths."""

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        message: str,
        logs_folder: str | Path | None = None,
    ) -> None:
        super().__init__(parent)
        value = str(message or "").strip() or "The operation failed."
        configure_dialog(self, title)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModal)

        root = QVBoxLayout(self)
        content = QHBoxLayout()
        icon = QLabel(self)
        icon.setPixmap(
            self.style()
            .standardIcon(QStyle.SP_MessageBoxCritical)
            .pixmap(QSize(40, 40))
        )
        icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        icon.setAccessibleName("Error")
        content.addWidget(icon, 0, Qt.AlignTop)

        body = QVBoxLayout()
        self.message_view = QPlainTextEdit(value, self)
        self.message_view.setObjectName("errorDialogMessage")
        self.message_view.setReadOnly(True)
        self.message_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        text_options = self.message_view.document().defaultTextOption()
        text_options.setWrapMode(QTextOption.WrapAnywhere)
        self.message_view.document().setDefaultTextOption(text_options)
        self.message_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.message_view.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.message_view.setMinimumSize(0, 88)
        self.message_view.setMaximumHeight(240)
        self.message_view.setAccessibleName("Error details")
        self.message_view.setAccessibleDescription(value)
        body.addWidget(self.message_view, 1)

        if logs_folder:
            hint = QLabel("Technical details are available in OpenADB logs.", self)
            hint.setObjectName("hintLabel")
            hint.setTextFormat(Qt.PlainText)
            hint.setWordWrap(True)
            hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            hint.setMinimumWidth(0)
            hint.setAccessibleDescription(hint.text())
            body.addWidget(hint)
        content.addLayout(body, 1)
        root.addLayout(content, 1)

        buttons = QDialogButtonBox(self)
        self.close_button = buttons.addButton("Close", QDialogButtonBox.RejectRole)
        self.close_button.setAccessibleName("Close error message")
        self.close_button.setDefault(True)
        self.open_logs_button: QPushButton | None = None
        if logs_folder:
            self.open_logs_button = buttons.addButton(
                "Open Logs",
                QDialogButtonBox.ActionRole,
            )
            self.open_logs_button.setAccessibleName("Open OpenADB logs folder")
            self.open_logs_button.clicked.connect(self.accept)
        self.close_button.clicked.connect(self.reject)
        root.addWidget(buttons)

        fit_dialog_to_available_screen(
            self,
            preferred=QSize(640, 300),
            minimum=QSize(380, 220),
        )


class BoundedMessageBox(QMessageBox):
    """QMessageBox variant whose prose and details cannot force it off-screen."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        preferred_size: QSize = QSize(640, 300),
        minimum_size: QSize = QSize(380, 220),
    ) -> None:
        super().__init__(parent)
        self._preferred_size = QSize(preferred_size)
        self._minimum_size = QSize(minimum_size)

    def prepare_layout(self) -> None:
        for label in self.findChildren(QLabel):
            if label.pixmap() is not None:
                continue
            value = label.text()
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            label.setMinimumWidth(0)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setToolTip(value)
            label.setAccessibleDescription(value)
        for details in self.findChildren(QTextEdit):
            details.setLineWrapMode(QTextEdit.WidgetWidth)
            details.setWordWrapMode(QTextOption.WrapAnywhere)
            details.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            value = details.toPlainText()
            details.setAccessibleName("Message details")
            details.setAccessibleDescription(value)
        fit_dialog_to_available_screen(
            self,
            preferred=self._preferred_size,
            minimum=self._minimum_size,
        )

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API name
        super().showEvent(event)
        # QMessageBox computes its native size during showEvent. Re-apply the
        # bounded policies afterwards so long details cannot undo the cap.
        self.prepare_layout()


def build_bounded_message_box(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    icon: QMessageBox.Icon = QMessageBox.Warning,
    buttons: QMessageBox.StandardButton = QMessageBox.Ok,
    default_button: QMessageBox.StandardButton = QMessageBox.Ok,
    detailed_text: str = "",
) -> BoundedMessageBox:
    box = BoundedMessageBox(parent)
    configure_dialog(box, title)
    box.setWindowTitle(title)
    box.setIcon(icon)
    box.setText(str(text or "").strip() or "The operation needs your attention.")
    if detailed_text:
        box.setDetailedText(str(detailed_text))
    box.setStandardButtons(buttons)
    if default_button != QMessageBox.NoButton:
        box.setDefaultButton(default_button)
    box.prepare_layout()
    return box


def exec_bounded_message_box(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    icon: QMessageBox.Icon = QMessageBox.Warning,
    buttons: QMessageBox.StandardButton = QMessageBox.Ok,
    default_button: QMessageBox.StandardButton = QMessageBox.Ok,
    detailed_text: str = "",
) -> QMessageBox.StandardButton:
    box = build_bounded_message_box(
        parent,
        title,
        text,
        icon=icon,
        buttons=buttons,
        default_button=default_button,
        detailed_text=detailed_text,
    )
    box.exec()
    clicked = box.clickedButton()
    return box.standardButton(clicked) if clicked is not None else QMessageBox.NoButton


def show_error_dialog(
    parent: QWidget | None,
    title: str,
    message: str,
    logs_folder: str | Path | None = None,
) -> None:
    """Show a user-facing error without a traceback and optionally link to logs."""
    dialog = ErrorDialog(parent, title, message, logs_folder)
    dialog.setObjectName("errorDialog")
    dialog.exec()
    if dialog.open_logs_button is not None and dialog.result() == QDialog.Accepted:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(logs_folder).expanduser())))
