from __future__ import annotations

from PySide6.QtCore import QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from openadb.core.device import DeviceManager
from openadb.core.settings_manager import SettingsManager
from openadb.models.device_info import DeviceInfo
from openadb.ui.workers import Worker, start_worker


class DeviceStatusBar(QFrame):
    device_refreshed = Signal(object)
    refresh_failed = Signal(str)

    COLORS = {
        "ADB": "#107c10",
        "Recovery": "#107c10",
        "Fastboot": "#f7630c",
        "Unauthorized": "#0078d4",
        "Offline": "#fce100",
        "No device": "#c42b1c",
        "Checking": "#8a8886",
    }

    def __init__(self, device_manager: DeviceManager, settings: SettingsManager, parent=None) -> None:
        super().__init__(parent)
        self.device_manager = device_manager
        self.settings = settings
        self.pool = QThreadPool.globalInstance()
        self._refresh_running = False
        self._has_device_snapshot = False
        self.setObjectName("deviceStatusBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.dot = QLabel("●")
        self.dot.setObjectName("statusDot")
        self.summary = QLabel("Checking device...")
        self.summary.setObjectName("statusSummary")
        self.details = QLabel("")
        self.details.setObjectName("statusDetails")
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        layout.addWidget(self.dot)
        layout.addWidget(self.summary)
        layout.addWidget(self.details, 1)
        layout.addWidget(self.refresh_button)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.configure_timer()

    def configure_timer(self) -> None:
        self.timer.stop()
        if self.settings.get("auto_refresh_device", True):
            interval = max(3, int(self.settings.get("refresh_interval_seconds", 8))) * 1000
            self.timer.start(interval)

    def refresh(self) -> None:
        if self._refresh_running:
            return
        self._refresh_running = True
        if not self._has_device_snapshot:
            self._set_checking()
        self.refresh_button.setEnabled(False)
        worker = Worker(self.device_manager.refresh)
        worker.signals.result.connect(self.set_device)
        worker.signals.result.connect(self.device_refreshed.emit)
        worker.signals.error.connect(lambda message, _trace: self.refresh_failed.emit(message))
        worker.signals.finished.connect(self._refresh_finished)
        start_worker(self, self.pool, worker)

    def _refresh_finished(self) -> None:
        self._refresh_running = False
        self.refresh_button.setEnabled(True)

    def set_device(self, device: DeviceInfo) -> None:
        color = self.COLORS.get(device.mode, "#8a8886")
        self.dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self.summary.setText(device.mode)
        details = [
            f"Serial: {device.serial or 'none'}",
            f"Model: {device.model or 'unknown'}",
            f"Manufacturer: {device.manufacturer or 'unknown'}",
            f"Android: {device.android_version or 'unknown'}",
            f"SDK: {device.sdk_version or 'unknown'}",
        ]
        self.details.setText("   ".join(details))
        self._has_device_snapshot = bool(device.serial)

    def _set_checking(self) -> None:
        self.dot.setStyleSheet(f"color: {self.COLORS['Checking']}; font-size: 18px;")
        self.summary.setText("Checking")
        self.details.setText("")
