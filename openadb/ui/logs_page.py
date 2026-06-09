from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QPushButton, QTextEdit, QVBoxLayout, QWidget

from openadb.models.command_result import CommandResult


class LogsPage(QWidget):
    def __init__(self, logs_folder: Path, parent=None) -> None:
        super().__init__(parent)
        self.logs_folder = logs_folder
        layout = QVBoxLayout(self)
        buttons = QHBoxLayout()
        self.clear_button = QPushButton("Clear logs view")
        self.save_button = QPushButton("Save logs")
        self.copy_button = QPushButton("Copy logs")
        self.open_button = QPushButton("Open logs folder")
        for button in [self.clear_button, self.save_button, self.copy_button, self.open_button]:
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setObjectName("logView")
        layout.addWidget(self.output, 1)

        self.clear_button.clicked.connect(self.output.clear)
        self.save_button.clicked.connect(self.save_logs)
        self.copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.output.toPlainText()))
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.logs_folder))))

    def set_logs_folder(self, logs_folder: Path, clear_view: bool = False) -> None:
        self.logs_folder = logs_folder
        if clear_view:
            self.output.clear()

    def append_result(self, result: CommandResult) -> None:
        color = "#107c10" if result.success else "#c42b1c"
        timestamp = html.escape(result.started_at.strftime("%H:%M:%S"))
        command = html.escape(result.command_text)
        stdout = html.escape(result.stdout.rstrip())
        stderr = html.escape(result.stderr.rstrip())
        status = html.escape(result.status)
        block = [
            f'<p><b style="color:{color}">[{timestamp}] {status}</b><br>',
            f"<code>$ {command}</code><br>",
            f"exit={result.exit_code} duration={result.duration:.2f}s",
        ]
        if stdout:
            block.append(f"<pre>{stdout}</pre>")
        if stderr:
            block.append(f'<pre style="color:#c42b1c">{stderr}</pre>')
        block.append("</p>")
        self.output.append("\n".join(block))

    def save_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save logs", str(self.logs_folder / "openadb-log.txt"), "Text files (*.txt)")
        if path:
            Path(path).write_text(self.output.toPlainText(), encoding="utf-8")
