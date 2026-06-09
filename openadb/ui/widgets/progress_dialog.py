from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QProgressDialog,
    QVBoxLayout,
    QWidget,
)


class ActivityDialog(QProgressDialog):
    def __init__(self, title: str, label: str, parent: QWidget | None = None) -> None:
        super().__init__(label, "Cancel", 0, 0, parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModal)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self.setMinimumDuration(300)
        self.setCancelButton(None)

    def set_status(self, text: str) -> None:
        self.setLabelText(text)


class TransferProgressDialog(QDialog):
    cancel_requested = Signal()

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 520)
        self.setWindowModality(Qt.WindowModal)
        self._done = False
        self._activity_base = "Preparing transfer"
        self._activity_step = 0
        self._last_done_bytes = 0
        self._last_total_bytes = 0
        self._last_done_files = 0
        self._last_total_files = 0
        self._elapsed = QElapsedTimer()
        self._elapsed.start()

        layout = QVBoxLayout(self)
        self.header = QLabel("Preparing transfer...")
        self.header.setObjectName("pageTitle")
        self.header.setWordWrap(True)
        layout.addWidget(self.header)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        form = QFormLayout()
        layout.addLayout(form)
        self.direction = QLabel("-")
        self.file_count = QLabel("-")
        self.bytes_label = QLabel("-")
        self.speed = QLabel("-")
        self.elapsed = QLabel("-")
        self.remaining = QLabel("-")
        self.current_file = QLabel("-")
        self.source = QLabel("-")
        self.destination = QLabel("-")
        self.command = QLabel("-")
        for label in [
            self.current_file,
            self.source,
            self.destination,
            self.command,
        ]:
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Direction", self.direction)
        form.addRow("Files", self.file_count)
        form.addRow("Bytes", self.bytes_label)
        form.addRow("Speed", self.speed)
        form.addRow("Elapsed", self.elapsed)
        form.addRow("Remaining", self.remaining)
        form.addRow("Current file", self.current_file)
        form.addRow("Source", self.source)
        form.addRow("Destination", self.destination)
        form.addRow("Command", self.command)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumBlockCount(1000)
        layout.addWidget(self.details, 1)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(False)
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.close_button.clicked.connect(self.accept)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def apply_update(self, update: dict) -> None:
        kind = update.get("type", "")
        if kind == "plan":
            self._elapsed.restart()
            self._set_activity(str(update.get("title", "Transfer started")))
            self.direction.setText(str(update.get("direction", "-")))
            self._last_done_bytes = 0
            self._last_total_bytes = 0
            self._last_done_files = 0
            self._last_total_files = 0
            self._set_file_count(0, update.get("total_files"))
            self.remaining.setText("-")
            total_bytes = update.get("total_bytes")
            if isinstance(total_bytes, int) and total_bytes > 0:
                self._last_total_bytes = total_bytes
                self.progress.setRange(0, 1000)
                self.bytes_label.setText(f"0 B / {_format_bytes(total_bytes)}")
            else:
                self.progress.setRange(0, 0)
                self.bytes_label.setText("Unknown total size")
            self.source.setText(str(update.get("source", "-")))
            self.destination.setText(str(update.get("destination", "-")))
            self.append_detail(str(update.get("message", "Transfer plan created.")))
        elif kind == "file_start":
            self.current_file.setText(str(update.get("current_file", "-")))
            self.command.setText(str(update.get("command", "-")))
            self._set_activity("ADB is transferring the current item")
            self.append_detail(str(update.get("message", "Starting file transfer.")))
        elif kind == "progress":
            self._set_progress(update)
            if "done_files" in update:
                self._set_file_count(update.get("done_files"), update.get("total_files"))
            if "current_file" in update:
                self.current_file.setText(str(update.get("current_file") or "-"))
            output = str(update.get("output", "")).strip()
            if output:
                self.append_detail(output)
            activity = str(update.get("activity", "")).strip()
            if activity:
                self._set_activity(activity)
        elif kind == "heartbeat":
            self._set_progress(update)
            if "done_files" in update:
                self._set_file_count(update.get("done_files"), update.get("total_files"))
            if "current_file" in update:
                self.current_file.setText(str(update.get("current_file") or "-"))
            activity = str(update.get("activity", "ADB transfer is still running")).strip()
            if activity:
                self._set_activity(activity)
        elif kind == "file_done":
            self._set_progress(update)
            self._set_file_count(update.get("done_files"), update.get("total_files"))
            self.append_detail(str(update.get("message", "File transfer finished.")))
        elif kind == "done":
            self._done = True
            self._timer.stop()
            self.progress.setRange(0, 1000)
            self.progress.setValue(1000 if update.get("success", False) else self.progress.value())
            self._update_time_labels()
            if update.get("success", False):
                self.remaining.setText("0:00")
            self.header.setText(str(update.get("message", "Transfer finished.")))
            self.cancel_button.setEnabled(False)
            self.close_button.setEnabled(True)
            self.append_detail(str(update.get("message", "Transfer finished.")))
        elif kind == "cancelled":
            self.header.setText("Cancelling transfer...")
            self.cancel_button.setEnabled(False)
            self.append_detail("Cancellation requested.")

    def append_detail(self, text: str) -> None:
        if not text:
            return
        self.details.appendPlainText(text.rstrip())

    def _set_progress(self, update: dict) -> None:
        total_bytes = update.get("total_bytes")
        done_bytes = max(self._last_done_bytes, int(update.get("done_bytes") or 0))
        if isinstance(total_bytes, int) and total_bytes > 0:
            total_bytes = max(self._last_total_bytes, total_bytes, done_bytes)
            self._last_done_bytes = done_bytes
            self._last_total_bytes = total_bytes
            self.progress.setRange(0, 1000)
            self.progress.setValue(max(0, min(1000, int(done_bytes * 1000 / total_bytes))))
            self.bytes_label.setText(f"{_format_bytes(done_bytes)} / {_format_bytes(total_bytes)}")
        elif self.progress.minimum() == 0 and self.progress.maximum() != 0:
            self.progress.setRange(0, 0)
        self.speed.setText(str(update.get("speed", "-")))
        self._update_time_labels()

    def _set_file_count(self, done_files, total_files) -> None:
        try:
            done = int(done_files)
        except (TypeError, ValueError):
            done = self._last_done_files
        done = max(self._last_done_files, done)
        try:
            total = int(total_files)
        except (TypeError, ValueError):
            total = self._last_total_files
        total = max(self._last_total_files, total, done)
        self._last_done_files = done
        self._last_total_files = total
        self.file_count.setText(f"{done} / {total if total > 0 else '?'}")

    def _set_activity(self, text: str) -> None:
        self._activity_base = text.strip() or "ADB transfer is running"
        self._activity_step = 0
        self.header.setText(self._activity_base)

    def _tick(self) -> None:
        if self._done:
            return
        self._update_time_labels()
        self._activity_step = (self._activity_step + 1) % 4
        suffix = "." * self._activity_step
        self.header.setText(f"{self._activity_base}{suffix}")

    def _update_time_labels(self) -> None:
        elapsed_seconds = self._elapsed.elapsed() / 1000
        self.elapsed.setText(_format_duration(elapsed_seconds))
        self.remaining.setText(_format_remaining(self._last_done_bytes, self._last_total_bytes, elapsed_seconds))

    def reject(self) -> None:
        if self._done:
            super().reject()
        else:
            self.cancel_requested.emit()


def _format_bytes(size: int | float | None) -> str:
    if size is None:
        return "Unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def _format_remaining(done_bytes: int, total_bytes: int, elapsed_seconds: float) -> str:
    if total_bytes <= 0 or done_bytes <= 0:
        return "-"
    remaining_bytes = max(0, total_bytes - done_bytes)
    if remaining_bytes == 0:
        return "0:00"
    if elapsed_seconds <= 0:
        return "-"
    bytes_per_second = done_bytes / elapsed_seconds
    if bytes_per_second <= 0:
        return "-"
    return _format_duration(remaining_bytes / bytes_per_second)
