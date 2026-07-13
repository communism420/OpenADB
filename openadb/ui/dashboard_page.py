from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from openadb.core.adb import is_mdns_wireless_serial
from openadb.core.settings_manager import SettingsManager
from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.design_system import configure_page_layout, set_button_role
from openadb.ui.widgets.collapsible_card import CollapsibleCard
from openadb.ui.widgets.elided_label import ElidedLabel
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox as QComboBox
from openadb.ui.widgets.no_wheel_widgets import NoWheelSpinBox as QSpinBox
from openadb.ui.widgets.wireless_pairing_dialog import WirelessPairingDialog


WIRELESS_MODE_MODERN = "modern"
WIRELESS_MODE_LEGACY = "legacy"
WIRELESS_SCENARIO_MODERN = "modern"
WIRELESS_SCENARIO_LEGACY = "legacy"
WIRELESS_SCENARIO_TV = "tv"
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
        self._last_device = DeviceInfo()
        self._last_tools = PlatformToolsInfo()
        self._recommended_action = "refresh"
        self._wireless_active_scenario = WIRELESS_SCENARIO_MODERN
        self._pairing_dialog_factory = WirelessPairingDialog

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.root = QWidget()
        self.root.setObjectName("dashboardRoot")
        self.setWidget(self.root)
        layout = QVBoxLayout(self.root)
        configure_page_layout(layout)

        layout.addLayout(self._page_header())
        layout.addWidget(self._connection_card())
        layout.addWidget(self._technical_details_card())
        layout.addWidget(self._wireless_card())
        layout.addStretch()

        self.labels = {
            "Device status": self.connection_status_title,
            "Device type": self.device_type_value,
            "Model": self.device_name,
            "Manufacturer": self.detail_labels["Manufacturer"],
            "Android version": self.android_value,
            "SDK version": self.detail_labels["SDK version"],
            "Serial number": self.detail_labels["Serial number"],
            "Connection mode": self.mode_value,
            "Platform Tools": self.detail_labels["Platform Tools status"],
            "ADB version": self.detail_labels["ADB version"],
            "Fastboot version": self.detail_labels["Fastboot version"],
            "Active path": self.detail_labels["Active path"],
        }
        self.hint = self.next_action_text

        self.details_card.expanded_changed.connect(
            lambda expanded: self.settings.set("dashboard_details_expanded", expanded)
        )
        self.wireless_card.expanded_changed.connect(
            lambda expanded: self.settings.set("dashboard_wireless_expanded", expanded)
        )

        self.reload_from_settings()
        self.update_device(self._last_device)
        self.update_tools(self._last_tools)

    def _page_header(self) -> QVBoxLayout:
        header = QVBoxLayout()
        header.setSpacing(0)
        title = QLabel("Dashboard")
        title.setObjectName("pageTitle")
        subtitle = QLabel("Device overview and connection")
        subtitle.setObjectName("pageSubtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        return header

    def _connection_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("connectionHero")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        badge_row = QHBoxLayout()
        self.status_badge = QLabel("NO DEVICE")
        self.status_badge.setObjectName("connectionStateBadge")
        self.status_badge.setProperty("connectionState", "neutral")
        badge_row.addWidget(self.status_badge)
        badge_row.addStretch()
        self.mode_value = QLabel("No device")
        self.mode_value.setObjectName("connectionModeValue")
        badge_row.addWidget(self.mode_value)
        layout.addLayout(badge_row)

        self.connection_status_title = QLabel("No Android device detected")
        self.connection_status_title.setObjectName("connectionStatusTitle")
        self.connection_status_title.setWordWrap(True)
        layout.addWidget(self.connection_status_title)

        self.device_name = ElidedLabel("Waiting for a device", elide_mode=Qt.ElideRight)
        self.device_name.setObjectName("connectionDeviceName")
        layout.addWidget(self.device_name)

        meta = QGridLayout()
        meta.setContentsMargins(0, 2, 0, 2)
        meta.setHorizontalSpacing(24)
        meta.setVerticalSpacing(2)
        android_caption = QLabel("Android version")
        android_caption.setObjectName("connectionMetaCaption")
        type_caption = QLabel("Device type")
        type_caption.setObjectName("connectionMetaCaption")
        self.android_value = ElidedLabel("—", elide_mode=Qt.ElideRight)
        self.android_value.setObjectName("connectionMetaValue")
        self.device_type_value = ElidedLabel("—", elide_mode=Qt.ElideRight)
        self.device_type_value.setObjectName("connectionMetaValue")
        meta.addWidget(android_caption, 0, 0)
        meta.addWidget(type_caption, 0, 1)
        meta.addWidget(self.android_value, 1, 0)
        meta.addWidget(self.device_type_value, 1, 1)
        meta.setColumnStretch(0, 1)
        meta.setColumnStretch(1, 1)
        layout.addLayout(meta)

        next_panel = QFrame()
        next_panel.setObjectName("nextActionPanel")
        next_layout = QGridLayout(next_panel)
        next_layout.setContentsMargins(12, 10, 12, 10)
        next_layout.setHorizontalSpacing(12)
        next_title = QLabel("Recommended next step")
        next_title.setObjectName("nextActionTitle")
        self.primary_action_button = QPushButton("Refresh")
        self.primary_action_button.setObjectName("primaryAction")
        self.primary_action_button.clicked.connect(self._run_recommended_action)
        self.next_action_text = QLabel("")
        self.next_action_text.setObjectName("nextActionText")
        self.next_action_text.setWordWrap(True)
        next_layout.addWidget(next_title, 0, 0)
        next_layout.addWidget(self.next_action_text, 1, 0)
        next_layout.addWidget(self.primary_action_button, 2, 0, alignment=Qt.AlignLeft)
        next_layout.setColumnStretch(0, 1)
        layout.addWidget(next_panel)

        quick_actions = QHBoxLayout()
        quick_actions.setSpacing(8)
        self.refresh_button = QPushButton("Refresh")
        set_button_role(self.refresh_button, "primary")
        self.refresh_button.clicked.connect(self.refresh_device_requested.emit)
        quick_actions.addWidget(self.refresh_button)

        self.reboot_button = QPushButton("Reboot")
        self.reboot_menu = QMenu(self.reboot_button)
        self.reboot_actions = {}
        for text, key in [
            ("System", "adb_reboot"),
            ("Recovery", "adb_reboot_recovery"),
            ("Bootloader", "adb_reboot_bootloader"),
            ("Sideload", "adb_reboot_sideload"),
        ]:
            action = self.reboot_menu.addAction(text)
            action.triggered.connect(lambda _checked=False, command=key: self.command_requested.emit(command))
            self.reboot_actions[key] = action
        self.reboot_button.setMenu(self.reboot_menu)
        quick_actions.addWidget(self.reboot_button)

        self.more_button = QPushButton("More actions")
        self.more_menu = QMenu(self.more_button)
        self.more_actions = {}
        self.more_actions["commands"] = self.more_menu.addAction(
            "Open Commands", lambda: self.open_page_requested.emit("Commands")
        )
        self.more_menu.addSeparator()
        self.more_actions["adb_devices"] = self.more_menu.addAction(
            "Check ADB devices", lambda: self.command_requested.emit("adb_devices")
        )
        self.more_actions["fastboot_devices"] = self.more_menu.addAction(
            "Check Fastboot devices", lambda: self.command_requested.emit("fastboot_devices")
        )
        self.more_menu.addSeparator()
        self.more_actions["detect_tools"] = self.more_menu.addAction(
            "Detect Platform Tools", self.detect_tools_requested.emit
        )
        self.more_actions["choose_tools"] = self.more_menu.addAction(
            "Choose Platform Tools folder", self.choose_tools_requested.emit
        )
        self.more_menu.addSeparator()
        self.more_actions["logs"] = self.more_menu.addAction(
            "Open Logs", lambda: self.open_page_requested.emit("Logs")
        )
        self.more_actions["settings"] = self.more_menu.addAction(
            "Open Settings", lambda: self.open_page_requested.emit("Settings")
        )
        self.more_button.setMenu(self.more_menu)
        quick_actions.addWidget(self.more_button)
        quick_actions.addStretch()
        layout.addLayout(quick_actions)
        return card

    def _technical_details_card(self) -> CollapsibleCard:
        card = CollapsibleCard(
            "Technical details",
            "Serial, SDK, and Platform Tools",
            expanded=bool(self.settings.get("dashboard_details_expanded", False)),
        )
        self.detail_labels: dict[str, ElidedLabel] = {}
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        for row, key in enumerate(
            [
                "Serial number",
                "Manufacturer",
                "SDK version",
                "Platform Tools status",
                "ADB version",
                "Fastboot version",
                "Active path",
            ]
        ):
            caption = QLabel(key)
            caption.setObjectName("detailCaption")
            value = ElidedLabel("Unknown", elide_mode=Qt.ElideMiddle)
            value.setObjectName("detailValue")
            grid.addWidget(caption, row, 0, alignment=Qt.AlignTop)
            grid.addWidget(value, row, 1)
            self.detail_labels[key] = value
        grid.setColumnStretch(1, 1)
        card.content_layout.addLayout(grid)
        self.details_card = card
        return card

    def _wireless_card(self) -> CollapsibleCard:
        card = CollapsibleCard(
            "Wireless ADB",
            "Connect wirelessly",
            expanded=bool(self.settings.get("dashboard_wireless_expanded", False)),
        )

        self.wireless_description = QLabel()
        self.wireless_description.setObjectName("sectionDescription")
        self.wireless_description.setWordWrap(True)
        card.content_layout.addWidget(self.wireless_description)

        scenario_panel = QFrame()
        scenario_panel.setObjectName("wirelessScenarioPanel")
        scenario_layout = QVBoxLayout(scenario_panel)
        scenario_layout.setContentsMargins(10, 10, 10, 10)
        scenario_layout.setSpacing(10)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.wireless_scenario = QComboBox()
        self.wireless_scenario.addItem("Modern Wireless Debugging", WIRELESS_SCENARIO_MODERN)
        self.wireless_scenario.addItem("Legacy TCP/IP", WIRELESS_SCENARIO_LEGACY)
        self.wireless_scenario.addItem("Android TV", WIRELESS_SCENARIO_TV)
        self.wireless_mode = self.wireless_scenario

        self.wireless_host_label = QLabel("Device IP / host")
        self.wireless_host = QLineEdit()
        self.wireless_port_label = QLabel("Connection port")
        self.wireless_port = QSpinBox()
        self.wireless_port.setRange(1, 65535)
        form.addRow("Scenario", self.wireless_scenario)
        form.addRow(self.wireless_host_label, self.wireless_host)
        form.addRow(self.wireless_port_label, self.wireless_port)
        scenario_layout.addLayout(form)

        self.wireless_actions_stack = QStackedWidget()
        self.wireless_qr_pair = QPushButton("Pair by QR code")
        self.wireless_pair = QPushButton("Pair with code…")
        self.wireless_actions_stack.addWidget(
            self._wireless_action_page([self.wireless_qr_pair, self.wireless_pair])
        )

        self.wireless_enable_tcpip = QPushButton("Enable TCP/IP over USB")
        self.wireless_detect_ip = QPushButton("Find device Wi-Fi IP")
        self.wireless_actions_stack.addWidget(
            self._wireless_action_page([self.wireless_enable_tcpip, self.wireless_detect_ip])
        )

        self.wireless_scan = QPushButton("Find Android TV")
        self.wireless_tv_pair = QPushButton("Pair TV with code…")
        self.wireless_tv_qr_pair = QPushButton("Pair TV by QR code")
        self.wireless_actions_stack.addWidget(
            self._wireless_action_page([self.wireless_scan, self.wireless_tv_pair, self.wireless_tv_qr_pair])
        )
        scenario_layout.addWidget(self.wireless_actions_stack)

        connect_actions = QGridLayout()
        connect_actions.setContentsMargins(0, 0, 0, 0)
        connect_actions.setHorizontalSpacing(8)
        connect_actions.setVerticalSpacing(8)
        self.wireless_connect = QPushButton("Connect")
        self.wireless_connect.setObjectName("primaryAction")
        self.wireless_disconnect = QPushButton("Disconnect")
        connect_actions.addWidget(self.wireless_connect, 0, 0)
        connect_actions.addWidget(self.wireless_disconnect, 0, 1)
        connect_actions.setColumnStretch(0, 1)
        connect_actions.setColumnStretch(1, 1)
        scenario_layout.addLayout(connect_actions)
        card.content_layout.addWidget(scenario_panel)

        self.wireless_message = QLabel("Wireless ADB commands are logged in Logs.")
        self.wireless_message.setObjectName("wirelessStatus")
        self.wireless_message.setWordWrap(True)
        card.content_layout.addWidget(self.wireless_message)

        self.wireless_scenario.currentIndexChanged.connect(self._wireless_scenario_changed)
        self.wireless_host.editingFinished.connect(self._save_wireless_settings)
        self.wireless_port.valueChanged.connect(lambda _value: self._save_wireless_settings())
        self.wireless_enable_tcpip.clicked.connect(self._request_wireless_tcpip)
        self.wireless_detect_ip.clicked.connect(self._request_wireless_detect_ip)
        self.wireless_pair.clicked.connect(self._request_wireless_pair)
        self.wireless_tv_pair.clicked.connect(self._request_wireless_pair)
        self.wireless_qr_pair.clicked.connect(self._request_wireless_qr_pair)
        self.wireless_tv_qr_pair.clicked.connect(self._request_wireless_qr_pair)
        self.wireless_scan.clicked.connect(self._request_wireless_scan)
        self.wireless_connect.clicked.connect(self._request_wireless_connect)
        self.wireless_disconnect.clicked.connect(self._request_wireless_disconnect)

        self.wireless_card = card
        return card

    def _wireless_action_page(self, buttons: list[QPushButton]) -> QWidget:
        page = QWidget()
        page.setObjectName("wirelessActionPage")
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for index, button in enumerate(buttons):
            # A single column remains usable when the application is narrowed
            # beside another window on a 900-1000 px wide desktop.
            grid.addWidget(button, index, 0)
        grid.setColumnStretch(0, 1)
        return page

    def reload_from_settings(self) -> None:
        self.wireless_message.setText("Wireless ADB commands are logged in Logs.")
        self.details_card.set_expanded(
            bool(self.settings.get("dashboard_details_expanded", False)), notify=False
        )
        self.wireless_card.set_expanded(
            bool(self.settings.get("dashboard_wireless_expanded", False)), notify=False
        )
        scenario = self._settings_wireless_scenario()
        self._set_wireless_scenario_combo(scenario)
        self._wireless_active_scenario = scenario
        self._load_wireless_settings_for_scenario(scenario)
        self._apply_wireless_scenario_ui()

    def update_device(self, device: DeviceInfo) -> None:
        self._last_device = device
        mode = device.mode or "Unknown"
        state, badge, status_title = self._connection_presentation(mode)
        self.status_badge.setText(badge)
        self.status_badge.setProperty("connectionState", state)
        style = self.status_badge.style()
        style.unpolish(self.status_badge)
        style.polish(self.status_badge)
        self.status_badge.update()

        self.connection_status_title.setText(status_title)
        if mode == "No device" or not device.serial:
            device_name = "Waiting for an Android device"
        else:
            device_name = device.model or device.serial or "Android device"
        self.device_name.setText(device_name)
        self.mode_value.setText(mode)
        self.android_value.setText(device.android_version or "—")
        self.device_type_value.setText(device.form_factor or ("Android" if device.serial else "—"))
        self.detail_labels["Serial number"].setText(device.serial or "None")
        self.detail_labels["Manufacturer"].setText(device.manufacturer or "Unknown")
        self.detail_labels["SDK version"].setText(device.sdk_version or "Unknown")
        self._update_details_summary()
        self._update_recommended_action()
        self._update_action_availability()

    def update_tools(self, tools: PlatformToolsInfo) -> None:
        self._last_tools = tools
        self.detail_labels["Platform Tools status"].setText(tools.status)
        self.detail_labels["ADB version"].setText(tools.adb_version or "Unknown")
        self.detail_labels["Fastboot version"].setText(tools.fastboot_version or "Unknown")
        self.detail_labels["Active path"].setText(tools.folder_text or "Not selected")
        self._update_details_summary()
        self._update_recommended_action()
        self._update_action_availability()

    def _connection_presentation(self, mode: str) -> tuple[str, str, str]:
        presentations = {
            "ADB": ("connected", "CONNECTED", "Connected via ADB"),
            "Recovery": ("connected", "RECOVERY", "Connected in Recovery mode"),
            "Fastboot": ("warning", "FASTBOOT", "Device is in Fastboot mode"),
            "Unauthorized": ("warning", "AUTHORIZATION REQUIRED", "USB debugging authorization required"),
            "Offline": ("error", "OFFLINE", "Device is offline"),
            "No device": ("neutral", "NO DEVICE", "No Android device detected"),
            "Checking": ("neutral", "CHECKING", "Checking device connection"),
        }
        return presentations.get(mode, ("neutral", mode.upper() or "UNKNOWN", "Device status is unknown"))

    def _update_recommended_action(self) -> None:
        tools_status = self._last_tools.status
        mode = self._last_device.mode or "Unknown"
        if tools_status == "Not found":
            self._set_recommended_action(
                "detect_tools",
                "Set up Platform Tools",
                "Android Platform Tools were not found. Detect them automatically or choose their folder from More actions.",
            )
        elif tools_status == "Partially found":
            self._set_recommended_action(
                "settings",
                "Review Platform Tools",
                "Only part of Platform Tools is available. Review the active folder before running device commands.",
            )
        elif mode == "No device":
            self._set_recommended_action(
                "refresh",
                "Refresh device",
                "Connect a device, enable USB debugging, confirm the RSA fingerprint, then refresh.",
            )
        elif mode == "Unauthorized":
            self._set_recommended_action(
                "refresh",
                "Refresh after authorizing",
                "Unlock the device and accept the USB debugging fingerprint prompt, then refresh.",
            )
        elif mode == "Offline":
            self._set_recommended_action(
                "refresh",
                "Retry connection",
                "Reconnect USB or restart the ADB connection, then check the device again.",
            )
        elif mode == "Fastboot":
            self._set_recommended_action(
                "commands",
                "Open Commands",
                "Fastboot commands are available. Apps and File Manager require an ADB connection.",
            )
        elif mode in {"ADB", "Recovery"}:
            self._set_recommended_action(
                "apps",
                "Open applications",
                "The active device is ready. Manage applications, files, backups, or open Commands for advanced tasks.",
            )
        else:
            self._set_recommended_action(
                "refresh",
                "Refresh status",
                "Check the current device and Platform Tools state again.",
            )

    def _set_recommended_action(self, action: str, button_text: str, message: str) -> None:
        self._recommended_action = action
        self.primary_action_button.setText(button_text)
        self.next_action_text.setText(message)

    def _run_recommended_action(self) -> None:
        if self._recommended_action == "detect_tools":
            self.detect_tools_requested.emit()
        elif self._recommended_action == "settings":
            self.open_page_requested.emit("Settings")
        elif self._recommended_action == "commands":
            self.open_page_requested.emit("Commands")
        elif self._recommended_action == "apps":
            self.open_page_requested.emit("Apps")
        else:
            self.refresh_device_requested.emit()

    def _update_action_availability(self) -> None:
        adb_ready = self._last_tools.has_adb and self._last_device.mode in {"ADB", "Recovery"}
        self.reboot_button.setEnabled(adb_ready)
        self.reboot_button.setToolTip("" if adb_ready else "Reboot options require an authorized ADB device.")
        for action in self.reboot_actions.values():
            action.setEnabled(adb_ready)
        self.more_actions["adb_devices"].setEnabled(self._last_tools.has_adb)
        self.more_actions["fastboot_devices"].setEnabled(self._last_tools.has_fastboot)

    def _update_details_summary(self) -> None:
        serial = self._last_device.serial or "No active device"
        self.details_card.set_summary(f"{serial} · Platform Tools: {self._last_tools.status}")

    def set_wireless_status(self, message: str) -> None:
        self.wireless_message.setText(message)
        self.wireless_card.set_summary(message)

    def wireless_scenario_value(self) -> str:
        return self._wireless_scenario_value()

    def set_wireless_busy(self, busy: bool) -> None:
        enabled = not bool(busy)
        for widget in (
            self.wireless_scenario,
            self.wireless_host,
            self.wireless_port,
            self.wireless_qr_pair,
            self.wireless_pair,
            self.wireless_enable_tcpip,
            self.wireless_detect_ip,
            self.wireless_scan,
            self.wireless_tv_pair,
            self.wireless_tv_qr_pair,
            self.wireless_connect,
            self.wireless_disconnect,
        ):
            widget.setEnabled(enabled)

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

    def _settings_wireless_scenario(self) -> str:
        scenario = str(self.settings.get("wireless_dashboard_scenario", "") or "").strip().lower()
        if scenario in {WIRELESS_SCENARIO_MODERN, WIRELESS_SCENARIO_LEGACY, WIRELESS_SCENARIO_TV}:
            return scenario
        return (
            WIRELESS_SCENARIO_LEGACY
            if self._settings_wireless_mode() == WIRELESS_MODE_LEGACY
            else WIRELESS_SCENARIO_MODERN
        )

    def _set_wireless_scenario_combo(self, scenario: str) -> None:
        index = self.wireless_scenario.findData(scenario)
        if index < 0:
            index = self.wireless_scenario.findData(WIRELESS_SCENARIO_MODERN)
        self.wireless_scenario.blockSignals(True)
        self.wireless_scenario.setCurrentIndex(max(0, index))
        self.wireless_scenario.blockSignals(False)

    def _set_wireless_mode_combo(self, mode: str) -> None:
        scenario = WIRELESS_SCENARIO_LEGACY if mode == WIRELESS_MODE_LEGACY else mode
        self._set_wireless_scenario_combo(scenario)

    def _wireless_scenario_value(self) -> str:
        value = str(self.wireless_scenario.currentData() or "")
        if value in {WIRELESS_SCENARIO_MODERN, WIRELESS_SCENARIO_LEGACY, WIRELESS_SCENARIO_TV}:
            return value
        return WIRELESS_SCENARIO_MODERN

    def _wireless_mode_value(self) -> str:
        return (
            WIRELESS_MODE_LEGACY
            if self._wireless_scenario_value() == WIRELESS_SCENARIO_LEGACY
            else WIRELESS_MODE_MODERN
        )

    def _wireless_scenario_changed(self) -> None:
        previous = getattr(self, "_wireless_active_scenario", WIRELESS_SCENARIO_MODERN)
        current = self._wireless_scenario_value()
        if previous == current:
            return
        self._save_wireless_settings_for_scenario(previous)
        self._wireless_active_scenario = current
        self._set_active_wireless_scenario(current)
        self._load_wireless_settings_for_scenario(current)
        self._save_wireless_settings_for_scenario(current)
        self._apply_wireless_scenario_ui()
        self.settings.save()

    def _set_active_wireless_scenario(self, scenario: str) -> None:
        mode = WIRELESS_MODE_LEGACY if scenario == WIRELESS_SCENARIO_LEGACY else WIRELESS_MODE_MODERN
        self.settings.set("wireless_dashboard_scenario", scenario, save=False)
        self.settings.set("wireless_connection_mode", mode, save=False)
        self.settings.set("wireless_adb_mode", mode, save=False)

    def _load_wireless_settings_for_scenario(self, scenario: str) -> None:
        if scenario == WIRELESS_SCENARIO_LEGACY:
            host = str(self.settings.get("wireless_legacy_host", "") or self.settings.get("wireless_adb_host", "") or "")
            port = WIRELESS_LEGACY_PORT
        elif scenario == WIRELESS_SCENARIO_TV:
            host = str(
                self.settings.get("wireless_tv_host", "")
                or self.settings.get("wireless_modern_host", "")
                or self.settings.get("wireless_adb_host", "")
                or ""
            )
            port = self._settings_port(
                "wireless_tv_port",
                self._settings_port("wireless_modern_port", WIRELESS_LEGACY_PORT),
            )
        else:
            host = str(self.settings.get("wireless_modern_host", "") or self.settings.get("wireless_adb_host", "") or "")
            port = self._settings_port(
                "wireless_modern_port",
                self._settings_port("wireless_adb_port", WIRELESS_LEGACY_PORT),
            )

        self.wireless_host.blockSignals(True)
        self.wireless_port.blockSignals(True)
        try:
            self.wireless_host.setText(host)
            self.wireless_port.setValue(port)
        finally:
            self.wireless_host.blockSignals(False)
            self.wireless_port.blockSignals(False)

    def _apply_wireless_scenario_ui(self) -> None:
        scenario = self._wireless_scenario_value()
        scenario_index = {
            WIRELESS_SCENARIO_MODERN: 0,
            WIRELESS_SCENARIO_LEGACY: 1,
            WIRELESS_SCENARIO_TV: 2,
        }[scenario]
        self.wireless_actions_stack.setCurrentIndex(scenario_index)
        show_port = scenario != WIRELESS_SCENARIO_LEGACY
        self.wireless_port_label.setVisible(show_port)
        self.wireless_port.setVisible(show_port)

        if scenario == WIRELESS_SCENARIO_LEGACY:
            self.wireless_description.setText(
                "Use an authorized USB connection to enable the legacy ADB TCP/IP service. "
                "OpenADB always uses port 5555 in this scenario."
            )
            self.wireless_host_label.setText("Device IP")
            self.wireless_host.setPlaceholderText("For example 192.168.1.42")
            self.wireless_connect.setText("Connect by IP")
            self.wireless_disconnect.setText("Disconnect IP")
        elif scenario == WIRELESS_SCENARIO_TV:
            self.wireless_description.setText(
                "Find an Android TV over mDNS or enter the address shown in its Network/Wireless debugging settings. "
                "Pairing controls open separately only when needed."
            )
            self.wireless_host_label.setText("TV IP / host")
            self.wireless_host.setPlaceholderText("Android TV address")
            self.wireless_connect.setText("Connect to TV")
            self.wireless_disconnect.setText("Disconnect TV")
        else:
            self.wireless_description.setText(
                "Android 11+ Wireless debugging supports QR pairing or a temporary pairing code. "
                "Pairing-only fields open in a separate dialog."
            )
            self.wireless_host_label.setText("Device IP / host")
            self.wireless_host.setPlaceholderText("Phone IP address or hostname")
            self.wireless_connect.setText("Connect")
            self.wireless_disconnect.setText("Disconnect")
        self._update_wireless_summary()

    def _settings_port(self, key: str, fallback: int) -> int:
        try:
            value = int(str(self.settings.get(key, fallback)).strip())
        except (TypeError, ValueError):
            return fallback
        return value if 1 <= value <= 65535 else fallback

    def _pairing_port_for_scenario(self, scenario: str) -> int | None:
        key = "wireless_tv_pair_port" if scenario == WIRELESS_SCENARIO_TV else "wireless_modern_pair_port"
        value = str(self.settings.get(key, "") or self.settings.get("wireless_adb_pair_port", "") or "").strip()
        if not value.isdigit():
            return None
        port = int(value)
        return port if 1 <= port <= 65535 else None

    def _save_wireless_settings(self) -> None:
        scenario = self._wireless_scenario_value()
        self._wireless_active_scenario = scenario
        self._set_active_wireless_scenario(scenario)
        self._save_wireless_settings_for_scenario(scenario)
        self.settings.save()
        self._update_wireless_summary()

    def _save_wireless_settings_for_scenario(self, scenario: str) -> None:
        host = self.wireless_host.text().strip()
        if scenario == WIRELESS_SCENARIO_LEGACY:
            self.settings.set("wireless_legacy_host", host, save=False)
            self.settings.set("wireless_adb_host", host, save=False)
            self.settings.set("wireless_adb_port", WIRELESS_LEGACY_PORT, save=False)
            return

        port = int(self.wireless_port.value())
        if scenario == WIRELESS_SCENARIO_TV:
            pair_port = str(self.settings.get("wireless_tv_pair_port", "") or "")
            self.settings.set("wireless_tv_host", host, save=False)
            self.settings.set("wireless_tv_port", port, save=False)
        else:
            pair_port = str(self.settings.get("wireless_modern_pair_port", "") or "")
            self.settings.set("wireless_modern_host", host, save=False)
            self.settings.set("wireless_modern_port", port, save=False)
        self.settings.set("wireless_adb_host", host, save=False)
        self.settings.set("wireless_adb_port", port, save=False)
        self.settings.set("wireless_adb_pair_port", pair_port, save=False)

    def _update_wireless_summary(self) -> None:
        scenario = self._wireless_scenario_value()
        names = {
            WIRELESS_SCENARIO_MODERN: "Modern Wireless Debugging",
            WIRELESS_SCENARIO_LEGACY: "Legacy TCP/IP",
            WIRELESS_SCENARIO_TV: "Android TV",
        }
        host = self.wireless_host.text().strip() or "not configured"
        self.wireless_card.set_summary(f"{names[scenario]} · {host}")

    def _wireless_host_text(self) -> str:
        host = self.wireless_host.text().strip()
        if not host:
            QMessageBox.warning(self, "Wireless ADB", "Enter the device IP address or hostname first.")
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

    def _request_wireless_tcpip(self) -> None:
        self._save_wireless_settings()
        self.wireless_tcpip_requested.emit(WIRELESS_LEGACY_PORT)

    def _request_wireless_detect_ip(self) -> None:
        self._save_wireless_settings()
        self.wireless_detect_ip_requested.emit()

    def _request_wireless_connect(self) -> None:
        host = self._wireless_host_text()
        if not host:
            return
        self._save_wireless_settings()
        port = (
            WIRELESS_LEGACY_PORT
            if self._wireless_mode_value() == WIRELESS_MODE_LEGACY
            else int(self.wireless_port.value())
        )
        self.wireless_connect_requested.emit(host, port)

    def _request_wireless_pair(self) -> None:
        scenario = self._wireless_scenario_value()
        if scenario == WIRELESS_SCENARIO_LEGACY:
            QMessageBox.information(self, "Wireless ADB pair", "Legacy TCP/IP does not use Wireless debugging pairing.")
            return
        dialog = self._pairing_dialog_factory(
            host=self.wireless_host.text().strip(),
            pairing_port=self._pairing_port_for_scenario(scenario),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        host, pair_port, code = dialog.values()
        self.wireless_host.setText(host)
        pair_key = "wireless_tv_pair_port" if scenario == WIRELESS_SCENARIO_TV else "wireless_modern_pair_port"
        self.settings.set(pair_key, str(pair_port), save=False)
        self._save_wireless_settings()
        self.wireless_pair_requested.emit(host, pair_port, code)

    def _request_wireless_qr_pair(self) -> None:
        if self._wireless_scenario_value() == WIRELESS_SCENARIO_LEGACY:
            QMessageBox.information(self, "Wireless ADB QR pair", "Legacy TCP/IP does not use QR pairing.")
            return
        self._save_wireless_settings()
        self.wireless_qr_pair_requested.emit()

    def _request_wireless_scan(self) -> None:
        if self._wireless_scenario_value() != WIRELESS_SCENARIO_TV:
            QMessageBox.information(self, "Find Android TV", "Select the Android TV scenario to use mDNS discovery.")
            return
        self._save_wireless_settings()
        self.wireless_scan_requested.emit()

    def _request_wireless_disconnect(self) -> None:
        host = self.wireless_host.text().strip()
        port = (
            WIRELESS_LEGACY_PORT
            if self._wireless_mode_value() == WIRELESS_MODE_LEGACY
            else int(self.wireless_port.value())
        )
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
            port_arg = None if is_mdns_wireless_serial(host) else port
        self._save_wireless_settings()
        self.wireless_disconnect_requested.emit(host, port_arg)
