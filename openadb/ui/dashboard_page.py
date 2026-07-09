from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from openadb.core.settings_manager import SettingsManager
from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.branding import logo_pixmap
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox as QComboBox
from openadb.ui.widgets.no_wheel_widgets import NoWheelSpinBox as QSpinBox


WIRELESS_MODE_MODERN = "modern"
WIRELESS_MODE_LEGACY = "legacy"
WIRELESS_LEGACY_PORT = 5555


class DashboardPage(QScrollArea):
    refresh_device_requested = Signal()
    detect_tools_requested = Signal()
    choose_tools_requested = Signal()
    command_requested = Signal(str)
    open_page_requested = Signal(str)
    wireless_tcpip_requested = Signal(int)
    wireless_detect_ip_requested = Signal()
    wireless_connect_requested = Signal(str, int)
    wireless_pair_requested = Signal(str, int, str)
    wireless_qr_pair_requested = Signal()
    wireless_scan_requested = Signal()
    wireless_disconnect_requested = Signal(str, object)

    def __init__(self, settings: SettingsManager, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._last_device_mode = "No device"
        self._last_device_title = "No Android device detected"
        self._last_tools_status = "Unknown"
        self.setWidgetResizable(True)
        root = QWidget()
        self.setWidget(root)
        layout = QVBoxLayout(root)
        title_row = QHBoxLayout()
        logo = QLabel()
        logo.setObjectName("dashboardLogo")
        pixmap = logo_pixmap(48)
        if not pixmap.isNull():
            logo.setPixmap(pixmap)
        logo.setFixedSize(54, 54)
        logo.setAlignment(Qt.AlignCenter)
        title = QLabel("OpenADB")
        title.setObjectName("pageTitle")
        title_row.addWidget(logo)
        title_row.addWidget(title)
        title_row.addStretch()
        layout.addLayout(title_row)

        self.grid = QGridLayout()
        layout.addLayout(self.grid)
        self.labels: dict[str, QLabel] = {}
        for index, key in enumerate(
            [
                "Device status",
                "Device type",
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

        layout.addSpacing(8)
        layout.addWidget(self._wireless_group())

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

    def reload_from_settings(self) -> None:
        mode = self._settings_wireless_mode()
        self._set_wireless_mode_combo(mode)
        self._wireless_active_mode = mode
        self._load_wireless_settings_for_mode(mode)
        self._apply_wireless_mode_ui()

    def update_device(self, device: DeviceInfo) -> None:
        self.labels["Device status"].setText(device.title)
        self.labels["Device type"].setText(device.form_factor or "Android")
        self.labels["Model"].setText(device.model or "Unknown")
        self.labels["Manufacturer"].setText(device.manufacturer or "Unknown")
        self.labels["Android version"].setText(device.android_version or "Unknown")
        self.labels["SDK version"].setText(device.sdk_version or "Unknown")
        self.labels["Serial number"].setText(device.serial or "None")
        self.labels["Connection mode"].setText(device.mode)
        self._last_device_mode = device.mode or "Unknown"
        self._last_device_title = device.title or "Android device"
        self._refresh_dashboard_hint()

    def update_tools(self, tools: PlatformToolsInfo) -> None:
        self.labels["Platform Tools"].setText(tools.status)
        self.labels["ADB version"].setText(tools.adb_version)
        self.labels["Fastboot version"].setText(tools.fastboot_version)
        self.labels["Active path"].setText(tools.folder_text or "Not selected")
        self._last_tools_status = tools.status or "Unknown"
        self._refresh_dashboard_hint()

    def _refresh_dashboard_hint(self) -> None:
        if self._last_tools_status == "Not found":
            message = "Android Platform Tools were not found. Use Detect Platform Tools or choose the folder manually."
        elif self._last_tools_status == "Partially found":
            message = "Android Platform Tools are only partially available. Check ADB and fastboot paths in Settings."
        elif self._last_device_mode == "No device":
            message = (
                "No Android device detected. Enable USB debugging, confirm the RSA fingerprint on the phone, "
                "check the USB cable and Android drivers, then press Refresh."
            )
        elif self._last_device_mode == "Unauthorized":
            message = "ADB unauthorized. Confirm RSA fingerprint on your phone, then press Refresh."
        elif self._last_device_mode == "Offline":
            message = "The device is offline. OpenADB will try to reconnect; reconnect USB or restart adb server if needed."
        elif self._last_device_mode == "Fastboot":
            message = "Device is in Fastboot mode. Fastboot commands are available; Apps and File Manager need ADB mode."
        elif self._last_device_mode in {"ADB", "Recovery"}:
            message = f"{self._last_device_title} is connected and ready. Apps, File Manager, Commands, and backups are available."
        else:
            message = "Device status is being checked. Press Refresh if this message does not update."
        self.hint.setText(message)

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

    def _wireless_group(self) -> QFrame:
        group = QFrame()
        group.setObjectName("wirelessGroup")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        title = QLabel("Wireless ADB")
        title.setObjectName("wirelessGroupTitle")
        layout.addWidget(title)

        hint = QLabel(
            "USB workflow: connect by cable, enable TCP/IP, find Wi-Fi IP, then connect. "
            "Android 11+ Wireless debugging: pair with QR code or pairing port/code, then connect. "
            "Android TV: enable Network debugging/Wireless debugging on the TV, then use Find Android TV."
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self.wireless_mode = QComboBox()
        self.wireless_mode.addItem("Modern Wireless debugging", WIRELESS_MODE_MODERN)
        self.wireless_mode.addItem("Legacy TCP/IP (IP only)", WIRELESS_MODE_LEGACY)
        self._wireless_active_mode = self._settings_wireless_mode()
        self._set_wireless_mode_combo(self._wireless_active_mode)

        self.wireless_host_label = QLabel("Device IP / host")
        self.wireless_port_label = QLabel("Connect port")
        self.wireless_pair_port_label = QLabel("Pairing port")
        self.wireless_pair_code_label = QLabel("Pairing code")

        self.wireless_host = QLineEdit()
        self.wireless_host.setPlaceholderText("Phone IP address, for example 192.168.1.42")
        self.wireless_port = QSpinBox()
        self.wireless_port.setRange(1, 65535)
        self.wireless_pair_port = QLineEdit()
        self.wireless_pair_port.setPlaceholderText("Pairing port from Wireless debugging")
        self.wireless_pair_code = QLineEdit()
        self.wireless_pair_code.setPlaceholderText("Pairing code")
        form.addRow("Mode", self.wireless_mode)
        form.addRow(self.wireless_host_label, self.wireless_host)
        form.addRow(self.wireless_port_label, self.wireless_port)
        form.addRow(self.wireless_pair_port_label, self.wireless_pair_port)
        form.addRow(self.wireless_pair_code_label, self.wireless_pair_code)
        layout.addLayout(form)

        buttons = QGridLayout()
        self.wireless_enable_tcpip = QPushButton("Enable TCP/IP on USB device")
        self.wireless_detect_ip = QPushButton("Find device Wi-Fi IP")
        self.wireless_pair = QPushButton("Pair")
        self.wireless_qr_pair = QPushButton("Pair by QR code")
        self.wireless_scan = QPushButton("Find Android TV")
        self.wireless_connect = QPushButton("Connect")
        self.wireless_disconnect = QPushButton("Disconnect")
        buttons.addWidget(self.wireless_enable_tcpip, 0, 0)
        buttons.addWidget(self.wireless_detect_ip, 0, 1)
        buttons.addWidget(self.wireless_pair, 0, 2)
        buttons.addWidget(self.wireless_qr_pair, 1, 0)
        buttons.addWidget(self.wireless_connect, 1, 1)
        buttons.addWidget(self.wireless_disconnect, 1, 2)
        buttons.addWidget(self.wireless_scan, 2, 0, 1, 3)
        layout.addLayout(buttons)

        self.wireless_message = QLabel("Wireless ADB commands are logged in Logs.")
        self.wireless_message.setObjectName("cardCaption")
        self.wireless_message.setWordWrap(True)
        layout.addWidget(self.wireless_message)

        self._load_wireless_settings_for_mode(self._wireless_active_mode)
        self._apply_wireless_mode_ui()

        self.wireless_mode.currentIndexChanged.connect(self._wireless_mode_changed)
        self.wireless_host.editingFinished.connect(self._save_wireless_settings)
        self.wireless_port.valueChanged.connect(lambda _value: self._save_wireless_settings())
        self.wireless_pair_port.editingFinished.connect(self._save_wireless_settings)
        self.wireless_enable_tcpip.clicked.connect(self._request_wireless_tcpip)
        self.wireless_detect_ip.clicked.connect(self._request_wireless_detect_ip)
        self.wireless_pair.clicked.connect(self._request_wireless_pair)
        self.wireless_qr_pair.clicked.connect(self._request_wireless_qr_pair)
        self.wireless_scan.clicked.connect(self._request_wireless_scan)
        self.wireless_connect.clicked.connect(self._request_wireless_connect)
        self.wireless_disconnect.clicked.connect(self._request_wireless_disconnect)
        return group

    def set_wireless_status(self, message: str) -> None:
        self.wireless_message.setText(message)

    def set_wireless_addresses(self, addresses: list[str]) -> None:
        if addresses:
            self.wireless_host.setText(addresses[0])
            self._save_wireless_settings()
            self.set_wireless_status("Detected Wi-Fi address(es): " + ", ".join(addresses))

    def set_wireless_target(self, target: str) -> None:
        host, port = self._split_wireless_target(target)
        if host:
            self.wireless_host.setText(host)
        if port is not None and self._wireless_mode_value() == WIRELESS_MODE_MODERN:
            self.wireless_port.setValue(port)
        if host or port is not None:
            self._save_wireless_settings()

    def _settings_wireless_mode(self) -> str:
        mode = str(
            self.settings.get("wireless_connection_mode", "")
            or self.settings.get("wireless_adb_mode", WIRELESS_MODE_MODERN)
            or ""
        ).strip().lower()
        return WIRELESS_MODE_LEGACY if mode == WIRELESS_MODE_LEGACY else WIRELESS_MODE_MODERN

    def _set_wireless_mode_combo(self, mode: str) -> None:
        index = self.wireless_mode.findData(mode)
        if index < 0:
            index = self.wireless_mode.findData(WIRELESS_MODE_MODERN)
        self.wireless_mode.blockSignals(True)
        self.wireless_mode.setCurrentIndex(max(0, index))
        self.wireless_mode.blockSignals(False)

    def _wireless_mode_value(self) -> str:
        value = self.wireless_mode.currentData()
        return WIRELESS_MODE_LEGACY if value == WIRELESS_MODE_LEGACY else WIRELESS_MODE_MODERN

    def _wireless_mode_changed(self) -> None:
        previous = getattr(self, "_wireless_active_mode", WIRELESS_MODE_MODERN)
        current = self._wireless_mode_value()
        if previous != current:
            self._save_wireless_settings_for_mode(previous)
            self._wireless_active_mode = current
            self.settings.set("wireless_connection_mode", current, save=False)
            self.settings.set("wireless_adb_mode", current, save=False)
            self._load_wireless_settings_for_mode(current)
            self._apply_wireless_mode_ui()
            self.settings.save()

    def _load_wireless_settings_for_mode(self, mode: str) -> None:
        modern = mode == WIRELESS_MODE_MODERN
        if modern:
            host = str(
                self.settings.get("wireless_modern_host", "")
                or self.settings.get("wireless_adb_host", "")
                or ""
            )
            port_key = "wireless_modern_port"
            fallback_port = self._settings_port("wireless_adb_port", WIRELESS_LEGACY_PORT)
            pair_port = str(
                self.settings.get("wireless_modern_pair_port", "")
                or self.settings.get("wireless_adb_pair_port", "")
                or ""
            )
        else:
            host = str(
                self.settings.get("wireless_legacy_host", "")
                or self.settings.get("wireless_adb_host", "")
                or ""
            )
            port_key = "wireless_adb_port"
            fallback_port = WIRELESS_LEGACY_PORT
            pair_port = ""

        for widget in [self.wireless_host, self.wireless_port, self.wireless_pair_port]:
            widget.blockSignals(True)
        try:
            self.wireless_host.setText(host)
            self.wireless_port.setValue(self._settings_port(port_key, fallback_port) if modern else WIRELESS_LEGACY_PORT)
            self.wireless_pair_port.setText(pair_port)
        finally:
            for widget in [self.wireless_host, self.wireless_port, self.wireless_pair_port]:
                widget.blockSignals(False)

    def _apply_wireless_mode_ui(self) -> None:
        modern = self._wireless_mode_value() == WIRELESS_MODE_MODERN
        self.wireless_host_label.setText("Device IP / host" if modern else "Device IP")
        self.wireless_host.setPlaceholderText(
            "Phone IP address, for example 192.168.1.42" if modern else "Device IP only, for example 192.168.1.42"
        )
        for widget in [
            self.wireless_port_label,
            self.wireless_port,
            self.wireless_pair_port_label,
            self.wireless_pair_port,
            self.wireless_pair_code_label,
            self.wireless_pair_code,
            self.wireless_pair,
            self.wireless_qr_pair,
            self.wireless_scan,
        ]:
            widget.setVisible(modern)
        self.wireless_enable_tcpip.setText("Enable TCP/IP on USB device" if modern else "Enable old TCP/IP 5555 on USB device")
        self.wireless_connect.setText("Connect" if modern else "Connect by IP")
        self.wireless_disconnect.setText("Disconnect" if modern else "Disconnect IP")
        if modern:
            self.wireless_message.setText("Modern mode uses Android Wireless debugging pairing, QR pairing, or mDNS discovery.")
        else:
            self.wireless_message.setText(
                "Legacy mode uses old adb tcpip on port 5555. Enter only the device IP; no Wireless debugging port or pairing code is needed."
            )

    def _settings_port(self, key: str, fallback: int) -> int:
        try:
            value = int(str(self.settings.get(key, fallback)).strip())
        except ValueError:
            return fallback
        return value if 1 <= value <= 65535 else fallback

    def _save_wireless_settings(self) -> None:
        mode = self._wireless_mode_value()
        self._save_wireless_settings_for_mode(mode)
        self._wireless_active_mode = mode
        self.settings.save()

    def _save_wireless_settings_for_mode(self, mode: str) -> None:
        host = self.wireless_host.text().strip()
        self.settings.set("wireless_connection_mode", mode, save=False)
        self.settings.set("wireless_adb_mode", mode, save=False)
        if mode == WIRELESS_MODE_LEGACY:
            self.settings.set("wireless_legacy_host", host, save=False)
            self.settings.set("wireless_adb_host", host, save=False)
            self.settings.set("wireless_adb_port", WIRELESS_LEGACY_PORT, save=False)
            return
        self.settings.set("wireless_modern_host", host, save=False)
        self.settings.set("wireless_modern_port", int(self.wireless_port.value()), save=False)
        self.settings.set("wireless_modern_pair_port", self.wireless_pair_port.text().strip(), save=False)
        self.settings.set("wireless_adb_host", host, save=False)
        self.settings.set("wireless_adb_port", int(self.wireless_port.value()), save=False)
        self.settings.set("wireless_adb_pair_port", self.wireless_pair_port.text().strip(), save=False)

    def _wireless_host_text(self) -> str:
        host = self.wireless_host.text().strip()
        if not host:
            QMessageBox.warning(self, "Wireless ADB", "Enter the phone IP address or hostname first.")
        return host

    def _split_wireless_target(self, target: str) -> tuple[str, int | None]:
        text = str(target or "").strip()
        if not text:
            return "", None
        host = text
        port: int | None = None
        if text.startswith("[") and "]:" in text:
            host_part, port_text = text[1:].split("]:", 1)
            host = host_part
            try:
                port = int(port_text)
            except ValueError:
                port = None
        elif ":" in text:
            host_part, port_text = text.rsplit(":", 1)
            if port_text.isdigit():
                host = host_part
                port = int(port_text)
        if port is not None and not 1 <= port <= 65535:
            port = None
        return host, port

    def _wireless_pair_port_value(self) -> int | None:
        text = self.wireless_pair_port.text().strip()
        if not text:
            QMessageBox.warning(self, "Wireless ADB", "Enter the pairing port shown in Android Wireless debugging.")
            return None
        try:
            port = int(text)
        except ValueError:
            QMessageBox.warning(self, "Wireless ADB", "Pairing port must be a number from 1 to 65535.")
            return None
        if port < 1 or port > 65535:
            QMessageBox.warning(self, "Wireless ADB", "Pairing port must be from 1 to 65535.")
            return None
        return port

    def _request_wireless_tcpip(self) -> None:
        self._save_wireless_settings()
        port = WIRELESS_LEGACY_PORT if self._wireless_mode_value() == WIRELESS_MODE_LEGACY else int(self.wireless_port.value())
        self.wireless_tcpip_requested.emit(port)

    def _request_wireless_detect_ip(self) -> None:
        self._save_wireless_settings()
        self.wireless_detect_ip_requested.emit()

    def _request_wireless_connect(self) -> None:
        host = self._wireless_host_text()
        if not host:
            return
        self._save_wireless_settings()
        port = WIRELESS_LEGACY_PORT if self._wireless_mode_value() == WIRELESS_MODE_LEGACY else int(self.wireless_port.value())
        self.wireless_connect_requested.emit(host, port)

    def _request_wireless_pair(self) -> None:
        if self._wireless_mode_value() == WIRELESS_MODE_LEGACY:
            QMessageBox.information(self, "Wireless ADB pair", "Legacy IP-only mode does not use Wireless debugging pairing.")
            return
        host = self._wireless_host_text()
        if not host:
            return
        pair_port = self._wireless_pair_port_value()
        if pair_port is None:
            return
        code = self.wireless_pair_code.text().strip()
        if not code:
            QMessageBox.warning(self, "Wireless ADB pair", "Enter the pairing code shown on the phone.")
            return
        self._save_wireless_settings()
        self.wireless_pair_requested.emit(host, pair_port, code)

    def _request_wireless_qr_pair(self) -> None:
        if self._wireless_mode_value() == WIRELESS_MODE_LEGACY:
            QMessageBox.information(self, "Wireless ADB QR pair", "Legacy IP-only mode does not use QR pairing.")
            return
        self._save_wireless_settings()
        self.wireless_qr_pair_requested.emit()

    def _request_wireless_scan(self) -> None:
        if self._wireless_mode_value() == WIRELESS_MODE_LEGACY:
            QMessageBox.information(self, "Find Android TV", "mDNS discovery is available in Modern Wireless debugging mode.")
            return
        self._save_wireless_settings()
        self.wireless_scan_requested.emit()

    def _request_wireless_disconnect(self) -> None:
        host = self.wireless_host.text().strip()
        port = WIRELESS_LEGACY_PORT if self._wireless_mode_value() == WIRELESS_MODE_LEGACY else int(self.wireless_port.value())
        if not host:
            answer = QMessageBox.warning(
                self,
                "Wireless ADB disconnect",
                "No host is entered. Disconnect all wireless ADB connections?",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if answer != QMessageBox.Ok:
                return
            port_arg: int | None = None
        else:
            port_arg = port
        self._save_wireless_settings()
        self.wireless_disconnect_requested.emit(host, port_arg)
