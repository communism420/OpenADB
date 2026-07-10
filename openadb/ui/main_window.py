from __future__ import annotations

import re
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from openadb import __version__
from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.fastboot import FastbootClient
from openadb.core.icon_extractor import IconExtractor
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.settings_manager import SettingsManager
from openadb.core.wireless_qr import generate_wireless_qr_payload
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.apps_page import AppsPage
from openadb.ui.backups_page import BackupsPage
from openadb.ui.branding import logo_icon, logo_pixmap
from openadb.ui.commands_page import CommandsPage
from openadb.ui.dashboard_page import DashboardPage
from openadb.ui.device_status_bar import DeviceStatusBar
from openadb.ui.file_manager_page import FileManagerPage
from openadb.ui.logs_page import LogsPage
from openadb.ui.settings_page import SettingsPage
from openadb.ui.style import apply_theme
from openadb.ui.widgets.device_picker_dialog import DevicePickerDialog
from openadb.ui.widgets.no_wheel_widgets import NoWheelListWidget as QListWidget
from openadb.ui.widgets.platform_tools_picker_dialog import PlatformToolsPickerDialog
from openadb.ui.widgets.wireless_qr_dialog import WirelessQrDialog
from openadb.ui.workers import Worker, start_worker


class MainWindow(QMainWindow):
    command_logged = Signal(object)

    def __init__(
        self,
        settings: SettingsManager,
        platform_tools: PlatformToolsManager,
        runner: CommandRunner,
        adb: ADBClient,
        fastboot: FastbootClient,
        device_manager: DeviceManager,
        backup_manager: BackupManager,
        icon_extractor: IconExtractor,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.platform_tools = platform_tools
        self.runner = runner
        self.adb = adb
        self.fastboot = fastboot
        self.device_manager = device_manager
        self.backup_manager = backup_manager
        self.icon_extractor = icon_extractor
        self._device_prompt_visible = False
        self._detecting_platform_tools = False
        self._wireless_qr_dialog: WirelessQrDialog | None = None
        self.setWindowTitle(f"OpenADB {__version__}")
        icon = logo_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1280, 820)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        self.device_bar = DeviceStatusBar(device_manager, settings)
        outer.addWidget(self.device_bar)
        body = QHBoxLayout()
        outer.addLayout(body, 1)

        side_panel = QWidget()
        side_panel.setObjectName("navPanel")
        side_panel.setFixedWidth(190)
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(6)

        brand = QWidget()
        brand.setObjectName("brandHeader")
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(10, 8, 10, 4)
        brand_layout.setSpacing(8)
        logo = QLabel()
        logo.setObjectName("brandLogo")
        pixmap = logo_pixmap(34)
        if not pixmap.isNull():
            logo.setPixmap(pixmap)
        logo.setFixedSize(38, 38)
        logo.setAlignment(Qt.AlignCenter)
        brand_title = QLabel("OpenADB")
        brand_title.setObjectName("brandTitle")
        brand_title.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        brand_version = QLabel(f"v{__version__}")
        brand_version.setObjectName("brandVersion")
        brand_version.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        brand_text = QWidget()
        brand_text_layout = QVBoxLayout(brand_text)
        brand_text_layout.setContentsMargins(0, 0, 0, 0)
        brand_text_layout.setSpacing(0)
        brand_text_layout.addWidget(brand_title)
        brand_text_layout.addWidget(brand_version)
        brand_layout.addWidget(logo)
        brand_layout.addWidget(brand_text, 1)
        side_layout.addWidget(brand)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        side_layout.addWidget(self.nav, 1)
        body.addWidget(side_panel)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        self.dashboard = DashboardPage(settings)
        self.apps_page = AppsPage(adb, backup_manager, device_manager, icon_extractor, settings)
        self.backups_page = BackupsPage(backup_manager, adb, device_manager)
        self.file_manager_page = FileManagerPage(adb, device_manager, settings)
        self.commands_page = CommandsPage(adb, fastboot, runner, settings, self.detect_platform_tools)
        self.logs_page = LogsPage(settings.logs_folder)
        self.settings_page = SettingsPage(settings)

        self.pages = {
            "Dashboard": self.dashboard,
            "Apps": self.apps_page,
            "Backups": self.backups_page,
            "File Manager": self.file_manager_page,
            "Commands": self.commands_page,
            "Logs": self.logs_page,
            "Settings": self.settings_page,
        }
        for name, widget in self.pages.items():
            self.nav.addItem(QListWidgetItem(name))
            self.stack.addWidget(widget)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(self._on_page_changed)
        self.nav.setCurrentRow(0)

        self.statusBar().showMessage("Ready")
        self.command_logged.connect(self.logs_page.append_result)
        self.runner.add_listener(self._on_command_logged)
        self._connect_signals()
        self._update_tools(platform_tools.active)
        QTimer.singleShot(100, lambda: self.detect_platform_tools(interactive=False))
        QTimer.singleShot(400, self.device_bar.refresh)

    def _connect_signals(self) -> None:
        self.device_bar.device_refreshed.connect(self._on_device_refreshed)
        self.device_bar.refresh_failed.connect(lambda message: self.statusBar().showMessage(message, 6000))
        self.dashboard.refresh_device_requested.connect(self.device_bar.refresh)
        self.dashboard.detect_tools_requested.connect(self.detect_platform_tools)
        self.dashboard.choose_tools_requested.connect(self.choose_platform_tools)
        self.dashboard.command_requested.connect(self.run_dashboard_command)
        self.dashboard.open_page_requested.connect(self.open_page)
        self.dashboard.wireless_tcpip_requested.connect(self.enable_wireless_tcpip)
        self.dashboard.wireless_detect_ip_requested.connect(self.detect_wireless_ip)
        self.dashboard.wireless_connect_requested.connect(self.connect_wireless_adb)
        self.dashboard.wireless_pair_requested.connect(self.pair_wireless_adb)
        self.dashboard.wireless_qr_pair_requested.connect(self.pair_wireless_adb_qr)
        self.dashboard.wireless_scan_requested.connect(self.scan_wireless_android_tv)
        self.dashboard.wireless_disconnect_requested.connect(self.disconnect_wireless_adb)
        self.settings_page.detect_tools_requested.connect(self.detect_platform_tools)
        self.settings_page.choose_tools_requested.connect(self.choose_platform_tools)
        self.settings_page.theme_changed.connect(lambda theme: apply_theme(QApplication.instance(), theme))
        self.settings_page.settings_changed.connect(self._settings_changed)
        self.settings_page.clear_icon_cache_requested.connect(self._clear_icon_cache)
        self.settings_page.reset_settings_and_caches_requested.connect(self._reset_all_settings_and_caches)

    def open_page(self, name: str) -> None:
        if name in self.pages:
            self.nav.setCurrentRow(list(self.pages).index(name))

    def _on_page_changed(self, index: int) -> None:
        if index < 0:
            return
        name = list(self.pages)[index]
        if name == "Apps" and self.device_manager.active.mode in {"ADB", "Recovery"} and not self.apps_page.apps:
            self.apps_page.refresh_apps()
        elif name == "Backups":
            self.backups_page.refresh()
        elif name == "File Manager":
            self.file_manager_page.refresh_all()

    def detect_platform_tools(self, interactive: bool = True) -> None:
        if self._detecting_platform_tools:
            return
        self._detecting_platform_tools = True
        self.statusBar().showMessage("Detecting Android Platform Tools...")
        worker = Worker(self.platform_tools.detect)
        worker.signals.result.connect(lambda candidates: self._platform_tools_detected(candidates, interactive))
        worker.signals.error.connect(lambda message, _trace: QMessageBox.warning(self, "Platform Tools", message))
        worker.signals.finished.connect(self._platform_tools_detection_finished)
        start_worker(self, self.device_bar.pool, worker)

    def _platform_tools_detection_finished(self) -> None:
        self._detecting_platform_tools = False
        self.statusBar().showMessage("Ready", 3000)

    def _platform_tools_detected(self, candidates: list[PlatformToolsInfo], interactive: bool) -> None:
        if interactive and len(candidates) > 1:
            dialog = PlatformToolsPickerDialog(candidates, self)
            if dialog.exec():
                selected = dialog.selected_info()
                if selected:
                    self.platform_tools.set_active(selected)
        elif interactive and not candidates:
            answer = QMessageBox.warning(
                self,
                "Platform Tools not found",
                "Android Platform Tools were not found. Choose the folder manually?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer == QMessageBox.Yes:
                self.choose_platform_tools()
        self._update_tools(self.platform_tools.active)

    def choose_platform_tools(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose platform-tools folder", self.platform_tools.active.folder_text)
        if not folder:
            return
        info = self.platform_tools.choose_folder(folder)
        self._update_tools(info)
        if not info.is_found:
            QMessageBox.warning(self, "Platform Tools", f"Selected folder status: {info.status}")

    def _update_tools(self, info: PlatformToolsInfo) -> None:
        self.dashboard.update_tools(info)
        self.settings_page.update_tools(info)
        self.statusBar().showMessage(f"Platform Tools: {info.status}", 5000)
        if info.has_adb:
            self.device_bar.restart_device_monitor()

    def _on_device_refreshed(self, device: DeviceInfo) -> None:
        saved_before_profile = str(self.settings.get("active_device_serial", "") or "")
        profile_changed = self._activate_device_profile(device)
        self.dashboard.update_device(device)
        self.apps_page.update_device_state(device)
        if self.stack.currentWidget() is self.file_manager_page:
            self.file_manager_page.refresh_all()
        if self.stack.currentWidget() is self.apps_page and device.mode in {"ADB", "Recovery"} and (profile_changed or not self.apps_page.apps):
            self.apps_page.refresh_apps()
        devices = self.device_manager.devices
        saved = saved_before_profile
        if len(devices) > 1 and not saved and not self._device_prompt_visible:
            self._device_prompt_visible = True
            dialog = DevicePickerDialog(devices, self)
            if dialog.exec():
                serial = dialog.selected_serial()
                if serial:
                    selected = self.device_manager.choose(serial)
                    profile_changed = self._activate_device_profile(selected)
                    self.device_bar.set_device(selected)
                    self.dashboard.update_device(selected)
                    self.apps_page.update_device_state(selected)
                    if profile_changed and self.stack.currentWidget() is self.apps_page and selected.mode in {"ADB", "Recovery"}:
                        self.apps_page.refresh_apps()
            self._device_prompt_visible = False

    def _activate_device_profile(self, device: DeviceInfo) -> bool:
        if not device.serial:
            return False
        display_name = " ".join(part for part in [device.manufacturer, device.model] if part).strip()
        changed = self.settings.activate_device_profile(device.serial, display_name, device.form_factor)
        if changed:
            self._settings_changed(profile_changed=True)
            self.apps_page.reset_for_device_profile()
            self.backups_page.refresh()
            self.statusBar().showMessage(f"Device profile: {device.serial}", 5000)
        return changed

    def run_dashboard_command(self, key: str) -> None:
        if key == "adb_reboot_sideload":
            answer = QMessageBox.warning(
                self,
                "Reboot to sideload",
                "Rebooting to sideload changes the device boot mode. Continue?",
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Ok:
                return
        commands = {
            "adb_reboot": lambda: self.adb.reboot(""),
            "adb_reboot_recovery": lambda: self.adb.reboot("recovery"),
            "adb_reboot_bootloader": lambda: self.adb.reboot("bootloader"),
            "adb_reboot_sideload": lambda: self.adb.reboot("sideload"),
            "adb_devices": lambda: self.adb.run_raw(["devices", "-l"], use_serial=False),
            "fastboot_devices": lambda: self.fastboot.run_raw(["devices"], use_serial=False),
        }
        fn = commands.get(key)
        if not fn:
            return
        worker = Worker(fn)
        worker.signals.result.connect(lambda result: QMessageBox.information(self, "Command", result.status))
        worker.signals.error.connect(lambda message, _trace: QMessageBox.critical(self, "Command", message))
        start_worker(self, self.device_bar.pool, worker)

    def enable_wireless_tcpip(self, port: int) -> None:
        self.dashboard.set_wireless_status(f"Enabling ADB TCP/IP mode on port {port}...")
        self._run_wireless_worker(
            lambda: self.adb.tcpip(port),
            "Enable TCP/IP",
            success_note=(
                f"ADB daemon was asked to listen on TCP port {port}. "
                "Keep the phone and PC on the same network, then use Find device Wi-Fi IP and Connect."
            ),
        )

    def detect_wireless_ip(self) -> None:
        self.dashboard.set_wireless_status("Detecting phone Wi-Fi IP address through ADB...")
        worker = Worker(self.adb.device_ip_addresses)
        worker.signals.result.connect(self._wireless_ips_detected)
        worker.signals.error.connect(lambda message, _trace: self._wireless_error("Find Wi-Fi IP", message))
        start_worker(self, self.device_bar.pool, worker)

    def _wireless_ips_detected(self, addresses: list[str]) -> None:
        if not addresses:
            message = "No usable Wi-Fi IPv4 address was detected. Keep USB connected and make sure Wi-Fi is enabled."
            self.dashboard.set_wireless_status(message)
            QMessageBox.warning(self, "Find Wi-Fi IP", message)
            return
        self.dashboard.set_wireless_addresses(addresses)
        QMessageBox.information(self, "Find Wi-Fi IP", "Detected address(es):\n" + "\n".join(addresses))

    def connect_wireless_adb(self, host: str, port: int) -> None:
        self.dashboard.set_wireless_status(f"Connecting to {host}:{port}...")
        self._run_wireless_worker(lambda: self.adb.connect_wireless(host, port), "Wireless ADB connect")

    def scan_wireless_android_tv(self) -> None:
        self.dashboard.set_wireless_status("Searching for Android TV / ADB over Wi-Fi services...")
        worker = Worker(lambda: self.adb.discover_wireless_connect_services(wait_seconds=2.5))
        worker.signals.result.connect(self._wireless_services_detected)
        worker.signals.error.connect(lambda message, _trace: self._wireless_error("Find Android TV", message))
        start_worker(self, self.device_bar.pool, worker)

    def _wireless_services_detected(self, services: list[dict[str, str]]) -> None:
        if not services:
            message = (
                "No wireless ADB service was found on the local network. On Android TV, enable Developer options -> "
                "Network debugging or Wireless debugging. If the TV shows an IP address and port, enter them manually "
                "and press Connect."
            )
            self.dashboard.set_wireless_status(message)
            QMessageBox.warning(self, "Find Android TV", message)
            return
        selected = services[0]
        if len(services) > 1:
            labels = [self._wireless_service_label(service) for service in services]
            item, ok = QInputDialog.getItem(
                self,
                "Find Android TV",
                "Choose discovered wireless ADB target:",
                labels,
                0,
                False,
            )
            if not ok or not item:
                self.dashboard.set_wireless_status("Android TV search cancelled.")
                return
            selected = services[labels.index(item)]
        self._connect_discovered_wireless_service(selected)

    def _connect_discovered_wireless_service(self, service: dict[str, str]) -> None:
        target = service.get("target", "") or service.get("connect_target", "")
        connect_target = service.get("connect_target", "") or target
        if target:
            self.dashboard.set_wireless_target(target)
        self.dashboard.set_wireless_status(f"Connecting to discovered Android TV / wireless ADB target: {target or connect_target}...")
        self._run_wireless_worker(lambda: self.adb.connect_wireless_target(connect_target), "Connect Android TV")

    @staticmethod
    def _wireless_service_label(service: dict[str, str]) -> str:
        name = service.get("name", "") or "ADB wireless service"
        target = service.get("target", "") or service.get("connect_target", "")
        source = service.get("source", "mDNS")
        return f"{name}   {target}   ({source})"

    def pair_wireless_adb(self, host: str, pair_port: int, code: str) -> None:
        self.dashboard.set_wireless_status(f"Pairing with {host}:{pair_port}...")
        self._run_wireless_worker(
            lambda: self.adb.pair_wireless(host, pair_port, code),
            "Wireless ADB pair",
            success_note="Pairing is complete. Now enter the Wireless debugging connection port and press Connect.",
        )

    def pair_wireless_adb_qr(self) -> None:
        try:
            payload = generate_wireless_qr_payload()
            dialog = WirelessQrDialog(payload, self)
        except Exception as exc:
            QMessageBox.critical(self, "Wireless ADB QR pair", str(exc))
            return

        cancel_event = threading.Event()
        self._wireless_qr_dialog = dialog
        self.dashboard.set_wireless_status("QR pairing is waiting for the phone to scan the code...")
        dialog.cancel_requested.connect(cancel_event.set)
        dialog.finished.connect(lambda _result: self._clear_wireless_qr_dialog(dialog))
        dialog.show()

        def run_qr_pair(progress_callback=None) -> CommandResult:
            return self.adb.pair_wireless_qr(
                payload.service_name,
                payload.password,
                timeout=90,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )

        worker = Worker(run_qr_pair)
        worker.signals.progress.connect(dialog.set_status)
        worker.signals.progress.connect(self.dashboard.set_wireless_status)
        worker.signals.result.connect(lambda result: self._wireless_qr_result(dialog, result))
        worker.signals.error.connect(lambda message, _trace: self._wireless_qr_error(dialog, message))
        start_worker(self, self.device_bar.pool, worker)

    def disconnect_wireless_adb(self, host: str, port: object) -> None:
        if host:
            self.dashboard.set_wireless_status(f"Disconnecting {host}:{port}...")
        else:
            self.dashboard.set_wireless_status("Disconnecting all wireless ADB connections...")
        self._run_wireless_worker(lambda: self.adb.disconnect_wireless(host, port), "Wireless ADB disconnect")

    def _run_wireless_worker(self, fn, title: str, success_note: str = "") -> None:
        worker = Worker(fn)
        worker.signals.result.connect(lambda result: self._wireless_result(title, result, success_note))
        worker.signals.error.connect(lambda message, _trace: self._wireless_error(title, message))
        start_worker(self, self.device_bar.pool, worker)

    def _wireless_result(self, title: str, result: CommandResult, success_note: str = "") -> None:
        message = self._command_result_message(result)
        if success_note and result.success:
            message = message + "\n\n" + success_note
        self.dashboard.set_wireless_status(result.status or ("Success" if result.success else "Command failed."))
        if result.success:
            QMessageBox.information(self, title, message)
            self.device_bar.refresh()
        else:
            QMessageBox.warning(self, title, message)

    def _wireless_error(self, title: str, message: str) -> None:
        self.dashboard.set_wireless_status(message)
        QMessageBox.critical(self, title, message)

    def _wireless_qr_result(self, dialog: WirelessQrDialog, result: CommandResult) -> None:
        dialog.mark_finished(result.success)
        dialog.set_status(result.status or ("Success" if result.success else "QR pairing failed."))
        self.dashboard.set_wireless_status(dialog.status.text())
        target = self._wireless_target_from_result(result)
        if target:
            self.dashboard.set_wireless_target(target)
        message = self._command_result_message(result)
        if result.success:
            self.device_bar.refresh()
            QMessageBox.information(self, "Wireless ADB QR pair", message)
        else:
            QMessageBox.warning(self, "Wireless ADB QR pair", message)

    def _wireless_qr_error(self, dialog: WirelessQrDialog, message: str) -> None:
        dialog.mark_finished(False)
        dialog.set_status(message)
        self.dashboard.set_wireless_status(message)
        QMessageBox.critical(self, "Wireless ADB QR pair", message)

    def _clear_wireless_qr_dialog(self, dialog: WirelessQrDialog) -> None:
        if self._wireless_qr_dialog is dialog:
            self._wireless_qr_dialog = None

    def _command_result_message(self, result: CommandResult) -> str:
        parts = [result.status]
        if result.stdout:
            parts.append(result.stdout.strip())
        if result.stderr:
            parts.append("stderr:\n" + result.stderr.strip())
        return "\n\n".join(part for part in parts if part) or "Command finished."

    def _wireless_target_from_result(self, result: CommandResult) -> str:
        text = "\n".join(part for part in [result.stdout, result.stderr, result.status] if part)
        bracketed = re.search(r"\[[0-9a-fA-F:.%]+\]:\d{1,5}", text)
        if bracketed:
            return bracketed.group(0)
        ipv4 = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b", text)
        return ipv4.group(0) if ipv4 else ""

    def _on_command_logged(self, result: CommandResult) -> None:
        self.command_logged.emit(result)

    def _settings_changed(self, profile_changed: bool = False) -> None:
        self.device_bar.configure_timer()
        self.backup_manager.refresh_root()
        self.runner.set_logs_folder(self.settings.logs_folder)
        self.logs_page.set_logs_folder(self.settings.logs_folder, clear_view=profile_changed)
        self.icon_extractor.refresh_root()
        self.apps_page.refresh_storage_roots()
        self.settings_page.reload_from_settings()
        self.dashboard.reload_from_settings()
        self.commands_page.reload_from_settings()
        self.file_manager_page.reload_from_settings()
        if profile_changed:
            apply_theme(QApplication.instance(), str(self.settings.get("theme", "System")))

    def _clear_icon_cache(self) -> None:
        self.icon_extractor.clear_cache()
        QMessageBox.information(self, "Icon cache", "Icon cache cleared.")

    def _reset_all_settings_and_caches(self) -> None:
        if getattr(self.apps_page, "_apps_loading", False) or getattr(self.apps_page, "_assets_loading", False):
            QMessageBox.information(
                self,
                "Reset settings and caches",
                "Apps data is still loading. Wait until it finishes, then reset settings and caches.",
            )
            return
        answer = QMessageBox.warning(
            self,
            "Reset all settings and caches",
            (
                "This will permanently delete all OpenADB settings and all cache data:\n\n"
                "- global settings\n"
                "- per-device settings\n"
                "- Apps metadata cache\n"
                "- app icon cache\n"
                "- APK label/cache temp files\n"
                "- ACBridge temporary cache\n\n"
                "Backups will not be deleted.\n\n"
                "Continue?"
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            self.statusBar().showMessage("Settings/cache reset cancelled.", 5000)
            return

        removed = self.settings.reset_settings_and_caches()
        self.platform_tools.active = PlatformToolsInfo()
        self._update_tools(self.platform_tools.active)
        self.apps_page.reset_for_device_profile()
        self._settings_changed(profile_changed=True)
        self.statusBar().showMessage("All settings and caches were reset.", 8000)
        detail = f"\n\nRemoved entries: {len(removed)}." if removed else ""
        QMessageBox.information(
            self,
            "Reset settings and caches",
            "All OpenADB settings and caches were reset. Backups were preserved." + detail,
        )

    def closeEvent(self, event) -> None:
        self.device_bar.stop_device_monitor()
        self.runner.remove_listener(self._on_command_logged)
        super().closeEvent(event)
