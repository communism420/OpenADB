from __future__ import annotations

import csv
import shutil
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from openadb.core.app_cache import AppInfoCache
from openadb.core.app_operation_coordinator import AppOperationCoordinator
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.operations import OperationConflictError, OperationToken
from openadb.core.safety import is_dangerous_package
from openadb.models.app_info import AppInfo
from openadb.ui.dialogs import show_error_dialog
from openadb.ui.workers import Worker


class AppsActionWorkflow:
    """Bulk package operations, exports, and cache-cleanup UI orchestration."""

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
    ) -> tuple[DeviceContext, AppOperationCoordinator, OperationToken] | None:
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
        coordinator = AppOperationCoordinator(
            context=context,
            adb=bound_adb,
            backup_manager=backup_manager,
            device=device,
            cancel_event=token.cancel_event,
            require_current=self._require_current_context,
            root_enabled=bool(self.settings.get("root_mode_enabled", False)),
        )
        return context, coordinator, token

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
        if not self._start_page_worker(worker, token):
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
        context, coordinator, token = prepared

        worker = Worker(lambda: coordinator.backup(apps))
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
        prepared = self._prepare_bulk_operation("Uninstall selected", "uninstall")
        if prepared is None:
            return
        context, coordinator, token = prepared

        worker = Worker(lambda: coordinator.uninstall(apps, require_backup=require_backup))
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
        context, coordinator, token = prepared

        worker = Worker(lambda: coordinator.set_enabled(apps, enabled=enabled))
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
        context, coordinator, token = prepared

        worker = Worker(lambda: coordinator.install_existing(apps))
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
        return AppOperationCoordinator.uninstall_method(app)

    def _operation_done(self, title: str, messages: list[str], refresh: bool = False) -> None:
        if refresh:
            self._refresh_after_bulk = True
        QMessageBox.information(self, title, "\n".join(messages[:80]) or "Done")
