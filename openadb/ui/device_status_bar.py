from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.device import DeviceManager
from openadb.core.settings_manager import SettingsManager
from openadb.models.device_info import DeviceInfo
from openadb.ui.design_system import configure_dialog
from openadb.ui.widgets.elided_label import ElidedLabel
from openadb.ui.workers import Worker, start_worker


DETAIL_FIELDS = (
    ("Serial", "serial"),
    ("Model", "model"),
    ("Manufacturer", "manufacturer"),
    ("Connection mode", "mode"),
    ("Device state", "state"),
    ("Android version", "android_version"),
    ("SDK", "sdk_version"),
    ("Device type", "form_factor"),
    ("Product", "product"),
    ("Transport ID", "transport_id"),
)


class DeviceDetailsDialog(QDialog):
    def __init__(self, device: DeviceInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        configure_dialog(self, "Device details")
        self.device = device
        self.setWindowTitle("Device details")
        self.resize(620, 420)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.fields: dict[str, QLineEdit] = {}
        for label, attribute in DETAIL_FIELDS:
            value = str(getattr(device, attribute, "") or "—")
            edit = QLineEdit(value)
            edit.setReadOnly(True)
            edit.setToolTip(value)
            edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.fields[attribute] = edit
            form.addRow(label, edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setDefault(True)
        self.copy_button = buttons.addButton("Copy details", QDialogButtonBox.ActionRole)
        self.copy_button.clicked.connect(self.copy_details)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def detail_text(self) -> str:
        return "\n".join(
            f"{label}: {getattr(self.device, attribute, '') or '—'}"
            for label, attribute in DETAIL_FIELDS
        )

    def copy_details(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.detail_text())


class DeviceStatusBar(QFrame):
    device_refreshed = Signal(object)
    refresh_failed = Signal(str)
    choose_device_requested = Signal()

    COLORS = {
        "ADB": "#107c10",
        "Recovery": "#107c10",
        "Fastboot": "#f7630c",
        "Unauthorized": "#0078d4",
        "Offline": "#d6a500",
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
        self._device = DeviceInfo(mode="Checking", state="checking")
        self._details_dialog_factory = DeviceDetailsDialog

        self.setObjectName("deviceStatusBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(7)

        self.dot = QLabel("●")
        self.dot.setObjectName("statusDot")
        self.dot.setAccessibleName("Device status indicator")
        self.summary = ElidedLabel("Checking")
        self.summary.setObjectName("statusSummary")
        self.device_name = ElidedLabel("Looking for devices")
        self.device_name.setObjectName("statusDeviceName")
        self.mode_label = ElidedLabel("Checking")
        self.mode_label.setObjectName("statusMode")
        self.mode_label.setAlignment(Qt.AlignCenter)
        self.state_label = ElidedLabel("Scanning for connected devices")
        self.state_label.setObjectName("statusState")
        self.details = self.state_label  # Backward-compatible attribute used by earlier integrations.

        self.details_button = QToolButton()
        self.details_button.setObjectName("deviceDetailsButton")
        self.details_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation)
        )
        self.details_button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.details_button.setAccessibleName("Show full device details")
        self.details_button.clicked.connect(self._show_details)
        self.device_button = QPushButton("Devices")
        self.device_button.setObjectName("devicePickerButton")
        self.device_button.setAccessibleName("Choose active Android device")
        self.device_button.clicked.connect(self.choose_device_requested.emit)
        self.device_button.hide()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setAccessibleName("Refresh device status")
        self.refresh_button.clicked.connect(self.refresh)

        layout.addWidget(self.dot)
        layout.addWidget(self.summary)
        layout.addWidget(self.device_name, 2)
        layout.addWidget(self.mode_label)
        layout.addWidget(self.state_label, 3)
        layout.addWidget(self.details_button)
        layout.addWidget(self.device_button)
        layout.addWidget(self.refresh_button)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.configure_timer()

        self.monitor_restart_timer = QTimer(self)
        self.monitor_restart_timer.setSingleShot(True)
        self.monitor_restart_timer.timeout.connect(self.start_device_monitor)
        self._render_device()

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
        self._update_device_button()
        worker = Worker(self.device_manager.refresh)
        worker.signals.result.connect(self.set_device)
        worker.signals.result.connect(self.device_refreshed.emit)
        worker.signals.error.connect(lambda message, _trace: self.refresh_failed.emit(message))
        worker.signals.finished.connect(self._refresh_finished)
        start_worker(self, self.pool, worker)

    def _refresh_finished(self) -> None:
        self._refresh_running = False
        self.refresh_button.setEnabled(not self._offline_reconnect_running)
        self._update_device_button()

    def set_device(self, device: DeviceInfo) -> None:
        self._device = device
        self._has_device_snapshot = bool(device.serial)
        self._render_device()
        if device.mode == "Offline":
            self._start_offline_reconnect(device.serial)
        elif device.mode != "Checking":
            self._offline_reconnect_exhausted_serial = ""

    def _render_device(self) -> None:
        device = self._device
        status, name, mode, short_state = self._display_values(device)
        color = self.COLORS.get(device.mode, self.COLORS["Checking"])
        self.dot.setStyleSheet(f"color: {color}; font-size: 18px;")
        self.dot.setAccessibleName(f"Device status indicator: {status}")
        self.dot.setToolTip(status)
        self.summary.setText(status)
        self.device_name.setText(name)
        self.mode_label.setText(mode)
        self.mode_label.setToolTip(f"Connection mode: {mode}")
        self.state_label.setText(short_state)
        detail_text = self._detail_text(device)
        self.details_button.setToolTip("Show full device details\n\n" + detail_text)
        self._update_device_button()

    def _display_values(self, device: DeviceInfo) -> tuple[str, str, str, str]:
        candidates = list(getattr(self.device_manager, "devices", []) or [])
        if device.mode == "Checking":
            return (
                "Checking",
                "Looking for devices",
                "Checking",
                "Scanning for connected devices",
            )
        if device.mode == "No device" and device.state == "selection_required" and candidates:
            count = len(candidates)
            noun = "device" if count == 1 else "devices"
            return (
                "Selection required",
                f"{count} {noun} available",
                "No active device",
                "Choose a device to continue",
            )
        if device.mode == "No device":
            return "No device", "No Android device", "Disconnected", "Connect a device to continue"

        name = device.model or device.product or device.serial or "Unknown device"
        if device.mode == "Unauthorized":
            return "Authorization required", name, "Unauthorized", "Confirm USB debugging on the device"
        if device.mode == "Offline":
            state = "Trying to reconnect" if self._offline_reconnect_running else "Device is not responding"
            return "Offline", name, "Offline", state
        if device.mode == "ADB":
            return "Connected", name, "ADB", "Ready"
        if device.mode == "Recovery":
            return "Connected", name, "Recovery", "Recovery interface"
        if device.mode == "Fastboot":
            return "Connected", name, "Fastboot", "Bootloader interface"
        return device.mode or "Unknown", name, device.mode or "Unknown", device.state or "Unknown state"

    def _update_device_button(self) -> None:
        devices = list(getattr(self.device_manager, "devices", []) or [])
        selection_needed = bool(devices and not self._device.serial)
        visible = len(devices) > 1 or selection_needed
        self.device_button.setVisible(visible)
        if not visible:
            return
        self.device_button.setText(f"Devices ({len(devices)})" if len(devices) > 1 else "Choose device")
        busy = self._refresh_running or self._offline_reconnect_running
        self.device_button.setEnabled(not busy)
        if busy:
            self.device_button.setToolTip(
                "Device selection is available after the current refresh or reconnect finishes."
            )
            return
        active = self._device.model or self._device.serial
        active_text = f" Current: {active}." if active else " No active device is selected."
        self.device_button.setToolTip(f"Choose the active device from {len(devices)} detected.{active_text}")

    def _show_details(self) -> None:
        dialog = self._details_dialog_factory(self._device, self)
        dialog.exec()

    @staticmethod
    def _detail_text(device: DeviceInfo) -> str:
        return "\n".join(
            f"{label}: {getattr(device, attribute, '') or '—'}"
            for label, attribute in DETAIL_FIELDS
        )

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
        self._render_device()
        worker = Worker(self.device_manager.reconnect_offline, serial, 4)
        worker.signals.progress.connect(self._set_reconnect_progress)
        worker.signals.result.connect(self._offline_reconnect_complete)
        worker.signals.error.connect(lambda message, _trace: self.refresh_failed.emit(message))
        worker.signals.finished.connect(self._offline_reconnect_finished)
        start_worker(self, self.pool, worker)

    def _set_reconnect_progress(self, message: str) -> None:
        self.dot.setStyleSheet(f"color: {self.COLORS['Offline']}; font-size: 18px;")
        self.summary.setText("Offline")
        self.mode_label.setText("Offline")
        self.state_label.setText(message)

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
        self._render_device()

    @staticmethod
    def _offline_reconnect_key(serial: str) -> str:
        return (serial or "__offline_without_serial__").strip()

    def _set_checking(self) -> None:
        self._device = DeviceInfo(mode="Checking", state="checking")
        self._render_device()
