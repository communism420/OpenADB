from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
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
from openadb.ui.widgets.platform_tools_picker_dialog import PlatformToolsPickerDialog
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

        self.dashboard = DashboardPage()
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

    def _on_device_refreshed(self, device: DeviceInfo) -> None:
        saved_before_profile = str(self.settings.get("active_device_serial", "") or "")
        profile_changed = self._activate_device_profile(device)
        self.dashboard.update_device(device)
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
                    if profile_changed and self.stack.currentWidget() is self.apps_page and selected.mode in {"ADB", "Recovery"}:
                        self.apps_page.refresh_apps()
            self._device_prompt_visible = False

    def _activate_device_profile(self, device: DeviceInfo) -> bool:
        if not device.serial:
            return False
        display_name = " ".join(part for part in [device.manufacturer, device.model] if part).strip()
        changed = self.settings.activate_device_profile(device.serial, display_name)
        if changed:
            self._settings_changed(profile_changed=True)
            self.apps_page.reset_for_device_profile()
            self.backups_page.refresh()
            self.statusBar().showMessage(f"Device profile: {device.serial}", 5000)
        return changed

    def run_dashboard_command(self, key: str) -> None:
        commands = {
            "adb_reboot": lambda: self.adb.reboot(""),
            "adb_reboot_recovery": lambda: self.adb.reboot("recovery"),
            "adb_reboot_bootloader": lambda: self.adb.reboot("bootloader"),
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
        self.runner.remove_listener(self._on_command_logged)
        super().closeEvent(event)
