from __future__ import annotations

import re
import threading
import time

from PySide6.QtCore import QRect, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openadb import __version__
from openadb.core.adb import ADBClient, _looks_like_wireless_serial, is_mdns_wireless_serial
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable, WirelessConnectionAttempt
from openadb.core.fastboot import FastbootClient
from openadb.core.icon_extractor import IconExtractor
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.operations import OperationConflictError, OperationToken
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
from openadb.ui.dialogs import show_error_dialog
from openadb.ui.file_manager_page import FileManagerPage
from openadb.ui.logs_page import LogsPage
from openadb.ui.material_icons import material_icon
from openadb.ui.settings_page import SettingsPage
from openadb.ui.style import apply_theme
from openadb.ui.widgets.device_picker_dialog import DevicePickerDialog
from openadb.ui.widgets.no_wheel_widgets import NoWheelListWidget as QListWidget
from openadb.ui.widgets.platform_tools_picker_dialog import PlatformToolsPickerDialog
from openadb.ui.widgets.wireless_qr_dialog import WirelessQrDialog
from openadb.ui.workers import Worker, start_worker


class MainWindow(QMainWindow):
    command_logged = Signal(object)

    MINIMUM_WINDOW_SIZE = QSize(720, 480)
    DEFAULT_WINDOW_SIZE = QSize(1280, 820)
    NAV_EXPANDED_MIN_WIDTH = 164
    NAV_EXPANDED_MAX_WIDTH = 220
    NAV_COMPACT_MIN_WIDTH = 56
    NAV_COMPACT_MAX_WIDTH = 76

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
        self._detecting_platform_tools = False
        self._verifying_platform_tools = False
        self._platform_tools_detection_token: OperationToken | None = None
        self._platform_tools_verification_token: OperationToken | None = None
        self._wireless_qr_dialog: WirelessQrDialog | None = None
        self._wireless_qr_cancel_event: threading.Event | None = None
        self._wireless_attempt: WirelessConnectionAttempt | None = None
        self._wireless_token: OperationToken | None = None
        self._wireless_discovery_token: OperationToken | None = None
        self._dashboard_command_tokens: dict[str, OperationToken] = {}
        self._closing = False
        self._last_device_refresh_signature: tuple[str, ...] | None = None
        self.setWindowTitle(f"OpenADB {__version__}")
        icon = logo_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setMinimumSize(self.MINIMUM_WINDOW_SIZE)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        self.device_bar = DeviceStatusBar(device_manager, settings)
        outer.addWidget(self.device_bar)
        body = QHBoxLayout()
        outer.addLayout(body, 1)

        self.side_panel = QWidget()
        self.side_panel.setObjectName("navPanel")
        self.side_panel.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        side_layout = QVBoxLayout(self.side_panel)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(6)

        brand = QWidget()
        brand.setObjectName("brandHeader")
        self.brand_layout = QHBoxLayout(brand)
        self.brand_layout.setContentsMargins(10, 8, 10, 4)
        self.brand_layout.setSpacing(8)
        self.brand_logo = QLabel()
        self.brand_logo.setObjectName("brandLogo")
        pixmap = logo_pixmap(34)
        if not pixmap.isNull():
            self.brand_logo.setPixmap(pixmap)
        self.brand_logo.setMinimumSize(34, 34)
        self.brand_logo.setMaximumSize(38, 38)
        self.brand_logo.setAlignment(Qt.AlignCenter)
        brand_title = QLabel("OpenADB")
        brand_title.setObjectName("brandTitle")
        brand_title.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        brand_version = QLabel(f"v{__version__}")
        brand_version.setObjectName("brandVersion")
        brand_version.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.brand_text = QWidget()
        brand_text_layout = QVBoxLayout(self.brand_text)
        brand_text_layout.setContentsMargins(0, 0, 0, 0)
        brand_text_layout.setSpacing(0)
        brand_text_layout.addWidget(brand_title)
        brand_text_layout.addWidget(brand_version)
        self.brand_layout.addWidget(self.brand_logo)
        self.brand_layout.addWidget(self.brand_text, 1)
        side_layout.addWidget(brand)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(22, 22))
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        side_layout.addWidget(self.nav, 1)
        self.nav_toggle = QToolButton()
        self.nav_toggle.setObjectName("navToggle")
        self.nav_toggle.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.nav_toggle.clicked.connect(self.toggle_navigation)
        side_layout.addWidget(self.nav_toggle, 0, Qt.AlignCenter)
        body.addWidget(self.side_panel)

        self.stack = QStackedWidget()
        self.stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        body.addWidget(self.stack, 1)

        self.dashboard = DashboardPage(settings)
        self.apps_page = AppsPage(adb, backup_manager, device_manager, icon_extractor, settings)
        self.backups_page = BackupsPage(backup_manager, adb, device_manager)
        self.file_manager_page = FileManagerPage(adb, device_manager, settings)
        self.commands_page = CommandsPage(adb, fastboot, runner, settings, device_manager, self.detect_platform_tools)
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
        nav_icons = self._navigation_icons()
        for name, widget in self.pages.items():
            item = QListWidgetItem(nav_icons[name], name)
            item.setData(Qt.UserRole, name)
            item.setData(Qt.AccessibleTextRole, name)
            item.setToolTip(name)
            self.nav.addItem(item)
            self.stack.addWidget(widget)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.currentRowChanged.connect(self._on_page_changed)
        self.nav.setCurrentRow(0)

        self.statusBar().showMessage("Ready")
        self.command_logged.connect(self.logs_page.append_result)
        self.runner.add_listener(self._on_command_logged)
        self._connect_signals()
        self._update_tools(platform_tools.active)
        self._set_navigation_collapsed(
            bool(self.settings.get_global("navigation_collapsed", False)),
            persist=False,
        )
        self._restore_window_state()
        QTimer.singleShot(100, lambda: self.detect_platform_tools(interactive=False))
        QTimer.singleShot(400, self.device_bar.refresh)

    def _navigation_icons(self) -> dict[str, QIcon]:
        return {
            "Dashboard": material_icon("dashboard"),
            "Apps": material_icon("apps"),
            "Backups": material_icon("backup"),
            "File Manager": material_icon("folder"),
            "Commands": material_icon("terminal"),
            "Logs": material_icon("description"),
            "Settings": material_icon("settings"),
        }

    def refresh_material_icons(self) -> None:
        icons = self._navigation_icons()
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            name = str(item.data(Qt.UserRole) or "")
            if name in icons:
                item.setIcon(icons[name])
        self.nav_toggle.setIcon(material_icon("chevron_right" if self.navigation_collapsed else "chevron_left"))

    def toggle_navigation(self) -> None:
        self._set_navigation_collapsed(not self.navigation_collapsed, persist=True)

    def _set_navigation_collapsed(self, collapsed: bool, persist: bool) -> None:
        self.navigation_collapsed = bool(collapsed)
        if self.navigation_collapsed:
            self.side_panel.setMinimumWidth(self.NAV_COMPACT_MIN_WIDTH)
            self.side_panel.setMaximumWidth(self.NAV_COMPACT_MAX_WIDTH)
            self.brand_layout.setContentsMargins(0, 8, 0, 4)
            self.brand_text.hide()
        else:
            self.side_panel.setMinimumWidth(self.NAV_EXPANDED_MIN_WIDTH)
            self.side_panel.setMaximumWidth(self.NAV_EXPANDED_MAX_WIDTH)
            self.brand_layout.setContentsMargins(10, 8, 10, 4)
            self.brand_text.show()
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            name = str(item.data(Qt.UserRole) or "")
            item.setText("" if self.navigation_collapsed else name)
            alignment = Qt.AlignCenter if self.navigation_collapsed else Qt.AlignVCenter | Qt.AlignLeft
            item.setTextAlignment(alignment)
        action = "Expand" if self.navigation_collapsed else "Collapse"
        self.nav_toggle.setIcon(material_icon("chevron_right" if self.navigation_collapsed else "chevron_left"))
        self.nav_toggle.setToolTip(f"{action} navigation")
        self.nav_toggle.setAccessibleName(f"{action} navigation")
        self.side_panel.setProperty("collapsed", self.navigation_collapsed)
        self.nav.setProperty("collapsed", self.navigation_collapsed)
        self.side_panel.style().unpolish(self.side_panel)
        self.side_panel.style().polish(self.side_panel)
        self.nav.style().unpolish(self.nav)
        self.nav.style().polish(self.nav)
        self.side_panel.updateGeometry()
        if persist:
            self.settings.set_global_values({"navigation_collapsed": self.navigation_collapsed})

    def _restore_window_state(self) -> None:
        width = self._safe_int(
            self.settings.get_global("window_width", self.DEFAULT_WINDOW_SIZE.width()),
            self.DEFAULT_WINDOW_SIZE.width(),
        )
        height = self._safe_int(
            self.settings.get_global("window_height", self.DEFAULT_WINDOW_SIZE.height()),
            self.DEFAULT_WINDOW_SIZE.height(),
        )
        saved_x = self.settings.get_global("window_x", None)
        saved_y = self.settings.get_global("window_y", None)
        screens = self._available_screen_geometries()
        if not screens:
            self.resize(
                max(width, self.minimumWidth()),
                max(height, self.minimumHeight()),
            )
        else:
            primary = screens[0]
            if saved_x is None or saved_y is None:
                width = min(max(width, self.minimumWidth()), primary.width())
                height = min(max(height, self.minimumHeight()), primary.height())
                candidate = QRect(0, 0, width, height)
                candidate.moveCenter(primary.center())
            else:
                candidate = QRect(
                    self._safe_int(saved_x, primary.x()),
                    self._safe_int(saved_y, primary.y()),
                    width,
                    height,
                )
            self.setGeometry(self._bounded_window_geometry(candidate, screens))
        if bool(self.settings.get_global("window_maximized", False)):
            self.setWindowState(self.windowState() | Qt.WindowMaximized)

    def _available_screen_geometries(self) -> list[QRect]:
        primary = QGuiApplication.primaryScreen()
        ordered = ([primary] if primary is not None else []) + [
            screen for screen in QGuiApplication.screens() if screen is not primary
        ]
        return [
            screen.availableGeometry()
            for screen in ordered
            if screen is not None and screen.availableGeometry().isValid()
        ]

    @classmethod
    def _bounded_window_geometry(cls, candidate: QRect, screens: list[QRect]) -> QRect:
        if not screens:
            return QRect(candidate)
        intersections = [candidate.intersected(screen) for screen in screens]
        areas = [max(0, rect.width()) * max(0, rect.height()) for rect in intersections]
        screen = screens[areas.index(max(areas))] if max(areas) > 0 else screens[0]
        width = min(max(candidate.width(), cls.MINIMUM_WINDOW_SIZE.width()), screen.width())
        height = min(max(candidate.height(), cls.MINIMUM_WINDOW_SIZE.height()), screen.height())
        if max(areas) == 0:
            result = QRect(0, 0, width, height)
            result.moveCenter(screen.center())
            return result
        max_x = screen.right() - width + 1
        max_y = screen.bottom() - height + 1
        x = min(max(candidate.x(), screen.left()), max_x)
        y = min(max(candidate.y(), screen.top()), max_y)
        return QRect(x, y, width, height)

    @staticmethod
    def _safe_int(value: object, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _save_window_state(self) -> None:
        geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        if not geometry.isValid():
            geometry = QRect(self.pos(), self.size())
        self.settings.set_global_values(
            {
                "window_x": geometry.x(),
                "window_y": geometry.y(),
                "window_width": geometry.width(),
                "window_height": geometry.height(),
                "window_maximized": self.isMaximized(),
                "navigation_collapsed": self.navigation_collapsed,
            }
        )

    def _connect_signals(self) -> None:
        self.device_bar.device_refreshed.connect(self._on_device_refreshed)
        self.device_bar.refresh_failed.connect(lambda message: self.statusBar().showMessage(message, 6000))
        self.device_bar.choose_device_requested.connect(self.choose_active_device)
        self.apps_page.refresh_device_requested.connect(self.device_bar.refresh)
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
        self.settings_page.verify_tools_requested.connect(self.verify_selected_platform_tools)
        self.settings_page.theme_changed.connect(lambda theme: apply_theme(QApplication.instance(), theme))
        self.settings_page.settings_changed.connect(self._settings_changed)
        self.settings_page.clear_icon_cache_requested.connect(self._clear_icon_cache)
        self.settings_page.clear_temp_requested.connect(self._clear_temporary_files)
        self.settings_page.reset_ui_settings_requested.connect(self._reset_ui_settings)
        self.settings_page.reset_settings_and_caches_requested.connect(self._reset_all_settings_and_caches)
        self.commands_page.open_logs_requested.connect(lambda: self.open_page("Logs"))
        self.commands_page.status_message.connect(self.statusBar().showMessage)
        self.commands_page.settings_changed.connect(self._settings_changed)

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
        if self._closing or self._detecting_platform_tools or self._verifying_platform_tools:
            return
        try:
            token = self.device_manager.operations.register(
                "platform-tools-detection",
                device_context=None,
                conflict_group="platform-tools-inspection",
            )
        except (OperationConflictError, RuntimeError) as exc:
            self.statusBar().showMessage(str(exc), 6000)
            return
        self._platform_tools_detection_token = token
        self._detecting_platform_tools = True
        self.statusBar().showMessage("Detecting Android Platform Tools...")
        # Selection and settings writes belong to the guarded UI callback. The
        # scanner itself must remain read-only so a late shutdown result cannot
        # change the selected installation from its worker thread.
        worker = Worker(lambda: self.platform_tools.detect(select=False))
        worker.signals.result.connect(
            lambda candidates: self._platform_tools_detected(
                candidates,
                interactive,
                token,
            )
        )
        worker.signals.error.connect(
            lambda message, _trace: self._platform_tools_detection_failed(token, message)
        )
        worker.signals.finished.connect(
            lambda: self._platform_tools_detection_finished(token)
        )
        try:
            started = start_worker(
                self,
                self.device_bar.pool,
                worker,
                operation_registry=self.device_manager.operations,
                operation_token=token,
            )
        except Exception as exc:
            self._platform_tools_detection_finished(token)
            if not self._closing:
                QMessageBox.warning(self, "Platform Tools", f"Detection could not start: {exc}")
            return
        if started is False:
            self._platform_tools_detection_finished(token)

    def _platform_tools_detection_finished(
        self,
        token: OperationToken | None = None,
    ) -> None:
        if token is not None:
            self.device_manager.operations.finish(token)
            if self._platform_tools_detection_token is not token:
                return
            self._platform_tools_detection_token = None
        self._detecting_platform_tools = False
        if not self._closing:
            self.statusBar().showMessage("Ready", 3000)

    def _platform_tools_detected(
        self,
        candidates: list[PlatformToolsInfo],
        interactive: bool,
        token: OperationToken | None = None,
    ) -> None:
        if token is not None and not self._platform_tools_callback_is_current(
            token,
            self._platform_tools_detection_token,
        ):
            return
        selection_cancelled = False
        if interactive and len(candidates) > 1:
            dialog = PlatformToolsPickerDialog(candidates, self)
            accepted = bool(dialog.exec())
            if not self._platform_tools_result_can_continue(token):
                return
            if accepted:
                selected = dialog.selected_info()
                if selected:
                    self.platform_tools.set_active(selected)
            else:
                selection_cancelled = True
        elif interactive and len(candidates) == 1:
            self.platform_tools.set_active(candidates[0])
        elif interactive and not candidates:
            self.platform_tools.active = PlatformToolsInfo()
            answer = QMessageBox.warning(
                self,
                "Platform Tools not found",
                "Android Platform Tools were not found. Choose the folder manually?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if not self._platform_tools_result_can_continue(token):
                return
            if answer == QMessageBox.Yes:
                self._choose_platform_tools_folder()
        elif not interactive:
            if candidates:
                selected = self.platform_tools._select_saved_or_best(candidates)
                self.platform_tools.set_active(
                    selected,
                    save=selected.is_found or selected.has_adb,
                )
            else:
                self.platform_tools.active = PlatformToolsInfo()
        if not self._platform_tools_result_can_continue(token):
            return
        self._update_tools(self.platform_tools.active)
        if selection_cancelled:
            self.settings_page.set_verification_result("Search finished; selection was cancelled and left unchanged.")
        else:
            self.settings_page.set_verification_result(
                f"Find result: {self.platform_tools.active.status}. "
                f"Source: {self.platform_tools.active.source or 'none'}."
            )

    def _platform_tools_detection_failed(
        self,
        token: OperationToken,
        message: str,
    ) -> None:
        if not self._platform_tools_callback_is_current(
            token,
            self._platform_tools_detection_token,
        ):
            return
        QMessageBox.warning(self, "Platform Tools", message)

    def choose_platform_tools(self) -> None:
        if self._detecting_platform_tools or self._verifying_platform_tools:
            return
        self._choose_platform_tools_folder()

    def _choose_platform_tools_folder(self) -> None:
        if self._closing:
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose platform-tools folder", self.platform_tools.active.folder_text)
        if self._closing or not folder:
            return
        info = self.platform_tools.choose_folder(folder)
        self._update_tools(info)
        self.settings_page.set_verification_result(f"Folder check: {info.status}.")
        if not info.is_found:
            QMessageBox.warning(self, "Platform Tools", f"Selected folder status: {info.status}")

    def verify_selected_platform_tools(self) -> None:
        if self._closing or self._verifying_platform_tools or self._detecting_platform_tools:
            return
        active = self.platform_tools.active
        if active.folder is None:
            self.settings_page.set_verification_result("Verification not run: no installation is selected.")
            QMessageBox.information(
                self,
                "Verify Platform Tools",
                "No Platform Tools folder is selected. Use Find Platform Tools or Choose folder first.",
            )
            return
        try:
            token = self.device_manager.operations.register(
                "platform-tools-verification",
                device_context=None,
                conflict_group="platform-tools-inspection",
            )
        except (OperationConflictError, RuntimeError) as exc:
            self.settings_page.set_verification_result(f"Verification not started: {exc}")
            self.statusBar().showMessage(str(exc), 6000)
            return
        self._platform_tools_verification_token = token
        self._verifying_platform_tools = True
        self.statusBar().showMessage("Verifying selected Platform Tools installation...")
        source = active.source or "Selected installation"
        worker = Worker(lambda: self.platform_tools.inspect_folder(active.folder, source))
        worker.signals.result.connect(
            lambda info: self._platform_tools_verified(info, token)
        )
        worker.signals.error.connect(
            lambda message, trace: self._platform_tools_verification_failed(
                message,
                trace,
                token,
            )
        )
        worker.signals.finished.connect(
            lambda: self._platform_tools_verification_finished(token)
        )
        try:
            started = start_worker(
                self,
                self.device_bar.pool,
                worker,
                operation_registry=self.device_manager.operations,
                operation_token=token,
            )
        except Exception as exc:
            self._platform_tools_verification_finished(token)
            if not self._closing:
                QMessageBox.warning(
                    self,
                    "Verify Platform Tools",
                    f"Verification could not start: {exc}",
                )
            return
        if started is False:
            self._platform_tools_verification_finished(token)

    def _platform_tools_verified(
        self,
        info: PlatformToolsInfo,
        token: OperationToken | None = None,
    ) -> None:
        if token is not None and not self._platform_tools_callback_is_current(
            token,
            self._platform_tools_verification_token,
        ):
            return
        self.platform_tools.set_active(info, save=info.has_adb or info.has_fastboot)
        self._update_tools(info)
        works = []
        if info.adb_works:
            works.append("adb responded")
        if info.fastboot_works:
            works.append("fastboot responded")
        detail = ", ".join(works) if works else "executables did not respond"
        self.settings_page.set_verification_result(f"Verification result: {info.status}; {detail}.")

    def _platform_tools_verification_failed(
        self,
        message: str,
        _trace: str,
        token: OperationToken | None = None,
    ) -> None:
        if token is not None and not self._platform_tools_callback_is_current(
            token,
            self._platform_tools_verification_token,
        ):
            return
        self.settings_page.set_verification_result(f"Verification failed: {message}")
        QMessageBox.warning(self, "Verify Platform Tools", message)

    def _platform_tools_verification_finished(
        self,
        token: OperationToken | None = None,
    ) -> None:
        if token is not None:
            self.device_manager.operations.finish(token)
            if self._platform_tools_verification_token is not token:
                return
            self._platform_tools_verification_token = None
        self._verifying_platform_tools = False
        if not self._closing:
            self.statusBar().showMessage("Platform Tools verification finished.", 5000)

    def _platform_tools_callback_is_current(
        self,
        token: OperationToken,
        current_token: OperationToken | None,
    ) -> bool:
        return bool(
            not self._closing
            and current_token is token
            and not token.cancelled
            and self.device_manager.operations.contains(token)
        )

    def _platform_tools_result_can_continue(
        self,
        token: OperationToken | None,
    ) -> bool:
        """Recheck shutdown after a nested picker/message-box event loop.

        A worker's queued ``finished`` signal may run while ``dialog.exec()`` is
        open and legitimately remove the token from the registry. Cancellation,
        rather than registry membership, is therefore the stable post-dialog
        shutdown signal.
        """

        return bool(not self._closing and (token is None or not token.cancelled))

    def _update_tools(self, info: PlatformToolsInfo) -> None:
        self.dashboard.update_tools(info)
        self.settings_page.update_tools(info)
        self.commands_page.update_tools_state()
        self.statusBar().showMessage(f"Platform Tools: {info.status}", 5000)
        if info.has_adb:
            self.device_bar.restart_device_monitor()

    def _on_device_refreshed(self, device: DeviceInfo) -> None:
        profile_changed = self._activate_device_profile(device)
        profile_ready = profile_changed is not None
        signature = (
            device.serial,
            device.mode,
            device.state,
            device.transport_id,
            device.model,
            device.android_version,
            device.sdk_version,
        )
        device_changed = signature != getattr(self, "_last_device_refresh_signature", None)
        self._last_device_refresh_signature = signature
        self.dashboard.update_device(device)
        self.apps_page.update_device_state(device)
        invalidate_file_view = getattr(
            self.file_manager_page,
            "invalidate_stale_device_view",
            None,
        )
        if callable(invalidate_file_view):
            invalidate_file_view()
        commands_page = getattr(self, "commands_page", None)
        if commands_page is not None:
            commands_page.update_device_state(device)
        if (
            profile_ready
            and self.stack.currentWidget() is self.file_manager_page
            and (profile_changed or device_changed)
        ):
            self.file_manager_page.refresh_all()
        if (
            profile_ready
            and self.stack.currentWidget() is self.apps_page
            and device.mode in {"ADB", "Recovery"}
            and (profile_changed or not self.apps_page.apps)
        ):
            self.apps_page.refresh_apps()

    def choose_active_device(self) -> None:
        devices = list(self.device_manager.devices)
        if not devices:
            QMessageBox.information(self, "Choose active device", "No Android devices are currently detected.")
            return
        dialog = DevicePickerDialog(
            devices,
            active_serial=self.device_manager.active.serial,
            parent=self,
        )
        if not dialog.exec():
            return
        serial = dialog.selected_serial()
        if not serial:
            return
        selected = self.device_manager.choose(serial)
        self.device_bar.set_device(selected)
        self._on_device_refreshed(selected)

    def _activate_device_profile(self, device: DeviceInfo) -> bool | None:
        if not device.serial:
            return False
        display_name = " ".join(part for part in [device.manufacturer, device.model] if part).strip()
        try:
            changed = self.settings.activate_device_profile(
                device.serial,
                display_name,
                device.form_factor,
            )
            try:
                current_context = self.device_manager.capture_context()
                profile_needs_sync = current_context.serial != device.serial
            except DeviceContextUnavailable:
                profile_needs_sync = True
            profile_changed = changed or profile_needs_sync
            if profile_changed:
                self._settings_changed(profile_changed=True)
                self.apps_page.reset_for_device_profile()
                self.statusBar().showMessage(f"Device profile: {device.serial}", 5000)
        except (OSError, RuntimeError, ValueError) as exc:
            self.device_manager.invalidate_profile("device profile activation failed")
            self.apps_page.reset_for_device_profile()
            self.backups_page.reset_for_device_profile()
            message = f"OpenADB could not activate the profile for {device.serial}: {exc}"
            self.statusBar().showMessage(message, 10000)
            QMessageBox.warning(self, "Device profile", message)
            return None
        return profile_changed

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
        reboot_targets = {
            "adb_reboot": "",
            "adb_reboot_recovery": "recovery",
            "adb_reboot_bootloader": "bootloader",
            "adb_reboot_sideload": "sideload",
        }
        if key in reboot_targets:
            try:
                context = self.device_manager.require_context(("ADB", "Recovery"))
            except DeviceContextUnavailable as exc:
                self.statusBar().showMessage(str(exc), 6000)
                return
            adb = self.adb.for_context(context)
            args = ["reboot"]
            if reboot_targets[key]:
                args.append(reboot_targets[key])
            self._start_dashboard_command(
                lambda cancel_event: adb.run_raw(args, timeout=60, cancel_event=cancel_event),
                context=context,
            )
            return
        if key == "adb_devices":
            self._start_dashboard_command(
                lambda cancel_event: self.adb.run_raw(
                    ["devices", "-l"],
                    use_serial=False,
                    cancel_event=cancel_event,
                ),
                context=None,
            )
        elif key == "fastboot_devices":
            self._start_dashboard_command(
                lambda cancel_event: self.fastboot.run_raw(
                    ["devices"],
                    use_serial=False,
                    cancel_event=cancel_event,
                ),
                context=None,
            )

    def _start_dashboard_command(self, fn, *, context: DeviceContext | None) -> None:
        try:
            token = self.device_manager.operations.register(
                "dashboard-command",
                device_context=context,
                conflict_group="device-command" if context is not None else "",
                conflict_groups=(f"device-exclusive:{context.serial}",) if context is not None else (),
            )
        except (OperationConflictError, RuntimeError) as exc:
            self.statusBar().showMessage(str(exc), 6000)
            return
        if context is not None and not self.device_manager.is_context_current(context):
            token.cancel("device context changed before dashboard command registration completed")
            self.device_manager.operations.finish(token)
            self.statusBar().showMessage(
                "The active device changed before the command could start. Review it and try again.",
                7000,
            )
            return
        self._dashboard_command_tokens[token.operation_id] = token
        worker = Worker(lambda: self._run_dashboard_operation(token, context, fn))
        worker.signals.result.connect(
            lambda result: self._dashboard_command_result(token, result)
        )
        worker.signals.error.connect(
            lambda message, _trace: self._dashboard_command_error(token, message)
        )
        worker.signals.finished.connect(lambda: self._dashboard_command_finished(token))
        started = start_worker(
            self,
            self.device_bar.pool,
            worker,
            operation_registry=self.device_manager.operations,
            operation_token=token,
        )
        if started is False:
            self._dashboard_command_finished(token)

    def _run_dashboard_operation(self, token: OperationToken, context: DeviceContext | None, fn):
        if token.cancelled:
            return None
        if context is not None and not self.device_manager.is_context_current(context):
            token.cancel("device context changed before dashboard worker execution")
            return None
        return fn(token.cancel_event)

    def _dashboard_command_result(self, token: OperationToken, result: CommandResult) -> None:
        if not self._operation_callback_is_current(token):
            self.statusBar().showMessage(
                "A command finished for a device that is no longer active; its result was not applied.",
                7000,
            )
            return
        QMessageBox.information(self, "Command", self._command_result_message(result))

    def _dashboard_command_error(self, token: OperationToken, message: str) -> None:
        if not self._operation_callback_is_current(token):
            return
        show_error_dialog(self, "Command failed", message, self.settings.logs_folder)

    def _dashboard_command_finished(self, token: OperationToken) -> None:
        if self._dashboard_command_tokens.get(token.operation_id) is token:
            del self._dashboard_command_tokens[token.operation_id]

    def _operation_callback_is_current(self, token: OperationToken) -> bool:
        if (
            self._closing
            or token.cancelled
            or self._dashboard_command_tokens.get(token.operation_id) is not token
        ):
            return False
        context = token.device_context
        return context is None or self.device_manager.is_context_current(context)

    def enable_wireless_tcpip(self, port: int) -> None:
        self.dashboard.set_wireless_status(f"Enabling ADB TCP/IP mode on port {port}...")
        try:
            context = self.device_manager.require_context(("ADB", "Recovery"))
        except DeviceContextUnavailable as exc:
            self._wireless_error("Enable TCP/IP", str(exc))
            return
        adb = self.adb.for_context(context)
        self._run_device_dashboard_worker(
            lambda cancel_event: adb.run_raw(
                ["tcpip", str(port)],
                timeout=30,
                cancel_event=cancel_event,
            ),
            context,
            "Enable TCP/IP",
            success_note=(
                f"ADB daemon was asked to listen on TCP port {port}. "
                "Keep the phone and PC on the same network, then use Find device Wi-Fi IP and Connect."
            ),
        )

    def detect_wireless_ip(self) -> None:
        self.dashboard.set_wireless_status("Detecting phone Wi-Fi IP address through ADB...")
        try:
            context = self.device_manager.require_context(("ADB", "Recovery"))
        except DeviceContextUnavailable as exc:
            self._wireless_error("Find Wi-Fi IP", str(exc))
            return
        adb = self.adb.for_context(context)
        self._run_device_dashboard_worker(
            lambda cancel_event: adb.device_ip_addresses(cancel_event=cancel_event),
            context,
            "Find Wi-Fi IP",
            result_callback=self._wireless_ips_detected,
        )

    def _run_device_dashboard_worker(
        self,
        fn,
        context: DeviceContext,
        title: str,
        *,
        success_note: str = "",
        result_callback=None,
    ) -> None:
        try:
            token = self.device_manager.operations.register(
                "dashboard-device-operation",
                device_context=context,
                conflict_group="device-command",
                conflict_groups=(f"device-exclusive:{context.serial}",),
            )
        except (OperationConflictError, RuntimeError) as exc:
            self.dashboard.set_wireless_status(str(exc))
            self.statusBar().showMessage(str(exc), 6000)
            return
        if not self.device_manager.is_context_current(context):
            token.cancel("device context changed before dashboard operation registration completed")
            self.device_manager.operations.finish(token)
            self.dashboard.set_wireless_status(
                "The active device changed before the operation could start. Review it and try again."
            )
            return
        self._dashboard_command_tokens[token.operation_id] = token
        worker = Worker(lambda: self._run_dashboard_operation(token, context, fn))

        def apply_result(result) -> None:
            if not self._operation_callback_is_current(token):
                self.statusBar().showMessage(
                    "The operation finished for a device that is no longer active; its result was not applied.",
                    7000,
                )
                return
            if result_callback is not None:
                result_callback(result)
            else:
                self._wireless_result(title, result, success_note)

        worker.signals.result.connect(apply_result)
        worker.signals.error.connect(
            lambda message, _trace: self._dashboard_device_error(token, title, message)
        )
        worker.signals.finished.connect(lambda: self._dashboard_command_finished(token))
        started = start_worker(
            self,
            self.device_bar.pool,
            worker,
            operation_registry=self.device_manager.operations,
            operation_token=token,
        )
        if started is False:
            self._dashboard_command_finished(token)

    def _dashboard_device_error(
        self,
        token: OperationToken,
        title: str,
        message: str,
    ) -> None:
        if self._operation_callback_is_current(token):
            self._wireless_error(title, message)

    def _wireless_ips_detected(self, addresses: list[str]) -> None:
        if not addresses:
            message = "No usable Wi-Fi IPv4 address was detected. Keep USB connected and make sure Wi-Fi is enabled."
            self.dashboard.set_wireless_status(message)
            QMessageBox.warning(self, "Find Wi-Fi IP", message)
            return
        self.dashboard.set_wireless_addresses(addresses)
        QMessageBox.information(self, "Find Wi-Fi IP", "Detected address(es):\n" + "\n".join(addresses))

    def connect_wireless_adb(self, host: str, port: int) -> None:
        if is_mdns_wireless_serial(host):
            self.dashboard.set_wireless_status(f"Connecting to {host}...")
            self._run_wireless_worker(
                lambda cancel_event: self.adb.connect_wireless_target(
                    host,
                    cancel_event=cancel_event,
                ),
                "Wireless ADB connect",
                action="connect",
                expected_host=host,
                connect_target=host,
                expected_ready_serials=(host,),
            )
            return
        self.dashboard.set_wireless_status(f"Connecting to {host}:{port}...")
        target = self._format_wireless_target(host, port)
        self._run_wireless_worker(
            lambda cancel_event: self.adb.connect_wireless(
                host,
                port,
                cancel_event=cancel_event,
            ),
            "Wireless ADB connect",
            action="connect",
            expected_host=host,
            expected_connect_port=port,
            connect_target=target,
            expected_ready_serials=(target,),
        )

    def scan_wireless_android_tv(self) -> None:
        self.dashboard.set_wireless_status("Searching for Android TV / ADB over Wi-Fi services...")
        try:
            token = self.device_manager.operations.register(
                "wireless-discovery",
                device_context=None,
                conflict_group="wireless-discovery",
            )
        except (OperationConflictError, RuntimeError) as exc:
            self.dashboard.set_wireless_status(str(exc))
            return
        self._wireless_discovery_token = token
        worker = Worker(
            lambda: self.adb.discover_wireless_connect_services(
                wait_seconds=2.5,
                cancel_event=token.cancel_event,
            )
        )
        worker.signals.result.connect(
            lambda services: self._wireless_services_detected(services, token)
        )
        worker.signals.error.connect(
            lambda message, _trace: self._wireless_discovery_error(token, message)
        )
        worker.signals.finished.connect(lambda: self._wireless_discovery_finished(token))
        started = start_worker(
            self,
            self.device_bar.pool,
            worker,
            operation_registry=self.device_manager.operations,
            operation_token=token,
        )
        if started is False:
            self._wireless_discovery_finished(token)

    def _wireless_services_detected(
        self,
        services: list[dict[str, str]],
        token: OperationToken | None = None,
    ) -> None:
        if token is not None and not self._wireless_discovery_is_current(token):
            return
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

    def _wireless_discovery_error(self, token: OperationToken, message: str) -> None:
        if self._wireless_discovery_is_current(token):
            self._wireless_error("Find Android TV", message)

    def _wireless_discovery_finished(self, token: OperationToken) -> None:
        if self._wireless_discovery_token is token:
            self._wireless_discovery_token = None

    def _wireless_discovery_is_current(self, token: OperationToken) -> bool:
        return bool(
            self._wireless_discovery_token is token
            and not token.cancelled
            and not self._closing
        )

    def _connect_discovered_wireless_service(self, service: dict[str, str]) -> None:
        target = service.get("target", "") or service.get("connect_target", "")
        connect_target = service.get("connect_target", "") or target
        if target:
            self.dashboard.set_wireless_target(target)
        self.dashboard.set_wireless_status(f"Connecting to discovered Android TV / wireless ADB target: {target or connect_target}...")
        self._run_wireless_worker(
            lambda cancel_event: self.adb.connect_wireless_target(
                connect_target,
                cancel_event=cancel_event,
            ),
            "Connect Android TV",
            action="connect",
            expected_host=target or connect_target,
            connect_target=connect_target,
            expected_ready_serials=(connect_target, target),
        )

    @staticmethod
    def _wireless_service_label(service: dict[str, str]) -> str:
        name = service.get("name", "") or "ADB wireless service"
        target = service.get("target", "") or service.get("connect_target", "")
        source = service.get("source", "mDNS")
        return f"{name}   {target}   ({source})"

    def pair_wireless_adb(self, host: str, pair_port: int, code: str) -> None:
        self.dashboard.set_wireless_status(f"Pairing with {host}:{pair_port}...")
        self._run_wireless_worker(
            lambda cancel_event: self.adb.pair_wireless(
                host,
                pair_port,
                code,
                cancel_event=cancel_event,
            ),
            "Wireless ADB pair",
            success_note="Pairing is complete. Now enter the Wireless debugging connection port and press Connect.",
            action="pair",
            expected_host=host,
            expected_pair_port=pair_port,
            pairing_target=self._format_wireless_target(host, pair_port),
        )

    def pair_wireless_adb_qr(self) -> None:
        if self._wireless_qr_dialog is not None:
            self._wireless_qr_dialog.show()
            self._wireless_qr_dialog.raise_()
            self._wireless_qr_dialog.activateWindow()
            return
        if self._wireless_attempt is not None:
            self.dashboard.set_wireless_status(
                "Another Wireless ADB connection attempt is still running. Cancel it or wait for completion."
            )
            return
        try:
            payload = generate_wireless_qr_payload()
            dialog = WirelessQrDialog(payload, self)
        except Exception as exc:
            show_error_dialog(self, "Wireless ADB QR pairing could not start", str(exc), self.settings.logs_folder)
            return

        started = self._begin_wireless_attempt(
            action="qr",
            expected_host="",
            pairing_target=payload.service_name,
        )
        if started is None:
            dialog.deleteLater()
            return
        attempt, token = started
        self._wireless_qr_dialog = dialog
        self.device_bar.set_offline_reconnect_suspended(True)
        self.dashboard.set_wireless_status("QR pairing is waiting for the phone to scan the code...")
        dialog.cancel_requested.connect(lambda: token.cancel("user cancelled"))
        dialog.finished.connect(
            lambda _result: self._clear_wireless_qr_dialog(dialog, attempt)
        )
        dialog.show()

        def run_qr_pair(progress_callback=None) -> CommandResult:
            return self.adb.pair_wireless_qr(
                payload.service_name,
                payload.password,
                timeout=90,
                progress_callback=progress_callback,
                cancel_event=token.cancel_event,
            )

        worker = Worker(run_qr_pair)
        worker.signals.progress.connect(
            lambda message: self._wireless_qr_progress(attempt, token, dialog, message)
        )
        worker.signals.result.connect(
            lambda result: self._wireless_qr_result(
                dialog,
                result,
                attempt=attempt,
                token=token,
            )
        )
        worker.signals.error.connect(
            lambda message, _trace: self._wireless_qr_error(
                dialog,
                message,
                attempt=attempt,
                token=token,
            )
        )
        worker.signals.finished.connect(
            lambda: self._wireless_qr_finished(attempt, token)
        )
        worker_started = start_worker(
            self,
            self.device_bar.pool,
            worker,
            operation_registry=self.device_manager.operations,
            operation_token=token,
        )
        if worker_started is False:
            self._wireless_qr_finished(attempt, token)

    def _wireless_qr_progress(
        self,
        attempt: WirelessConnectionAttempt,
        token: OperationToken,
        dialog: WirelessQrDialog,
        message: str,
    ) -> None:
        if not self._wireless_attempt_is_current(attempt, token):
            return
        dialog.set_status(message)
        self.dashboard.set_wireless_status(message)

    def disconnect_wireless_adb(self, host: str, port: object) -> None:
        active_serial = str(self.device_manager.active.serial or "").strip()
        if _looks_like_wireless_serial(active_serial):
            host, port = active_serial, None
        elif is_mdns_wireless_serial(host):
            port = None
        if host:
            target = host if port is None else f"{host}:{port}"
            self.dashboard.set_wireless_status(f"Disconnecting {target}...")
        else:
            self.dashboard.set_wireless_status("Disconnecting all wireless ADB connections...")
        self._run_wireless_worker(
            lambda cancel_event: self.adb.disconnect_wireless(
                host,
                port,
                cancel_event=cancel_event,
            ),
            "Wireless ADB disconnect",
            action="disconnect",
            expected_host=host,
            expected_connect_port=port if isinstance(port, int) else None,
            connect_target=target if host else "",
        )

    def _run_wireless_worker(
        self,
        fn,
        title: str,
        success_note: str = "",
        *,
        action: str = "connect",
        expected_host: str = "",
        expected_pair_port: int | None = None,
        expected_connect_port: int | None = None,
        pairing_target: str = "",
        connect_target: str = "",
        expected_ready_serials: tuple[str, ...] = (),
    ) -> None:
        started = self._begin_wireless_attempt(
            action=action,
            expected_host=expected_host,
            expected_pair_port=expected_pair_port,
            expected_connect_port=expected_connect_port,
            pairing_target=pairing_target,
            connect_target=connect_target,
            expected_ready_serials=expected_ready_serials,
        )
        if started is None:
            return
        attempt, token = started

        def run_attempt() -> CommandResult | None:
            if token.cancelled:
                return None
            result = fn(token.cancel_event)
            if action == "connect" and result.success:
                self._wait_for_expected_wireless_transport(attempt, token, result)
            return result

        worker = Worker(run_attempt)
        worker.signals.result.connect(
            lambda result: self._wireless_result(
                title,
                result,
                success_note,
                attempt=attempt,
                token=token,
            )
        )
        worker.signals.error.connect(
            lambda message, _trace: self._wireless_error(
                title,
                message,
                attempt=attempt,
                token=token,
            )
        )
        worker.signals.finished.connect(
            lambda: self._wireless_attempt_finished(attempt, token)
        )
        worker_started = start_worker(
            self,
            self.device_bar.pool,
            worker,
            operation_registry=self.device_manager.operations,
            operation_token=token,
        )
        if worker_started is False:
            self._wireless_attempt_finished(attempt, token)

    def _begin_wireless_attempt(
        self,
        *,
        action: str,
        expected_host: str = "",
        expected_pair_port: int | None = None,
        expected_connect_port: int | None = None,
        pairing_target: str = "",
        connect_target: str = "",
        expected_ready_serials: tuple[str, ...] = (),
    ) -> tuple[WirelessConnectionAttempt, OperationToken] | None:
        if self._wireless_attempt is not None:
            self.dashboard.set_wireless_status(
                "Another Wireless ADB connection attempt is still running. Cancel it or wait for completion."
            )
            return None
        try:
            token = self.device_manager.operations.register(
                "wireless-connection",
                device_context=None,
                conflict_group="wireless-connection",
            )
        except (OperationConflictError, RuntimeError) as exc:
            self.dashboard.set_wireless_status(str(exc))
            return None
        scenario_getter = getattr(self.dashboard, "wireless_scenario_value", None)
        scenario = scenario_getter() if callable(scenario_getter) else "modern"
        ready_serials: list[str] = []
        for value in expected_ready_serials:
            value = str(value or "").strip()
            if value and value not in ready_serials:
                ready_serials.append(value)
            if value and is_mdns_wireless_serial(value):
                alternate = value.rstrip(".") if value.endswith(".") else value + "."
                if alternate not in ready_serials:
                    ready_serials.append(alternate)
            elif value.casefold().startswith("adb-") and ":" not in value and " " not in value:
                mdns_serial = value.rstrip(".") + "._adb-tls-connect._tcp"
                for candidate in (mdns_serial, mdns_serial + "."):
                    if candidate not in ready_serials:
                        ready_serials.append(candidate)
        attempt = WirelessConnectionAttempt(
            attempt_id=token.operation_id,
            action=action,
            scenario=scenario,
            expected_host=str(expected_host or ""),
            expected_pair_port=expected_pair_port,
            expected_connect_port=expected_connect_port,
            pairing_target=str(pairing_target or ""),
            connect_target=str(connect_target or ""),
            expected_ready_serials=tuple(ready_serials),
            started_generation=self.device_manager.current_generation,
        )
        self._wireless_attempt = attempt
        self._wireless_token = token
        self._wireless_qr_cancel_event = token.cancel_event if action == "qr" else None
        if hasattr(self.dashboard, "set_wireless_busy"):
            self.dashboard.set_wireless_busy(True)
        return attempt, token

    def _wait_for_expected_wireless_transport(
        self,
        attempt: WirelessConnectionAttempt,
        token: OperationToken,
        result: CommandResult,
    ) -> None:
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and not token.cancelled:
            devices = self.adb.list_devices(cancel_event=token.cancel_event)
            if any(self._attempt_accepts_transport(attempt, device) for device in devices):
                return
            time.sleep(0.35)
        if token.cancelled:
            result.success = False
            result.error_type = "cancelled"
            result.status = "Wireless ADB connection cancelled"
            return
        result.success = False
        result.error_type = "connection_not_ready"
        result.status = (
            "ADB accepted the connection, but the expected Wireless debugging transport "
            "did not become ready."
        )

    @staticmethod
    def _attempt_accepts_transport(
        attempt: WirelessConnectionAttempt,
        device: DeviceInfo,
    ) -> bool:
        accepts = getattr(attempt, "accepts_transport", None)
        if callable(accepts):
            return bool(accepts(device.serial, device.state))
        return bool(device.state == "device" and attempt.accepts_ready_serial(device.serial))

    def _wireless_attempt_is_current(
        self,
        attempt: WirelessConnectionAttempt,
        token: OperationToken,
        *,
        allow_user_cancel: bool = False,
    ) -> bool:
        if (
            self._closing
            or self._wireless_attempt is not attempt
            or self._wireless_token is not token
        ):
            return False
        if token.cancelled and not (
            allow_user_cancel and token.cancellation_reason == "user cancelled"
        ):
            return False
        return True

    def _wireless_attempt_finished(
        self,
        attempt: WirelessConnectionAttempt,
        token: OperationToken,
    ) -> None:
        if self._wireless_attempt is not attempt or self._wireless_token is not token:
            return
        self._wireless_attempt = None
        self._wireless_token = None
        self._wireless_qr_cancel_event = None
        if hasattr(self.dashboard, "set_wireless_busy"):
            self.dashboard.set_wireless_busy(False)

    @staticmethod
    def _format_wireless_target(host: str, port: int | None) -> str:
        host = str(host or "").strip()
        if not host or port is None:
            return host
        if is_mdns_wireless_serial(host):
            return host
        if host.startswith("["):
            return host if re.search(r"\]:\d+$", host) else f"{host}:{port}"
        if host.count(":") == 1 and re.search(r":\d+$", host):
            return host
        if ":" in host:
            return f"[{host}]:{port}"
        return f"{host}:{port}"

    def _wireless_result(
        self,
        title: str,
        result: CommandResult,
        success_note: str = "",
        *,
        attempt: WirelessConnectionAttempt | None = None,
        token: OperationToken | None = None,
    ) -> None:
        if attempt is not None and token is not None and not self._wireless_attempt_is_current(attempt, token):
            return
        message = self._command_result_message(result)
        if success_note and result.success:
            message = message + "\n\n" + success_note
        self.dashboard.set_wireless_status(result.status or ("Success" if result.success else "Command failed."))
        if result.success:
            QMessageBox.information(self, title, message)
            self.device_bar.refresh()
        else:
            QMessageBox.warning(self, title, message)

    def _wireless_error(
        self,
        title: str,
        message: str,
        *,
        attempt: WirelessConnectionAttempt | None = None,
        token: OperationToken | None = None,
    ) -> None:
        if attempt is not None and token is not None and not self._wireless_attempt_is_current(attempt, token):
            return
        self.dashboard.set_wireless_status(message)
        show_error_dialog(self, title, message, self.settings.logs_folder)

    def _wireless_qr_result(
        self,
        dialog: WirelessQrDialog,
        result: CommandResult,
        *,
        attempt: WirelessConnectionAttempt | None = None,
        token: OperationToken | None = None,
    ) -> None:
        legacy_call = attempt is None or token is None
        attempt = attempt or self._wireless_attempt
        token = token or self._wireless_token
        if attempt is not None and token is not None and not self._wireless_attempt_is_current(
            attempt,
            token,
            allow_user_cancel=True,
        ):
            return
        if token is not None and token.cancelled:
            if self._wireless_qr_dialog is dialog:
                dialog.mark_finished(False)
                dialog.set_status("QR pairing cancelled")
            return
        dialog.mark_finished(result.success)
        dialog.set_status(result.status or ("Success" if result.success else "QR pairing failed."))
        self.dashboard.set_wireless_status(dialog.status.text())
        target = self._wireless_target_from_result(result)
        if target:
            self.dashboard.set_wireless_target(target)
        message = self._command_result_message(result)
        if result.success:
            QMessageBox.information(self, "Wireless ADB QR pair", message)
        else:
            QMessageBox.warning(self, "Wireless ADB QR pair", message)
        if legacy_call:
            self.device_bar.refresh_after_wireless_pairing()

    def _wireless_qr_error(
        self,
        dialog: WirelessQrDialog,
        message: str,
        *,
        attempt: WirelessConnectionAttempt | None = None,
        token: OperationToken | None = None,
    ) -> None:
        legacy_call = attempt is None or token is None
        attempt = attempt or self._wireless_attempt
        token = token or self._wireless_token
        if attempt is not None and token is not None and not self._wireless_attempt_is_current(
            attempt,
            token,
            allow_user_cancel=True,
        ):
            return
        if token is not None and token.cancelled:
            return
        dialog.mark_finished(False)
        dialog.set_status(message)
        self.dashboard.set_wireless_status(message)
        show_error_dialog(self, "Wireless ADB QR pairing failed", message, self.settings.logs_folder)
        if legacy_call:
            self.device_bar.refresh_after_wireless_pairing()

    def _wireless_qr_finished(
        self,
        attempt: WirelessConnectionAttempt,
        token: OperationToken,
    ) -> None:
        if self._wireless_attempt is not attempt or self._wireless_token is not token:
            return
        self.device_bar.refresh_after_wireless_pairing()
        self._wireless_attempt_finished(attempt, token)

    def _clear_wireless_qr_dialog(
        self,
        dialog: WirelessQrDialog,
        attempt: WirelessConnectionAttempt | None = None,
    ) -> None:
        if attempt is not None and self._wireless_attempt is not attempt:
            return
        if self._wireless_qr_dialog is dialog:
            self._wireless_qr_dialog = None

    def _command_result_message(self, result: CommandResult) -> str:
        def result_text(name: str) -> str:
            value = getattr(result, name, "")
            return value.strip() if isinstance(value, str) else ""

        status = result_text("status")
        stdout = result_text("stdout")
        stderr = result_text("stderr")
        log_warning = result_text("log_warning")
        parts = [status]
        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append("stderr:\n" + stderr)
        if log_warning:
            parts.append("Log warning:\n" + log_warning)
        return "\n\n".join(part for part in parts if part) or "Command finished."

    def _wireless_target_from_result(self, result: CommandResult) -> str:
        text = "\n".join(part for part in [result.stdout, result.stderr, result.status] if part)
        for token in text.split():
            candidate = token.strip("'\"()[]{}<>,;").rstrip(".")
            if is_mdns_wireless_serial(candidate):
                return candidate
        bracketed = re.search(r"\[[0-9a-fA-F:.%]+\]:\d{1,5}", text)
        if bracketed:
            return bracketed.group(0)
        ipv4 = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b", text)
        return ipv4.group(0) if ipv4 else ""

    def _on_command_logged(self, result: CommandResult) -> None:
        if result.device_generation is not None:
            try:
                context = self.device_manager.capture_context()
            except Exception:
                return
            if (
                result.device_generation != context.generation
                or result.device_serial != context.serial
            ):
                return
        self.command_logged.emit(result)

    def _settings_changed(self, profile_changed: bool = False) -> None:
        previous_backup_root = self.backup_manager.root
        self.device_manager.notify_profile_changed(
            str(getattr(self.settings, "active_profile_serial", "") or ""),
            str(getattr(self.settings, "active_profile_kind", "") or ""),
        )
        self.device_bar.configure_timer()
        self.backup_manager.refresh_root()
        backup_root_changed = previous_backup_root != self.backup_manager.root
        self.runner.set_logs_folder(self.settings.logs_folder)
        self.logs_page.set_logs_folder(self.settings.logs_folder, clear_view=profile_changed)
        self.icon_extractor.refresh_root()
        self.apps_page.refresh_storage_roots()
        self.settings_page.reload_from_settings()
        self.dashboard.reload_from_settings()
        self.commands_page.reload_from_settings()
        self.file_manager_page.reload_from_settings()
        if backup_root_changed:
            self.backups_page.reset_for_device_profile()
            self.backups_page.refresh()
        if profile_changed:
            apply_theme(QApplication.instance(), str(self.settings.get("theme", "System")))

    def _clear_icon_cache(self) -> None:
        self.icon_extractor.clear_cache()
        QMessageBox.information(self, "Icon cache", "Icon cache cleared.")

    def _clear_temporary_files(self) -> None:
        folder = str(self.settings.get("temp_folder", ""))
        answer = QMessageBox.warning(
            self,
            "Clear temporary files",
            (
                "Delete all files in the active OpenADB temporary folder?\n\n"
                f"{folder}\n\nAPK backups and logs will not be deleted."
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            self.statusBar().showMessage("Temporary file cleanup cancelled.", 5000)
            return
        removed = self.settings.clear_temporary_files(expected_path=folder)
        if removed is None:
            QMessageBox.warning(
                self,
                "Clear temporary files",
                (
                    "The temporary folder changed while confirmation was open, or it could not be "
                    "verified as OpenADB-owned. Nothing was deleted."
                ),
            )
            return
        QMessageBox.information(
            self,
            "Temporary files",
            f"Temporary files cleared. Removed entries: {len(removed)}.",
        )

    def _reset_ui_settings(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Reset UI settings",
            (
                "Reset theme, window/navigation layout, Dashboard expansion, application filters, and "
                "File Manager view state for the global configuration and active device profile?\n\n"
                "Platform Tools, storage folders, safety preferences, profiles, caches, logs, and APK "
                "backups will be preserved."
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            self.statusBar().showMessage("UI settings reset cancelled.", 5000)
            return
        reset_keys = self.settings.reset_ui_settings()
        self._set_navigation_collapsed(False, persist=False)
        self.apps_page.reload_filter_state()
        self.file_manager_page.restore_ui_state()
        self._settings_changed(profile_changed=True)
        self.showNormal()
        self._restore_window_state()
        self.statusBar().showMessage("UI settings were reset.", 8000)
        QMessageBox.information(
            self,
            "Reset UI settings",
            f"UI settings were reset without deleting profiles or files. Reset values: {len(reset_keys)}.",
        )

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
                "This affects the global configuration and every Phone/TV device profile.\n\n"
                "Preserved:\n"
                "- APK backup folders and their contents\n"
                "- log files\n"
                "- files outside verified OpenADB cache/temp folders\n\n"
                "Deleting APK backups is not part of this reset.\n\n"
                "Continue?"
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            self.statusBar().showMessage("Settings/cache reset cancelled.", 5000)
            return

        self.device_manager.invalidate_profile("settings and profile data were reset")
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
        if self._closing:
            super().closeEvent(event)
            return
        self._closing = True
        self.file_manager_page.save_ui_state()
        self._save_window_state()
        worker_owners = (
            self,
            self.device_bar,
            self.apps_page,
            self.backups_page,
            self.file_manager_page,
            self.commands_page,
        )
        for owner in worker_owners:
            owner._workers_shutting_down = True
        self.device_manager.operations.shutdown()
        self.commands_page.cancel_running_command()
        self.file_manager_page.cancel_active_transfers()
        if self._wireless_qr_cancel_event is not None:
            self._wireless_qr_cancel_event.set()
        self.device_bar.stop_device_monitor()
        self.runner.remove_listener(self._on_command_logged)
        self.runner.shutdown()
        pool = QThreadPool.globalInstance()
        pool.clear()
        pool.waitForDone(2000)
        QApplication.processEvents()
        super().closeEvent(event)
