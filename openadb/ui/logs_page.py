from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from openadb.models.command_result import CommandResult
from openadb.ui.design_system import configure_page_layout
from openadb.ui.widgets.empty_state import EmptyState


class LogsPage(QWidget):
    def __init__(self, logs_folder: Path, parent=None) -> None:
        super().__init__(parent)
        self.logs_folder = logs_folder
        layout = QVBoxLayout(self)
        configure_page_layout(layout)
        title = QLabel("Logs")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Review command results from the current OpenADB session.")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        toolbar = QFrame()
        toolbar.setObjectName("toolbarCard")
        buttons = QGridLayout()
        toolbar.setLayout(buttons)
        self.clear_button = QPushButton("Clear logs view")
        self.save_button = QPushButton("Save logs")
        self.copy_button = QPushButton("Copy logs")
        self.open_button = QPushButton("Open logs folder")
        action_buttons = [self.clear_button, self.save_button, self.copy_button, self.open_button]
        for index, button in enumerate(action_buttons):
            buttons.addWidget(button, index // 2, index % 2)
        buttons.setColumnStretch(0, 1)
        buttons.setColumnStretch(1, 1)
        layout.addWidget(toolbar)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setObjectName("logView")
        self.output.setAccessibleName("OpenADB command log")
        self.empty_state = EmptyState(
            "No logs",
            "Run a command or open the log folder to inspect previous log files.",
            "Open logs folder",
        )
        self.content = QStackedWidget()
        self.content.addWidget(self.output)
        self.content.addWidget(self.empty_state)
        self.content.setCurrentWidget(self.empty_state)
        layout.addWidget(self.content, 1)

        self.clear_button.clicked.connect(self.clear_logs_view)
        self.save_button.clicked.connect(self.save_logs)
        self.copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.output.toPlainText()))
        self.open_button.clicked.connect(self.open_logs_folder)
        self.empty_state.action_requested.connect(self.open_logs_folder)
        self._update_actions()

    def set_logs_folder(self, logs_folder: Path, clear_view: bool = False) -> None:
        self.logs_folder = logs_folder
        if clear_view:
            self.clear_logs_view()

    def append_result(self, result: CommandResult) -> None:
        timestamp = html.escape(result.started_at.strftime("%H:%M:%S"))
        command = html.escape(result.command_text)
        stdout = html.escape(result.stdout.rstrip())
        stderr = html.escape(result.stderr.rstrip())
        status = html.escape(result.status)
        outcome = "SUCCESS" if result.success else "ERROR"
        block = [
            f"<p><b>[{timestamp}] {outcome}: {status}</b><br>",
            f"<code>$ {command}</code><br>",
            f"exit={result.exit_code} duration={result.duration:.2f}s",
        ]
        if stdout:
            block.append(f"<pre>{stdout}</pre>")
        if stderr:
            block.append(f"<pre>[stderr]\n{stderr}</pre>")
        block.append("</p>")
        self.output.append("\n".join(block))
        self.content.setCurrentWidget(self.output)
        self._update_actions()

    def clear_logs_view(self) -> None:
        self.output.clear()
        self.content.setCurrentWidget(self.empty_state)
        self._update_actions()

    def open_logs_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.logs_folder)))

    def _update_actions(self) -> None:
        has_text = bool(self.output.toPlainText().strip())
        self.clear_button.setEnabled(has_text)
        self.copy_button.setEnabled(has_text)
        self.save_button.setEnabled(has_text)

    def save_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save logs", str(self.logs_folder / "openadb-log.txt"), "Text files (*.txt)")
        if path:
            Path(path).write_text(self.output.toPlainText(), encoding="utf-8")
