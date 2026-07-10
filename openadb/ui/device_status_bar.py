from __future__ import annotations

import threading

from PySide6.QtCore import QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from openadb.core.device import DeviceManager
from openadb.core.settings_manager import SettingsManager
from openadb.models.device_info import DeviceInfo
from openadb.ui.widgets.elided_label import ElidedLabel
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
        self._offline_reconnect_running = False
        self._offline_reconnect_exhausted_serial = ""
        self._offline_reconnect_target_serial = ""
        self._device_monitor_running = False
        self._device_monitor_shutting_down = False
        self._device_monitor_cancel_event: threading.Event | None = None
        self._device_monitor_refresh_pending = False
        self._has_device_snapshot = False
        self.setObjectName("deviceStatusBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.dot = QLabel("●")
        self.dot.setObjectName("statusDot")
        self.summary = QLabel("Checking device...")
        self.summary.setObjectName("statusSummary")
        self.details = ElidedLabel("")
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

        self.monitor_restart_timer = QTimer(self)
        self.monitor_restart_timer.setSingleShot(True)
        self.monitor_restart_timer.timeout.connect(self.start_device_monitor)

    def configure_timer(self) -> None:
        self.timer.stop()
        if self.settings.get("auto_refresh_device", True):
            interval = max(3, int(self.settings.get("refresh_interval_seconds", 8))) * 1000
            self.timer.start(interval)

    def start_device_monitor(self) -> None:
        if self._device_monitor_running or self._device_monitor_shutting_down:
            return
        if not self.device_manager.adb.platform_tools.active.has_adb:
            return
        self._device_monitor_cancel_event = threading.Event()
        self._device_monitor_running = True
        worker = Worker(self._run_device_monitor)
        worker.signals.progress.connect(self._device_monitor_changed)
        worker.signals.error.connect(lambda message, _trace: self.refresh_failed.emit(message))
        worker.signals.finished.connect(self._device_monitor_finished)
        start_worker(self, self.pool, worker)

    def stop_device_monitor(self) -> None:
        self._device_monitor_shutting_down = True
        self.monitor_restart_timer.stop()
        if self._device_monitor_cancel_event is not None:
            self._device_monitor_cancel_event.set()

    def restart_device_monitor(self) -> None:
        if self._device_monitor_running:
            if self._device_monitor_cancel_event is not None:
                self._device_monitor_cancel_event.set()
            return
        self._device_monitor_shutting_down = False
        self.start_device_monitor()

    def _run_device_monitor(self, progress_callback=None):
        cancel_event = self._device_monitor_cancel_event

        def output_callback(_channel: str, text: str) -> None:
            if progress_callback is not None and text.strip():
                progress_callback.emit("devices-changed")

        return self.device_manager.adb.track_devices(output_callback=output_callback, cancel_event=cancel_event)

    def _device_monitor_changed(self, _message: str) -> None:
        if self._device_monitor_refresh_pending:
            return
        self._device_monitor_refresh_pending = True
        QTimer.singleShot(150, self._refresh_from_device_monitor)

    def _refresh_from_device_monitor(self) -> None:
        self._device_monitor_refresh_pending = False
        self.refresh()

    def _device_monitor_finished(self) -> None:
        self._device_monitor_running = False
        self._device_monitor_cancel_event = None
        if not self._device_monitor_shutting_down:
            self.monitor_restart_timer.start(5000)

    def refresh(self) -> None:
        if self._refresh_running or self._offline_reconnect_running:
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
        self.refresh_button.setEnabled(not self._offline_reconnect_running)

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
        if device.form_factor:
            details.append(f"Type: {device.form_factor}")
        self.details.setText("   ".join(details))
        self._has_device_snapshot = bool(device.serial)
        if device.mode == "Offline":
            self._start_offline_reconnect(device.serial)
        elif device.mode != "Checking":
            self._offline_reconnect_exhausted_serial = ""

    def _start_offline_reconnect(self, serial: str) -> None:
        serial = (serial or "").strip()
        reconnect_key = self._offline_reconnect_key(serial)
        if self._offline_reconnect_running:
            return
        if reconnect_key == self._offline_reconnect_exhausted_serial:
            return
        self._offline_reconnect_running = True
        self._offline_reconnect_target_serial = serial
        self.refresh_button.setEnabled(False)
        worker = Worker(self.device_manager.reconnect_offline, serial, 4)
        worker.signals.progress.connect(self._set_reconnect_progress)
        worker.signals.result.connect(self._offline_reconnect_complete)
        worker.signals.error.connect(lambda message, _trace: self.refresh_failed.emit(message))
        worker.signals.finished.connect(self._offline_reconnect_finished)
        start_worker(self, self.pool, worker)

    def _set_reconnect_progress(self, message: str) -> None:
        self.dot.setStyleSheet(f"color: {self.COLORS['Offline']}; font-size: 18px;")
        self.summary.setText("Offline")
        self.details.setText(message)

    def _offline_reconnect_complete(self, device: DeviceInfo) -> None:
        if device.mode == "Offline":
            self._offline_reconnect_exhausted_serial = self._offline_reconnect_key(
                device.serial or self._offline_reconnect_target_serial
            )
        else:
            self._offline_reconnect_exhausted_serial = ""
        self.set_device(device)
        self.device_refreshed.emit(device)

    def _offline_reconnect_finished(self) -> None:
        self._offline_reconnect_running = False
        self._offline_reconnect_target_serial = ""
        self.refresh_button.setEnabled(True)

    @staticmethod
    def _offline_reconnect_key(serial: str) -> str:
        return (serial or "__offline_without_serial__").strip()

    def _set_checking(self) -> None:
        self.dot.setStyleSheet(f"color: {self.COLORS['Checking']}; font-size: 18px;")
        self.summary.setText("Checking")
        self.details.setText("")
