from __future__ import annotations

import csv
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, fields
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.acbridge import ACBridgeClient
from openadb.core.adb import ADBClient
from openadb.core.app_cache import AppInfoCache
from openadb.core.apk_metadata import APKMetadataExtractor
from openadb.core.backup_manager import BackupManager
from openadb.core.bloatware_db import BloatwareDatabase
from openadb.core.device import DeviceManager
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable, StaleDeviceContext
from openadb.core.icon_extractor import IconExtractor
from openadb.core.operations import OperationConflictError, OperationRegistry, OperationToken
from openadb.core.path_utils import ensure_dir, format_bytes, safe_filename
from openadb.core.safety import is_dangerous_package
from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.models.device_info import DeviceInfo
from openadb.ui.design_system import configure_page_layout, set_button_role
from openadb.ui.dialogs import show_error_dialog
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.widgets.app_list_widget import APP_SORT_MODES, AppFilterState, AppTable
from openadb.ui.widgets.elided_label import ElidedLabel
from openadb.ui.workers import Worker, start_worker


@dataclass(frozen=True, slots=True)
class _CapturedProfileSettings:
    """Minimal immutable settings view used by one device-bound worker."""

    config_dir: Path
    backups_folder: Path
    temp_folder: Path
    logs_folder: Path


@dataclass(slots=True)
class _AppsProfileServices:
    settings: _CapturedProfileSettings
    app_cache: AppInfoCache
    apk_metadata: APKMetadataExtractor
    icon_extractor: IconExtractor
    include_system: bool


class VisibleSelectionCheckBox(QCheckBox):
    """Two-state user control that can display a computed partial state."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setTristate(True)

    def nextCheckState(self) -> None:  # noqa: N802 - Qt API name
        self.setCheckState(Qt.Unchecked if self.checkState() == Qt.Checked else Qt.Checked)


class AppsPage(QWidget):
    refresh_device_requested = Signal()

    def __init__(
        self,
        adb: ADBClient,
        backup_manager: BackupManager,
        device_manager: DeviceManager,
        icon_extractor: IconExtractor,
        settings: SettingsManager,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.adb = adb
        self.backup_manager = backup_manager
        self.device_manager = device_manager
        self.icon_extractor = icon_extractor
        self.apk_metadata = APKMetadataExtractor(settings)
        self.app_cache = AppInfoCache(settings)
        self.bloatware_db = BloatwareDatabase()
        self.settings = settings
        operations = getattr(device_manager, "operations", None)
        self.operations = operations if isinstance(operations, OperationRegistry) else OperationRegistry()
        self.pool = QThreadPool.globalInstance()
        self.apps: list[AppInfo] = []
        self._apps_loading = False
        self._assets_loading = False
        self._metadata_cache_updates_since_flush = 0
        self._asset_cache_updates_since_flush = 0
        self._asset_progress_status = ""
        self._suppress_cache_save = False
        self._sort_mode = "name"
        self._bulk_operation_busy = False
        self._bulk_operation_name = ""
        self._refresh_after_bulk = False
        self._apps_load_token: OperationToken | None = None
        self._metadata_token: OperationToken | None = None
        self._assets_token: OperationToken | None = None
        self._bulk_token: OperationToken | None = None
        self._apps_view_context: DeviceContext | None = None
        self._apps_view_serial = ""
        self._apps_view_profile_path = Path(self.settings.config_dir)
        self._device_mode = str(
            getattr(getattr(self.device_manager, "active", None), "mode", "No device") or "No device"
        )
        self._search_filter_timer = QTimer(self)
        self._search_filter_timer.setSingleShot(True)
        self._search_filter_timer.setInterval(120)
        layout = QVBoxLayout(self)
        configure_page_layout(layout)

        header = QHBoxLayout()
        title = QLabel("Applications")
        title.setObjectName("pageTitle")
        self.total_label = QLabel("Showing 0 of 0 applications")
        self.total_label.setObjectName("appCountLabel")
        self.active_filters_label = ElidedLabel("No active filters")
        self.active_filters_label.setObjectName("appFilterSummary")
        header.addWidget(title)
        header.addWidget(self.total_label)
        header.addStretch()
        layout.addLayout(header)

        toolbar = QFrame()
        toolbar.setObjectName("appsTopBar")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 8, 8, 8)
        toolbar_layout.setSpacing(6)

        controls = QHBoxLayout()
        self.refresh_button = QPushButton("Load applications")
        set_button_role(self.refresh_button, "primary")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search application name or package...")
        self.sort_button = QPushButton("Sort: name")
        self.sort_button.setToolTip("Choose application size sorting")
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.search, 1)
        controls.addWidget(self.sort_button)
        toolbar_layout.addLayout(controls)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.filters_button = QToolButton()
        self.filters_button.setObjectName("appsFiltersButton")
        self.filters_button.setText("Filters")
        self.filters_button.setPopupMode(QToolButton.InstantPopup)
        self.filters_menu = QMenu(self.filters_button)
        self.filters_button.setMenu(self.filters_menu)
        self._filter_values = {"type": "all", "state": "any", "uad": "any"}
        self._filter_action_groups: dict[str, QActionGroup] = {}
        self._filter_actions: dict[str, dict[str, QAction]] = {}
        self._add_filter_menu_group("type", "Type", [("All", "all"), ("User", "user"), ("System", "system")])
        self._add_filter_menu_group(
            "state",
            "State",
            [("Any", "any"), ("Enabled", "enabled"), ("Disabled", "disabled")],
        )
        self._add_filter_menu_group(
            "uad",
            "UAD category",
            [
                ("Any", "any"),
                ("Recommended", "recommended"),
                ("Advanced", "advanced"),
                ("Expert", "expert"),
                ("Unsafe", "unsafe"),
                ("Not listed", "not listed"),
            ],
        )
        self.reset_filters_button = QPushButton("Reset filters")
        self.reset_filters_button.setObjectName("appsResetFilters")
        filters.addWidget(self.filters_button)
        filters.addWidget(self.reset_filters_button)
        filters.addWidget(self.active_filters_label, 1)
        toolbar_layout.addLayout(filters)

        layout.addWidget(toolbar)

        self.bulk_action_bar = QFrame()
        self.bulk_action_bar.setObjectName("appsBulkActionBar")
        bulk_layout = QGridLayout(self.bulk_action_bar)
        bulk_layout.setContentsMargins(8, 8, 8, 8)
        bulk_layout.setHorizontalSpacing(8)
        bulk_layout.setVerticalSpacing(6)
        self.select_all_check = VisibleSelectionCheckBox("Select visible")
        self.selection_summary_label = ElidedLabel("0 selected")
        self.selection_summary_label.setObjectName("appsSelectionSummary")
        self.clear_selection_button = QPushButton("Clear selection")
        self.backup_button = QPushButton("Backup selected")
        self.uninstall_button = QPushButton("Uninstall selected")
        self.uninstall_button.setProperty("danger", True)
        set_button_role(self.uninstall_button, "danger")
        self.disable_button = QPushButton("Disable selected")
        self.enable_button = QPushButton("Enable selected")
        self.more_button = QToolButton()
        self.more_button.setObjectName("appsMoreButton")
        self.more_button.setText("More")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_menu = QMenu(self.more_button)
        self.install_existing_action = self.more_menu.addAction("Install existing")
        self.export_action = self.more_menu.addAction("Export package list")
        self.more_menu.addSeparator()
        self.clear_cache_action = self.more_menu.addAction("Clear apps cache…")
        self.more_button.setMenu(self.more_menu)

        bulk_layout.addWidget(self.select_all_check, 0, 0)
        bulk_layout.addWidget(self.selection_summary_label, 0, 1)
        bulk_layout.addWidget(self.clear_selection_button, 0, 2)
        bulk_layout.addWidget(self.more_button, 0, 3)
        bulk_layout.addWidget(self.backup_button, 1, 0, 1, 2)
        bulk_layout.addWidget(self.uninstall_button, 1, 2, 1, 2)
        bulk_layout.addWidget(self.enable_button, 2, 0, 1, 2)
        bulk_layout.addWidget(self.disable_button, 2, 2, 1, 2)
        for column in range(4):
            bulk_layout.setColumnStretch(column, 1)
        layout.addWidget(self.bulk_action_bar)

        self.table = AppTable()
        self.apps_empty_state = EmptyState(
            "No applications loaded",
            "Load applications from the active Android device to begin.",
            "Load applications",
        )
        self.apps_content = QStackedWidget()
        self.apps_content.addWidget(self.table)
        self.apps_content.addWidget(self.apps_empty_state)
        layout.addWidget(self.apps_content, 1)

        self.status_label = QLabel("Press Load applications to read packages from the connected device.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.refresh_button.clicked.connect(self.refresh_apps)
        self.search.textChanged.connect(self._schedule_search_filter)
        self.sort_button.clicked.connect(self._show_sort_menu_from_button)
        self.reset_filters_button.clicked.connect(self.reset_filters)
        self._search_filter_timer.timeout.connect(self.apply_filter)
        self.select_all_check.stateChanged.connect(self._select_visible_state_changed)
        self.table.selection_changed.connect(self._update_app_count)
        self.clear_selection_button.clicked.connect(self.table.unselect_all)
        self.backup_button.clicked.connect(self.backup_selected)
        self.uninstall_button.clicked.connect(self.uninstall_selected)
        self.disable_button.clicked.connect(lambda: self.set_enabled_selected(False))
        self.enable_button.clicked.connect(lambda: self.set_enabled_selected(True))
        self.install_existing_action.triggered.connect(self.install_existing_selected)
        self.export_action.triggered.connect(self.export_packages)
        self.clear_cache_action.triggered.connect(self.clear_apps_cache)
        self.apps_empty_state.action_requested.connect(self._handle_empty_state_action)
        self.reload_filter_state()
        self._load_cached_apps_for_saved_device()
        self._update_action_states()

    def _captured_profile_settings(self, context: DeviceContext) -> _CapturedProfileSettings:
        return _CapturedProfileSettings(
            config_dir=context.profile_path,
            backups_folder=context.backups_path,
            temp_folder=context.temp_path,
            logs_folder=context.logs_path,
        )

    def _profile_services(
        self,
        context: DeviceContext,
        include_system: bool | None = None,
    ) -> _AppsProfileServices:
        settings = self._captured_profile_settings(context)
        return _AppsProfileServices(
            settings=settings,
            app_cache=AppInfoCache(settings),  # type: ignore[arg-type]
            apk_metadata=APKMetadataExtractor(settings),  # type: ignore[arg-type]
            icon_extractor=IconExtractor(settings),  # type: ignore[arg-type]
            include_system=(
                bool(self.settings.get("show_system_apps", True))
                if include_system is None
                else bool(include_system)
            ),
        )

    def _backup_manager_for_context(self, context: DeviceContext) -> BackupManager:
        return BackupManager(self._captured_profile_settings(context))  # type: ignore[arg-type]

    def _require_apps_context(self) -> DeviceContext:
        require_context = getattr(self.device_manager, "require_context", None)
        if callable(require_context):
            context = require_context({"ADB", "Recovery"})
            if isinstance(context, DeviceContext):
                return context

        # Compatibility for lightweight UI test doubles. Production always uses
        # DeviceManager.require_context(), so a mismatched mutable client is never
        # accepted by this fallback.
        active = getattr(self.device_manager, "active", None)
        raw_serial = getattr(active, "serial", "")
        raw_mode = getattr(active, "mode", "No device")
        serial = raw_serial if isinstance(raw_serial, str) else ""
        mode = raw_mode if isinstance(raw_mode, str) else "No device"
        if not serial or mode not in {"ADB", "Recovery"}:
            raise DeviceContextUnavailable("An authorized ADB or Recovery device is required")
        profile_path = Path(getattr(self.settings, "config_dir", Path.cwd()))

        def profile_path_for(key: str, fallback: str) -> Path:
            value = str(self.settings.get(key, "") or "").strip()
            return Path(value).expanduser() if value else profile_path / fallback

        return DeviceContext(
            serial=serial,
            mode=mode,
            transport_id=str(getattr(active, "transport_id", "") or ""),
            profile_key=safe_filename(serial),
            profile_kind=str(getattr(self.settings, "active_profile_kind", "") or "Phone"),
            profile_path=profile_path,
            backups_path=profile_path_for("backups_folder", "backups"),
            temp_path=profile_path_for("temp_folder", "temp"),
            logs_path=profile_path_for("logs_folder", "logs"),
            generation=int(getattr(self.device_manager, "current_generation", 0) or 0),
        )

    def _bound_adb_for_context(self, context: DeviceContext):
        for_context = getattr(self.adb, "for_context", None)
        if callable(for_context):
            return for_context(context)
        if self.adb is not None and str(getattr(self.adb, "serial", "") or "") == context.serial:
            return self.adb
        raise DeviceContextUnavailable("ADB client cannot be safely bound to the active device")

    def _is_context_current(self, context: DeviceContext) -> bool:
        is_current = getattr(self.device_manager, "is_context_current", None)
        if callable(is_current):
            return bool(is_current(context))
        active = getattr(self.device_manager, "active", None)
        return (
            str(getattr(active, "serial", "") or "") == context.serial
            and str(getattr(active, "mode", "") or "") == context.mode
            and Path(getattr(self.settings, "config_dir", Path.cwd())) == context.profile_path
        )

    def _require_current_context(self, context: DeviceContext) -> None:
        require_current = getattr(self.device_manager, "require_current", None)
        if callable(require_current):
            require_current(context)
            return
        if not self._is_context_current(context):
            raise StaleDeviceContext("The active device or profile changed")

    def _can_apply_operation(self, token: OperationToken, context: DeviceContext) -> bool:
        return (
            self.operations.contains(token)
            and not token.cancelled
            and self._is_context_current(context)
        )

    def _set_apps_view_identity(
        self,
        serial: str,
        context: DeviceContext | None = None,
    ) -> None:
        self._apps_view_context = context
        self._apps_view_serial = str(serial or "")
        self._apps_view_profile_path = (
            context.profile_path if context is not None else Path(self.settings.config_dir)
        )

    def _apps_view_matches_context(self, context: DeviceContext) -> bool:
        if (
            self._apps_view_serial != context.serial
            or self._apps_view_profile_path != context.profile_path
        ):
            return False
        view_context = self._apps_view_context
        return view_context is None or (
            view_context == context and self._is_context_current(view_context)
        )

    def _register_operation(
        self,
        context: DeviceContext,
        suffix: str,
        conflict: str,
        *,
        additional_conflicts: tuple[str, ...] = (),
    ) -> OperationToken:
        token = self.operations.register(
            f"apps.{suffix}",
            device_context=context,
            conflict_group=f"{conflict}:{context.serial}",
            conflict_groups=additional_conflicts,
        )
        if not self._is_context_current(context):
            token.cancel("device context changed during operation registration")
            self.operations.finish(token)
            raise StaleDeviceContext(
                "The active device changed before the application operation could start"
            )
        return token

    def _device_snapshot(self, context: DeviceContext) -> DeviceInfo:
        active = getattr(self.device_manager, "active", DeviceInfo())
        values = {field.name: getattr(active, field.name) for field in fields(DeviceInfo)}
        values["serial"] = context.serial
        values["mode"] = context.mode
        values["transport_id"] = context.transport_id
        return DeviceInfo(**values)

    def refresh_storage_roots(self) -> None:
        self.app_cache.refresh_root()
        self.apk_metadata.refresh_root()

    def reset_for_device_profile(self) -> None:
        self.operations.cancel_owner("apps.list", "application profile changed")
        self.operations.cancel_owner("apps.metadata", "application profile changed")
        self.operations.cancel_owner("apps.assets", "application profile changed")
        self.operations.cancel_owner("apps.bulk", "application profile changed")
        self._apps_load_token = None
        self._metadata_token = None
        self._assets_token = None
        self._bulk_token = None
        self._set_apps_view_identity("")
        self._apps_loading = False
        self._assets_loading = False
        self._bulk_operation_busy = False
        self._bulk_operation_name = ""
        self._refresh_after_bulk = False
        self._search_filter_timer.stop()
        self.apps = []
        self.table.set_apps_sorted([], self._sort_mode)
        self._asset_progress_status = ""
        self._suppress_cache_save = False
        self.reload_filter_state()
        self.status_label.setText("Press Load applications to read packages from the active device profile.")
        self._update_app_count()

    def refresh_apps(self) -> None:
        if self._apps_loading or self._assets_loading or self._bulk_operation_busy:
            return
        self._suppress_cache_save = False
        include_system = bool(self.settings.get("show_system_apps", True))
        self._show_cached_apps_for_current_device(include_system)
        try:
            context = self._require_apps_context()
            bound_adb = self._bound_adb_for_context(context)
            services = self._profile_services(context, include_system)
            token = self._register_operation(context, "list", "apps-list")
        except (DeviceContextUnavailable, OperationConflictError, RuntimeError) as exc:
            if not self.apps:
                QMessageBox.warning(self, "Apps", str(exc) or "Connect an authorized ADB device first.")
            self._update_action_states()
            return
        self._apps_load_token = token
        self._apps_loading = True
        self.status_label.setText("Refreshing package list from Android...")
        self._update_action_states()
        def load_packages() -> list[AppInfo]:
            if token.cancelled:
                return []
            self._require_current_context(context)
            return bound_adb.list_packages(
                include_system=include_system,
                load_details=False,
                cancel_event=token.cancel_event,
            )

        worker = Worker(load_packages)
        worker.signals.result.connect(
            lambda apps: self._apps_loaded(
                token,
                context,
                services,
                include_system,
                apps,
            )
        )
        worker.signals.error.connect(
            lambda message, trace: self._apps_load_failed(token, context, message, trace)
        )
        worker.signals.finished.connect(lambda: self._apps_load_finished(token, context))
        if not start_worker(
            self,
            self.pool,
            worker,
            operation_registry=self.operations,
            operation_token=token,
        ):
            self._apps_load_finished(token, context)

    def _apps_loaded(
        self,
        token: OperationToken,
        context: DeviceContext,
        services: _AppsProfileServices,
        include_system: bool,
        apps: list[AppInfo],
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        cached_apps, _saved_at = services.app_cache.load(context.serial, include_system)
        if cached_apps:
            apps = services.app_cache.merge(apps, cached_apps)
        self._prepare_cached_display_labels(apps)
        self.bloatware_db.annotate(apps)
        self._apply_cached_icons(apps, context.serial, services.icon_extractor)
        self._set_apps_view_identity(context.serial, context)
        self.apps = apps
        self.table.set_apps_sorted(apps, self._sort_mode)
        self.apply_filter(save_state=False)
        self._save_app_cache_from_table(
            context,
            services.app_cache,
            include_system=services.include_system,
        )
        self._start_missing_app_background_work(context, services, apps)

    def _load_cached_apps_for_saved_device(self) -> None:
        include_system = bool(self.settings.get("show_system_apps", True))
        serial = str(
            self.settings.get("active_device_serial", "")
            or self.settings.get("last_apps_device_serial", "")
            or self.settings.get("last_connected_device_serial", "")
            or ""
        )
        if serial:
            self._show_cached_apps(
                serial,
                include_system,
                "Loaded cached app data. Connect the device and press Refresh applications to update it.",
            )

    def _show_cached_apps_for_current_device(self, include_system: bool) -> bool:
        serial = self._current_cache_serial()
        return self._show_cached_apps(serial, include_system, "Loaded cached app data instantly; refreshing from Android in the background.")

    def _show_cached_apps(self, serial: str, include_system: bool, status: str) -> bool:
        cached_apps, saved_at = self._load_cached_apps(serial, include_system)
        if not cached_apps:
            return False
        self._prepare_cached_display_labels(cached_apps)
        self.bloatware_db.annotate(cached_apps)
        self._apply_cached_icons(cached_apps, serial)
        cached_context: DeviceContext | None = None
        try:
            candidate = self._require_apps_context()
            if candidate.serial == serial:
                cached_context = candidate
        except DeviceContextUnavailable:
            pass
        self._set_apps_view_identity(serial, cached_context)
        self.apps = cached_apps
        self.table.set_apps_sorted(cached_apps, self._sort_mode)
        self.apply_filter(save_state=False)
        suffix = f" Last saved: {saved_at}." if saved_at else ""
        self.status_label.setText(status + suffix)
        return True

    def _load_cached_apps(self, serial: str, include_system: bool) -> tuple[list[AppInfo], str]:
        if not serial:
            return [], ""
        return self.app_cache.load(serial, include_system)

    def _apply_cached_icons(
        self,
        apps: list[AppInfo],
        device_serial: str = "",
        icon_extractor: IconExtractor | None = None,
    ) -> None:
        device_serial = device_serial or self._current_cache_serial()
        for app in apps:
            cached_icon = self._cached_icon_path(app, device_serial, icon_extractor)
            if cached_icon:
                app.icon_path = str(cached_icon)

    def _prepare_cached_display_labels(self, apps: list[AppInfo]) -> None:
        for app in apps:
            normalized = self._normalize_display_label(app.app_label, app.package_name, app.apk_paths)
            if normalized != (app.app_label or "").strip():
                app.app_label = normalized
                app.assets_checked = False

    def _apps_load_failed(
        self,
        token: OperationToken,
        context: DeviceContext,
        message: str,
        trace: str,
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        self.status_label.setText(f"Failed to load apps: {message}")
        show_error_dialog(self, "Applications could not be loaded", message, context.logs_path)

    def _apps_load_finished(self, token: OperationToken, context: DeviceContext) -> None:
        self.operations.finish(token)
        if self._apps_load_token is not token:
            return
        self._apps_load_token = None
        self._apps_loading = False
        self._update_action_states()
        self._update_app_count()

    def _start_missing_app_background_work(
        self,
        context: DeviceContext,
        services: _AppsProfileServices,
        apps: list[AppInfo],
    ) -> None:
        metadata_targets = [app for app in apps if not app.metadata_checked or not self._has_known_size(app)]
        asset_targets = [
            app
            for app in apps
            if not app.assets_checked or self._is_placeholder_label(app.app_label, app.package_name) or not app.icon_path
        ]
        if not metadata_targets and not asset_targets:
            self.status_label.setText(
                f"Loaded {len(apps)} apps from cache. App metadata, labels and icons are already cached."
            )
            return

        parts: list[str] = []
        if metadata_targets:
            parts.append(f"metadata/sizes for {len(metadata_targets)}")
        if asset_targets:
            parts.append(f"labels/icons for {len(asset_targets)}")
        self.status_label.setText(f"Loaded {len(apps)} packages. Refreshing only missing {' and '.join(parts)} in the background.")

        bridge_targets_by_package = {app.package_name: app for app in asset_targets}
        for app in metadata_targets:
            bridge_targets_by_package.setdefault(app.package_name, app)
        if bridge_targets_by_package:
            self._load_apk_assets_background(
                context,
                services,
                apps,
                list(bridge_targets_by_package.values()),
                metadata_targets,
            )

    def _load_metadata_background(
        self,
        apps: list[AppInfo],
        context: DeviceContext | None = None,
        services: _AppsProfileServices | None = None,
    ) -> None:
        context = context or self._require_apps_context()
        services = services or self._profile_services(context)
        bound_adb = self._bound_adb_for_context(context)
        package_names = [app.package_name for app in apps]
        app_by_package = {app.package_name: app for app in apps}
        metadata_parallelism = self.settings.get("apps_metadata_parallelism", 6)
        self._metadata_cache_updates_since_flush = 0

        def build_updated_app(app: AppInfo, details: dict[str, str]) -> AppInfo:
            size_text = self._size_text_from_metadata(details) or app.size
            return AppInfo(
                package_name=app.package_name,
                app_label=details.get("appLabel", "") or app.app_label,
                app_type=app.app_type,
                state=app.state,
                version_name=details.get("versionName", ""),
                version_code=details.get("versionCode", "") or app.version_code,
                apk_paths=app.apk_paths,
                size=size_text,
                icon_path=app.icon_path,
                bloatware_removal=app.bloatware_removal,
                bloatware_list=app.bloatware_list,
                bloatware_description=app.bloatware_description,
                bloatware_labels=list(app.bloatware_labels),
                metadata_checked=True,
                assets_checked=app.assets_checked,
            )

        def load_metadata(progress_callback=None, item_callback=None) -> list[AppInfo]:
            updated_apps: list[AppInfo] = []
            max_workers = self._metadata_worker_count(len(apps), metadata_parallelism)
            updated_by_package: dict[str, AppInfo] = {}

            def on_progress(done: int, total: int, package_name: str, details: dict[str, str]) -> None:
                if token.cancelled:
                    return
                app = app_by_package.get(package_name)
                if not app:
                    return
                updated = build_updated_app(app, details)
                updated_by_package[package_name] = updated
                if item_callback:
                    item_callback.emit(updated)
                if progress_callback:
                    progress_callback.emit(
                        f"App metadata: {done}/{total} packages loaded in parallel ({max_workers} workers). Current: {package_name}"
                    )

            if token.cancelled:
                return []
            bound_adb.get_package_details_many(
                package_names,
                max_workers=max_workers,
                progress_callback=on_progress,
                cancel_event=token.cancel_event,
            )
            if token.cancelled:
                return []
            for app in apps:
                updated_apps.append(updated_by_package.get(app.package_name) or build_updated_app(app, {}))
            return updated_apps

        if not package_names:
            return
        try:
            token = self._register_operation(context, "metadata", "apps-metadata")
        except (OperationConflictError, RuntimeError):
            return
        self._metadata_token = token
        worker = Worker(load_metadata)
        worker.signals.progress.connect(lambda message: self._metadata_progress(token, context, message))
        worker.signals.item.connect(lambda app: self._metadata_item_loaded(token, context, services, app))
        worker.signals.result.connect(lambda apps: self._metadata_loaded(token, context, services, apps))
        worker.signals.error.connect(
            lambda message, trace: self._metadata_failed(token, context, message, trace)
        )
        worker.signals.finished.connect(lambda: self._metadata_finished(token))
        if not start_worker(
            self,
            self.pool,
            worker,
            operation_registry=self.operations,
            operation_token=token,
        ):
            self._metadata_finished(token)

    def _metadata_worker_count(self, target_count: int, configured=None) -> int:
        if target_count <= 1:
            return 1
        if configured is None:
            configured = self.settings.get("apps_metadata_parallelism", 6)
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = 6
        return max(2, min(value, target_count, 8))

    def _metadata_progress(self, token: OperationToken, context: DeviceContext, message: str) -> None:
        if not self._can_apply_operation(token, context):
            return
        if self._assets_loading and self._asset_progress_status:
            return
        self.status_label.setText(message)

    def _metadata_item_loaded(
        self,
        token: OperationToken,
        context: DeviceContext,
        services: _AppsProfileServices,
        app: AppInfo,
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        self.table.update_app_details(app)
        self._metadata_cache_updates_since_flush += 1
        if self._metadata_cache_updates_since_flush >= 48:
            self._metadata_cache_updates_since_flush = 0
            self._save_app_cache_from_table(
                context,
                services.app_cache,
                include_system=services.include_system,
            )

    def _metadata_loaded(
        self,
        token: OperationToken,
        context: DeviceContext,
        services: _AppsProfileServices,
        updated_apps: list[AppInfo],
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        for app in updated_apps:
            self.table.update_app_details(app)
        self.table.apply_sort(self._sort_mode)
        self.apply_filter(save_state=False)
        self._save_app_cache_from_table(
            context,
            services.app_cache,
            include_system=services.include_system,
        )
        apps = list(getattr(self.table, "apps", []) or self.apps)
        pending = sum(1 for app in apps if not app.metadata_checked)
        if self._assets_loading and self._asset_progress_status:
            self.status_label.setText(self._asset_progress_status)
        elif pending:
            self.status_label.setText(f"Version metadata cached for {len(updated_apps)} apps. {pending} apps still need metadata.")
        else:
            self.status_label.setText("Version metadata cache is complete. App labels and icons may still be loading.")

    def _metadata_failed(
        self,
        token: OperationToken,
        context: DeviceContext,
        message: str,
        trace: str,
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        if self._assets_loading and self._asset_progress_status:
            self.status_label.setText(self._asset_progress_status)
            return
        self.status_label.setText(f"Version metadata refresh failed: {message}")

    def _metadata_finished(self, token: OperationToken) -> None:
        self.operations.finish(token)
        if self._metadata_token is token:
            self._metadata_token = None

    def _load_apk_assets_background(
        self,
        context: DeviceContext,
        services: _AppsProfileServices,
        apps: list[AppInfo],
        targets: list[AppInfo],
        metadata_targets: list[AppInfo] | None = None,
    ) -> None:
        if not self._is_context_current(context):
            return
        try:
            token = self._register_operation(context, "assets", "apps-assets")
        except (OperationConflictError, RuntimeError):
            return
        bound_adb = self._bound_adb_for_context(context)
        metadata_parallelism = self.settings.get("apps_metadata_parallelism", 6)
        device_serial = context.serial
        target_apps = list(targets)
        metadata_target_packages = {app.package_name for app in (metadata_targets or [])}
        if not target_apps:
            self.operations.finish(token)
            return
        self._assets_token = token
        self._assets_loading = True
        self._asset_cache_updates_since_flush = 0
        self._asset_progress_status = self._asset_progress_text(
            total=len(target_apps),
            labels=0,
            icons=0,
            processed=0,
            phase="Preparing app label and icon cache refresh.",
        )
        self.status_label.setText(self._asset_progress_status)
        self._update_action_states()

        def load_assets(progress_callback=None, item_callback=None) -> list[AppInfo]:
            if token.cancelled:
                return []
            updated_apps: list[AppInfo] = []
            pull_dir = ensure_dir(context.temp_path / "apk-assets")
            total = len(target_apps)
            pull_plan: list[tuple[str, Path]] = []
            local_apks: dict[str, list[Path]] = {}
            apps_by_package = {app.package_name: (app.version_name, app.version_code) for app in target_apps}
            cached_labels: dict[str, str] = {}
            cached_icons: dict[str, Path] = {}
            bridge_metadata: dict[str, dict[str, str]] = {}
            last_percent = 0

            def progress_text(
                labels: int,
                icons: int,
                processed: int,
                phase: str,
                stage_done: int = 0,
                stage_total: int = 0,
                stage_weight: int = 30,
            ) -> str:
                nonlocal last_percent
                percent = self._asset_progress_percent(total, labels, icons)
                if stage_total > 0:
                    stage_done = min(max(0, stage_done), stage_total)
                    stage_percent = int(round((stage_done / stage_total) * max(0, stage_weight)))
                    percent = max(percent, stage_percent)
                percent = max(percent, last_percent)
                last_percent = percent
                return self._asset_progress_text(
                    total=total,
                    labels=labels,
                    icons=icons,
                    processed=processed,
                    phase=phase,
                    percent_override=percent,
                )

            for app in target_apps:
                if token.cancelled:
                    return []
                cached_label = self._cached_display_label(app, services.apk_metadata)
                if cached_label:
                    cached_labels[app.package_name] = cached_label
                cached_icon = self._cached_acbridge_icon_path(
                    app,
                    device_serial,
                    services.icon_extractor,
                ) or self._cached_icon_path(app, device_serial, services.icon_extractor)
                if cached_icon:
                    cached_icons[app.package_name] = cached_icon

            missing_labels = {app.package_name for app in target_apps if app.package_name not in cached_labels}
            missing_icons = {
                app.package_name
                for app in target_apps
                if app.package_name not in cached_icons
            }
            missing_metadata = {
                app.package_name
                for app in target_apps
                if app.package_name in metadata_target_packages and (not app.metadata_checked or not self._has_known_size(app))
            }

            if progress_callback:
                progress_callback.emit(
                    progress_text(
                        len(cached_labels),
                        len(cached_icons),
                        0,
                        "Checked local cache before downloading missing app data.",
                    )
                )

            if not missing_labels and not missing_icons and not missing_metadata:
                if progress_callback:
                    progress_callback.emit(
                        progress_text(total, total, total, "All app labels and icons were loaded from local cache.")
                    )
                return [
                    AppInfo(
                        package_name=app.package_name,
                        app_label=cached_labels.get(app.package_name, app.app_label),
                        app_type=app.app_type,
                        state=app.state,
                        version_name=app.version_name,
                        version_code=app.version_code,
                        apk_paths=app.apk_paths,
                        size=app.size,
                        icon_path=str(cached_icons[app.package_name]),
                        bloatware_removal=app.bloatware_removal,
                        bloatware_list=app.bloatware_list,
                        bloatware_description=app.bloatware_description,
                        bloatware_labels=list(app.bloatware_labels),
                        metadata_checked=app.metadata_checked,
                        assets_checked=True,
                    )
                    for app in target_apps
                ]

            if progress_callback:
                progress_callback.emit(
                    progress_text(
                        len(cached_labels),
                        len(cached_icons),
                        0,
                        "Installing or starting ACBridge helper for app labels and icons.",
                    )
                )
            if token.cancelled:
                return []
            bridge = ACBridgeClient(bound_adb, services.settings, services.icon_extractor)  # type: ignore[arg-type]
            bridge_package_names = missing_labels | missing_icons | missing_metadata
            bridge_root = self._apps_root_available_for_acbridge(
                bound_adb,
                cancel_event=token.cancel_event,
            )
            if token.cancelled:
                return []
            bridge_progress = self._bridge_progress_adapter(
                progress_callback,
                progress_text,
                len(cached_labels),
                len(cached_icons),
            )
            try:
                bridge_result = bridge.load_app_data(
                    {package: apps_by_package[package] for package in bridge_package_names if package in apps_by_package},
                    device_serial=device_serial,
                    icon_size=96,
                    need_labels=bool(missing_labels),
                    need_icons=bool(missing_icons),
                    need_metadata=bool(missing_metadata),
                    use_root=bridge_root,
                    progress_callback=bridge_progress,
                    cancel_event=token.cancel_event,
                )
                if token.cancelled:
                    return []
                apps_by_name = {app.package_name: app for app in target_apps}
                bridge_labels: dict[str, str] = {}
                for package_name, label in bridge_result.labels.items():
                    app = apps_by_name.get(package_name)
                    normalized = self._normalize_display_label(label, package_name, app.apk_paths if app else [])
                    if normalized:
                        bridge_labels[package_name] = normalized
                cached_labels.update(bridge_labels)
                bridge_metadata.update(bridge_result.metadata)
                cached_icons.update(bridge_result.icons)
                bridge_message = bridge_result.message
            except Exception as exc:
                if token.cancelled:
                    return []
                bridge_message = f"ACBridge failed: {exc}. OpenADB fallback APK parser will continue."

            missing_metadata_after_bridge = [package for package in missing_metadata if package not in bridge_metadata]
            if missing_metadata_after_bridge:
                if progress_callback:
                    progress_callback.emit(
                        progress_text(
                            len(cached_labels),
                            len(cached_icons),
                            0,
                            (
                                "ACBridge metadata was incomplete. "
                                f"Using slower ADB fallback for {len(missing_metadata_after_bridge)} packages."
                            ),
                        )
                    )
                if token.cancelled:
                    return []
                details_by_package = bound_adb.get_package_details_many(
                    missing_metadata_after_bridge,
                    max_workers=self._metadata_worker_count(
                        len(missing_metadata_after_bridge),
                        metadata_parallelism,
                    ),
                    cancel_event=token.cancel_event,
                )
                if token.cancelled:
                    return []
                bridge_metadata.update(details_by_package)
            missing_sizes_after_bridge = [
                app.package_name
                for app in target_apps
                if not self._has_known_size(app) and not self._metadata_has_size(bridge_metadata.get(app.package_name, {}))
            ]
            if missing_sizes_after_bridge:
                if progress_callback:
                    progress_callback.emit(
                        progress_text(
                            len(cached_labels),
                            len(cached_icons),
                            0,
                            f"Resolving APK sizes for {len(missing_sizes_after_bridge)} apps through ADB.",
                        )
                    )
                if token.cancelled:
                    return []
                sizes_by_package = bound_adb.get_package_sizes_bulk(
                    missing_sizes_after_bridge,
                    use_root=bridge_root,
                    cancel_event=token.cancel_event,
                )
                if token.cancelled:
                    return []
                for package_name, size_bytes in sizes_by_package.items():
                    bridge_metadata.setdefault(package_name, {})["sizeBytes"] = str(size_bytes)
            apps_by_name = {app.package_name: app for app in apps}
            if token.cancelled:
                return []
            for package_name, label in cached_labels.items():
                app = apps_by_name.get(package_name)
                if app:
                    services.apk_metadata.set_cached_label(app, label)
            if progress_callback:
                progress_callback.emit(
                    progress_text(
                        len(cached_labels),
                        len(cached_icons),
                        0,
                        bridge_message,
                    )
                )

            fallback_apps = [
                app
                for app in target_apps
                if app.package_name not in cached_labels or app.package_name not in cached_icons
            ]
            apk_paths_by_package = {}
            if fallback_apps:
                if progress_callback:
                    progress_callback.emit(
                        progress_text(
                            len(cached_labels),
                            len(cached_icons),
                            0,
                            f"Resolving APK paths for {len(fallback_apps)} apps still missing labels or icons.",
                        )
                    )
                if token.cancelled:
                    return []
                apk_paths_by_package = bound_adb.get_package_paths_bulk(
                    [app.package_name for app in fallback_apps],
                    cancel_event=token.cancel_event,
                )
                if token.cancelled:
                    return []

            for app in fallback_apps:
                if token.cancelled:
                    return []
                apk_paths = apk_paths_by_package.get(app.package_name) or app.apk_paths
                targets: list[Path] = []
                for index, apk_path in enumerate(apk_paths):
                    apk_name = safe_filename(Path(apk_path).name or f"part_{index}.apk")
                    target = pull_dir / (
                        f"{safe_filename(app.package_name)}_{safe_filename(app.version_code or '0')}_{index}_{apk_name}"
                    )
                    targets.append(target)
                local_apks[app.package_name] = targets
                needs_apk = app.package_name not in cached_labels or app.package_name not in cached_icons
                if needs_apk:
                    for apk_path, target in zip(apk_paths, targets):
                        if not target.exists():
                            pull_plan.append((apk_path, target))

            if pull_plan:
                if progress_callback:
                    progress_callback.emit(
                        progress_text(
                            len(cached_labels),
                            len(cached_icons),
                            0,
                            f"Pulling {len(pull_plan)} APK parts through ADB for fallback label/icon extraction.",
                        )
                    )

                def pull_progress(done: int, part_total: int, remote: str, local: str, success: bool) -> None:
                    if not progress_callback:
                        return
                    status = "pulled" if success else "failed"
                    current_name = Path(local).name or Path(remote).name or remote
                    progress_callback.emit(
                        progress_text(
                            len(cached_labels),
                            len(cached_icons),
                            0,
                            (
                                f"Downloading APK parts for fallback extraction: "
                                f"{done}/{part_total} {status}. Current: {current_name}"
                            ),
                            stage_done=done,
                            stage_total=part_total,
                            stage_weight=35,
                        )
                    )

                if token.cancelled:
                    return []
                bound_adb.pull_files_via_temp(
                    pull_plan,
                    chunk_size=16,
                    timeout=900,
                    progress_callback=pull_progress,
                    parallel_chunks=2,
                    use_root=bridge_root,
                    cancel_event=token.cancel_event,
                )
                if token.cancelled:
                    return []

            def build_updated_app(app: AppInfo) -> AppInfo:
                initial_label = "" if self._is_placeholder_label(app.app_label, app.package_name) else app.app_label
                updated = AppInfo(
                    package_name=app.package_name,
                    app_label=initial_label,
                    app_type=app.app_type,
                    state=app.state,
                    version_name=app.version_name,
                    version_code=app.version_code,
                    apk_paths=apk_paths_by_package.get(app.package_name) or app.apk_paths,
                    size=app.size,
                    icon_path=app.icon_path,
                    bloatware_removal=app.bloatware_removal,
                    bloatware_list=app.bloatware_list,
                    bloatware_description=app.bloatware_description,
                    bloatware_labels=list(app.bloatware_labels),
                    metadata_checked=app.metadata_checked,
                    assets_checked=True,
                )

                metadata = bridge_metadata.get(app.package_name, {})
                if metadata:
                    updated.version_name = metadata.get("versionName", "") or updated.version_name
                    updated.version_code = metadata.get("versionCode", "") or updated.version_code
                    updated.size = self._size_text_from_metadata(metadata) or updated.size
                    updated.metadata_checked = True

                cached_label = cached_labels.get(app.package_name, "")
                if cached_label:
                    updated.app_label = cached_label

                cached_icon = cached_icons.get(app.package_name)
                if cached_icon:
                    updated.icon_path = str(cached_icon)

                if (updated.app_label and updated.icon_path) or not updated.apk_paths:
                    if not updated.app_label:
                        updated.app_label = self._fallback_label_from_package(updated.package_name, updated.apk_paths)
                    return updated

                targets = local_apks.get(app.package_name, [])
                if not any(target.exists() for target in targets):
                    if not updated.app_label:
                        updated.app_label = self._fallback_label_from_package(updated.package_name, updated.apk_paths)
                    return updated

                for target in targets:
                    if not target.exists():
                        continue
                    if not updated.app_label:
                        label = services.apk_metadata.extract_label(target)
                        label = self._normalize_display_label(label, app.package_name, updated.apk_paths)
                        if label:
                            updated.app_label = label
                            services.apk_metadata.set_cached_label(app, label)
                    if not updated.icon_path:
                        icon = services.icon_extractor.extract_from_apk(
                            target,
                            app.package_name,
                            app.version_name,
                            app.version_code,
                        )
                        if icon:
                            updated.icon_path = str(icon)
                    if updated.app_label and updated.icon_path:
                        break

                if not updated.app_label:
                    updated.app_label = self._fallback_label_from_package(updated.package_name, updated.apk_paths)

                return updated

            max_workers = min(8, max(1, (len(target_apps) + 24) // 25))
            label_packages = set(cached_labels)
            icon_packages = set(cached_icons)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(build_updated_app, app) for app in target_apps]
                for index, future in enumerate(as_completed(futures), start=1):
                    if token.cancelled:
                        for pending in futures:
                            pending.cancel()
                        break
                    updated = future.result()
                    updated_apps.append(updated)
                    if progress_callback:
                        if updated.app_label:
                            label_packages.add(updated.package_name)
                        if updated.icon_path:
                            icon_packages.add(updated.package_name)
                        progress_callback.emit(
                            progress_text(
                                len(label_packages),
                                len(icon_packages),
                                index,
                                "Applying downloaded app labels and icons.",
                            )
                        )
                    if item_callback:
                        item_callback.emit(updated)
            return updated_apps

        worker = Worker(load_assets)
        worker.signals.progress.connect(
            lambda message: self._set_asset_progress_status(token, context, message)
        )
        worker.signals.item.connect(
            lambda app: self._apk_asset_loaded(token, context, services, app)
        )
        worker.signals.result.connect(
            lambda apps: self._apk_assets_loaded(token, context, services, apps)
        )
        worker.signals.error.connect(
            lambda message, trace: self._apk_assets_failed(
                token,
                context,
                services,
                message,
                trace,
            )
        )
        worker.signals.finished.connect(lambda: self._apk_assets_finished(token))
        if not start_worker(
            self,
            self.pool,
            worker,
            operation_registry=self.operations,
            operation_token=token,
        ):
            self._apk_assets_finished(token)

    def _apk_assets_finished(self, token: OperationToken) -> None:
        self.operations.finish(token)
        if self._assets_token is not token:
            return
        self._assets_token = None
        self._assets_loading = False
        self._update_action_states()

    def _apk_assets_failed(
        self,
        token: OperationToken,
        context: DeviceContext,
        services: _AppsProfileServices,
        message: str,
        trace: str,
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        self._save_app_cache_from_table(
            context,
            services.app_cache,
            include_system=services.include_system,
        )
        self.status_label.setText(f"App labels and icons failed to load: {message}")

    def _set_asset_progress_status(
        self,
        token: OperationToken,
        context: DeviceContext,
        message: str,
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        self._asset_progress_status = message
        self.status_label.setText(message)

    def _bridge_progress_adapter(self, progress_callback, progress_text, cached_label_count: int, cached_icon_count: int):
        if not progress_callback:
            return None

        class BridgeProgress:
            def emit(_, message: str) -> None:
                match = re.search(
                    r"ACBRIDGE_PROGRESS\s+labels=(\d+)\s+icons=(\d+)\s+total=(\d+)\s+stage=([A-Za-z0-9_-]+)",
                    message or "",
                )
                if match:
                    labels = cached_label_count + int(match.group(1))
                    icons = cached_icon_count + int(match.group(2))
                    total = max(1, int(match.group(3)))
                    stage = match.group(4)
                    done = int(match.group(1)) + int(match.group(2))
                    phase = (
                        f"ACBridge is rendering app labels and icons on the phone "
                        f"({stage}, {done}/{total * 2} items)."
                    )
                    progress_callback.emit(
                        progress_text(
                            labels,
                            icons,
                            max(int(match.group(1)), int(match.group(2))),
                            phase,
                            stage_done=done,
                            stage_total=total * 2,
                            stage_weight=70,
                        )
                    )
                    return
                progress_callback.emit(progress_text(cached_label_count, cached_icon_count, 0, message))

        return BridgeProgress()

    def _asset_progress_percent(self, total: int, labels: int, icons: int) -> int:
        total = max(0, total)
        if total <= 0:
            return 100
        labels = min(max(0, labels), total)
        icons = min(max(0, icons), total)
        return int(round(((labels + icons) / (total * 2)) * 100))

    def _asset_progress_text(
        self,
        total: int,
        labels: int,
        icons: int,
        processed: int,
        phase: str,
        percent_override: int | None = None,
    ) -> str:
        total = max(0, total)
        if total <= 0:
            return f"App labels/icons: no apps need loading. {phase}"
        labels = min(max(0, labels), total)
        icons = min(max(0, icons), total)
        processed = min(max(0, processed), total)
        percent = self._asset_progress_percent(total, labels, icons) if percent_override is None else percent_override
        percent = min(max(0, percent), 100)
        return (
            f"App labels/icons: {percent}% | "
            f"labels {labels}/{total}, icons {icons}/{total}, processed {processed}/{total}. "
            f"{phase}"
        )

    def _has_known_size(self, app: AppInfo) -> bool:
        return bool((app.size or "").strip()) and (app.size or "").strip().lower() != "unknown"

    def _metadata_has_size(self, metadata: dict[str, str]) -> bool:
        return bool(self._size_text_from_metadata(metadata))

    def _size_text_from_metadata(self, metadata: dict[str, str]) -> str:
        raw = str(metadata.get("sizeBytes", "") or "").strip()
        if not raw:
            return ""
        try:
            return format_bytes(max(0, int(raw)))
        except ValueError:
            return ""

    def _cached_icon_path(
        self,
        app: AppInfo,
        device_serial: str = "",
        icon_extractor: IconExtractor | None = None,
    ) -> Path | None:
        serial_key = safe_filename(device_serial or self.adb.serial or "device")
        extractor = icon_extractor or self.icon_extractor
        return extractor.cached_icon_path(
            app.package_name,
            app.version_name,
            app.version_code,
            source_keys=[f"acbridge_{serial_key}", ""],
        )

    def _cached_acbridge_icon_path(
        self,
        app: AppInfo,
        device_serial: str = "",
        icon_extractor: IconExtractor | None = None,
    ) -> Path | None:
        serial_key = safe_filename(device_serial or self.adb.serial or "device")
        extractor = icon_extractor or self.icon_extractor
        path = extractor.cache_path(
            app.package_name,
            app.version_name,
            app.version_code,
            source_key=f"acbridge_{serial_key}",
        )
        try:
            return path if path.is_file() and path.stat().st_size > 0 else None
        except OSError:
            return None

    def _cached_display_label(
        self,
        app: AppInfo,
        apk_metadata: APKMetadataExtractor | None = None,
    ) -> str:
        metadata = apk_metadata or self.apk_metadata
        for label in (app.app_label, metadata.cached_label(app)):
            value = " ".join((label or "").replace("\n", " ").replace("\r", " ").split())
            if self._is_placeholder_label(value, app.package_name) or self._looks_like_internal_label(value, app.package_name):
                continue
            normalized = self._compact_display_label(value, app.package_name)
            if normalized and not self._looks_like_generated_package_label(normalized, app.package_name):
                return normalized
        return ""

    def _is_placeholder_label(self, label: str, package_name: str) -> bool:
        value = (label or "").strip()
        if not value:
            return True
        if value == (package_name or "").strip():
            return True
        return value.lower() in {"unknown", "not extracted", "package", "application"}

    def _normalize_display_label(self, label: str, package_name: str, apk_paths: list[str] | None = None) -> str:
        value = " ".join((label or "").replace("\n", " ").replace("\r", " ").split())
        if self._is_placeholder_label(value, package_name) or self._looks_like_internal_label(value, package_name):
            value = self._fallback_label_from_package(package_name, apk_paths)
        if not value:
            value = self._fallback_label_from_package(package_name, apk_paths)
        return self._compact_display_label(value, package_name)

    def _looks_like_internal_label(self, label: str, package_name: str) -> bool:
        value = (label or "").strip()
        if not value:
            return True
        if " " in value:
            return self._looks_like_generated_package_label(value, package_name)
        lowered = value.lower()
        if lowered.startswith(("com.", "org.", "net.", "android.")) and value.count(".") >= 2:
            return True
        package_prefix = f"{(package_name or '').strip()}."
        if package_prefix != "." and value.startswith(package_prefix):
            return True
        return value.endswith(("Application", ".Application")) and value.count(".") >= 1

    def _looks_like_generated_package_label(self, label: str, package_name: str) -> bool:
        value = " ".join((label or "").split()).strip().lower()
        package_name = (package_name or "").strip()
        if not value or not package_name:
            return False
        generated = self._label_from_package_tokens(package_name).strip().lower()
        if generated and value == generated:
            return True
        compact_value = re.sub(r"[^a-z0-9]+", "", value)
        package_compact = re.sub(r"[^a-z0-9]+", "", package_name.lower())
        if package_compact and compact_value == package_compact:
            return True
        package_tail = re.sub(r"[^a-z0-9]+", "", package_name.split(".")[-1].lower())
        return bool(package_tail and compact_value == package_tail)

    def _compact_display_label(self, label: str, package_name: str) -> str:
        value = " ".join((label or "").split()).strip(" -_")
        if not value:
            return ""
        if value == package_name:
            value = self._label_from_package_tokens(package_name)
        if len(value) <= 64:
            return value
        words = value.split()
        compact: list[str] = []
        for word in words:
            candidate = " ".join(compact + [word])
            if len(candidate) > 64:
                break
            compact.append(word)
        if compact:
            return " ".join(compact)
        return value[:64].rstrip()

    def _apk_asset_loaded(
        self,
        token: OperationToken,
        context: DeviceContext,
        services: _AppsProfileServices,
        app: AppInfo,
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        self.table.update_app_details(app)
        if app.icon_path:
            self.table.set_icon_for_package(app.package_name, app.icon_path)
        self._asset_cache_updates_since_flush += 1
        if self._asset_cache_updates_since_flush >= 32:
            self._asset_cache_updates_since_flush = 0
            self._save_app_cache_from_table(
                context,
                services.app_cache,
                include_system=services.include_system,
            )

    def _apk_assets_loaded(
        self,
        token: OperationToken,
        context: DeviceContext,
        services: _AppsProfileServices,
        updated_apps: list[AppInfo],
    ) -> None:
        if not self._can_apply_operation(token, context):
            return
        for app in updated_apps:
            self.table.update_app_details(app)
            if app.icon_path:
                self.table.set_icon_for_package(app.package_name, app.icon_path)
        self.table.apply_sort(self._sort_mode)
        self.apply_filter(save_state=False)
        self._save_app_cache_from_table(
            context,
            services.app_cache,
            include_system=services.include_system,
        )
        apps = list(getattr(self.table, "apps", []) or self.apps)
        resolved = sum(1 for app in apps if app.app_label)
        checked = sum(1 for app in apps if app.assets_checked)
        pending = len(apps) - checked
        missing = len(apps) - resolved
        if pending > 0:
            self.status_label.setText(
                f"Cached app labels/icons for {checked}/{len(apps)} apps. {pending} apps still need asset extraction."
            )
            return
        if missing > 0:
            self.status_label.setText(
                f"App labels/icons cache is complete. Loaded display names for {resolved}/{len(apps)} apps; "
                f"{missing} packages still have no usable display name."
            )
        else:
            self.status_label.setText(f"App labels/icons cache is complete for {len(apps)} apps.")

    def _add_filter_menu_group(
        self,
        kind: str,
        title: str,
        options: list[tuple[str, str]],
    ) -> None:
        if self.filters_menu.actions():
            self.filters_menu.addSeparator()
        self.filters_menu.addSection(title)
        group = QActionGroup(self)
        group.setExclusive(True)
        actions: dict[str, QAction] = {}
        for text, value in options:
            action = self.filters_menu.addAction(text)
            action.setCheckable(True)
            action.setData(value)
            group.addAction(action)
            action.triggered.connect(
                lambda checked=False, filter_kind=kind, filter_value=value: self._filter_action_triggered(
                    filter_kind,
                    filter_value,
                    checked,
                )
            )
            actions[value] = action
        self._filter_action_groups[kind] = group
        self._filter_actions[kind] = actions
        if options:
            actions[options[0][1]].setChecked(True)

    def _filter_action_triggered(self, kind: str, value: str, checked: bool) -> None:
        if not checked:
            return
        self._filter_values[kind] = value
        self.apply_filter()

    def apply_filter(self, save_state: bool = True) -> None:
        filter_state = self._current_filter_state()
        self.table.apply_filters(filter_state)
        self._update_filter_summary(filter_state)
        self._update_app_count()
        if save_state:
            self._save_filter_state(filter_state)

    def reload_filter_state(self) -> None:
        self._search_filter_timer.stop()
        filter_state = AppFilterState.from_values(
            search_text=str(self.settings.get("apps_filter_search", "") or ""),
            app_type=str(self.settings.get("apps_filter_type", "all") or "all"),
            app_state=str(self.settings.get("apps_filter_state", "any") or "any"),
            uad_category=str(self.settings.get("apps_filter_uad", "any") or "any"),
        )
        self._set_filter_menu_value("type", filter_state.app_type)
        self._set_filter_menu_value("state", filter_state.app_state)
        self._set_filter_menu_value("uad", filter_state.uad_category)
        self.search.blockSignals(True)
        self.search.setText(filter_state.search_text)
        self.search.blockSignals(False)
        saved_sort = str(self.settings.get("apps_sort_mode", "name") or "name")
        self._sort_mode = saved_sort if saved_sort in APP_SORT_MODES else "name"
        self._update_sort_button_text()
        self.table.apply_sort(self._sort_mode)
        self.apply_filter(save_state=False)

    def reset_filters(self) -> None:
        self._search_filter_timer.stop()
        self._set_filter_menu_value("type", "all")
        self._set_filter_menu_value("state", "any")
        self._set_filter_menu_value("uad", "any")
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.apply_filter()

    def _schedule_search_filter(self, _text: str) -> None:
        self._search_filter_timer.start()

    def _current_filter_state(self) -> AppFilterState:
        return AppFilterState.from_values(
            search_text=self.search.text(),
            app_type=self._filter_values["type"],
            app_state=self._filter_values["state"],
            uad_category=self._filter_values["uad"],
        )

    def _set_filter_menu_value(self, kind: str, value: str) -> None:
        actions = self._filter_actions[kind]
        defaults = {"type": "all", "state": "any", "uad": "any"}
        normalized = value if value in actions else defaults[kind]
        self._filter_values[kind] = normalized
        actions[normalized].setChecked(True)

    def _save_filter_state(self, filter_state: AppFilterState) -> None:
        self.settings.set("apps_filter_type", filter_state.app_type, save=False)
        self.settings.set("apps_filter_state", filter_state.app_state, save=False)
        self.settings.set("apps_filter_uad", filter_state.uad_category, save=False)
        self.settings.set("apps_filter_search", filter_state.search_text, save=False)
        self.settings.set("apps_sort_mode", self._sort_mode, save=False)
        self.settings.save()

    def _update_filter_summary(self, filter_state: AppFilterState) -> None:
        active: list[str] = []
        if filter_state.app_type != "all":
            active.append(self._filter_actions["type"][filter_state.app_type].text())
        if filter_state.app_state != "any":
            active.append(self._filter_actions["state"][filter_state.app_state].text())
        if filter_state.uad_category != "any":
            active.append(self._filter_actions["uad"][filter_state.uad_category].text())
        if filter_state.search_text:
            active.append(f'Search: "{filter_state.search_text}"')
        self.active_filters_label.setText(" · ".join(active) if active else "No active filters")
        menu_filter_count = sum(
            value != default
            for value, default in zip(
                (filter_state.app_type, filter_state.app_state, filter_state.uad_category),
                ("all", "any", "any"),
                strict=True,
            )
        )
        self.filters_button.setText(f"Filters ({menu_filter_count})" if menu_filter_count else "Filters")
        self.filters_button.setToolTip(
            "\n".join(
                [
                    f"Type: {self._filter_actions['type'][filter_state.app_type].text()}",
                    f"State: {self._filter_actions['state'][filter_state.app_state].text()}",
                    f"UAD category: {self._filter_actions['uad'][filter_state.uad_category].text()}",
                ]
            )
        )
        self.reset_filters_button.setEnabled(bool(active))

    def _show_sort_menu_from_button(self) -> None:
        self._show_sort_context_menu(self.sort_button.mapToGlobal(self.sort_button.rect().bottomLeft()))

    def _show_sort_context_menu(self, global_position) -> None:
        menu = QMenu(self)
        name_action = menu.addAction("Sort by name")
        name_action.setCheckable(True)
        name_action.setChecked(self._sort_mode == "name")
        menu.addSeparator()
        heavy_action = menu.addAction("Size: largest to smallest")
        heavy_action.setCheckable(True)
        heavy_action.setChecked(self._sort_mode == "size_desc")
        light_action = menu.addAction("Size: smallest to largest")
        light_action.setCheckable(True)
        light_action.setChecked(self._sort_mode == "size_asc")

        selected = menu.exec(global_position)
        if selected is heavy_action:
            self._set_sort_mode("size_desc")
        elif selected is light_action:
            self._set_sort_mode("size_asc")
        elif selected is name_action:
            self._set_sort_mode("name")

    def _set_sort_mode(self, mode: str) -> None:
        self._sort_mode = mode if mode in APP_SORT_MODES else "name"
        self._update_sort_button_text()
        self.table.apply_sort(self._sort_mode)
        self.apply_filter(save_state=False)
        self._save_filter_state(self._current_filter_state())

    def _update_sort_button_text(self) -> None:
        labels = {
            "name": "Sort: name",
            "size_desc": "Size: largest first",
            "size_asc": "Size: smallest first",
        }
        self.sort_button.setText(labels.get(self._sort_mode, labels["name"]))

    def update_device_state(self, device=None) -> None:
        active = device if device is not None else getattr(self.device_manager, "active", None)
        self._device_mode = str(getattr(active, "mode", "No device") or "No device")
        serial = str(getattr(active, "serial", "") or "")
        if self.apps and serial and not self._apps_view_serial:
            context: DeviceContext | None = None
            try:
                candidate = self._require_apps_context()
                if candidate.serial == serial:
                    context = candidate
            except DeviceContextUnavailable:
                pass
            self._set_apps_view_identity(serial, context)
        self._update_action_states()
        self._update_app_count()

    def _device_available_for_apps(self) -> bool:
        return self._device_mode in {"ADB", "Recovery"}

    def _select_visible_state_changed(self, state: int) -> None:
        check_state = Qt.CheckState(state)
        if check_state == Qt.Checked:
            self.table.select_all_visible()
        elif check_state == Qt.Unchecked:
            self.table.unselect_all_visible()
        self._update_app_count()

    def _update_app_count(self) -> None:
        total = self.table.rowCount()
        visible = self.table.visible_count()
        selected = len(self.table.checked_package_names())
        visible_selected = self.table.visible_checked_count()
        hidden_selected = max(0, selected - visible_selected)
        self.total_label.setText(f"Showing {visible} of {total} applications")
        if hidden_selected:
            selection_text = f"{selected} selected · {hidden_selected} hidden by filters"
        else:
            selection_text = f"{selected} selected"
        self.selection_summary_label.setText(selection_text)

        if visible <= 0 or visible_selected <= 0:
            check_state = Qt.Unchecked
        elif visible_selected >= visible:
            check_state = Qt.Checked
        else:
            check_state = Qt.PartiallyChecked
        self.select_all_check.blockSignals(True)
        self.select_all_check.setCheckState(check_state)
        self.select_all_check.blockSignals(False)
        self._update_action_states()
        self._update_apps_empty_state(total, visible)

    def _update_apps_empty_state(self, total: int, visible: int) -> None:
        if total > 0 and visible > 0:
            self.apps_content.setCurrentWidget(self.table)
            return
        if self._apps_loading and total == 0:
            self.apps_empty_state.set_content(
                "Loading applications",
                "OpenADB is reading the package list from the active device.",
            )
        elif total > 0:
            self.apps_empty_state.set_content(
                "Search returned no results",
                "No applications match the current search and filters.",
                "Reset search and filters",
            )
        elif not self._device_available_for_apps():
            self.apps_empty_state.set_content(
                "No device connected",
                "Connect and authorize an ADB device, then refresh its status.",
                "Refresh device status",
                kind="warning",
            )
        else:
            self.apps_empty_state.set_content(
                "No applications loaded",
                "Load applications from the active Android device to begin.",
                "Load applications",
            )
        self.apps_content.setCurrentWidget(self.apps_empty_state)

    def _handle_empty_state_action(self) -> None:
        if self.table.rowCount() > 0 and self.table.visible_count() == 0:
            self.reset_filters()
        elif self._device_available_for_apps():
            self.refresh_apps()
        else:
            self.refresh_device_requested.emit()

    def _update_action_states(self) -> None:
        selected_apps = self.table.checked_apps(include_hidden=True)
        has_selection = bool(selected_apps)
        has_apps = self.table.rowCount() > 0
        device_ready = self._device_available_for_apps()
        risky_selection = any(app.is_system or is_dangerous_package(app.package_name) for app in selected_apps)

        busy_reason = ""
        if self._bulk_operation_busy:
            operation = self._bulk_operation_name or "another application operation"
            busy_reason = f"Wait for {operation} to finish."
        elif self._apps_loading:
            busy_reason = "Wait for the application list to finish loading."
        elif self._assets_loading:
            busy_reason = "Wait for application labels and icons to finish loading."

        device_reason = (
            ""
            if device_ready
            else f"Requires an authorized ADB or Recovery device (current mode: {self._device_mode})."
        )
        selection_reason = "" if has_selection else "Select one or more applications first."
        common_reason = busy_reason or device_reason or selection_reason

        self.refresh_button.setText("Refresh applications" if has_apps else "Load applications")
        self._set_available(
            self.refresh_button,
            not bool(busy_reason or device_reason),
            "Load the application list from the active device.",
            busy_reason or device_reason,
        )
        self._set_available(
            self.backup_button,
            not bool(common_reason),
            "Back up the selected applications.",
            common_reason,
        )

        danger_note = " Selection includes system or protected packages; an additional confirmation is required."
        self._set_available(
            self.uninstall_button,
            not bool(common_reason),
            "Uninstall the selected applications." + (danger_note if risky_selection else ""),
            common_reason,
        )

        states = {str(app.state or "").strip().casefold() for app in selected_apps}
        enable_reason = common_reason
        disable_reason = common_reason
        enable_allowed = not bool(common_reason)
        disable_allowed = not bool(common_reason)
        if not common_reason:
            if states == {"disabled"}:
                disable_allowed = False
                disable_reason = "All selected applications are already disabled."
            elif states == {"enabled"}:
                enable_allowed = False
                enable_reason = "All selected applications are already enabled."
            else:
                enable_allowed = False
                disable_allowed = False
                enable_reason = disable_reason = (
                    "Selection mixes enabled and disabled applications; adjust the selection first."
                )
        self._set_available(
            self.enable_button,
            enable_allowed,
            "Enable the selected disabled applications.",
            enable_reason,
        )
        self._set_available(
            self.disable_button,
            disable_allowed,
            "Disable the selected enabled applications." + (danger_note if risky_selection else ""),
            disable_reason,
        )

        self._set_available(
            self.install_existing_action,
            not bool(common_reason),
            "Ask Android to install an existing system package for the current user.",
            common_reason,
        )
        export_reason = busy_reason or ("Load applications before exporting." if not has_apps else "")
        self._set_available(
            self.export_action,
            not bool(export_reason),
            "Export the current application list to CSV.",
            export_reason,
        )
        self._set_available(
            self.clear_cache_action,
            not bool(busy_reason),
            "Delete cached application metadata, labels and icons.",
            busy_reason,
        )

        visible = self.table.visible_count()
        self.select_all_check.setEnabled(visible > 0 and not self._bulk_operation_busy)
        if self._bulk_operation_busy:
            select_tooltip = busy_reason
        elif visible > 0:
            select_tooltip = "Select or clear all applications currently visible after filtering."
        else:
            select_tooltip = "No visible applications to select."
        self.select_all_check.setToolTip(select_tooltip)
        self._set_available(
            self.clear_selection_button,
            has_selection and not self._bulk_operation_busy,
            "Clear all selected applications, including rows hidden by filters.",
            "No applications are selected." if not has_selection else busy_reason,
        )
        self.more_button.setToolTip("Additional application actions")

    def _set_available(self, control, enabled: bool, available_tooltip: str, reason: str) -> None:
        control.setEnabled(enabled)
        tooltip = available_tooltip if enabled else (reason or "This action is currently unavailable.")
        control.setToolTip(tooltip)
        if isinstance(control, QAction):
            control.setStatusTip(tooltip)

    def _save_app_cache_from_table(
        self,
        context: DeviceContext | None = None,
        app_cache: AppInfoCache | None = None,
        *,
        include_system: bool | None = None,
    ) -> None:
        if self._suppress_cache_save:
            return
        if context is not None and not self._is_context_current(context):
            return
        serial = context.serial if context is not None else self._current_cache_serial()
        if not serial:
            return
        if include_system is None:
            include_system = bool(self.settings.get("show_system_apps", True))
        apps = list(getattr(self.table, "apps", []) or self.apps)
        if apps:
            (app_cache or self.app_cache).save(serial, include_system, apps)
            if (
                (context is None or Path(self.settings.config_dir) == context.profile_path)
                and self.settings.get("last_apps_device_serial", "") != serial
            ):
                self.settings.set("last_apps_device_serial", serial)

    def _current_cache_serial(self) -> str:
        return str(
            self.device_manager.active.serial
            or self.adb.serial
            or self.settings.get("active_device_serial", "")
            or self.settings.get("last_apps_device_serial", "")
            or self.settings.get("last_connected_device_serial", "")
            or ""
        )

    def selected_apps(self) -> list[AppInfo]:
        apps = self.table.checked_apps(include_hidden=True)
        if not apps:
            QMessageBox.information(self, "Apps", "Select one or more apps first.")
        return apps

    def _can_start_bulk_operation(self, action: str) -> bool:
        reason = ""
        if self._bulk_operation_busy:
            reason = f"Another application operation is already running: {self._bulk_operation_name or 'busy'}."
        elif self._apps_loading or self._assets_loading:
            reason = "Wait for application data loading to finish before starting a bulk operation."
        elif not self._device_available_for_apps():
            reason = f"{action} requires an authorized ADB or Recovery device."
        if reason:
            QMessageBox.information(self, action, reason)
            self._update_action_states()
            return False
        return True

    def _set_bulk_operation_busy(self, busy: bool, operation_name: str = "") -> None:
        self._bulk_operation_busy = bool(busy)
        self._bulk_operation_name = operation_name if busy else ""
        if busy:
            self._refresh_after_bulk = False
        self._update_action_states()

    def _prepare_bulk_operation(
        self,
        action_title: str,
        operation_name: str,
    ) -> tuple[DeviceContext, object, BackupManager, DeviceInfo, OperationToken] | None:
        try:
            context = self._require_apps_context()
            if not self._apps_view_matches_context(context):
                raise DeviceContextUnavailable(
                    "The application list belongs to another device or profile. Refresh applications before continuing."
                )
            bound_adb = self._bound_adb_for_context(context)
            backup_manager = self._backup_manager_for_context(context)
            device = self._device_snapshot(context)
            token = self._register_operation(
                context,
                "bulk",
                "device-package-workflow",
                additional_conflicts=(f"device-exclusive:{context.serial}",),
            )
        except (DeviceContextUnavailable, OperationConflictError, RuntimeError) as exc:
            QMessageBox.information(self, action_title, str(exc))
            return None
        self._bulk_token = token
        self._set_bulk_operation_busy(True, operation_name)
        return context, bound_adb, backup_manager, device, token

    def _bulk_information(
        self,
        token: OperationToken,
        context: DeviceContext,
        title: str,
        messages: list[str],
    ) -> None:
        if self._can_apply_operation(token, context):
            QMessageBox.information(self, title, "\n".join(messages[:80]) or "Done")

    def _bulk_operation_done(
        self,
        token: OperationToken,
        context: DeviceContext,
        title: str,
        messages: list[str],
    ) -> None:
        if self._can_apply_operation(token, context):
            self._operation_done(title, messages, refresh=True)

    def _bulk_failed(
        self,
        token: OperationToken,
        context: DeviceContext,
        title: str,
        message: str,
    ) -> None:
        if self._can_apply_operation(token, context):
            show_error_dialog(self, title, message, context.logs_path)

    def _start_bulk_worker(
        self,
        token: OperationToken,
        context: DeviceContext,
        worker: Worker,
    ) -> None:
        worker.signals.finished.connect(lambda: self._finish_bulk_operation(token, context))
        if not start_worker(
            self,
            self.pool,
            worker,
            operation_registry=self.operations,
            operation_token=token,
        ):
            self._finish_bulk_operation(token, context)

    def _finish_bulk_operation(
        self,
        token: OperationToken | None = None,
        context: DeviceContext | None = None,
    ) -> None:
        token = token or self._bulk_token
        if token is not None:
            self.operations.finish(token)
            if self._bulk_token is not token:
                return
            context = context or token.device_context
        self._bulk_token = None
        refresh = self._refresh_after_bulk
        self._refresh_after_bulk = False
        self._set_bulk_operation_busy(False)
        if refresh and (context is None or self._is_context_current(context)) and not (token and token.cancelled):
            self.refresh_apps()

    def backup_selected(self) -> None:
        if not self._can_start_bulk_operation("Backup selected"):
            return
        apps = self.selected_apps()
        if not apps:
            return
        prepared = self._prepare_bulk_operation("Backup selected", "backup")
        if prepared is None:
            return
        context, bound_adb, backup_manager, device, token = prepared
        root_enabled = bool(self.settings.get("root_mode_enabled", False))

        def run_backup() -> list[str]:
            messages: list[str] = []
            if token.cancelled:
                return messages
            self._require_current_context(context)
            use_root = self._apps_root_enabled(
                bound_adb,
                root_enabled,
                cancel_event=token.cancel_event,
            )
            if use_root:
                messages.append("Root mode: APK backups use su/root streaming when normal adb pull is blocked.")
            for app in apps:
                if token.cancelled:
                    break
                self._require_current_context(context)
                ok, _backup, message = backup_manager.create_backup(
                    app,
                    bound_adb,
                    device,
                    self._uninstall_method(app),
                    app.icon_path,
                    use_root=use_root,
                    cancel_event=token.cancel_event,
                )
                if token.cancelled:
                    break
                messages.append(f"{app.package_name}: {'OK' if ok else 'FAILED'} - {message}")
            return messages

        worker = Worker(run_backup)
        worker.signals.result.connect(
            lambda messages: self._bulk_information(token, context, "Backup selected", messages)
        )
        worker.signals.error.connect(
            lambda message, _trace: self._bulk_failed(
                token,
                context,
                "Selected applications could not be backed up",
                message,
            )
        )
        self._start_bulk_worker(token, context, worker)

    def uninstall_selected(self) -> None:
        if not self._can_start_bulk_operation("Uninstall selected"):
            return
        apps = self.selected_apps()
        if not apps:
            return
        if not self._confirm_apps("Uninstall selected apps", apps, uninstall=True):
            return
        require_backup = bool(self.settings.get("require_backup_before_uninstall", True))
        root_enabled = bool(self.settings.get("root_mode_enabled", False))
        prepared = self._prepare_bulk_operation("Uninstall selected", "uninstall")
        if prepared is None:
            return
        context, bound_adb, backup_manager, device, token = prepared

        def run_uninstall() -> list[str]:
            messages: list[str] = []
            if token.cancelled:
                return messages
            self._require_current_context(context)
            use_root = self._apps_root_enabled(
                bound_adb,
                root_enabled,
                cancel_event=token.cancel_event,
            )
            for app in apps:
                if token.cancelled:
                    break
                self._require_current_context(context)
                if require_backup:
                    ok, _backup, message = backup_manager.create_backup(
                        app,
                        bound_adb,
                        device,
                        self._uninstall_method(app),
                        app.icon_path,
                        use_root=use_root,
                        cancel_event=token.cancel_event,
                    )
                    if token.cancelled:
                        break
                    if not ok:
                        messages.append(f"{app.package_name}: skipped, backup failed - {message}")
                        continue
                # A device switch after a successful safety backup must never
                # allow the destructive half of this workflow to begin.
                self._require_current_context(context)
                if token.cancelled:
                    break
                result = bound_adb.uninstall_package(
                    app.package_name,
                    system_app=app.is_system,
                    use_root=use_root,
                    cancel_event=token.cancel_event,
                )
                messages.append(f"{app.package_name}: {result.status}")
            return messages

        worker = Worker(run_uninstall)
        worker.signals.result.connect(
            lambda messages: self._bulk_operation_done(
                token,
                context,
                "Uninstall selected",
                messages,
            )
        )
        worker.signals.error.connect(
            lambda message, _trace: self._bulk_failed(
                token,
                context,
                "Selected applications could not be uninstalled",
                message,
            )
        )
        self._start_bulk_worker(token, context, worker)

    def set_enabled_selected(self, enabled: bool) -> None:
        action = "Enable" if enabled else "Disable"
        if not self._can_start_bulk_operation(f"{action} selected"):
            return
        apps = self.selected_apps()
        if not apps:
            return
        required_state = "disabled" if enabled else "enabled"
        states = {str(app.state or "").strip().casefold() for app in apps}
        if states != {required_state}:
            QMessageBox.information(
                self,
                f"{action} selected",
                f"{action} is available only when every selected application is {required_state}.",
            )
            self._update_action_states()
            return
        if not self._confirm_apps(f"{action} selected apps", apps, uninstall=False):
            return
        prepared = self._prepare_bulk_operation(f"{action} selected", action.casefold())
        if prepared is None:
            return
        context, bound_adb, _backup_manager, _device, token = prepared

        def run() -> list[str]:
            messages: list[str] = []
            for app in apps:
                if token.cancelled:
                    break
                self._require_current_context(context)
                result = (
                    bound_adb.enable_package(
                        app.package_name,
                        cancel_event=token.cancel_event,
                    )
                    if enabled
                    else bound_adb.disable_package(
                        app.package_name,
                        cancel_event=token.cancel_event,
                    )
                )
                messages.append(f"{app.package_name}: {result.status}")
            return messages

        worker = Worker(run)
        worker.signals.result.connect(
            lambda messages: self._bulk_operation_done(
                token,
                context,
                f"{action} selected",
                messages,
            )
        )
        worker.signals.error.connect(
            lambda message, _trace: self._bulk_failed(
                token,
                context,
                f"{action} operation failed",
                message,
            )
        )
        self._start_bulk_worker(token, context, worker)

    def install_existing_selected(self) -> None:
        if not self._can_start_bulk_operation("Install existing"):
            return
        apps = self.selected_apps()
        if not apps:
            return
        prepared = self._prepare_bulk_operation("Install existing", "install existing")
        if prepared is None:
            return
        context, bound_adb, _backup_manager, _device, token = prepared

        def run() -> list[str]:
            messages: list[str] = []
            for app in apps:
                if token.cancelled:
                    break
                self._require_current_context(context)
                result = bound_adb.restore_existing_package(
                    app.package_name,
                    cancel_event=token.cancel_event,
                )
                messages.append(f"{app.package_name}: {result.status}")
            return messages

        worker = Worker(run)
        worker.signals.result.connect(
            lambda messages: self._bulk_operation_done(
                token,
                context,
                "Install existing",
                messages,
            )
        )
        worker.signals.error.connect(
            lambda message, _trace: self._bulk_failed(
                token,
                context,
                "Existing application could not be installed",
                message,
            )
        )
        self._start_bulk_worker(token, context, worker)

    def export_packages(self) -> None:
        if not self.apps:
            QMessageBox.information(self, "Export package list", "Load applications first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export package list", "openadb-packages.csv", "CSV files (*.csv)")
        if not path:
            return
        with Path(path).open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                [
                    "label",
                    "package",
                    "type",
                    "state",
                    "versionName",
                    "versionCode",
                    "apkPaths",
                    "size",
                    "bloatwareRemoval",
                    "bloatwareList",
                ]
            )
            for app in self.apps:
                writer.writerow(
                    [
                        app.display_name,
                        app.package_name,
                        app.app_type,
                        app.state,
                        app.version_name,
                        app.version_code,
                        app.apk_path_text,
                        app.size,
                        app.bloatware_removal,
                        app.bloatware_list,
                    ]
                )
        QMessageBox.information(self, "Export package list", "Package list exported.")

    def clear_apps_cache(self) -> None:
        if self._bulk_operation_busy or self._apps_loading or self._assets_loading:
            QMessageBox.information(
                self,
                "Clear Apps cache",
                "Application data or another operation is still running. Wait until it finishes, then clear the cache.",
            )
            return
        cleanup_identity = self._apps_cache_identity()
        answer = QMessageBox.warning(
            self,
            "Clear Apps cache",
            (
                "This will permanently delete the Apps cache:\n\n"
                "- cached app list and metadata\n"
                "- cached app icons\n"
                "- cached APK labels\n"
                "- temporary pulled APK/app data\n\n"
                "The current table can stay visible until you refresh, but the next app load will rebuild everything from the device. Continue?"
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Ok:
            self.status_label.setText("Apps cache cleanup cancelled.")
            return
        if cleanup_identity != self._apps_cache_identity():
            self.status_label.setText(
                "Apps cache cleanup cancelled because the active device profile or cache folders changed."
            )
            return
        removed = self._clear_apps_cache_files()
        self._suppress_cache_save = True
        detail = ", ".join(removed) if removed else "nothing was present"
        self.status_label.setText(
            f"Apps cache cleared ({detail}). Press Refresh applications to rebuild it from the connected device."
        )
        QMessageBox.information(self, "Clear Apps cache", "Apps cache cleared.")

    def _apps_cache_identity(self) -> tuple[str, str, str]:
        return (
            str(Path(self.settings.config_dir).expanduser()),
            str(Path(self.settings.temp_folder).expanduser()),
            str(getattr(self.settings, "active_profile_serial", "") or ""),
        )

    def _clear_apps_cache_files(self) -> list[str]:
        removed: list[str] = []
        cache_targets = [
            ("app metadata cache", self.app_cache.clear_cache),
            ("icon cache", self.icon_extractor.clear_cache),
            ("APK label cache", self.apk_metadata.clear_cache),
        ]
        for name, clear in cache_targets:
            clear()
            removed.append(name)
        for name in ["apk-assets", "acbridge", "icon-cache"]:
            path = self.settings.temp_folder / name
            if not path.exists():
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed.append(name)
            except OSError:
                continue
        if self.settings.get("last_apps_device_serial", ""):
            self.settings.set("last_apps_device_serial", "")
        return removed

    def _confirm_apps(self, title: str, apps: list[AppInfo], uninstall: bool) -> bool:
        lines = []
        dangerous = []
        for app in apps:
            method = self._uninstall_method(app) if uninstall else ("pm enable" if app.is_disabled else "pm disable-user")
            lines.append(f"{app.display_name}\n{app.package_name}\nType: {app.app_type}; planned method: {method}")
            if app.is_system or is_dangerous_package(app.package_name):
                dangerous.append(app.package_name)
        text = "\n\n".join(lines[:20])
        if len(lines) > 20:
            text += f"\n\n...and {len(lines) - 20} more"
        if dangerous:
            text += (
                "\n\nWarning: selected system or critical packages can break Android features. "
                "System app uninstall uses pm uninstall --user 0 and can be restored with cmd package install-existing."
            )
        answer = QMessageBox.warning(self, title, text, QMessageBox.Ok | QMessageBox.Cancel)
        if answer != QMessageBox.Ok:
            return False
        if dangerous:
            typed, ok = QInputDialog.getText(self, "Manual confirmation", "Type CONFIRM to continue:")
            return ok and typed == "CONFIRM"
        return True

    def _uninstall_method(self, app: AppInfo) -> str:
        return "pm uninstall --user 0" if app.is_system else "pm uninstall"

    def _apps_root_enabled(
        self,
        adb=None,
        setting_enabled: bool | None = None,
        cancel_event=None,
    ) -> bool:
        if setting_enabled is None:
            setting_enabled = bool(self.settings.get("root_mode_enabled", False))
        if not setting_enabled:
            return False
        if cancel_event is not None and cancel_event.is_set():
            return False
        return (adb or self.adb).root_available(cancel_event=cancel_event)

    def _apps_root_available_for_acbridge(self, adb=None, cancel_event=None) -> bool:
        return (adb or self.adb).root_available(cancel_event=cancel_event)

    def _fallback_label_from_package(self, package_name: str, apk_paths: list[str] | None = None) -> str:
        package_name = (package_name or "").strip()
        if not package_name:
            return ""
        apk_label = self._label_from_apk_paths(apk_paths or [])
        lowered = package_name.lower()
        if "auto_generated" in lowered or lowered.endswith("_rro") or ".overlay" in lowered:
            base = self._overlay_label_source(package_name, apk_paths or [])
            return self._compact_display_label(f"{base} overlay" if base else apk_label or "Generated overlay", package_name)
        if apk_label and len(apk_label) <= 48:
            return self._compact_display_label(apk_label, package_name)
        return self._compact_display_label(self._label_from_package_tokens(package_name), package_name)

    def _label_from_package_tokens(self, package_name: str) -> str:
        tokens = [part for part in re.split(r"[._-]+", package_name) if part]
        while tokens and tokens[0].lower() in {"com", "org", "net", "android", "apps", "app", "io", "dev", "co"}:
            tokens.pop(0)
        while tokens and tokens[0].lower() in {"google", "android"} and len(tokens) > 1:
            tokens.pop(0)
        while tokens and tokens[0].lower() in {"apps", "app"} and len(tokens) > 1:
            tokens.pop(0)
        while tokens and tokens[0].lower() in {"ai", "x"} and len(tokens) > 1:
            tokens.pop(0)
        if len(tokens) == 2 and self._looks_like_publisher_token(tokens[0], tokens[1]):
            tokens = tokens[1:]
        useful = tokens[-3:] if len(tokens) > 3 else tokens
        label = " ".join(self._label_token(token) for token in useful)
        return " ".join(label.split()) or package_name

    def _looks_like_publisher_token(self, publisher: str, product: str) -> bool:
        publisher = (publisher or "").lower()
        product = (product or "").lower()
        if not publisher or not product:
            return False
        if product in {"manager", "service", "provider", "settings", "launcher", "shell", "systemui"}:
            return False
        if publisher in {"google", "android", "microsoft", "samsung", "xiaomi", "huawei", "sony", "meta"}:
            return False
        return bool(re.search(r"(app|pro|plus|manager|player|viewer|editor|analyzer|vpn|camera|browser|store|tool)$", product))

    def _label_from_apk_paths(self, apk_paths: list[str]) -> str:
        for path in apk_paths:
            stem = Path(path).stem
            if not stem or stem.lower() in {"base", "split_config"}:
                continue
            stem = re.sub(r"__.*$", "", stem)
            stem = re.sub(r"(?i)(prebuilt|release|signed)$", "", stem)
            stem = re.sub(r"(?i)(google)?overlay$", "", stem)
            stem = stem.strip("._- ")
            if not stem:
                continue
            label = self._split_identifier(stem)
            if label:
                return label
        return ""

    def _overlay_label_source(self, package_name: str, apk_paths: list[str]) -> str:
        path_text = " ".join(apk_paths)
        candidates: list[str] = []
        for path in apk_paths:
            stem = Path(path).stem
            stem = re.sub(r"__.*$", "", stem)
            stem = re.sub(r"(?i)auto_generated.*$", "", stem)
            stem = re.sub(r"(?i)overlay$", "", stem)
            if stem:
                candidates.append(stem)
        candidates.extend(part for part in re.split(r"[._-]+", package_name) if part)
        ignored = {
            "com",
            "android",
            "google",
            "auto",
            "generated",
            "rro",
            "product",
            "vendor",
            "characteristics",
            "overlay",
            "pixel",
            "husky",
            "nosdcard",
        }
        words: list[str] = []
        for candidate in candidates:
            for token in re.findall(r"[A-Za-z0-9]+", candidate):
                if token.lower() in ignored:
                    continue
                for word in self._split_identifier(self._label_token(token)).split():
                    if word.lower() in {existing.lower() for existing in words}:
                        continue
                    words.append(word)
                if len(words) >= 3:
                    return " ".join(words)
        if "framework-res" in path_text:
            return "Framework resources"
        return ""

    def _label_token(self, token: str) -> str:
        if not token:
            return ""
        known = {
            "aicore": "AI Core",
            "androidauto": "Android Auto",
            "backupconfirm": "Backup Confirm",
            "cellbroadcastreceiver": "Cell Broadcast Receiver",
            "cellbroadcastservice": "Cell Broadcast Service",
            "companiondevicemanager": "Companion Device Manager",
            "ctsshim": "CTS Shim",
            "devicediagnostics": "Device Diagnostics",
            "filemanager": "File Manager",
            "gms": "Google Mobile Services",
            "gsf": "Google Services Framework",
            "imsserviceentitlement": "IMS Service Entitlement",
            "inputdevices": "Input Devices",
            "localtransport": "Local Transport",
            "managedprovisioning": "Managed Provisioning",
            "mmsservice": "MMS Service",
            "partnerbookmarks": "Partner Bookmarks",
            "permissioncontroller": "Permission Controller",
            "pixeldisplayservice": "Pixel Display Service",
            "packageinstaller": "Package Installer",
            "sandbox": "Sandbox",
            "settingsintelligence": "Settings Intelligence",
            "systemui": "System UI",
            "wifianalyzer": "WiFi Analyzer",
            "wifianalyzerpro": "WiFi Analyzer Pro",
        }
        acronyms = {"apk": "APK", "cts": "CTS", "ims": "IMS", "ons": "ONS", "qns": "QNS", "uwb": "UWB", "nfc": "NFC", "sdk": "SDK", "rro": "RRO"}
        lowered = token.lower()
        if lowered in known:
            return known[lowered]
        if lowered in acronyms:
            return acronyms[lowered]
        spaced = self._split_identifier(token)
        return spaced[:1].upper() + spaced[1:]

    def _split_identifier(self, value: str) -> str:
        value = re.sub(r"[_\-.]+", " ", value or "")
        value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
        value = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", value)
        value = re.sub(r"(?<=\D)(?=\d)|(?<=\d)(?=\D)", " ", value)
        return " ".join(part for part in value.split() if part)

    def _operation_done(self, title: str, messages: list[str], refresh: bool = False) -> None:
        if refresh:
            self._refresh_after_bulk = True
        QMessageBox.information(self, title, "\n".join(messages[:80]) or "Done")
