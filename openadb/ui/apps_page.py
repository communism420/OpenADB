from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.adb import ADBClient
from openadb.core.apk_metadata import APKMetadataExtractor
from openadb.core.backup_manager import BackupManager
from openadb.core.device import DeviceManager
from openadb.core.icon_extractor import IconExtractor
from openadb.core.path_utils import ensure_dir, safe_filename
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
        self.settings = settings
        self.pool = QThreadPool.globalInstance()
        self.apps: list[AppInfo] = []
        self._apps_loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        title = QLabel("Apps")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        toolbar = QFrame()
        toolbar.setObjectName("toolbarCard")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 10, 10, 10)
        toolbar_layout.setSpacing(8)

        controls = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh apps")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search app label or package name")
        self.filter = QComboBox()
        self.filter.addItems(["All", "User apps", "System apps", "Enabled", "Disabled"])
        self.select_visible = QPushButton("Select all visible")
        self.unselect = QPushButton("Unselect all")
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.search, 1)
        controls.addWidget(self.filter)
        controls.addWidget(self.select_visible)
        controls.addWidget(self.unselect)
        toolbar_layout.addLayout(controls)

        actions = QHBoxLayout()
        self.backup_button = QPushButton("Backup selected")
        self.uninstall_button = QPushButton("Uninstall selected")
        self.disable_button = QPushButton("Disable selected")
        self.enable_button = QPushButton("Enable selected")
        self.restore_existing_button = QPushButton("Install existing")
        self.export_button = QPushButton("Export package list")
        for button in [
            self.backup_button,
            self.uninstall_button,
            self.disable_button,
            self.enable_button,
            self.restore_existing_button,
            self.export_button,
        ]:
            actions.addWidget(button)
        actions.addStretch()
        toolbar_layout.addLayout(actions)
        layout.addWidget(toolbar)

        self.table = AppTable()
        layout.addWidget(self.table, 1)
        self.status_label = QLabel("Press Refresh apps to load packages from the connected device.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.refresh_button.clicked.connect(self.refresh_apps)
        self.search.textChanged.connect(self.apply_filter)
        self.filter.currentTextChanged.connect(self.apply_filter)
        self.select_visible.clicked.connect(self.table.select_all_visible)
        self.unselect.clicked.connect(self.table.unselect_all)
        self.backup_button.clicked.connect(self.backup_selected)
        self.uninstall_button.clicked.connect(self.uninstall_selected)
        self.disable_button.clicked.connect(lambda: self.set_enabled_selected(False))
        self.enable_button.clicked.connect(lambda: self.set_enabled_selected(True))
        self.restore_existing_button.clicked.connect(self.install_existing_selected)
        self.export_button.clicked.connect(self.export_packages)

    def refresh_apps(self) -> None:
        if self._apps_loading:
            return
        if self.device_manager.active.mode not in {"ADB", "Recovery"}:
            QMessageBox.warning(self, "Apps", "Connect an authorized ADB device first.")
            return
        self._apps_loading = True
        self.status_label.setText("Loading package list from Android...")
        self._set_busy(True)
        include_system = bool(self.settings.get("show_system_apps", True))
        worker = Worker(lambda: self.adb.list_packages(include_system=include_system, load_details=False))
        worker.signals.result.connect(self._apps_loaded)
        worker.signals.error.connect(self._apps_load_failed)
        worker.signals.finished.connect(self._apps_load_finished)
        start_worker(self, self.pool, worker)

    def _apps_loaded(self, apps: list[AppInfo]) -> None:
        self.apps = apps
        self.table.set_apps(apps)
        self.apply_filter()
        self.status_label.setText(f"Loaded {len(apps)} packages. App labels, version names and icons are loading in the background.")
        self._load_metadata_background(apps)
        self._load_apk_assets_background(apps)

    def _apps_load_failed(self, message: str, trace: str) -> None:
        self.status_label.setText(f"Failed to load apps: {message}")
        QMessageBox.critical(self, "Apps", message)

    def _apps_load_finished(self) -> None:
        self._apps_loading = False
        self._set_busy(False)

    def _load_metadata_background(self, apps: list[AppInfo]) -> None:
        package_names = [app.package_name for app in apps]

        def load_metadata() -> list[AppInfo]:
            updated_apps: list[AppInfo] = []
            for app in apps:
                details = self.adb.get_package_details(app.package_name)
                updated = AppInfo(
                    package_name=app.package_name,
                    app_label=details.get("appLabel", "") or app.app_label,
                    app_type=app.app_type,
                    state=app.state,
                    version_name=details.get("versionName", ""),
                    version_code=details.get("versionCode", "") or app.version_code,
                    apk_paths=app.apk_paths,
                    size=app.size,
                    icon_path=app.icon_path,
                )
                updated_apps.append(updated)
            return updated_apps

        if not package_names:
            return
        worker = Worker(load_metadata)
        worker.signals.result.connect(self._metadata_loaded)
        start_worker(self, self.pool, worker)

    def _metadata_loaded(self, updated_apps: list[AppInfo]) -> None:
        for app in updated_apps:
            self.table.update_app_details(app)
        self.apply_filter()
        self.status_label.setText(f"Loaded {len(self.apps)} apps. Version metadata refresh complete; app labels and icons may still be loading.")

    def _load_apk_assets_background(self, apps: list[AppInfo]) -> None:
        def load_assets(progress_callback=None, item_callback=None) -> list[AppInfo]:
            updated_apps: list[AppInfo] = []
            pull_dir = ensure_dir(self.settings.temp_folder / "apk-assets")
            total = len(apps)
            pull_plan: list[tuple[str, Path]] = []
            local_apks: dict[str, list[Path]] = {}

            if progress_callback:
                progress_callback.emit("Resolving base and split APK paths from Android...")
            apk_paths_by_package = self.adb.get_package_paths_bulk([app.package_name for app in apps])

            for app in apps:
                apk_paths = apk_paths_by_package.get(app.package_name) or app.apk_paths
                targets: list[Path] = []
                for index, apk_path in enumerate(apk_paths):
                    apk_name = safe_filename(Path(apk_path).name or f"part_{index}.apk")
                    target = pull_dir / (
                        f"{safe_filename(app.package_name)}_{safe_filename(app.version_code or '0')}_{index}_{apk_name}"
                    )
                    targets.append(target)
                local_apks[app.package_name] = targets
                cached_label = self.apk_metadata.cached_label(app)
                cache = self.icon_extractor.cache_path(app.package_name, app.version_name, app.version_code)
                needs_apk = not cached_label or not cache.exists()
                if needs_apk:
                    for apk_path, target in zip(apk_paths, targets):
                        if not target.exists():
                            pull_plan.append((apk_path, target))

            if pull_plan:
                if progress_callback:
                    progress_callback.emit(f"Pulling {len(pull_plan)} APK parts in batches through ADB...")
                self.adb.pull_files_via_temp(pull_plan, chunk_size=24, timeout=900)

            for app in apps:
                if progress_callback:
                    progress_callback.emit(f"Loading app labels from APK: {len(updated_apps) + 1}/{total}")

                updated = AppInfo(
                    package_name=app.package_name,
                    app_label=app.app_label,
                    app_type=app.app_type,
                    state=app.state,
                    version_name=app.version_name,
                    version_code=app.version_code,
                    apk_paths=apk_paths_by_package.get(app.package_name) or app.apk_paths,
                    size=app.size,
                    icon_path=app.icon_path,
                )

                cached_label = self.apk_metadata.cached_label(app)
                if cached_label:
                    updated.app_label = cached_label

                cache = self.icon_extractor.cache_path(app.package_name, app.version_name, app.version_code)
                if cache.exists():
                    updated.icon_path = str(cache)

                if (updated.app_label and updated.icon_path) or not updated.apk_paths:
                    updated_apps.append(updated)
                    if item_callback:
                        item_callback.emit(updated)
                    continue

                targets = local_apks.get(app.package_name, [])
                if not any(target.exists() for target in targets):
                    updated_apps.append(updated)
                    if item_callback:
                        item_callback.emit(updated)
                    continue

                for target in targets:
                    if not target.exists():
                        continue
                    if not updated.app_label:
                        label = self.apk_metadata.extract_label(target)
                        if label:
                            updated.app_label = label
                            self.apk_metadata.set_cached_label(app, label)
                    if not updated.icon_path:
                        icon = self.icon_extractor.extract_from_apk(target, app.package_name, app.version_name, app.version_code)
                        if icon:
                            updated.icon_path = str(icon)
                    if updated.app_label and updated.icon_path:
                        break

                updated_apps.append(updated)
                if item_callback:
                    item_callback.emit(updated)
            return updated_apps

        worker = Worker(load_assets)
        worker.signals.progress.connect(self.status_label.setText)
        worker.signals.item.connect(self._apk_asset_loaded)
        worker.signals.result.connect(self._apk_assets_loaded)
        start_worker(self, self.pool, worker)

    def _apk_asset_loaded(self, app: AppInfo) -> None:
        self.table.update_app_details(app)
        if app.icon_path:
            self.table.set_icon_for_package(app.package_name, app.icon_path)

    def _apk_assets_loaded(self, updated_apps: list[AppInfo]) -> None:
        resolved = 0
        for app in updated_apps:
            if app.app_label:
                resolved += 1
            self.table.update_app_details(app)
            if app.icon_path:
                self.table.set_icon_for_package(app.package_name, app.icon_path)
        self.table.sort_by_label()
        self.apply_filter()
        missing = len(self.apps) - resolved
        if missing > 0:
            self.status_label.setText(
                f"Loaded real labels for {resolved}/{len(self.apps)} apps. {missing} labels are not exposed in the APK resources available without root."
            )
        else:
            self.status_label.setText(f"Loaded real app labels for {resolved}/{len(self.apps)} apps.")

    def apply_filter(self) -> None:
        self.table.apply_filter(self.search.text(), self.filter.currentText())

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
            for app in apps:
                ok, _backup, message = self.backup_manager.create_backup(
                    app,
                    self.adb,
                    self.device_manager.active,
                    self._uninstall_method(app),
                    app.icon_path,
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
            for app in apps:
                if require_backup:
                    ok, _backup, message = self.backup_manager.create_backup(
                        app,
                        self.adb,
                        self.device_manager.active,
                        self._uninstall_method(app),
                        app.icon_path,
                    )
                    if not ok:
                        messages.append(f"{app.package_name}: skipped, backup failed - {message}")
                        continue
                result = self.adb.uninstall_package(app.package_name, system_app=app.is_system)
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
            writer.writerow(["label", "package", "type", "state", "versionName", "versionCode", "apkPaths", "size"])
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
                    ]
                )
        QMessageBox.information(self, "Export package list", "Package list exported.")

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
