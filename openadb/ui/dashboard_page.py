from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo


class DashboardPage(QScrollArea):
    refresh_device_requested = Signal()
    detect_tools_requested = Signal()
    choose_tools_requested = Signal()
    command_requested = Signal(str)
    open_page_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        root = QWidget()
        self.setWidget(root)
        layout = QVBoxLayout(root)
        title = QLabel("OpenADB")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        self.grid = QGridLayout()
        layout.addLayout(self.grid)
        self.labels: dict[str, QLabel] = {}
        for index, key in enumerate(
            [
                "Device status",
                "Model",
                "Manufacturer",
                "Android version",
                "SDK version",
                "Serial number",
                "Connection mode",
                "Platform Tools",
                "ADB version",
                "Fastboot version",
                "Active path",
            ]
        ):
            card, value = self._card(key, "Unknown")
            self.labels[key] = value
            self.grid.addWidget(card, index // 2, index % 2)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("hintLabel")
        layout.addWidget(self.hint)

        button_row = QHBoxLayout()
        buttons = [
            ("Refresh Device", lambda: self.refresh_device_requested.emit()),
            ("Detect Platform Tools", lambda: self.detect_tools_requested.emit()),
            ("Reboot System", lambda: self.command_requested.emit("adb_reboot")),
            ("Reboot Recovery", lambda: self.command_requested.emit("adb_reboot_recovery")),
            ("Reboot Bootloader", lambda: self.command_requested.emit("adb_reboot_bootloader")),
            ("Check ADB Devices", lambda: self.command_requested.emit("adb_devices")),
            ("Check Fastboot Devices", lambda: self.command_requested.emit("fastboot_devices")),
            ("Open Logs", lambda: self.open_page_requested.emit("Logs")),
            ("Open Settings", lambda: self.open_page_requested.emit("Settings")),
        ]
        for text, slot in buttons:
            button = QPushButton(text)
            button.clicked.connect(slot)
            button_row.addWidget(button)
        button_row.addStretch()
        layout.addLayout(button_row)
        layout.addStretch()

    def update_device(self, device: DeviceInfo) -> None:
        self.labels["Device status"].setText(device.title)
        self.labels["Model"].setText(device.model or "Unknown")
        self.labels["Manufacturer"].setText(device.manufacturer or "Unknown")
        self.labels["Android version"].setText(device.android_version or "Unknown")
        self.labels["SDK version"].setText(device.sdk_version or "Unknown")
        self.labels["Serial number"].setText(device.serial or "None")
        self.labels["Connection mode"].setText(device.mode)
        if device.mode == "No device":
            self.hint.setText(
                "No Android device detected. Enable USB debugging, confirm the RSA fingerprint on the phone, "
                "check the USB cable and Android drivers, then press Refresh."
            )
        elif device.mode == "Unauthorized":
            self.hint.setText("ADB unauthorized. Confirm RSA fingerprint on your phone, then press Refresh.")
        elif device.mode == "Offline":
            self.hint.setText("The device is offline. Reconnect USB or restart adb server, then refresh.")
        else:
            self.hint.setText("")

    def update_tools(self, tools: PlatformToolsInfo) -> None:
        self.labels["Platform Tools"].setText(tools.status)
        self.labels["ADB version"].setText(tools.adb_version)
        self.labels["Fastboot version"].setText(tools.fastboot_version)
        self.labels["Active path"].setText(tools.folder_text or "Not selected")
        if tools.status == "Not found":
            self.hint.setText(
                "Android Platform Tools were not found. Use Detect Platform Tools or choose the folder manually."
            )

    def _card(self, title: str, value: str) -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        caption = QLabel(title)
        caption.setObjectName("cardCaption")
        label = QLabel(value)
        label.setObjectName("cardValue")
        label.setWordWrap(True)
        layout.addWidget(caption)
        layout.addWidget(label)
        return frame, label
