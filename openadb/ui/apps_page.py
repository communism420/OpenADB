from __future__ import annotations

import csv
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QMenu,
    QPushButton,
    QRadioButton,
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
from openadb.core.icon_extractor import IconExtractor
from openadb.core.path_utils import ensure_dir, format_bytes, safe_filename
from openadb.core.safety import is_dangerous_package
from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.ui.widgets.app_list_widget import AppTable
from openadb.ui.workers import Worker, start_worker


class AppsPage(QWidget):
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
        self.pool = QThreadPool.globalInstance()
        self.apps: list[AppInfo] = []
        self._apps_loading = False
        self._assets_loading = False
        self._metadata_cache_updates_since_flush = 0
        self._asset_cache_updates_since_flush = 0
        self._asset_progress_status = ""
        self._suppress_cache_save = False
        self._sort_mode = "name"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("Applications")
        title.setObjectName("pageTitle")
        self.total_label = QLabel("Total: 0")
        self.total_label.setObjectName("appCountLabel")
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
        self.select_all_check = QCheckBox("Select all visible")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search...")
        self.sort_button = QPushButton("Sort: name")
        self.sort_button.setToolTip("Choose application size sorting")
        controls.addWidget(self.select_all_check)
        controls.addStretch()
        controls.addWidget(self.search, 1)
        controls.addWidget(self.sort_button)
        toolbar_layout.addLayout(controls)

        filters = QHBoxLayout()
        filters.setSpacing(14)
        self.filter_group = QButtonGroup(self)
        self.filter_buttons: dict[str, QRadioButton] = {}
        for text, mode in [
            ("All", "All"),
            ("Enabled", "Enabled"),
            ("User", "User apps"),
            ("System", "System apps"),
            ("Disabled", "Disabled"),
        ]:
            button = QRadioButton(text)
            button.setProperty("filterMode", mode)
            self.filter_group.addButton(button)
            self.filter_buttons[mode] = button
            filters.addWidget(button)
        self.filter_buttons["All"].setChecked(True)
        filters.addStretch()
        toolbar_layout.addLayout(filters)

        bloatware_filters = QHBoxLayout()
        bloatware_filters.setSpacing(14)
        bloatware_label = QLabel("Bloatware category:")
        bloatware_label.setObjectName("cardCaption")
        bloatware_filters.addWidget(bloatware_label)
        for text, mode in [
            ("Recommended", "Recommended"),
            ("Advanced", "Advanced"),
            ("Expert", "Expert"),
            ("Unsafe", "Unsafe"),
            ("Not listed", "Not listed"),
        ]:
            button = QRadioButton(text)
            button.setProperty("filterMode", mode)
            self.filter_group.addButton(button)
            self.filter_buttons[mode] = button
            bloatware_filters.addWidget(button)
        bloatware_filters.addStretch()
        toolbar_layout.addLayout(bloatware_filters)

        content = QHBoxLayout()
        content.setSpacing(10)
        main_area = QVBoxLayout()
        main_area.setSpacing(8)
        main_area.addWidget(toolbar)

        self.table = AppTable()
        main_area.addWidget(self.table, 1)
        content.addLayout(main_area, 1)

        action_panel = QFrame()
        action_panel.setObjectName("appsActionPanel")
        action_panel.setFixedWidth(218)
        action_layout = QVBoxLayout(action_panel)
        action_layout.setContentsMargins(10, 10, 10, 10)
        action_layout.setSpacing(8)
        self.refresh_button = QPushButton("Get app data")
        self.select_visible = QPushButton("Select visible")
        self.unselect = QPushButton("Unselect all")
        self.backup_button = QPushButton("Backup selected")
        self.uninstall_button = QPushButton("Uninstall selected")
        self.uninstall_button.setProperty("danger", True)
        self.disable_button = QPushButton("Disable selected")
        self.enable_button = QPushButton("Enable selected")
        self.restore_existing_button = QPushButton("Install existing")
        self.export_button = QPushButton("Export package list")
        self.clear_cache_button = QPushButton("Clear apps cache")
        self.clear_cache_button.setProperty("danger", True)
        for button in [
            self.refresh_button,
            self.select_visible,
            self.unselect,
            self.backup_button,
            self.uninstall_button,
            self.disable_button,
            self.enable_button,
            self.restore_existing_button,
            self.export_button,
            self.clear_cache_button,
        ]:
            action_layout.addWidget(button)
        action_layout.addStretch()
        content.addWidget(action_panel)
        layout.addLayout(content, 1)

        self.status_label = QLabel("Press Get app data to load packages from the connected device.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.refresh_button.clicked.connect(self.refresh_apps)
        self.search.textChanged.connect(self.apply_filter)
        self.sort_button.clicked.connect(self._show_sort_menu_from_button)
        self.filter_group.buttonClicked.connect(lambda _button: self.apply_filter())
        self.select_all_check.toggled.connect(self._select_visible_toggled)
        self.table.selection_changed.connect(self._update_app_count)
        self.select_visible.clicked.connect(self.table.select_all_visible)
        self.unselect.clicked.connect(self.table.unselect_all)
        self.backup_button.clicked.connect(self.backup_selected)
        self.uninstall_button.clicked.connect(self.uninstall_selected)
        self.disable_button.clicked.connect(lambda: self.set_enabled_selected(False))
        self.enable_button.clicked.connect(lambda: self.set_enabled_selected(True))
        self.restore_existing_button.clicked.connect(self.install_existing_selected)
        self.export_button.clicked.connect(self.export_packages)
        self.clear_cache_button.clicked.connect(self.clear_apps_cache)
        self._load_cached_apps_for_saved_device()

    def refresh_storage_roots(self) -> None:
        self.app_cache.refresh_root()
        self.apk_metadata.refresh_root()

    def reset_for_device_profile(self) -> None:
        self.apps = []
        self.table.set_apps([])
        self._asset_progress_status = ""
        self._suppress_cache_save = False
        self.status_label.setText("Press Get app data to load packages from the active device profile.")
        self._update_app_count()

    def refresh_apps(self) -> None:
        if self._apps_loading:
            return
        self._suppress_cache_save = False
        include_system = bool(self.settings.get("show_system_apps", True))
        self._show_cached_apps_for_current_device(include_system)
        if self.device_manager.active.mode not in {"ADB", "Recovery"}:
            if not self.apps:
                QMessageBox.warning(self, "Apps", "Connect an authorized ADB device first.")
            return
        self._apps_loading = True
        self.status_label.setText("Refreshing package list from Android...")
        self._set_busy(True)
        worker = Worker(lambda: self.adb.list_packages(include_system=include_system, load_details=False))
        worker.signals.result.connect(self._apps_loaded)
        worker.signals.error.connect(self._apps_load_failed)
        worker.signals.finished.connect(self._apps_load_finished)
        start_worker(self, self.pool, worker)

    def _apps_loaded(self, apps: list[AppInfo]) -> None:
        include_system = bool(self.settings.get("show_system_apps", True))
        cached_apps, _saved_at = self._load_cached_apps(self._current_cache_serial(), include_system)
        if cached_apps:
            apps = self.app_cache.merge(apps, cached_apps)
        self._prepare_cached_display_labels(apps)
        self.bloatware_db.annotate(apps)
        self._apply_cached_icons(apps)
        self.apps = apps
        self.table.set_apps(apps)
        self.apply_filter()
        self._save_app_cache_from_table()
        self._start_missing_app_background_work(apps)

    def _load_cached_apps_for_saved_device(self) -> None:
        include_system = bool(self.settings.get("show_system_apps", True))
        serial = str(
            self.settings.get("active_device_serial", "")
            or self.settings.get("last_apps_device_serial", "")
            or self.settings.get("last_connected_device_serial", "")
            or ""
        )
        if serial:
            self._show_cached_apps(serial, include_system, "Loaded cached app data. Connect the device and press Refresh apps to update it.")

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
        self.apps = cached_apps
        self.table.set_apps(cached_apps)
        self.apply_filter()
        suffix = f" Last saved: {saved_at}." if saved_at else ""
        self.status_label.setText(status + suffix)
        return True

    def _load_cached_apps(self, serial: str, include_system: bool) -> tuple[list[AppInfo], str]:
        if not serial:
            return [], ""
        return self.app_cache.load(serial, include_system)

    def _apply_cached_icons(self, apps: list[AppInfo], device_serial: str = "") -> None:
        device_serial = device_serial or self._current_cache_serial()
        for app in apps:
            cached_icon = self._cached_icon_path(app, device_serial)
            if cached_icon:
                app.icon_path = str(cached_icon)

    def _prepare_cached_display_labels(self, apps: list[AppInfo]) -> None:
        for app in apps:
            normalized = self._normalize_display_label(app.app_label, app.package_name, app.apk_paths)
            if normalized != (app.app_label or "").strip():
                app.app_label = normalized
                app.assets_checked = False

    def _apps_load_failed(self, message: str, trace: str) -> None:
        self.status_label.setText(f"Failed to load apps: {message}")
        QMessageBox.critical(self, "Apps", message)

    def _apps_load_finished(self) -> None:
        self._apps_loading = False
        self._set_busy(False)

    def _start_missing_app_background_work(self, apps: list[AppInfo]) -> None:
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
            self._load_apk_assets_background(apps, list(bridge_targets_by_package.values()), metadata_targets)

    def _load_metadata_background(self, apps: list[AppInfo]) -> None:
        package_names = [app.package_name for app in apps]
        app_by_package = {app.package_name: app for app in apps}
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
            max_workers = self._metadata_worker_count(len(apps))
            updated_by_package: dict[str, AppInfo] = {}

            def on_progress(done: int, total: int, package_name: str, details: dict[str, str]) -> None:
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

            self.adb.get_package_details_many(package_names, max_workers=max_workers, progress_callback=on_progress)
            for app in apps:
                updated_apps.append(updated_by_package.get(app.package_name) or build_updated_app(app, {}))
            return updated_apps

        if not package_names:
            return
        worker = Worker(load_metadata)
        worker.signals.progress.connect(self._metadata_progress)
        worker.signals.item.connect(self._metadata_item_loaded)
        worker.signals.result.connect(self._metadata_loaded)
        worker.signals.error.connect(self._metadata_failed)
        start_worker(self, self.pool, worker)

    def _metadata_worker_count(self, target_count: int) -> int:
        if target_count <= 1:
            return 1
        configured = self.settings.get("apps_metadata_parallelism", 6)
        try:
            value = int(configured)
        except (TypeError, ValueError):
            value = 6
        return max(2, min(value, target_count, 8))

    def _metadata_progress(self, message: str) -> None:
        if self._assets_loading and self._asset_progress_status:
            return
        self.status_label.setText(message)

    def _metadata_item_loaded(self, app: AppInfo) -> None:
        self.table.update_app_details(app)
        self._metadata_cache_updates_since_flush += 1
        if self._metadata_cache_updates_since_flush >= 48:
            self._metadata_cache_updates_since_flush = 0
            self._save_app_cache_from_table()

    def _metadata_loaded(self, updated_apps: list[AppInfo]) -> None:
        for app in updated_apps:
            self.table.update_app_details(app)
        self.apply_filter()
        self._save_app_cache_from_table()
        apps = list(getattr(self.table, "apps", []) or self.apps)
        pending = sum(1 for app in apps if not app.metadata_checked)
        if self._assets_loading and self._asset_progress_status:
            self.status_label.setText(self._asset_progress_status)
        elif pending:
            self.status_label.setText(f"Version metadata cached for {len(updated_apps)} apps. {pending} apps still need metadata.")
        else:
            self.status_label.setText("Version metadata cache is complete. App labels and icons may still be loading.")

    def _metadata_failed(self, message: str, trace: str) -> None:
        if self._assets_loading and self._asset_progress_status:
            self.status_label.setText(self._asset_progress_status)
            return
        self.status_label.setText(f"Version metadata refresh failed: {message}")

    def _load_apk_assets_background(
        self,
        apps: list[AppInfo],
        targets: list[AppInfo],
        metadata_targets: list[AppInfo] | None = None,
    ) -> None:
        device_serial = self._current_cache_serial()
        target_apps = list(targets)
        metadata_target_packages = {app.package_name for app in (metadata_targets or [])}
        if not target_apps:
            return
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
        self.clear_cache_button.setEnabled(False)

        def load_assets(progress_callback=None, item_callback=None) -> list[AppInfo]:
            updated_apps: list[AppInfo] = []
            pull_dir = ensure_dir(self.settings.temp_folder / "apk-assets")
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
                cached_label = self._cached_display_label(app)
                if cached_label:
                    cached_labels[app.package_name] = cached_label
                cached_icon = self._cached_acbridge_icon_path(app, device_serial) or self._cached_icon_path(app, device_serial)
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
            bridge = ACBridgeClient(self.adb, self.settings, self.icon_extractor)
            bridge_package_names = missing_labels | missing_icons | missing_metadata
            bridge_root = self._apps_root_available_for_acbridge()
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
                )
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
                details_by_package = self.adb.get_package_details_many(
                    missing_metadata_after_bridge,
                    max_workers=self._metadata_worker_count(len(missing_metadata_after_bridge)),
                )
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
                sizes_by_package = self.adb.get_package_sizes_bulk(
                    missing_sizes_after_bridge,
                    use_root=bridge_root,
                )
                for package_name, size_bytes in sizes_by_package.items():
                    bridge_metadata.setdefault(package_name, {})["sizeBytes"] = str(size_bytes)
            apps_by_name = {app.package_name: app for app in apps}
            for package_name, label in cached_labels.items():
                app = apps_by_name.get(package_name)
                if app:
                    self.apk_metadata.set_cached_label(app, label)
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
                apk_paths_by_package = self.adb.get_package_paths_bulk([app.package_name for app in fallback_apps])

            for app in fallback_apps:
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

                self.adb.pull_files_via_temp(
                    pull_plan,
                    chunk_size=16,
                    timeout=900,
                    progress_callback=pull_progress,
                    parallel_chunks=2,
                    use_root=bridge_root,
                )

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
                        label = self.apk_metadata.extract_label(target)
                        label = self._normalize_display_label(label, app.package_name, updated.apk_paths)
                        if label:
                            updated.app_label = label
                            self.apk_metadata.set_cached_label(app, label)
                    if not updated.icon_path:
                        icon = self.icon_extractor.extract_from_apk(target, app.package_name, app.version_name, app.version_code)
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
        worker.signals.progress.connect(self._set_asset_progress_status)
        worker.signals.item.connect(self._apk_asset_loaded)
        worker.signals.result.connect(self._apk_assets_loaded)
        worker.signals.error.connect(self._apk_assets_failed)
        worker.signals.finished.connect(self._apk_assets_finished)
        start_worker(self, self.pool, worker)

    def _apk_assets_finished(self) -> None:
        self._assets_loading = False
        self.clear_cache_button.setEnabled(not self._apps_loading)

    def _apk_assets_failed(self, message: str, trace: str) -> None:
        self._save_app_cache_from_table()
        self.status_label.setText(f"App labels and icons failed to load: {message}")

    def _set_asset_progress_status(self, message: str) -> None:
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

    def _cached_icon_path(self, app: AppInfo, device_serial: str = "") -> Path | None:
        serial_key = safe_filename(device_serial or self.adb.serial or "device")
        return self.icon_extractor.cached_icon_path(
            app.package_name,
            app.version_name,
            app.version_code,
            source_keys=[f"acbridge_{serial_key}", ""],
        )

    def _cached_acbridge_icon_path(self, app: AppInfo, device_serial: str = "") -> Path | None:
        serial_key = safe_filename(device_serial or self.adb.serial or "device")
        path = self.icon_extractor.cache_path(
            app.package_name,
            app.version_name,
            app.version_code,
            source_key=f"acbridge_{serial_key}",
        )
        try:
            return path if path.is_file() and path.stat().st_size > 0 else None
        except OSError:
            return None

    def _cached_display_label(self, app: AppInfo) -> str:
        for label in (app.app_label, self.apk_metadata.cached_label(app)):
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

    def _apk_asset_loaded(self, app: AppInfo) -> None:
        self.table.update_app_details(app)
        if app.icon_path:
            self.table.set_icon_for_package(app.package_name, app.icon_path)
        self._asset_cache_updates_since_flush += 1
        if self._asset_cache_updates_since_flush >= 32:
            self._asset_cache_updates_since_flush = 0
            self._save_app_cache_from_table()

    def _apk_assets_loaded(self, updated_apps: list[AppInfo]) -> None:
        for app in updated_apps:
            self.table.update_app_details(app)
            if app.icon_path:
                self.table.set_icon_for_package(app.package_name, app.icon_path)
        self.table.sort_by_label()
        self.apply_filter()
        self._save_app_cache_from_table()
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

    def apply_filter(self) -> None:
        self.table.apply_filter(
            self.search.text(),
            self._current_filter_mode(),
            self._sort_mode,
        )
        self._update_app_count()

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
        self._sort_mode = mode if mode in {"name", "size_desc", "size_asc"} else "name"
        self._update_sort_button_text()
        self.apply_filter()

    def _update_sort_button_text(self) -> None:
        labels = {
            "name": "Sort: name",
            "size_desc": "Size: largest first",
            "size_asc": "Size: smallest first",
        }
        self.sort_button.setText(labels.get(self._sort_mode, labels["name"]))

    def _current_filter_mode(self) -> str:
        checked = self.filter_group.checkedButton()
        if checked is None:
            return "All"
        return str(checked.property("filterMode") or "All")

    def _select_visible_toggled(self, checked: bool) -> None:
        if checked:
            self.table.select_all_visible()
        else:
            self.table.unselect_all()
        self._update_app_count()

    def _update_app_count(self) -> None:
        total = self.table.rowCount()
        visible = self.table.visible_count()
        selected = len(self.table.checked_apps())
        self.total_label.setText(f"Total: {total}    Visible: {visible}    Selected: {selected}")
        self.select_all_check.blockSignals(True)
        self.select_all_check.setChecked(visible > 0 and selected >= visible)
        self.select_all_check.blockSignals(False)

    def _save_app_cache_from_table(self) -> None:
        if self._suppress_cache_save:
            return
        serial = self._current_cache_serial()
        if not serial:
            return
        include_system = bool(self.settings.get("show_system_apps", True))
        apps = list(getattr(self.table, "apps", []) or self.apps)
        if apps:
            self.app_cache.save(serial, include_system, apps)
            if self.settings.get("last_apps_device_serial", "") != serial:
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
        apps = self.table.checked_apps()
        if not apps:
            QMessageBox.information(self, "Apps", "Select one or more apps first.")
        return apps

    def backup_selected(self) -> None:
        apps = self.selected_apps()
        if not apps:
            return
        self._set_busy(True)

        def run_backup() -> list[str]:
            messages: list[str] = []
            use_root = self._apps_root_enabled()
            if use_root:
                messages.append("Root mode: APK backups use su/root streaming when normal adb pull is blocked.")
            for app in apps:
                ok, _backup, message = self.backup_manager.create_backup(
                    app,
                    self.adb,
                    self.device_manager.active,
                    self._uninstall_method(app),
                    app.icon_path,
                    use_root=use_root,
                )
                messages.append(f"{app.package_name}: {'OK' if ok else 'FAILED'} - {message}")
            return messages

        worker = Worker(run_backup)
        worker.signals.result.connect(lambda messages: QMessageBox.information(self, "Backup selected", "\n".join(messages)))
        worker.signals.error.connect(lambda message, _trace: QMessageBox.critical(self, "Backup selected", message))
        worker.signals.finished.connect(lambda: self._set_busy(False))
        start_worker(self, self.pool, worker)

    def uninstall_selected(self) -> None:
        apps = self.selected_apps()
        if not apps:
            return
        if not self._confirm_apps("Uninstall selected apps", apps, uninstall=True):
            return
        require_backup = bool(self.settings.get("require_backup_before_uninstall", True))
        self._set_busy(True)

        def run_uninstall() -> list[str]:
            messages: list[str] = []
            use_root = self._apps_root_enabled()
            for app in apps:
                if require_backup:
                    ok, _backup, message = self.backup_manager.create_backup(
                        app,
                        self.adb,
                        self.device_manager.active,
                        self._uninstall_method(app),
                        app.icon_path,
                        use_root=use_root,
                    )
                    if not ok:
                        messages.append(f"{app.package_name}: skipped, backup failed - {message}")
                        continue
                result = self.adb.uninstall_package(app.package_name, system_app=app.is_system, use_root=use_root)
                messages.append(f"{app.package_name}: {result.status}")
            return messages

        worker = Worker(run_uninstall)
        worker.signals.result.connect(lambda messages: self._operation_done("Uninstall selected", messages, refresh=True))
        worker.signals.error.connect(lambda message, _trace: QMessageBox.critical(self, "Uninstall selected", message))
        worker.signals.finished.connect(lambda: self._set_busy(False))
        start_worker(self, self.pool, worker)

    def set_enabled_selected(self, enabled: bool) -> None:
        apps = self.selected_apps()
        if not apps:
            return
        action = "Enable" if enabled else "Disable"
        if not self._confirm_apps(f"{action} selected apps", apps, uninstall=False):
            return
        self._set_busy(True)

        def run() -> list[str]:
            messages: list[str] = []
            for app in apps:
                result = self.adb.enable_package(app.package_name) if enabled else self.adb.disable_package(app.package_name)
                messages.append(f"{app.package_name}: {result.status}")
            return messages

        worker = Worker(run)
        worker.signals.result.connect(lambda messages: self._operation_done(f"{action} selected", messages, refresh=True))
        worker.signals.error.connect(lambda message, _trace: QMessageBox.critical(self, action, message))
        worker.signals.finished.connect(lambda: self._set_busy(False))
        start_worker(self, self.pool, worker)

    def install_existing_selected(self) -> None:
        apps = self.selected_apps()
        if not apps:
            return
        self._set_busy(True)

        def run() -> list[str]:
            messages: list[str] = []
            for app in apps:
                result = self.adb.restore_existing_package(app.package_name)
                messages.append(f"{app.package_name}: {result.status}")
            return messages

        worker = Worker(run)
        worker.signals.result.connect(lambda messages: self._operation_done("Install existing", messages, refresh=True))
        worker.signals.error.connect(lambda message, _trace: QMessageBox.critical(self, "Install existing", message))
        worker.signals.finished.connect(lambda: self._set_busy(False))
        start_worker(self, self.pool, worker)

    def export_packages(self) -> None:
        if not self.apps:
            QMessageBox.information(self, "Export package list", "Refresh apps first.")
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
        if self._assets_loading:
            QMessageBox.information(
                self,
                "Clear Apps cache",
                "App labels and icons are still loading. Wait until loading finishes, then clear the cache.",
            )
            return
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
        removed = self._clear_apps_cache_files()
        self._suppress_cache_save = True
        detail = ", ".join(removed) if removed else "nothing was present"
        self.status_label.setText(f"Apps cache cleared ({detail}). Press Refresh apps to rebuild it from the connected device.")
        QMessageBox.information(self, "Clear Apps cache", "Apps cache cleared.")

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

    def _apps_root_enabled(self) -> bool:
        if not bool(self.settings.get("root_mode_enabled", False)):
            return False
        return self.adb.root_available()

    def _apps_root_available_for_acbridge(self) -> bool:
        return self.adb.root_available()

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
        QMessageBox.information(self, title, "\n".join(messages[:80]) or "Done")
        if refresh:
            self.refresh_apps()

    def _set_busy(self, busy: bool) -> None:
        for button in [
            self.refresh_button,
            self.backup_button,
            self.uninstall_button,
            self.disable_button,
            self.enable_button,
            self.restore_existing_button,
            self.export_button,
        ]:
            button.setEnabled(not busy)
        self.clear_cache_button.setEnabled(not busy and not self._assets_loading)
