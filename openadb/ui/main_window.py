from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from inspect import getattr_static

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
from openadb.core.acbridge import (
    ACBridgeClient,
    ACBridgePrivilegeResult,
    ACBridgeUpdateResult,
)
from openadb.core.adb import (
    ADBClient,
    _looks_like_wireless_serial,
    is_mdns_wireless_serial,
)
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.device_context import (
    DeviceContext,
    DeviceContextUnavailable,
    WirelessConnectionAttempt,
)
from openadb.core.fastboot import FastbootClient
from openadb.core.icon_extractor import IconExtractor
from openadb.core.operations import OperationConflictError, OperationToken
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.privilege import (
    PrivilegeBackend,
    PrivilegeManager,
    PrivilegeStatus,
)
from openadb.core.settings_manager import (
    SettingsManager,
    read_privilege_backend_setting,
)
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
from openadb.ui.dialogs import exec_bounded_message_box, show_error_dialog
from openadb.ui.file_manager_page import (
    PASSIVE_APPS_OPERATION_OWNERS,
    FileManagerPage,
)
from openadb.ui.logs_page import LogsPage
from openadb.ui.material_icons import material_icon
from openadb.ui.settings_page import SettingsPage
from openadb.ui.system_theme import SystemThemeController
from openadb.ui.widgets.device_picker_dialog import DevicePickerDialog
from openadb.ui.widgets.elided_label import ElidedLabel
from openadb.ui.widgets.no_wheel_widgets import NoWheelListWidget as QListWidget
from openadb.ui.widgets.platform_tools_picker_dialog import PlatformToolsPickerDialog
from openadb.ui.widgets.privilege_selector import PrivilegeModeSelector
from openadb.ui.widgets.wireless_qr_dialog import WirelessQrDialog
from openadb.ui.windows_taskbar import WindowsTaskbarProgress
from openadb.ui.workers import Worker, start_worker


@dataclass(frozen=True, slots=True)
class _PrivilegeHandshakeResult:
    """One global access transition, retaining shell and bridge decisions."""

    shell: PrivilegeStatus
    bridge: ACBridgePrivilegeResult | None = None


class MainWindow(QMainWindow):
    command_logged = Signal(object)
    settings_recovery_available = Signal()
    privilege_status_changed = Signal(object)
    privilege_runtime_invalidated = Signal()

    MINIMUM_WINDOW_SIZE = QSize(720, 480)
    DEFAULT_WINDOW_SIZE = QSize(1280, 820)
    NAV_EXPANDED_MIN_WIDTH = 164
    NAV_EXPANDED_MAX_WIDTH = 220
    NAV_COMPACT_MIN_WIDTH = 56
    NAV_COMPACT_MAX_WIDTH = 76
    AUTOMATIC_SHIZUKU_MAX_ATTEMPTS = 2

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
        self.privilege_manager = PrivilegeManager(adb, settings, device_manager)
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
        self._acbridge_update_token: OperationToken | None = None
        self._last_acbridge_update_key: tuple[str, int] | None = None
        self._pending_acbridge_update_context: DeviceContext | None = None
        self._acbridge_update_retry_key: tuple[str, int] | None = None
        self._acbridge_update_attempts: dict[tuple[str, int], int] = {}
        self._pending_acbridge_feature_refresh: set[str] = set()
        self._acbridge_maintenance_ui_busy = False
        self._privilege_token: OperationToken | None = None
        self._pending_privilege_recheck = False
        self._last_privilege_connection_key: tuple[str, int | None] | None = None
        self._last_automatic_shizuku_key: tuple[str, int] | None = None
        self._automatic_shizuku_inflight_key: tuple[str, int] | None = None
        self._pending_automatic_shizuku_context: DeviceContext | None = None
        self._automatic_shizuku_scheduled_key: tuple[str, int] | None = None
        self._automatic_shizuku_attempts: dict[tuple[str, int], int] = {}
        self._automatic_shizuku_failure_status: PrivilegeStatus | None = None
        self._automatic_shizuku_ui_busy = False
        self._privilege_feature_barrier_busy = False
        self._privilege_operation_busy_message = ""
        self._privilege_transition_blocker_ids: set[str] = set()
        self._privilege_recheck_callback_scheduled = False
        self._privilege_barrier_waits_for_recheck = False
        self._privilege_transition_drain_scheduled = False
        self._acbridge_privilege_result: ACBridgePrivilegeResult | None = None
        self._acbridge_privilege_key: tuple[PrivilegeBackend, str, int | None] | None = None
        self._privilege_profile_available = False
        self._last_privilege_backend = PrivilegeBackend.normalize(
            read_privilege_backend_setting(
                self.settings,
                profile_available=False,
            )
        )
        self._closing = False
        self._settings_recovery_callback = self.settings_recovery_available.emit
        self._settings_recovery_dialog_active = False
        self._settings_recovery_follow_up_pending = False
        self._settings_recovery_timer = QTimer(self)
        self._settings_recovery_timer.setSingleShot(True)
        self._settings_recovery_timer.setInterval(0)
        self._settings_recovery_timer.timeout.connect(
            self._show_pending_settings_recovery
        )
        self._last_device_refresh_signature: tuple[object, ...] | None = None
        app = QApplication.instance()
        if app is None:
            raise RuntimeError("MainWindow requires a QApplication instance")
        self.system_theme_controller = SystemThemeController(app, parent=self)
        self.setWindowTitle(f"OpenADB {__version__}")
        icon = logo_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.setMinimumSize(self.MINIMUM_WINDOW_SIZE)
        self.taskbar_progress = WindowsTaskbarProgress(lambda: int(self.winId()))

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
        self.apps_page = AppsPage(
            adb,
            backup_manager,
            device_manager,
            icon_extractor,
            settings,
            privilege_manager=self.privilege_manager,
        )
        self.backups_page = BackupsPage(
            backup_manager,
            adb,
            device_manager,
            privilege_manager=self.privilege_manager,
        )
        self.file_manager_page = FileManagerPage(
            adb,
            device_manager,
            settings,
            privilege_manager=self.privilege_manager,
        )
        self.commands_page = CommandsPage(
            adb,
            fastboot,
            runner,
            settings,
            device_manager,
            self.detect_platform_tools,
            privilege_manager=self.privilege_manager,
        )
        self.logs_page = LogsPage(settings.logs_folder)
        self.settings_page = SettingsPage(settings)
        self.privilege_status_changed.connect(self._apply_privilege_status)
        self._privilege_status_callback = self.privilege_status_changed.emit
        self.privilege_manager.add_status_listener(self._privilege_status_callback)
        self.privilege_runtime_invalidated.connect(
            self._recover_privilege_status_after_runtime_invalidation,
            Qt.QueuedConnection,
        )
        self._privilege_invalidation_callback = (
            self.privilege_runtime_invalidated.emit
        )
        self.privilege_manager.add_invalidation_listener(
            self._privilege_invalidation_callback
        )

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

        self.statusBar().setAccessibleName("OpenADB operation status")
        self.statusBar().messageChanged.connect(self._status_bar_message_changed)
        self.statusBar().showMessage("Ready")
        self._install_privilege_selector()
        self.command_logged.connect(self.logs_page.append_result)
        self.runner.add_listener(self._on_command_logged)
        self.settings_recovery_available.connect(
            self._schedule_settings_recovery_warning,
            Qt.QueuedConnection,
        )
        self._connect_signals()
        self._apply_privilege_status(None)
        self._set_privilege_profile_available(False)
        self._update_tools(platform_tools.active)
        self._set_navigation_collapsed(
            bool(self.settings.get_global("navigation_collapsed", False)),
            persist=False,
        )
        self._restore_window_state()
        self.system_theme_controller.start(str(self.settings.get("theme", "System")))
        # Register only after successful construction so an exception above
        # cannot retain a partially initialized window through SettingsManager.
        self.settings.add_recovery_listener(self._settings_recovery_callback)
        self._schedule_settings_recovery_warning()
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

    def _status_bar_message_changed(self, message: str) -> None:
        """Keep clipped transient status text available to mouse and AT users."""

        value = str(message or "")
        status_bar = self.statusBar()
        status_bar.setToolTip(value)
        status_bar.setAccessibleDescription(value)

    def _install_privilege_selector(self) -> None:
        panel = QWidget(self)
        panel.setObjectName("privilegeStatusPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(6, 0, 2, 0)
        layout.setSpacing(6)
        caption = QLabel("Access")
        caption.setObjectName("privilegeStatusCaption")
        selector = PrivilegeModeSelector(panel, compact=True)
        selector.set_backend(self._configured_privilege_backend())
        caption.setBuddy(selector)
        status = ElidedLabel("", panel, elide_mode=Qt.ElideRight)
        status.setObjectName("privilegeRuntimeStatus")
        status.setAccessibleName("Privilege access status")
        layout.addWidget(caption)
        layout.addWidget(selector)
        layout.addWidget(status, 1)
        self.privilege_status_panel = panel
        self.privilege_mode_selector = selector
        self.privilege_runtime_status = status
        self.statusBar().addPermanentWidget(panel)

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
        self.privilege_mode_selector.backend_changed.connect(
            self._privilege_backend_changed
        )
        self.device_bar.device_refreshed.connect(self._on_device_refreshed)
        self.device_bar.refresh_failed.connect(lambda message: self.statusBar().showMessage(message, 6000))
        self.device_bar.choose_device_requested.connect(self.choose_active_device)
        self.apps_page.refresh_device_requested.connect(self.device_bar.refresh)
        self.dashboard.refresh_device_requested.connect(self.device_bar.refresh)
        self.dashboard.reconnect_device_requested.connect(self.device_bar.reconnect_offline)
        self.dashboard.detect_tools_requested.connect(self.detect_platform_tools)
        self.dashboard.choose_tools_requested.connect(self.choose_platform_tools)
        self.dashboard.verify_tools_requested.connect(self.verify_selected_platform_tools)
        self.dashboard.command_requested.connect(self.run_dashboard_command)
        self.dashboard.open_page_requested.connect(self.open_page)
        self.dashboard.open_commands_requested.connect(self.open_dashboard_commands)
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
        self.settings_page.theme_changed.connect(self.system_theme_controller.set_theme)
        self.settings_page.settings_changed.connect(self._settings_changed)
        self.settings_page.check_privilege_requested.connect(
            self.check_privilege_access
        )
        self.settings_page.request_shizuku_permission_requested.connect(
            self.request_shizuku_permission
        )
        self.settings_page.open_shizuku_requested.connect(self.open_shizuku)
        self.settings_page.clear_icon_cache_requested.connect(self._clear_icon_cache)
        self.settings_page.clear_temp_requested.connect(self._clear_temporary_files)
        self.settings_page.reset_ui_settings_requested.connect(self._reset_ui_settings)
        self.settings_page.reset_settings_and_caches_requested.connect(self._reset_all_settings_and_caches)
        self.commands_page.open_logs_requested.connect(lambda: self.open_page("Logs"))
        self.commands_page.status_message.connect(self.statusBar().showMessage)
        self.commands_page.settings_changed.connect(self._settings_changed)
        self.commands_page.check_privilege_requested.connect(
            self.check_privilege_access
        )
        self.commands_page.request_shizuku_permission_requested.connect(
            self.request_shizuku_permission
        )
        self.commands_page.open_shizuku_requested.connect(self.open_shizuku)
        self.commands_page.privilege_status_invalidated.connect(
            self._invalidate_privilege_status
        )
        self.file_manager_page.passive_apps_preempted.connect(
            self._queue_apps_refresh_after_file_manager_preemption
        )
        self.file_manager_page.transfer_started.connect(self.taskbar_progress.begin)
        self.file_manager_page.transfer_progress_changed.connect(
            self.taskbar_progress.apply_update
        )
        self.file_manager_page.transfer_finished.connect(self.taskbar_progress.finish)

    def _queue_apps_refresh_after_file_manager_preemption(self) -> None:
        """Resume only-missing app details when Apps is opened again."""

        self._pending_acbridge_feature_refresh.add("apps")

    def _privilege_backend_changed(self, value: str) -> None:
        backend = PrivilegeBackend.normalize(value)
        configured_value = self._configured_privilege_value()
        if (
            backend is PrivilegeBackend.normalize(configured_value)
            and (
                self._privilege_profile_available
                or bool(str(getattr(configured_value, "value", configured_value) or "").strip())
            )
        ):
            self.privilege_mode_selector.set_backend(backend)
            return
        self.settings.select_privilege_backend(
            backend.value,
            profile_available=self._privilege_profile_available,
        )
        self._settings_changed()

    def _configured_privilege_backend(self) -> PrivilegeBackend:
        return PrivilegeBackend.normalize(self._configured_privilege_value())

    def _configured_privilege_value(self) -> object:
        return read_privilege_backend_setting(
            self.settings,
            profile_available=getattr(
                self,
                "_privilege_profile_available",
                False,
            ),
        )

    def open_page(self, name: str) -> None:
        if name in self.pages:
            self.nav.setCurrentRow(list(self.pages).index(name))

    def open_dashboard_commands(self, category: str = "") -> None:
        """Open Commands with an optional catalog category selected."""

        self.open_page("Commands")
        if not category:
            return
        index = self.commands_page.category_filter.findText(category, Qt.MatchFixedString)
        if index >= 0:
            self.commands_page.category_filter.setCurrentIndex(index)

    def _on_page_changed(self, index: int) -> None:
        if index < 0:
            return
        name = list(self.pages)[index]
        pending_feature = {
            "Apps": "apps",
            "Backups": "backups",
            "File Manager": "file-manager",
        }.get(name)
        transition_pending = bool(
            self._automatic_shizuku_workflow_pending()
            or MainWindow._acbridge_update_workflow_pending(self)
            or bool(getattr(self, "_privilege_barrier_waits_for_recheck", False))
        )
        if (
            pending_feature == "file-manager"
            and not transition_pending
            and MainWindow._yield_passive_apps_work_to_file_manager(self)
        ):
            return
        if pending_feature and transition_pending:
            self._pending_acbridge_feature_refresh.add(pending_feature)
            return
        if (
            pending_feature
            and pending_feature in self._pending_acbridge_feature_refresh
        ):
            MainWindow._dispatch_pending_feature_refresh_if_current(
                self,
                pending_feature,
            )
            return
        backend_usable = MainWindow._selected_backend_usable_for_device_features(
            self
        )
        if name == "Apps" and backend_usable and not self.apps_page.apps:
            self.apps_page.refresh_apps()
        elif name == "Backups":
            self.backups_page.refresh()
        elif name == "File Manager" and backend_usable:
            self.file_manager_page.refresh_all()

    def _yield_passive_apps_work_to_file_manager(self) -> bool:
        """Give foreground file browsing priority over background app cache work."""

        try:
            context = self.device_manager.require_context(("ADB", "Recovery"))
        except DeviceContextUnavailable:
            return False
        maintenance_group = f"acbridge-maintenance:{context.serial}"
        passive_tokens = tuple(
            token
            for token in self.device_manager.operations.active_tokens()
            if (
                token.owner_key in PASSIVE_APPS_OPERATION_OWNERS
                and maintenance_group in token.conflict_groups
            )
        )
        if not passive_tokens:
            return False
        for token in passive_tokens:
            token.cancel(
                "File Manager foreground refresh is waiting for passive application details to stop."
            )
        self._pending_acbridge_feature_refresh.update({"apps", "file-manager"})
        self.file_manager_page.status_label.setText(
            "Preparing Android files. Stopping background application details first..."
        )
        MainWindow._queue_privilege_transition_drain_check(self)
        return True

    def _dispatch_device_feature_refresh(self, feature: str) -> None:
        """Honor instance overrides while supporting lightweight test hosts."""

        refresh = getattr(self, "_refresh_device_feature", None)
        if callable(refresh):
            refresh(feature)
            return
        MainWindow._refresh_device_feature(self, feature)

    def _refresh_device_feature(self, feature: str) -> None:
        """Refresh one page after ACBridge/access-mode barriers have drained."""

        if (
            feature in {"apps", "file-manager"}
            and not MainWindow._selected_backend_usable_for_device_features(self)
        ):
            self._pending_acbridge_feature_refresh.add(feature)
            return
        if feature == "apps":
            page = self.apps_page
            refresh_after_backend = getattr_static(
                page,
                "request_privilege_backend_refresh",
                None,
            )
            if callable(refresh_after_backend):
                page.request_privilege_backend_refresh()
            else:
                page.refresh_apps()
        elif feature == "backups":
            self.backups_page.refresh()
        elif feature == "file-manager":
            page = self.file_manager_page
            refresh_after_backend = getattr_static(
                page,
                "request_privilege_backend_refresh",
                None,
            )
            if callable(refresh_after_backend):
                page.request_privilege_backend_refresh()
            else:
                page.refresh_all()

    def _schedule_settings_recovery_warning(self) -> None:
        if getattr(self, "_closing", False):
            return
        if self._settings_recovery_dialog_active:
            self._settings_recovery_follow_up_pending = True
            return
        self._settings_recovery_timer.start()

    def _show_pending_settings_recovery(self) -> None:
        """Present each actual settings recovery once, batching queued scopes."""

        if self._closing:
            return
        if self._settings_recovery_dialog_active:
            self._settings_recovery_follow_up_pending = True
            return
        notices = []
        while notice := self.settings.consume_recovery_notice():
            notices.append(notice)
        if not notices:
            return
        if len(notices) == 1:
            message = notices[0].message
        else:
            sections = [
                f"Recovered settings scope {index}:\n{notice.message}"
                for index, notice in enumerate(notices, start=1)
            ]
            message = "OpenADB recovered multiple settings scopes.\n\n" + "\n\n".join(
                sections
            )
        self._settings_recovery_dialog_active = True
        try:
            QMessageBox.warning(self, "Settings recovery", message)
        finally:
            self._settings_recovery_dialog_active = False
            if self._settings_recovery_follow_up_pending and not self._closing:
                self._settings_recovery_follow_up_pending = False
                self._settings_recovery_timer.start()

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
        profile_ready = profile_changed is not None and bool(device.serial)
        set_profile_available = getattr(self, "_set_privilege_profile_available", None)
        if callable(set_profile_available) and profile_ready != getattr(
            self,
            "_privilege_profile_available",
            False,
        ):
            set_profile_available(profile_ready)
        acbridge_update_pending = False
        if (
            profile_ready
            and device.serial
            and device.mode == "ADB"
            and str(device.state or "").casefold() == "device"
        ):
            manager = getattr(self, "device_manager", None)
            schedule_update = getattr(self, "_schedule_acbridge_update", None)
            require_context = getattr(manager, "require_context", None)
            if callable(schedule_update) and callable(require_context):
                try:
                    context = require_context(("ADB",))
                except DeviceContextUnavailable:
                    context = None
                if context is not None:
                    acbridge_update_pending = bool(schedule_update(context))
        signature = (
            device.serial,
            device.mode,
            device.state,
            device.transport_id,
            device.model,
            device.android_version,
            device.sdk_version,
            getattr(
                getattr(self, "device_manager", None),
                "current_generation",
                None,
            ),
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
        generation = getattr(
            getattr(self, "device_manager", None),
            "current_generation",
            None,
        )
        privilege_connection_key = (
            (device.serial, generation)
            if device.serial
            else None
        )
        privilege_connection_changed = privilege_connection_key != getattr(
            self,
            "_last_privilege_connection_key",
            None,
        )
        self._last_privilege_connection_key = privilege_connection_key
        privilege_barrier_is_draining = bool(
            getattr(self, "_privilege_token", None) is not None
            or MainWindow._privilege_transition_blockers_are_draining(self)
            or (
                getattr(self, "_privilege_transition_drain_scheduled", False)
                and getattr(self, "_privilege_barrier_waits_for_recheck", False)
            )
        )
        if privilege_connection_changed:
            self._last_automatic_shizuku_key = None
            attempts = getattr(self, "_automatic_shizuku_attempts", None)
            if attempts is not None:
                attempts.clear()
            self._automatic_shizuku_failure_status = None
            if privilege_barrier_is_draining:
                self._privilege_barrier_waits_for_recheck = True
                MainWindow._set_privilege_feature_barrier_busy(self, True)
            else:
                self._privilege_barrier_waits_for_recheck = False
                MainWindow._set_privilege_feature_barrier_busy(self, False)
        privilege_manager = getattr(self, "privilege_manager", None)
        if (
            profile_changed or privilege_connection_changed
        ) and privilege_manager is not None:
            privilege_manager.reset()
            MainWindow._clear_acbridge_privilege_result(self)
            self._apply_privilege_status(None)
            selected_backend = privilege_manager.selected_backend
            backend_mode_ready = (
                selected_backend is PrivilegeBackend.SHIZUKU
                and device.mode == "ADB"
            ) or (
                selected_backend is PrivilegeBackend.ROOT
                and device.mode in {"ADB", "Recovery"}
            )
            if profile_ready and backend_mode_ready:
                self._schedule_privilege_recheck(
                    force_defer=acbridge_update_pending,
                )
            else:
                clear_automatic = getattr(
                    self,
                    "_clear_pending_automatic_shizuku",
                    None,
                )
                if callable(clear_automatic):
                    clear_automatic()
                self._last_automatic_shizuku_key = None
                self._pending_privilege_recheck = False
                if not privilege_barrier_is_draining:
                    self._automatic_shizuku_inflight_key = None
                set_automatic_busy = getattr(
                    self,
                    "_set_automatic_shizuku_ui_busy",
                    None,
                )
                if callable(set_automatic_busy) and not privilege_barrier_is_draining:
                    set_automatic_busy(False)
        automatic_shizuku_pending = False
        automatic_pending = getattr(
            self,
            "_automatic_shizuku_workflow_pending",
            None,
        )
        if callable(automatic_pending):
            automatic_shizuku_pending = bool(automatic_pending())
        refresh_file_manager = (
            profile_ready
            and self.stack.currentWidget() is self.file_manager_page
            and (profile_changed or device_changed)
        )
        if refresh_file_manager:
            if acbridge_update_pending or automatic_shizuku_pending:
                pending_refreshes = getattr(
                    self,
                    "_pending_acbridge_feature_refresh",
                    None,
                )
                if pending_refreshes is not None:
                    pending_refreshes.add("file-manager")
            else:
                self.file_manager_page.refresh_all()
        refresh_apps = (
            profile_ready
            and self.stack.currentWidget() is self.apps_page
            and device.mode in {"ADB", "Recovery"}
            and (profile_changed or not self.apps_page.apps)
        )
        if refresh_apps:
            if acbridge_update_pending or automatic_shizuku_pending:
                pending_refreshes = getattr(
                    self,
                    "_pending_acbridge_feature_refresh",
                    None,
                )
                if pending_refreshes is not None:
                    pending_refreshes.add("apps")
            else:
                self.apps_page.refresh_apps()

    def _schedule_acbridge_update(self, context: DeviceContext) -> bool:
        """Queue one non-blocking ACBridge version check per device generation."""

        if self._closing or not self.device_manager.is_context_current(context):
            return False
        key = (context.serial, context.generation)
        current_token = self._acbridge_update_token
        if current_token is not None:
            MainWindow._set_acbridge_maintenance_ui_busy(self, True)
            current_context = current_token.device_context
            current_key = (
                (current_context.serial, current_context.generation)
                if current_context is not None
                else None
            )
            if current_key != key:
                self._pending_acbridge_update_context = context
            return True
        if self._last_acbridge_update_key == key:
            return False

        MainWindow._set_acbridge_maintenance_ui_busy(self, True)

        try:
            token = self.device_manager.operations.register(
                "acbridge-auto-update",
                device_context=context,
                conflict_group=f"acbridge-auto-update:{context.serial}",
                conflict_groups=(
                    f"device-exclusive:{context.serial}",
                    f"acbridge-maintenance:{context.serial}",
                ),
            )
        except OperationConflictError:
            self._queue_acbridge_update_retry(context)
            return True
        except RuntimeError:
            QTimer.singleShot(
                0,
                lambda: MainWindow._release_acbridge_maintenance_ui_if_idle(self),
            )
            return False
        if not self.device_manager.is_context_current(context):
            token.cancel("device context changed before ACBridge update check")
            self.device_manager.operations.finish(token)
            QTimer.singleShot(
                0,
                lambda: MainWindow._release_acbridge_maintenance_ui_if_idle(self),
            )
            return False

        self._acbridge_update_token = token
        self._last_acbridge_update_key = key
        self._acbridge_update_retry_key = None
        attempt = self._acbridge_update_attempts.get(key, 0) + 1
        self._acbridge_update_attempts = {key: attempt}
        worker = Worker(lambda: self._run_acbridge_update(token, context))
        worker.signals.result.connect(
            lambda result: self._acbridge_update_result(token, result)
        )
        worker.signals.error.connect(
            lambda message, _trace: self._acbridge_update_error(token, message)
        )
        worker.signals.finished.connect(
            lambda: self._acbridge_update_finished(token)
        )
        try:
            started = start_worker(
                self,
                self.device_bar.pool,
                worker,
                operation_registry=self.device_manager.operations,
                operation_token=token,
            )
        except RuntimeError as exc:
            token.cancel("ACBridge update worker could not be started")
            self.device_manager.operations.finish(token)
            self._acbridge_update_token = None
            self._last_acbridge_update_key = None
            self.statusBar().showMessage(
                f"ACBridge update check could not start: {exc}",
                10000,
            )
            QTimer.singleShot(
                0,
                lambda: MainWindow._release_acbridge_maintenance_ui_if_idle(self),
            )
            return False
        if started is False:
            if self._last_acbridge_update_key == key:
                self._last_acbridge_update_key = None
            self._acbridge_update_finished(token)
            return False
        return True

    def _queue_acbridge_update_retry(self, context: DeviceContext) -> None:
        key = (context.serial, context.generation)
        if self._closing or self._acbridge_update_retry_key == key:
            return
        MainWindow._set_acbridge_maintenance_ui_busy(self, True)
        self._acbridge_update_retry_key = key
        QTimer.singleShot(
            1000,
            lambda retry_context=context, retry_key=key: self._retry_acbridge_update(
                retry_context,
                retry_key,
            ),
        )

    def _retry_acbridge_update(
        self,
        context: DeviceContext,
        key: tuple[str, int],
    ) -> None:
        if self._acbridge_update_retry_key != key:
            return
        self._acbridge_update_retry_key = None
        if self._closing or not self.device_manager.is_context_current(context):
            QTimer.singleShot(
                0,
                lambda: MainWindow._release_acbridge_maintenance_ui_if_idle(self),
            )
            return
        if not self._schedule_acbridge_update(context):
            self._resume_privilege_recheck_after_acbridge()
            QTimer.singleShot(
                0,
                lambda: MainWindow._release_acbridge_maintenance_ui_if_idle(self),
            )

    def _run_acbridge_update(
        self,
        token: OperationToken,
        context: DeviceContext,
    ) -> ACBridgeUpdateResult | None:
        if token.cancelled or not self.device_manager.is_context_current(context):
            token.cancel("device context changed before ACBridge update execution")
            return None
        bound_adb = self.adb.for_context(context)
        bridge = ACBridgeClient(
            bound_adb,
            self.settings,
            self.icon_extractor,
            temp_folder=context.temp_path,
        )
        return bridge.update_if_outdated(cancel_event=token.cancel_event)

    def _acbridge_update_result(
        self,
        token: OperationToken,
        result: ACBridgeUpdateResult | None,
    ) -> None:
        if not self._acbridge_update_callback_is_current(token) or result is None:
            return
        context = token.device_context
        key = (
            (context.serial, context.generation)
            if context is not None
            else None
        )
        if (
            result.should_retry
            and context is not None
            and key is not None
            and self._acbridge_update_attempts.get(key, 0) < 3
        ):
            if self._last_acbridge_update_key == key:
                self._last_acbridge_update_key = None
            self._queue_acbridge_update_retry(context)
            return
        if result.changed:
            self._invalidate_privilege_status()
            if self.privilege_manager.selected_backend is not PrivilegeBackend.STANDARD:
                self._pending_privilege_recheck = True
            self.statusBar().showMessage(result.message, 10000)
        elif result.failed:
            self.statusBar().showMessage(
                f"ACBridge automatic setup: {result.message}",
                12000,
            )
        elif result.state == "newer":
            self.statusBar().showMessage(result.message, 8000)

    def _acbridge_update_error(
        self,
        token: OperationToken,
        message: str,
    ) -> None:
        if not self._acbridge_update_callback_is_current(token):
            return
        context = token.device_context
        key = (
            (context.serial, context.generation)
            if context is not None
            else None
        )
        if (
            context is not None
            and key is not None
            and self._acbridge_update_attempts.get(key, 0) < 3
        ):
            if self._last_acbridge_update_key == key:
                self._last_acbridge_update_key = None
            self._queue_acbridge_update_retry(context)
            return
        self.statusBar().showMessage(
            f"ACBridge automatic setup failed: {message}",
            12000,
        )

    def _acbridge_update_finished(self, token: OperationToken) -> None:
        if self._acbridge_update_token is not token:
            return
        self._acbridge_update_token = None
        pending_context = self._pending_acbridge_update_context
        self._pending_acbridge_update_context = None
        if self._closing:
            return
        if (
            pending_context is not None
            and self.device_manager.is_context_current(pending_context)
        ):
            QTimer.singleShot(
                0,
                lambda context=pending_context: self._resume_acbridge_update(context),
            )
            return
        if self._acbridge_update_retry_key is not None:
            return
        self._resume_privilege_recheck_after_acbridge()
        resume_features = getattr(self, "_resume_feature_refresh_after_acbridge", None)
        if callable(resume_features):
            resume_features()
        QTimer.singleShot(
            0,
            lambda: MainWindow._release_acbridge_maintenance_ui_if_idle(self),
        )

    def _release_acbridge_maintenance_ui_if_idle(self) -> None:
        if self._closing or MainWindow._acbridge_update_workflow_pending(self):
            return
        MainWindow._set_acbridge_maintenance_ui_busy(self, False)

    def _resume_feature_refresh_after_acbridge(self) -> None:
        if not self._pending_acbridge_feature_refresh or self._closing:
            return
        if (
            self._automatic_shizuku_workflow_pending()
            or MainWindow._acbridge_update_workflow_pending(self)
            or bool(getattr(self, "_privilege_barrier_waits_for_recheck", False))
        ):
            return
        current_feature = {
            self.apps_page: "apps",
            self.backups_page: "backups",
            self.file_manager_page: "file-manager",
        }.get(self.stack.currentWidget())
        if (
            current_feature is None
            or current_feature not in self._pending_acbridge_feature_refresh
        ):
            return
        if current_feature in {"apps", "file-manager"}:
            if not MainWindow._selected_backend_usable_for_device_features(self):
                return
            if MainWindow._privilege_access_operation_conflict_is_active(self):
                MainWindow._queue_privilege_transition_drain_check(
                    self,
                    delay_ms=750,
                )
                return
        QTimer.singleShot(
            0,
            lambda feature=current_feature: MainWindow._dispatch_pending_feature_refresh_if_current(
                self,
                feature,
            ),
        )

    def _dispatch_pending_feature_refresh_if_current(self, feature: str) -> None:
        """Consume a queued refresh only when its page is still foreground-safe."""

        if self._closing or feature not in self._pending_acbridge_feature_refresh:
            return
        if (
            self._automatic_shizuku_workflow_pending()
            or MainWindow._acbridge_update_workflow_pending(self)
            or bool(getattr(self, "_privilege_barrier_waits_for_recheck", False))
        ):
            return
        current_feature = {
            self.apps_page: "apps",
            self.backups_page: "backups",
            self.file_manager_page: "file-manager",
        }.get(self.stack.currentWidget())
        if current_feature != feature:
            return
        if feature in {"apps", "file-manager"}:
            if not MainWindow._selected_backend_usable_for_device_features(self):
                return
            if MainWindow._privilege_access_operation_conflict_is_active(self):
                MainWindow._queue_privilege_transition_drain_check(
                    self,
                    delay_ms=750,
                )
                return
        self._pending_acbridge_feature_refresh.discard(feature)
        MainWindow._dispatch_device_feature_refresh(self, feature)

    def _acbridge_update_workflow_pending(self) -> bool:
        """Return whether ACBridge still owns or is waiting for device maintenance."""

        return bool(
            getattr(self, "_acbridge_update_token", None) is not None
            or getattr(self, "_acbridge_update_retry_key", None) is not None
            or getattr(self, "_pending_acbridge_update_context", None) is not None
        )

    def _resume_acbridge_update(self, context: DeviceContext) -> None:
        if not self._schedule_acbridge_update(context):
            self._resume_privilege_recheck_after_acbridge()

    def _resume_privilege_recheck_after_acbridge(self) -> None:
        if not self._pending_privilege_recheck:
            return
        active = self.device_manager.active
        if (
            self.privilege_manager.selected_backend is PrivilegeBackend.ROOT
            and active.mode in {"ADB", "Recovery"}
            and str(active.state or "").casefold() == "device"
        ):
            # ACBridge and privilege workers share the device-maintenance
            # conflict group.  Keep feature pages gated across the queued
            # hand-off instead of briefly re-enabling them between workers.
            self._privilege_barrier_waits_for_recheck = True
            MainWindow._set_privilege_feature_barrier_busy(self, True)
        self._schedule_privilege_recheck()

    def _schedule_privilege_recheck(self, *, force_defer: bool = False) -> None:
        """Start the selected backend's connection-time access workflow."""

        if self._closing:
            self._pending_privilege_recheck = False
            return
        backend = self.privilege_manager.selected_backend
        active = self.device_manager.active
        backend_mode_ready = (
            backend is PrivilegeBackend.SHIZUKU
            and active.mode == "ADB"
        ) or (
            backend is PrivilegeBackend.ROOT
            and active.mode in {"ADB", "Recovery"}
        )
        if (
            backend is PrivilegeBackend.STANDARD
            or not backend_mode_ready
            or str(active.state or "").casefold() != "device"
        ):
            self._pending_privilege_recheck = False
            clear_automatic = getattr(
                self,
                "_clear_pending_automatic_shizuku",
                None,
            )
            if callable(clear_automatic):
                clear_automatic()
            return
        if backend is PrivilegeBackend.SHIZUKU:
            schedule_handshake = getattr(
                self,
                "_schedule_automatic_shizuku_handshake",
                None,
            )
            if callable(schedule_handshake):
                if schedule_handshake(force_defer=force_defer):
                    return
                self._pending_privilege_recheck = False
                return
        if (
            backend is PrivilegeBackend.SHIZUKU
            and (
                force_defer
                or self._acbridge_update_token is not None
                or self._acbridge_update_retry_key is not None
                or self._pending_acbridge_update_context is not None
            )
        ):
            self._pending_privilege_recheck = True
            return
        self._pending_privilege_recheck = True
        MainWindow._queue_privilege_recheck_callback(self)

    def _capture_privilege_transition_blockers(self) -> None:
        """Snapshot workers that were created against the outgoing backend."""

        operations = getattr(
            getattr(self, "device_manager", None),
            "operations",
            None,
        )
        active_tokens = getattr(operations, "active_tokens", None)
        if not callable(active_tokens):
            return
        blocker_ids = getattr(self, "_privilege_transition_blocker_ids", None)
        if blocker_ids is None:
            blocker_ids = set()
            self._privilege_transition_blocker_ids = blocker_ids
        blocker_ids.update(
            token.operation_id
            for token in active_tokens()
            if token.privilege_lease is not None
        )

    def _privilege_transition_blockers_are_draining(self) -> bool:
        """Wait only for workers captured against the outgoing backend."""

        operations = getattr(
            getattr(self, "device_manager", None),
            "operations",
            None,
        )
        active_tokens = getattr(operations, "active_tokens", None)
        if not callable(active_tokens):
            return False
        tokens = active_tokens()
        blocker_ids = getattr(self, "_privilege_transition_blocker_ids", None)
        if blocker_ids is None:
            # Lightweight legacy/test hosts do not run the snapshot hook.
            return any(token.privilege_lease is not None for token in tokens)
        active_ids = {token.operation_id for token in tokens}
        blocker_ids.intersection_update(active_ids)
        return bool(blocker_ids)

    def _privilege_access_operation_conflict_is_active(self) -> bool:
        """Return whether independent work currently owns access-check groups."""

        active = getattr(getattr(self, "device_manager", None), "active", None)
        serial = str(getattr(active, "serial", "") or "")
        if not serial:
            return False
        operations = getattr(getattr(self, "device_manager", None), "operations", None)
        active_tokens = getattr(operations, "active_tokens", None)
        if not callable(active_tokens):
            return False
        required_groups = {
            f"device-exclusive:{serial}",
            f"acbridge-maintenance:{serial}",
        }
        return any(
            token.owner_key != "privilege-access"
            and bool(token.conflict_groups.intersection(required_groups))
            for token in active_tokens()
        )

    def _selected_backend_usable_for_device_features(self) -> bool:
        """Check whether Apps/File Manager can use the selected backend now."""

        if not bool(getattr(self, "_privilege_profile_available", True)):
            return False
        device_manager = getattr(self, "device_manager", None)
        active = getattr(device_manager, "active", None)
        if active is None:
            return True
        mode = str(getattr(active, "mode", "No device") or "No device")
        state = str(getattr(active, "state", "") or "").casefold()
        if mode not in {"ADB", "Recovery"} or state not in {"", "device"}:
            return False
        manager = getattr(self, "privilege_manager", None)
        backend = PrivilegeBackend.normalize(
            getattr(manager, "selected_backend", PrivilegeBackend.STANDARD)
        )
        if backend is not PrivilegeBackend.SHIZUKU:
            return True
        if mode != "ADB" or manager is None:
            return False
        cached_status = getattr(manager, "cached_status", None)
        status = cached_status() if callable(cached_status) else None
        return bool(
            status is not None
            and status.backend is PrivilegeBackend.SHIZUKU
            and status.available
        )

    def _queue_privilege_transition_drain_check(
        self,
        *,
        delay_ms: int = 50,
    ) -> None:
        if getattr(self, "_closing", False) or getattr(
            self,
            "_privilege_transition_drain_scheduled",
            False,
        ):
            return
        self._privilege_transition_drain_scheduled = True
        QTimer.singleShot(
            max(0, int(delay_ms)),
            lambda: MainWindow._run_privilege_transition_drain_check(self),
        )

    def _run_privilege_transition_drain_check(self) -> None:
        if not getattr(self, "_privilege_transition_drain_scheduled", False):
            return
        self._privilege_transition_drain_scheduled = False
        if self._closing:
            return
        if (
            self._privilege_token is not None
            or MainWindow._privilege_transition_blockers_are_draining(self)
        ):
            MainWindow._queue_privilege_transition_drain_check(self)
            return
        if self._pending_privilege_recheck:
            if MainWindow._privilege_access_operation_conflict_is_active(self):
                MainWindow._set_privilege_feature_barrier_busy(self, True)
                MainWindow._queue_privilege_transition_drain_check(
                    self,
                    delay_ms=750,
                )
                return
            self._schedule_privilege_recheck()
            return
        self._privilege_barrier_waits_for_recheck = False
        MainWindow._set_privilege_feature_barrier_busy(self, False)
        self._set_automatic_shizuku_ui_busy(False)
        self._resume_feature_refresh_after_acbridge()

    def _queue_privilege_recheck_callback(
        self,
        *,
        delay_ms: int = 0,
    ) -> None:
        if self._closing or bool(
            getattr(self, "_privilege_recheck_callback_scheduled", False)
        ):
            return
        self._privilege_recheck_callback_scheduled = True
        QTimer.singleShot(
            max(0, int(delay_ms)),
            lambda: MainWindow._run_queued_privilege_recheck(self),
        )

    def _run_queued_privilege_recheck(self) -> None:
        if not bool(getattr(self, "_privilege_recheck_callback_scheduled", False)):
            return
        self._privilege_recheck_callback_scheduled = False
        if self._closing or not self._pending_privilege_recheck:
            return
        if self.privilege_manager.selected_backend is PrivilegeBackend.SHIZUKU:
            self._schedule_privilege_recheck()
            return
        if MainWindow._privilege_access_operation_conflict_is_active(self):
            MainWindow._set_privilege_feature_barrier_busy(self, True)
            MainWindow._queue_privilege_recheck_callback(self, delay_ms=750)
            return
        self._pending_privilege_recheck = False
        if getattr(self, "_privilege_barrier_waits_for_recheck", False):
            MainWindow._set_privilege_feature_barrier_busy(self, True)
        started = self.check_privilege_access(interactive=False)
        if started is not False:
            return
        active = self.device_manager.active
        backend = self.privilege_manager.selected_backend
        mode_ready = (
            backend is PrivilegeBackend.ROOT
            and active.mode in {"ADB", "Recovery"}
            and str(active.state or "").casefold() == "device"
        )
        if not self._closing and mode_ready:
            self._pending_privilege_recheck = True
            if getattr(self, "_privilege_start_failure_kind", "") == "busy":
                MainWindow._set_privilege_feature_barrier_busy(self, True)
            MainWindow._queue_privilege_recheck_callback(self, delay_ms=750)

    def _schedule_automatic_shizuku_handshake(
        self,
        *,
        force_defer: bool = False,
    ) -> bool:
        """Queue one permission request + verification per device generation."""

        if self._closing:
            self._clear_pending_automatic_shizuku()
            return False
        if self.privilege_manager.selected_backend is not PrivilegeBackend.SHIZUKU:
            self._clear_pending_automatic_shizuku()
            return False
        active = self.device_manager.active
        if active.mode != "ADB" or str(active.state or "").casefold() != "device":
            self._clear_pending_automatic_shizuku()
            return False
        try:
            context = self.device_manager.require_context(("ADB",))
        except DeviceContextUnavailable:
            self._clear_pending_automatic_shizuku()
            return False
        if not self.device_manager.is_context_current(context):
            self._clear_pending_automatic_shizuku()
            return False

        key = (context.serial, context.generation)
        running_automatic = (
            self._privilege_token is not None
            and not bool(getattr(self._privilege_token, "cancelled", False))
            and getattr(self, "_privilege_operation_kind", "")
            == "automatic-shizuku"
            and self._automatic_shizuku_inflight_key == key
        )
        if self._last_automatic_shizuku_key == key:
            if running_automatic:
                self._pending_privilege_recheck = False
                return True
            self._clear_pending_automatic_shizuku(key=key)
            self._pending_privilege_recheck = False
            return True

        self._pending_automatic_shizuku_context = context
        self._pending_privilege_recheck = True
        self._set_automatic_shizuku_ui_busy(True)
        if (
            force_defer
            or self._acbridge_update_token is not None
            or self._acbridge_update_retry_key is not None
            or self._pending_acbridge_update_context is not None
        ):
            return True
        self._queue_automatic_shizuku_start(context)
        return True

    def _queue_automatic_shizuku_start(
        self,
        context: DeviceContext,
        *,
        delay_ms: int = 0,
    ) -> None:
        key = (context.serial, context.generation)
        if self._closing or self._automatic_shizuku_scheduled_key == key:
            return
        self._automatic_shizuku_scheduled_key = key
        QTimer.singleShot(
            max(0, int(delay_ms)),
            lambda scheduled_context=context, scheduled_key=key: (
                self._start_automatic_shizuku_handshake(
                    scheduled_context,
                    scheduled_key,
                )
            ),
        )

    def _start_automatic_shizuku_handshake(
        self,
        context: DeviceContext,
        key: tuple[str, int],
    ) -> None:
        if self._automatic_shizuku_scheduled_key != key:
            return
        self._automatic_shizuku_scheduled_key = None
        pending = self._pending_automatic_shizuku_context
        pending_key = (
            (pending.serial, pending.generation)
            if pending is not None
            else None
        )
        if (
            self._closing
            or pending_key != key
            or self.privilege_manager.selected_backend
            is not PrivilegeBackend.SHIZUKU
            or not self.device_manager.is_context_current(context)
        ):
            if pending_key == key:
                self._pending_automatic_shizuku_context = None
            return
        active = self.device_manager.active
        if active.mode != "ADB" or str(active.state or "").casefold() != "device":
            self._pending_automatic_shizuku_context = None
            self._pending_privilege_recheck = False
            return
        if (
            self._acbridge_update_token is not None
            or self._acbridge_update_retry_key is not None
            or self._pending_acbridge_update_context is not None
        ):
            return
        if MainWindow._privilege_access_operation_conflict_is_active(self):
            self._pending_privilege_recheck = True
            self._set_automatic_shizuku_ui_busy(True)
            self._queue_automatic_shizuku_start(context, delay_ms=750)
            return

        self._set_automatic_shizuku_ui_busy(True)
        privilege_lease = self.privilege_manager.capture_operation_lease()
        self._pending_automatic_shizuku_context = None
        self._pending_privilege_recheck = False
        self._automatic_shizuku_inflight_key = key
        attempts = getattr(self, "_automatic_shizuku_attempts", None)
        if attempts is None:
            attempts = {}
            self._automatic_shizuku_attempts = attempts
        attempts[key] = attempts.get(key, 0) + 1
        started = self._start_privilege_operation(
            "automatic-shizuku",
            context,
            lambda cancel_event: self.privilege_manager.request_and_check_shizuku(
                context,
                cancel_event=cancel_event,
                privilege_lease=privilege_lease,
            ),
            "Requesting and verifying Shizuku access on Android…",
            interactive=False,
        )
        if started:
            return
        if getattr(self, "_privilege_start_failure_kind", "busy") == "busy":
            remaining_attempts = max(0, attempts.get(key, 1) - 1)
            if remaining_attempts:
                attempts[key] = remaining_attempts
            else:
                attempts.pop(key, None)
        if self._automatic_shizuku_inflight_key == key:
            self._automatic_shizuku_inflight_key = None
        if self._last_automatic_shizuku_key == key:
            self._set_automatic_shizuku_ui_busy(False)
            self._resume_feature_refresh_after_acbridge()
            return
        if (
            not self._closing
            and self.privilege_manager.selected_backend is PrivilegeBackend.SHIZUKU
            and self.device_manager.is_context_current(context)
        ):
            self._pending_automatic_shizuku_context = context
            self._pending_privilege_recheck = True
            if getattr(self, "_privilege_start_failure_kind", "") == "busy":
                self._set_automatic_shizuku_ui_busy(True)
            self._queue_automatic_shizuku_start(context, delay_ms=750)
            return
        if not self._automatic_shizuku_workflow_pending():
            self._set_automatic_shizuku_ui_busy(False)
            self._resume_feature_refresh_after_acbridge()

    def _clear_pending_automatic_shizuku(
        self,
        *,
        key: tuple[str, int] | None = None,
    ) -> None:
        pending = getattr(self, "_pending_automatic_shizuku_context", None)
        pending_key = (
            (pending.serial, pending.generation)
            if pending is not None
            else None
        )
        scheduled_key = getattr(self, "_automatic_shizuku_scheduled_key", None)
        if key is not None and pending_key not in {None, key} and scheduled_key != key:
            return
        if key is None or pending_key == key:
            self._pending_automatic_shizuku_context = None
        if key is None or scheduled_key == key:
            self._automatic_shizuku_scheduled_key = None

    def _automatic_shizuku_workflow_pending(self) -> bool:
        return bool(
            self._pending_automatic_shizuku_context is not None
            or self._automatic_shizuku_scheduled_key is not None
            or self._automatic_shizuku_inflight_key is not None
            or (
                self._privilege_token is not None
                and getattr(self, "_privilege_operation_kind", "")
                == "automatic-shizuku"
            )
        )

    def _set_automatic_shizuku_ui_busy(self, busy: bool) -> None:
        """Prevent device pages from racing the connection-time handshake."""

        self._automatic_shizuku_ui_busy = bool(busy)
        MainWindow._update_device_feature_page_enabled_state(self)
        MainWindow._refresh_privilege_busy_controls(self)

    def _set_acbridge_maintenance_ui_busy(self, busy: bool) -> None:
        """Keep device pages idle while ACBridge owns maintenance groups."""

        self._acbridge_maintenance_ui_busy = bool(busy)
        MainWindow._update_device_feature_page_enabled_state(self)

    def _set_privilege_feature_barrier_busy(self, busy: bool) -> None:
        """Gate every privilege entry point while an old backend drains."""

        self._privilege_feature_barrier_busy = bool(busy)
        MainWindow._update_device_feature_page_enabled_state(self)
        MainWindow._refresh_privilege_busy_controls(self)

    def _refresh_privilege_busy_controls(self) -> None:
        """Render independent privilege busy sources without clearing another."""

        backend = PrivilegeBackend.normalize(
            getattr(
                getattr(self, "privilege_manager", None),
                "selected_backend",
                PrivilegeBackend.STANDARD,
            )
        )
        backend_label = {
            PrivilegeBackend.STANDARD: "Standard ADB",
            PrivilegeBackend.ROOT: "Root",
            PrivilegeBackend.SHIZUKU: "Shizuku",
        }[backend]
        operation_busy = getattr(self, "_privilege_token", None) is not None
        barrier_busy = bool(
            getattr(self, "_privilege_feature_barrier_busy", False)
        )
        automatic_busy = bool(
            getattr(self, "_automatic_shizuku_ui_busy", False)
        )
        if operation_busy:
            message = str(
                getattr(self, "_privilege_operation_busy_message", "")
                or "Checking privileged access…"
            )
        elif barrier_busy:
            message = (
                f"Applying {backend_label} access after active device operations finish…"
            )
        elif automatic_busy:
            message = "Preparing automatic Shizuku permission and access check…"
        else:
            message = ""
        for page_name in ("settings_page", "commands_page"):
            page = getattr(self, page_name, None)
            setter = getattr(page, "set_privilege_busy", None)
            if callable(setter):
                if message:
                    setter(True, message)
                else:
                    setter(False)
        if message:
            status_setter = getattr(
                self,
                "_set_global_privilege_status_text",
                None,
            )
            if callable(status_setter):
                status_setter(message)
        else:
            # Busy renderers temporarily replace both the global text and the
            # page-local privilege summaries.  Merely enabling the controls
            # again leaves that transient text behind (most visibly as an
            # endless "Preparing automatic Shizuku..." after a successful
            # connection-time handshake).  Re-apply the cached, device-bound
            # result once the final independent busy source has drained.
            apply_status = getattr(self, "_apply_privilege_status", None)
            if (
                hasattr(self, "_last_privilege_display_status")
                and callable(apply_status)
            ):
                apply_status(self._last_privilege_display_status)

    def _update_device_feature_page_enabled_state(self) -> None:
        busy = bool(
            getattr(self, "_automatic_shizuku_ui_busy", False)
            or getattr(self, "_privilege_feature_barrier_busy", False)
            or getattr(self, "_acbridge_maintenance_ui_busy", False)
        )
        for page_name in ("apps_page", "backups_page", "file_manager_page"):
            page = getattr(self, page_name, None)
            setter = getattr(page, "setEnabled", None)
            if callable(setter):
                setter(not busy)

    def _acbridge_update_callback_is_current(self, token: OperationToken) -> bool:
        if (
            self._closing
            or token.cancelled
            or self._acbridge_update_token is not token
            or not self.device_manager.operations.contains(token)
        ):
            return False
        context = getattr(token, "device_context", None)
        return context is not None and self.device_manager.is_context_current(context)

    def check_privilege_access(self, interactive: bool = True) -> bool:
        backend = self.privilege_manager.selected_backend
        allowed_modes = {
            PrivilegeBackend.STANDARD: ("ADB", "Recovery", "Sideload"),
            PrivilegeBackend.ROOT: ("ADB", "Recovery"),
            PrivilegeBackend.SHIZUKU: ("ADB",),
        }[backend]
        try:
            context = self.device_manager.require_context(allowed_modes)
        except DeviceContextUnavailable as exc:
            status = PrivilegeStatus(
                backend=backend,
                state="unavailable",
                level="unavailable",
                message=str(exc),
            )
            self._apply_privilege_status(status)
            self.statusBar().showMessage(str(exc), 7000)
            return False
        privilege_lease = self.privilege_manager.capture_operation_lease()
        return self._start_privilege_operation(
            "check",
            context,
            lambda cancel_event: self._check_shell_and_acbridge_access(
                context,
                backend=backend,
                cancel_event=cancel_event,
                privilege_lease=privilege_lease,
            ),
            (
                "Checking Root access for Android shell and ACBridge…"
                if backend is PrivilegeBackend.ROOT and context.mode == "ADB"
                else "Checking privileged access…"
            ),
            interactive=interactive,
        )

    def _check_shell_and_acbridge_access(
        self,
        context: DeviceContext,
        *,
        backend: PrivilegeBackend,
        cancel_event,
        privilege_lease,
    ) -> _PrivilegeHandshakeResult:
        """Run one ordered, page-independent access handshake.

        Android root managers grant the ADB shell UID and the ACBridge app UID
        independently.  Both checks deliberately share the existing privilege
        worker/token so no page can race between the two decisions.  Shizuku is
        already ACBridge-owned and therefore remains on its single official
        Activity/UserService flow.
        """

        bridge_client = None
        permission_host = None
        if backend is PrivilegeBackend.ROOT and context.mode == "ADB":
            bridge_client = ACBridgeClient(
                self.adb.for_context(context),
                self.settings,
                self.icon_extractor,
                temp_folder=context.temp_path,
            )
            permission_host = bridge_client.start_permission_host(
                PrivilegeBackend.ROOT.value,
                # The orphan guard must outlive every bounded Root phase. The
                # normal close comes from PrivilegeActivity's terminal result.
                timeout=420,
                cancel_event=cancel_event,
            )
            if not permission_host.started:
                raise RuntimeError(
                    permission_host.message
                    or "Android could not keep ACBridge visible for the Root permission request."
                )

        try:
            shell_status = self.privilege_manager.check(
                context,
                backend=backend,
                cancel_event=cancel_event,
                privilege_lease=privilege_lease,
            )
            if (
                backend is not PrivilegeBackend.ROOT
                or context.mode != "ADB"
                or (cancel_event is not None and cancel_event.is_set())
            ):
                return _PrivilegeHandshakeResult(shell=shell_status)

            self.privilege_manager.validate_operation_lease(
                privilege_lease,
                "The selected access mode changed before ACBridge requested Root access.",
            )
            if not self.device_manager.is_context_current(context):
                raise RuntimeError(
                    "The active device changed before ACBridge requested Root access."
            )
            assert bridge_client is not None
            assert permission_host is not None
            bridge = bridge_client.request_privilege_access(
                PrivilegeBackend.ROOT.value,
                cancel_event=cancel_event,
                bridge_is_current=True,
                permission_host_request_id=permission_host.request_id,
            )
            self.privilege_manager.validate_operation_lease(
                privilege_lease,
                "The selected access mode changed while ACBridge requested Root access.",
            )
            if not self.device_manager.is_context_current(context):
                raise RuntimeError(
                    "The active device changed while ACBridge requested Root access."
                )
            return _PrivilegeHandshakeResult(shell=shell_status, bridge=bridge)
        finally:
            if permission_host is not None and permission_host.request_id:
                bridge_client.dismiss_permission_host(permission_host.request_id)

    def _clear_acbridge_privilege_result(self) -> None:
        self._acbridge_privilege_result = None
        self._acbridge_privilege_key = None

    def _record_acbridge_privilege_result(
        self,
        status: PrivilegeStatus,
        result: ACBridgePrivilegeResult | None,
    ) -> None:
        if result is None:
            MainWindow._clear_acbridge_privilege_result(self)
            return
        self._acbridge_privilege_result = result
        self._acbridge_privilege_key = (
            status.backend,
            status.device_serial,
            status.device_generation,
        )

    def _status_with_acbridge_privilege(
        self,
        status: PrivilegeStatus | None,
    ) -> PrivilegeStatus | None:
        if status is None:
            return None
        key = (
            status.backend,
            status.device_serial,
            status.device_generation,
        )
        bridge = getattr(self, "_acbridge_privilege_result", None)
        if (
            status.backend is not PrivilegeBackend.ROOT
            or bridge is None
            or getattr(self, "_acbridge_privilege_key", None) != key
            or bridge.backend != PrivilegeBackend.ROOT.value
        ):
            return status
        bridge_message = (
            "ACBridge Root: granted (UID 0)."
            if bridge.ready
            else f"ACBridge Root: {bridge.message}"
        )
        return replace(
            status,
            message=f"Android shell: {status.message} {bridge_message}".strip(),
        )

    def request_shizuku_permission(self) -> None:
        if self.privilege_manager.selected_backend is not PrivilegeBackend.SHIZUKU:
            self.statusBar().showMessage(
                "Select Shizuku as the privileged-access backend first.",
                7000,
            )
            return
        try:
            context = self.device_manager.require_context(("ADB",))
        except DeviceContextUnavailable as exc:
            self.statusBar().showMessage(str(exc), 7000)
            return
        privilege_lease = self.privilege_manager.capture_operation_lease()
        self._start_privilege_operation(
            "request",
            context,
            lambda cancel_event: self.privilege_manager.request_shizuku(
                context,
                cancel_event=cancel_event,
                privilege_lease=privilege_lease,
            ),
            "Waiting for the Shizuku permission decision on Android…",
            interactive=True,
        )

    def open_shizuku(self) -> None:
        if self.privilege_manager.selected_backend is not PrivilegeBackend.SHIZUKU:
            self.statusBar().showMessage(
                "Select Shizuku as the privileged-access backend first.",
                7000,
            )
            return
        try:
            context = self.device_manager.require_context(("ADB",))
        except DeviceContextUnavailable as exc:
            self.statusBar().showMessage(str(exc), 7000)
            return
        privilege_lease = self.privilege_manager.capture_operation_lease()
        self._start_privilege_operation(
            "open",
            context,
            lambda cancel_event: self.privilege_manager.open_shizuku_manager(
                context,
                cancel_event=cancel_event,
                privilege_lease=privilege_lease,
            ),
            "Opening Shizuku on Android…",
            interactive=True,
        )

    def _start_privilege_operation(
        self,
        operation: str,
        context: DeviceContext,
        fn,
        busy_message: str,
        *,
        interactive: bool,
    ) -> bool:
        self._privilege_start_failure_kind = ""
        if self._privilege_token is not None:
            self._privilege_start_failure_kind = "busy"
            if not interactive:
                self._pending_privilege_recheck = True
            if interactive:
                self.statusBar().showMessage(
                    "Another privileged-access operation is already running.",
                    6000,
                )
            return False
        try:
            token = self.device_manager.operations.register(
                "privilege-access",
                device_context=context,
                conflict_group="device-command",
                conflict_groups=(
                    f"device-exclusive:{context.serial}",
                    f"acbridge-maintenance:{context.serial}",
                ),
            )
        except (OperationConflictError, RuntimeError) as exc:
            self._privilege_start_failure_kind = "busy"
            if not interactive:
                self._pending_privilege_recheck = True
            if interactive:
                self.statusBar().showMessage(str(exc), 7000)
            return False
        if not self.device_manager.is_context_current(context):
            self._privilege_start_failure_kind = "stale"
            token.cancel("device context changed before privilege operation started")
            self.device_manager.operations.finish(token)
            if interactive:
                self.statusBar().showMessage(
                    "The active device changed before the access check could start.",
                    7000,
                )
            return False
        self._privilege_token = token
        self._privilege_operation_kind = operation
        self._privilege_operation_interactive = bool(interactive)
        self._privilege_operation_busy_message = str(busy_message or "")
        MainWindow._refresh_privilege_busy_controls(self)
        worker = Worker(
            lambda: self._run_privilege_operation(token, context, fn)
        )
        worker.signals.result.connect(
            lambda result: self._privilege_operation_result(token, result)
        )
        worker.signals.error.connect(
            lambda message, _trace: self._privilege_operation_error(token, message)
        )
        worker.signals.finished.connect(
            lambda: self._privilege_operation_finished(token)
        )
        try:
            started = start_worker(
                self,
                self.device_bar.pool,
                worker,
                operation_registry=self.device_manager.operations,
                operation_token=token,
            )
        except RuntimeError as exc:
            self._privilege_start_failure_kind = "worker"
            token.cancel("privileged-access worker could not be started")
            self.device_manager.operations.finish(token)
            self._privilege_operation_finished(
                token,
                resume_automatic_features=False,
            )
            if interactive:
                self.statusBar().showMessage(str(exc), 7000)
            return False
        if started is False:
            self._privilege_start_failure_kind = "worker"
            self._privilege_operation_finished(
                token,
                resume_automatic_features=False,
            )
            return False
        return True

    def _run_privilege_operation(
        self,
        token: OperationToken,
        context: DeviceContext,
        fn,
    ):
        if token.cancelled:
            return None
        if not self.device_manager.is_context_current(context):
            token.cancel("device context changed before privilege worker execution")
            return None
        return fn(token.cancel_event)

    def _privilege_operation_result(self, token: OperationToken, result) -> None:
        if not self._privilege_callback_is_current(token) or result is None:
            return
        operation = getattr(self, "_privilege_operation_kind", "check")
        if isinstance(result, _PrivilegeHandshakeResult):
            MainWindow._record_acbridge_privilege_result(
                self,
                result.shell,
                result.bridge,
            )
            result = MainWindow._status_with_acbridge_privilege(
                self,
                result.shell,
            )
        if isinstance(result, PrivilegeStatus):
            if operation == "automatic-shizuku":
                context = token.device_context
                key = (
                    (context.serial, context.generation)
                    if context is not None
                    else None
                )
                result_key = (result.device_serial, result.device_generation)
                accepted = bool(
                    key is not None
                    and self._automatic_shizuku_inflight_key == key
                    and result.backend is PrivilegeBackend.SHIZUKU
                    and result.state not in {"cancelled", "error"}
                    and result_key == key
                )
                if accepted:
                    self._automatic_shizuku_inflight_key = None
                    self._last_automatic_shizuku_key = key
                    getattr(self, "_automatic_shizuku_attempts", {}).pop(
                        key,
                        None,
                    )
                    self._automatic_shizuku_failure_status = None
                    self._pending_privilege_recheck = False
                else:
                    return
            elif result.backend is PrivilegeBackend.SHIZUKU and result.state != "cancelled":
                self._automatic_shizuku_failure_status = None
            self._apply_privilege_status(result)
            self.statusBar().showMessage(result.message, 8000)
            return
        if isinstance(result, CommandResult):
            message = result.status or result.stderr or (
                "Shizuku opened on Android."
                if result.success
                else "Could not open Shizuku on Android."
            )
            self.statusBar().showMessage(message, 8000)
            if not result.success and getattr(
                self,
                "_privilege_operation_interactive",
                False,
            ):
                show_error_dialog(
                    self,
                    "Shizuku",
                    message,
                    self.settings.logs_folder,
                )
            elif operation == "open":
                QTimer.singleShot(
                    750,
                    lambda: self.check_privilege_access(interactive=False),
                )

    def _privilege_operation_error(
        self,
        token: OperationToken,
        message: str,
    ) -> None:
        if not self._privilege_callback_is_current(token):
            return
        self.statusBar().showMessage(message, 8000)
        if getattr(self, "_privilege_operation_interactive", False):
            show_error_dialog(
                self,
                "Privileged access",
                message,
                self.settings.logs_folder,
            )

    def _privilege_operation_finished(
        self,
        token: OperationToken,
        *,
        resume_automatic_features: bool = True,
    ) -> None:
        if self._privilege_token is not token:
            return
        completed_kind = getattr(self, "_privilege_operation_kind", "")
        context = getattr(token, "device_context", None)
        completed_key = (
            (context.serial, context.generation)
            if context is not None
            else None
        )
        if (
            completed_kind == "automatic-shizuku"
            and completed_key is not None
            and self._automatic_shizuku_inflight_key == completed_key
        ):
            self._automatic_shizuku_inflight_key = None
            automatic_is_current = bool(
                not self._closing
                and self.privilege_manager.selected_backend
                is PrivilegeBackend.SHIZUKU
                and context is not None
                and self.device_manager.is_context_current(context)
            )
            if automatic_is_current:
                attempts = getattr(self, "_automatic_shizuku_attempts", {})
                attempt_count = attempts.get(
                    completed_key,
                    0,
                )
                maximum_attempts = getattr(
                    self,
                    "AUTOMATIC_SHIZUKU_MAX_ATTEMPTS",
                    MainWindow.AUTOMATIC_SHIZUKU_MAX_ATTEMPTS,
                )
                if attempt_count < maximum_attempts:
                    self._pending_automatic_shizuku_context = context
                    self._pending_privilege_recheck = True
                    self._queue_automatic_shizuku_start(context, delay_ms=750)
                else:
                    failure_status = PrivilegeStatus(
                        backend=PrivilegeBackend.SHIZUKU,
                        state="error",
                        level="unavailable",
                        message=(
                            "Automatic Shizuku permission request and access check "
                            "could not complete after two attempts. Use Check access "
                            "or Request permission to retry."
                        ),
                        device_serial=context.serial,
                        device_generation=context.generation,
                    )
                    self._automatic_shizuku_failure_status = failure_status
                    self._last_automatic_shizuku_key = completed_key
                    attempts.pop(completed_key, None)
                    self._pending_privilege_recheck = False
        self._privilege_token = None
        self._privilege_operation_kind = ""
        self._privilege_operation_interactive = False
        self._privilege_operation_busy_message = ""
        cached_status = MainWindow._status_with_acbridge_privilege(
            self,
            self.privilege_manager.cached_status(),
        )
        failure_status = getattr(
            self,
            "_automatic_shizuku_failure_status",
            None,
        )
        if failure_status is not None:
            status_is_current = getattr(
                self.privilege_manager,
                "status_is_current",
                None,
            )
            failure_is_current = False
            if callable(status_is_current):
                failure_is_current = bool(status_is_current(failure_status))
            elif context is not None and (
                failure_status.device_serial,
                failure_status.device_generation,
            ) == (context.serial, context.generation):
                failure_is_current = True
            if failure_is_current and (
                cached_status is None
                or str(cached_status.state or "").casefold()
                in {"cancelled", "error"}
            ):
                cached_status = failure_status
        self._apply_privilege_status(cached_status)
        transition_blockers_draining = (
            MainWindow._privilege_transition_blockers_are_draining(self)
        )
        if not transition_blockers_draining:
            self._privilege_transition_drain_scheduled = False
        if transition_blockers_draining and not self._closing:
            MainWindow._queue_privilege_transition_drain_check(self)
        if self._pending_privilege_recheck and not self._closing:
            if transition_blockers_draining:
                MainWindow._queue_privilege_transition_drain_check(self)
            else:
                MainWindow._schedule_privilege_recheck(self)
        feature_barrier_active = bool(
            completed_kind == "automatic-shizuku"
            or getattr(self, "_privilege_barrier_waits_for_recheck", False)
        )
        if feature_barrier_active and not self._closing:
            workflow_pending = bool(
                self._automatic_shizuku_workflow_pending()
                or getattr(self, "_privilege_transition_drain_scheduled", False)
                or transition_blockers_draining
                or (
                    self._privilege_barrier_waits_for_recheck
                    and (
                        self._privilege_token is not None
                        or self._pending_privilege_recheck
                        or bool(
                            getattr(
                                self,
                                "_privilege_recheck_callback_scheduled",
                                False,
                            )
                        )
                    )
                )
            )
            if workflow_pending:
                if self._automatic_shizuku_workflow_pending():
                    self._set_automatic_shizuku_ui_busy(True)
                if self._privilege_barrier_waits_for_recheck:
                    MainWindow._set_privilege_feature_barrier_busy(self, True)
            else:
                self._privilege_barrier_waits_for_recheck = False
                MainWindow._set_privilege_feature_barrier_busy(self, False)
                self._set_automatic_shizuku_ui_busy(False)
                if resume_automatic_features:
                    self._resume_feature_refresh_after_acbridge()
        elif (
            resume_automatic_features
            and not self._closing
            and bool(getattr(self, "_pending_acbridge_feature_refresh", None))
            and MainWindow._selected_backend_usable_for_device_features(self)
        ):
            self._resume_feature_refresh_after_acbridge()
        MainWindow._refresh_privilege_busy_controls(self)

    def _apply_privilege_status(
        self,
        status: PrivilegeStatus | None,
    ) -> None:
        if getattr(self, "_closing", False):
            return
        manager = getattr(self, "privilege_manager", None)
        profile_available = getattr(self, "_privilege_profile_available", True)
        configured_value = (
            self._configured_privilege_value()
            if hasattr(self, "settings")
            else getattr(status, "backend", PrivilegeBackend.STANDARD)
        )
        backend = PrivilegeBackend.normalize(configured_value)
        pending_queued = profile_available or bool(
            str(getattr(configured_value, "value", configured_value) or "").strip()
        )
        if not profile_available:
            status = None
        elif status is None:
            active = getattr(getattr(self, "device_manager", None), "active", None)
            active_serial = str(getattr(active, "serial", "") or "")
            active_mode = str(getattr(active, "mode", "No device") or "No device")
            active_state = str(getattr(active, "state", "") or "").casefold()
            unavailable_message = ""
            if active_serial and active_state == "unauthorized":
                unavailable_message = "Authorize ADB on the Android device first."
            elif active_serial and active_state == "offline":
                unavailable_message = "The selected Android device is offline."
            elif (
                active_serial
                and backend is PrivilegeBackend.SHIZUKU
                and active_mode != "ADB"
            ):
                unavailable_message = (
                    f"Shizuku is unavailable while the device is in {active_mode} mode."
                )
            elif (
                active_serial
                and backend is PrivilegeBackend.ROOT
                and active_mode not in {"ADB", "Recovery"}
            ):
                unavailable_message = (
                    f"Root shell access is unavailable while the device is in {active_mode} mode."
                )
            elif (
                active_serial
                and backend is PrivilegeBackend.STANDARD
                and active_mode not in {"ADB", "Recovery", "Sideload"}
            ):
                unavailable_message = (
                    f"Standard ADB shell access is unavailable while the device is in {active_mode} mode."
                )
            if unavailable_message:
                status = PrivilegeStatus(
                    backend=backend,
                    state="unavailable",
                    level="unavailable",
                    message=unavailable_message,
                    device_serial=active_serial,
                    device_generation=getattr(
                        getattr(self, "device_manager", None),
                        "current_generation",
                        None,
                    ),
                )
        if status is not None and manager is not None:
            status_backend_matches = status.backend is backend
            has_device_identity = bool(status.device_serial) or status.device_generation is not None
            status_is_current = getattr(manager, "status_is_current", None)
            if not status_backend_matches or (
                has_device_identity
                and callable(status_is_current)
                and not status_is_current(status)
            ):
                status = None
        self._last_privilege_display_status = status
        selector = getattr(self, "privilege_mode_selector", None)
        if selector is not None:
            if profile_available:
                selector.set_backend(backend)
            else:
                selector.set_pending_backend(configured_value)
        text = MainWindow._privilege_status_text(
            status,
            backend,
            profile_available=profile_available,
            pending_queued=pending_queued,
        )
        MainWindow._set_global_privilege_status_text(self, text)
        self.settings_page.set_privilege_status(status)
        self.commands_page.set_privilege_status(status)
        apps_page = getattr(self, "apps_page", None)
        update_apps_status = getattr(
            apps_page,
            "update_privilege_status",
            None,
        )
        if callable(update_apps_status):
            update_apps_status(status)
        backups_page = getattr(self, "backups_page", None)
        update_backups_status = getattr(
            backups_page,
            "update_privilege_status",
            None,
        )
        if callable(update_backups_status):
            update_backups_status(status)
        if profile_available:
            self.dashboard.update_privilege_status(status)
        else:
            self.dashboard.update_privilege_status(
                status,
                backend=backend,
                profile_available=False,
                pending_queued=pending_queued,
            )
        file_manager = getattr(self, "file_manager_page", None)
        set_file_manager_status = getattr(file_manager, "set_privilege_status", None)
        if callable(set_file_manager_status):
            if profile_available:
                set_file_manager_status(status)
            else:
                set_file_manager_status(
                    status,
                    backend=backend,
                    profile_available=False,
                    pending_queued=pending_queued,
                )
        # A reset or worker result may update the underlying status while a
        # backend transition is still draining.  Keep the transient barrier
        # visible until its final owner releases it; otherwise users briefly
        # see "not checked" and can mistake an in-progress switch for failure.
        if bool(
            getattr(self, "_privilege_token", None) is not None
            or getattr(self, "_privilege_feature_barrier_busy", False)
            or getattr(self, "_automatic_shizuku_ui_busy", False)
        ):
            MainWindow._refresh_privilege_busy_controls(self)

    @staticmethod
    def _privilege_status_text(
        status: PrivilegeStatus | None,
        backend: PrivilegeBackend,
        *,
        profile_available: bool = True,
        pending_queued: bool = True,
    ) -> str:
        if status is None:
            if not profile_available:
                if not pending_queued:
                    return "Next device: choose an access mode"
                return {
                    PrivilegeBackend.STANDARD: "Next device: Standard ADB",
                    PrivilegeBackend.ROOT: "Next device: Root when available",
                    PrivilegeBackend.SHIZUKU: "Next device: Shizuku when available",
                }[backend]
            return {
                PrivilegeBackend.STANDARD: "Standard ADB; no Root or Shizuku requested",
                PrivilegeBackend.ROOT: "Root: not checked",
                PrivilegeBackend.SHIZUKU: "Shizuku: not checked",
            }[backend]
        prefix = {
            PrivilegeBackend.STANDARD: "Standard ADB",
            PrivilegeBackend.ROOT: "Root",
            PrivilegeBackend.SHIZUKU: "Shizuku",
        }.get(status.backend, "Privilege")
        message = str(status.message or "Unavailable").strip()
        normalized_message = message.casefold()
        normalized_prefix = prefix.casefold()
        if normalized_message == normalized_prefix or normalized_message.startswith(
            (f"{normalized_prefix} ", f"{normalized_prefix}:")
        ):
            return message
        return f"{prefix}: {message}"

    def _set_global_privilege_status_text(self, text: str) -> None:
        text = str(text or "Privilege status unavailable.")
        label = getattr(self, "privilege_runtime_status", None)
        if label is not None:
            label.setText(text)
            label.setAccessibleName(f"Privilege access status: {text}")
        selector = getattr(self, "privilege_mode_selector", None)
        if selector is not None:
            selector.set_runtime_status(text)

    def _set_privilege_profile_available(self, available: bool) -> None:
        """Select for the active profile or queue a mode for the next one."""

        available = bool(available)
        self._privilege_profile_available = available
        configured_value = self._configured_privilege_value()
        selector = getattr(self, "privilege_mode_selector", None)
        if selector is not None:
            selector.setEnabled(True)
            selector.set_profile_available(available)
            if available:
                selector.set_backend(configured_value)
            else:
                selector.set_pending_backend(configured_value)
        for page_name in ("settings_page", "commands_page"):
            page = getattr(self, page_name, None)
            setter = getattr(page, "set_privilege_profile_available", None)
            if callable(setter):
                setter(available)
        panel = getattr(self, "privilege_status_panel", None)
        if panel is not None:
            pending_queued = bool(
                str(getattr(configured_value, "value", configured_value) or "").strip()
            )
            panel.setToolTip(
                (
                    (
                        "No device is active. The selected access mode will be applied "
                        "once to the next active Android device profile."
                    )
                    if pending_queued
                    else (
                        "No device is active. Choose an access mode to apply it once "
                        "to the next active Android device profile."
                    )
                )
                if not available
                else "Access mode for the active Android device profile."
            )
        cached_status = None
        if available:
            manager = getattr(self, "privilege_manager", None)
            cached_status_getter = getattr(manager, "cached_status", None)
            if callable(cached_status_getter):
                cached_status = cached_status_getter()
        self._apply_privilege_status(cached_status)
        if not available:
            self._last_privilege_backend = self._configured_privilege_backend()

    def _invalidate_privilege_status(self) -> None:
        self.privilege_manager.reset()
        MainWindow._clear_acbridge_privilege_result(self)
        MainWindow._recover_privilege_status_after_runtime_invalidation(self)

    def _recover_privilege_status_after_runtime_invalidation(self) -> None:
        """Re-establish the selected backend after a live session becomes stale."""

        if self._closing:
            return
        self._apply_privilege_status(None)
        backend = self.privilege_manager.selected_backend
        if backend is PrivilegeBackend.SHIZUKU:
            self._last_automatic_shizuku_key = None
            self._automatic_shizuku_attempts.clear()
            self._automatic_shizuku_failure_status = None
        if backend is not PrivilegeBackend.STANDARD and not self._closing:
            self._pending_privilege_recheck = True
            self._schedule_privilege_recheck()

    def _privilege_callback_is_current(self, token: OperationToken) -> bool:
        if (
            self._closing
            or token.cancelled
            or self._privilege_token is not token
            or not self.device_manager.operations.contains(token)
        ):
            return False
        context = token.device_context
        return context is None or self.device_manager.is_context_current(context)

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
                self._set_privilege_profile_available(True)
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
            lambda _result: self._clear_wireless_qr_dialog(dialog)
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
        self.dashboard.set_wireless_status(dialog.status.full_text())
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
    ) -> None:
        # The worker normally releases the attempt before the user closes the
        # completed/cancelled dialog.  Dialog identity, rather than the already
        # finished attempt, is therefore the authoritative stale-callback guard.
        if self._wireless_qr_dialog is not dialog:
            return
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
        active_serial = str(getattr(self.device_manager.active, "serial", "") or "")
        profile_serial = str(
            getattr(self.settings, "active_profile_serial", "") or ""
        )
        if profile_changed:
            profile_available = bool(active_serial and active_serial == profile_serial)
            if profile_available != self._privilege_profile_available:
                self._set_privilege_profile_available(profile_available)
        previous_privilege_backend = self._last_privilege_backend
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
        current_privilege_value = self._configured_privilege_value()
        current_privilege_backend = PrivilegeBackend.normalize(
            current_privilege_value
        )
        if self._privilege_profile_available:
            self.privilege_mode_selector.set_backend(current_privilege_backend)
        else:
            self.privilege_mode_selector.set_pending_backend(
                current_privilege_value
            )
        self._last_privilege_backend = current_privilege_backend
        if current_privilege_backend is not previous_privilege_backend:
            privilege_barrier_is_draining = self._privilege_token is not None
            MainWindow._capture_privilege_transition_blockers(self)
            cancel_privilege_operations = getattr(
                self.device_manager.operations,
                "cancel_privilege_operations",
                None,
            )
            if callable(cancel_privilege_operations):
                cancel_privilege_operations("selected access mode changed")
            feature_operations_are_draining = (
                MainWindow._privilege_transition_blockers_are_draining(self)
            )
            privilege_transition_is_draining = bool(
                privilege_barrier_is_draining or feature_operations_are_draining
            )
            active = self.device_manager.active
            device_features_connected = bool(
                self._privilege_profile_available
                and active.mode in {"ADB", "Recovery"}
                and str(active.state or "").casefold() == "device"
            )
            device_features_available = bool(
                device_features_connected
                and not (
                    current_privilege_backend is PrivilegeBackend.SHIZUKU
                    and active.mode != "ADB"
                )
            )
            if device_features_connected:
                invalidate_file_manager = getattr(
                    self.file_manager_page,
                    "invalidate_privilege_backend_view",
                    None,
                )
                if callable(invalidate_file_manager):
                    invalidate_file_manager()
            pending_feature_refresh = getattr(
                self,
                "_pending_acbridge_feature_refresh",
                None,
            )
            if pending_feature_refresh is None:
                pending_feature_refresh = set()
                self._pending_acbridge_feature_refresh = pending_feature_refresh
            if device_features_available:
                pending_feature_refresh.update(
                    {"apps", "file-manager"}
                )
            else:
                pending_feature_refresh.difference_update(
                    {"apps", "file-manager"}
                )
            if privilege_transition_is_draining:
                self._privilege_barrier_waits_for_recheck = True
                MainWindow._set_privilege_feature_barrier_busy(self, True)
                MainWindow._queue_privilege_transition_drain_check(self)
            if self._privilege_token is not None:
                self._privilege_token.cancel("privileged-access backend changed")
            self.privilege_manager.reset()
            MainWindow._clear_acbridge_privilege_result(self)
            self._apply_privilege_status(None)
            self._last_automatic_shizuku_key = None
            self._automatic_shizuku_attempts.clear()
            self._automatic_shizuku_failure_status = None
            if current_privilege_backend is not PrivilegeBackend.SHIZUKU:
                self._clear_pending_automatic_shizuku()
                self._pending_privilege_recheck = False
                if not privilege_transition_is_draining:
                    self._automatic_shizuku_inflight_key = None
                    self._set_automatic_shizuku_ui_busy(False)
            should_recheck = bool(
                not profile_changed
                and (
                    (
                        current_privilege_backend is PrivilegeBackend.SHIZUKU
                        and self.device_manager.active.mode == "ADB"
                    )
                    or (
                        current_privilege_backend is PrivilegeBackend.ROOT
                        and self.device_manager.active.mode in {"ADB", "Recovery"}
                    )
                )
                and str(self.device_manager.active.state or "").casefold()
                == "device"
            )
            if should_recheck:
                self._privilege_barrier_waits_for_recheck = True
                MainWindow._set_privilege_feature_barrier_busy(self, True)
                if privilege_transition_is_draining:
                    self._pending_privilege_recheck = True
                else:
                    self._schedule_privilege_recheck()
            elif not privilege_transition_is_draining:
                self._clear_pending_automatic_shizuku()
                self._pending_privilege_recheck = False
                self._automatic_shizuku_inflight_key = None
                self._privilege_barrier_waits_for_recheck = False
                MainWindow._set_privilege_feature_barrier_busy(self, False)
                self._set_automatic_shizuku_ui_busy(False)
                self._resume_feature_refresh_after_acbridge()
        elif not self._privilege_profile_available:
            # An empty queue and an explicit Standard override normalize to the
            # same backend, but they are distinct offline UI states.
            self._apply_privilege_status(None)
        if backup_root_changed:
            self.backups_page.reset_for_device_profile()
            self.backups_page.refresh()
        if profile_changed:
            self.system_theme_controller.set_theme(
                str(self.settings.get("theme", "System"))
            )
            self._schedule_settings_recovery_warning()

    def _clear_icon_cache(self) -> None:
        self.icon_extractor.clear_cache()
        QMessageBox.information(self, "Icon cache", "Icon cache cleared.")

    def _clear_temporary_files(self) -> None:
        folder = str(self.settings.get("temp_folder", ""))
        answer = exec_bounded_message_box(
            self,
            "Clear temporary files",
            (
                "Delete all files in the active OpenADB temporary folder?\n\n"
                "APK backups and logs will not be deleted."
            ),
            icon=QMessageBox.Warning,
            buttons=QMessageBox.Ok | QMessageBox.Cancel,
            default_button=QMessageBox.Cancel,
            detailed_text=f"Active OpenADB temporary folder:\n{folder}",
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
        answer = exec_bounded_message_box(
            self,
            "Reset UI settings",
            (
                "Reset theme, window/navigation layout, Dashboard expansion, application filters, and "
                "File Manager view state for the global configuration and active device profile?\n\n"
                "Platform Tools, storage folders, safety preferences, profiles, caches, logs, and APK "
                "backups will be preserved."
            ),
            icon=QMessageBox.Warning,
            buttons=QMessageBox.Ok | QMessageBox.Cancel,
            default_button=QMessageBox.Cancel,
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
        delete_backups = bool(
            self.settings_page.delete_apk_backups_on_full_reset.isChecked()
        )
        app_data_busy = bool(
            getattr(self.apps_page, "_apps_loading", False)
            or getattr(self.apps_page, "_assets_loading", False)
            or getattr(self.apps_page, "_bulk_operation_busy", False)
        )
        backup_data_busy = bool(
            getattr(self.backups_page, "_loading", False)
            or getattr(self.backups_page, "_action_busy", False)
        )
        if app_data_busy or (delete_backups and backup_data_busy):
            if delete_backups:
                self.settings_page.delete_apk_backups_on_full_reset.setChecked(False)
            QMessageBox.information(
                self,
                "Reset settings and caches",
                (
                    "An application or APK-backup operation is still running. Wait until it finishes, "
                    "then reset settings and caches. No data was deleted."
                ),
            )
            return
        backup_roots = self.settings.apk_backup_folders() if delete_backups else ()
        backup_section = (
            "Also permanently deleted:\n"
            "- all recognized OpenADB APK backup snapshots\n"
            "- base and split APK files\n"
            "- backup metadata, icons, command logs, and incomplete backups\n\n"
            "No APK backup will remain available for restoring uninstalled applications.\n\n"
            if delete_backups
            else (
                "Preserved:\n"
                "- APK backup folders and their contents\n"
                "- log files\n"
                "- files outside verified OpenADB cache/temp folders\n\n"
                "Deleting APK backups is not part of this reset.\n\n"
            )
        )
        answer = exec_bounded_message_box(
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
                f"{backup_section}"
                "Continue?"
            ),
            icon=QMessageBox.Warning,
            buttons=QMessageBox.Ok | QMessageBox.Cancel,
            default_button=QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            self.settings_page.delete_apk_backups_on_full_reset.setChecked(False)
            self.statusBar().showMessage("Settings/cache reset cancelled.", 5000)
            return

        if delete_backups:
            existing_roots = [path for path in backup_roots if path.exists()]
            displayed_roots = existing_roots[:10]
            paths_text = "\n".join(f"- {path}" for path in displayed_roots)
            if len(existing_roots) > len(displayed_roots):
                paths_text += (
                    f"\n- ... and {len(existing_roots) - len(displayed_roots)} more configured folder(s)"
                )
            if not paths_text:
                paths_text = "- No existing configured backup folders were found."
            destructive_answer = exec_bounded_message_box(
                self,
                "PERMANENTLY DELETE ALL APK BACKUPS",
                (
                    "IRREVERSIBLE DATA LOSS\n\n"
                    "You selected deletion of APK backups from every global, Phone, TV, and legacy "
                    "OpenADB profile. Base APKs, split APKs, metadata, icons, snapshot command logs, "
                    "failed backups, and incomplete backups will be permanently removed.\n\n"
                    "The files will NOT be moved to the Recycle Bin and cannot be recovered by OpenADB. "
                    "Applications uninstalled after creating these backups may no longer be restorable.\n\n"
                    "If a storage device disconnects or an I/O error occurs during cleanup, some backups "
                    "may already be permanently gone even though the remaining reset is stopped.\n\n"
                    "Open Details to review every configured backup location before continuing.\n\n"
                    "Unrelated files in shared external folders will be preserved.\n\n"
                    "Permanently delete the APK backups and continue the full reset?"
                ),
                icon=QMessageBox.Critical,
                buttons=QMessageBox.Yes | QMessageBox.Cancel,
                default_button=QMessageBox.Cancel,
                detailed_text=f"Configured backup locations:\n{paths_text}",
            )
            if destructive_answer != QMessageBox.Yes:
                self.settings_page.delete_apk_backups_on_full_reset.setChecked(False)
                self.statusBar().showMessage(
                    "Full reset with APK backup deletion cancelled.",
                    6000,
                )
                return

        self.settings_page.reset_all.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        cleanup_result = None
        removed: list[str] = []
        operation_error = ""
        try:
            if delete_backups:
                self.statusBar().showMessage(
                    "Permanently deleting recognized OpenADB APK backups..."
                )
                cleanup_result = self.settings.clear_apk_backups(
                    expected_folders=backup_roots,
                )
                if not cleanup_result.success:
                    partial_note = (
                        f"\n\n{len(cleanup_result.removed_snapshots)} recognized snapshot(s) had already "
                        "been permanently deleted before the error."
                        if cleanup_result.removed_snapshots
                        else "\n\nNo backup snapshot was deleted."
                    )
                    operation_error = (
                        "APK backup cleanup was not completed. Settings and caches were preserved so "
                        "the configured locations remain available.\n\n"
                        + "\n".join(cleanup_result.failures[:10])
                        + partial_note
                    )
            if not operation_error:
                self.device_manager.invalidate_profile(
                    "settings and profile data were reset"
                )
                removed = self.settings.reset_settings_and_caches()
                self.platform_tools.active = PlatformToolsInfo()
                self._update_tools(self.platform_tools.active)
                self.apps_page.reset_for_device_profile()
                self._settings_changed(profile_changed=True)
                if delete_backups:
                    self.backups_page.reset_for_device_profile()
                    self.backups_page.refresh()
        except Exception as exc:  # noqa: BLE001 - report local reset failures without crashing the GUI
            operation_error = f"The full reset could not be completed: {exc}"
            if cleanup_result is not None and cleanup_result.removed_snapshots:
                operation_error += (
                    f"\n\n{len(cleanup_result.removed_snapshots)} APK backup snapshot(s) were already "
                    "permanently deleted before the reset failed."
                )
        finally:
            QApplication.restoreOverrideCursor()
            self.settings_page.reset_all.setEnabled(True)
            self.settings_page.delete_apk_backups_on_full_reset.setChecked(False)

        if operation_error:
            if cleanup_result is not None and cleanup_result.removed_snapshots:
                self.backups_page.reset_for_device_profile()
                self.backups_page.refresh()
            self.statusBar().showMessage("Full reset was not completed.", 10000)
            show_error_dialog(
                self,
                "Reset was not completed",
                operation_error,
                self.settings.logs_folder,
            )
            return

        status = (
            "All settings, caches, and recognized APK backups were reset."
            if delete_backups
            else "All settings and caches were reset."
        )
        self.statusBar().showMessage(status, 8000)
        detail = f"\n\nRemoved entries: {len(removed)}." if removed else ""
        if delete_backups and cleanup_result is not None:
            backup_summary = (
                f" APK backups were permanently deleted ({len(cleanup_result.removed_snapshots)} snapshot(s))."
            )
        else:
            backup_summary = " Backups were preserved."
        QMessageBox.information(
            self,
            "Reset settings and caches",
            "All OpenADB settings and caches were reset."
            + backup_summary
            + detail,
        )

    def closeEvent(self, event) -> None:
        if self._closing:
            super().closeEvent(event)
            return
        self._closing = True
        self._privilege_transition_drain_scheduled = False
        self.privilege_manager.remove_status_listener(self._privilege_status_callback)
        self.privilege_manager.remove_invalidation_listener(
            self._privilege_invalidation_callback
        )
        self.settings.remove_recovery_listener(self._settings_recovery_callback)
        self._settings_recovery_timer.stop()
        self.system_theme_controller.stop()
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
        self._pending_acbridge_update_context = None
        self._acbridge_update_retry_key = None
        self._acbridge_update_attempts.clear()
        self._acbridge_maintenance_ui_busy = False
        self._pending_automatic_shizuku_context = None
        self._automatic_shizuku_scheduled_key = None
        self._automatic_shizuku_inflight_key = None
        self._automatic_shizuku_attempts.clear()
        self._automatic_shizuku_failure_status = None
        self._automatic_shizuku_ui_busy = False
        self._privilege_feature_barrier_busy = False
        self._privilege_operation_busy_message = ""
        self._privilege_transition_blocker_ids.clear()
        self._privilege_recheck_callback_scheduled = False
        self._privilege_barrier_waits_for_recheck = False
        self.device_manager.operations.shutdown()
        self.commands_page.cancel_running_command()
        self.file_manager_page.cancel_active_transfers()
        self.taskbar_progress.close()
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
