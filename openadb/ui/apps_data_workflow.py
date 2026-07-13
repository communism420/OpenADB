from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from openadb.core.app_asset_loader import (
    LABEL_FORMATTER,
    AppAssetLoader,
    asset_progress_percent,
    asset_progress_text,
    cached_acbridge_icon_path,
    cached_icon_path,
)
from openadb.core.app_metadata_loader import (
    AppMetadataLoader,
    has_known_size,
    metadata_has_size,
    metadata_worker_count,
    size_text_from_metadata,
)
from openadb.core.apk_metadata import APKMetadataExtractor
from openadb.core.apps_controller import AppsProfileServices as _AppsProfileServices
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.icon_extractor import IconExtractor
from openadb.core.operations import OperationConflictError, OperationToken
from openadb.models.app_info import AppInfo
from openadb.ui.dialogs import show_error_dialog
from openadb.ui.workers import Worker


class AppsDataWorkflow:
    """Application list, cache, metadata, and asset-loading UI orchestration."""

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
        if not self._start_page_worker(worker, token):
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
        self._set_table_apps(apps)
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
        self._set_table_apps(cached_apps)
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
        if not apps:
            return
        try:
            context = context or self._require_apps_context()
            services = services or self._profile_services(context)
            bound_adb = self._bound_adb_for_context(context)
            loader = AppMetadataLoader(
                bound_adb,
                self.settings.get("apps_metadata_parallelism", 6),
            )
        except (DeviceContextUnavailable, RuntimeError) as exc:
            self.status_label.setText(f"App metadata refresh could not start: {exc}")
            return
        try:
            token = self._register_operation(context, "metadata", "apps-metadata")
        except (OperationConflictError, RuntimeError):
            return
        self._metadata_cache_updates_since_flush = 0
        self._metadata_token = token

        def load_metadata(progress_callback=None, item_callback=None) -> list[AppInfo]:
            return loader.load(
                apps,
                cancel_event=token.cancel_event,
                progress_callback=(
                    progress_callback.emit if progress_callback is not None else None
                ),
                item_callback=item_callback.emit if item_callback is not None else None,
            )

        worker = Worker(load_metadata)
        worker.signals.progress.connect(
            lambda message: self._metadata_progress(token, context, message)
        )
        worker.signals.item.connect(
            lambda app: self._metadata_item_loaded(token, context, services, app)
        )
        worker.signals.result.connect(
            lambda updated: self._metadata_loaded(token, context, services, updated)
        )
        worker.signals.error.connect(
            lambda message, trace: self._metadata_failed(token, context, message, trace)
        )
        worker.signals.finished.connect(lambda: self._metadata_finished(token))
        if not self._start_page_worker(worker, token):
            self._metadata_finished(token)

    def _metadata_worker_count(self, target_count: int, configured=None) -> int:
        if configured is None:
            configured = self.settings.get("apps_metadata_parallelism", 6)
        return metadata_worker_count(target_count, configured)

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
        target_apps = list(targets)
        if not target_apps:
            return

        try:
            bound_adb = self._bound_adb_for_context(context)
            loader = AppAssetLoader(
                bound_adb,
                services.settings,
                services.apk_metadata,
                services.icon_extractor,
                device_serial=context.serial,
                temp_path=context.temp_path,
                metadata_parallelism=self.settings.get("apps_metadata_parallelism", 6),
            )
        except (DeviceContextUnavailable, RuntimeError) as exc:
            self.status_label.setText(f"App labels and icons could not start loading: {exc}")
            return
        try:
            token = self._register_operation(context, "assets", "apps-assets")
        except (OperationConflictError, RuntimeError):
            return
        self._assets_token = token
        self._assets_loading = True
        self._asset_cache_updates_since_flush = 0
        self._asset_progress_status = asset_progress_text(
            total=len(target_apps),
            labels=0,
            icons=0,
            processed=0,
            phase="Preparing app label and icon cache refresh.",
        )
        self.status_label.setText(self._asset_progress_status)
        self._update_action_states()

        def load_assets(progress_callback=None, item_callback=None) -> list[AppInfo]:
            return loader.load(
                apps,
                target_apps,
                metadata_targets,
                cancel_event=token.cancel_event,
                progress_callback=(
                    progress_callback.emit if progress_callback is not None else None
                ),
                item_callback=item_callback.emit if item_callback is not None else None,
            )

        worker = Worker(load_assets)
        worker.signals.progress.connect(
            lambda message: self._set_asset_progress_status(token, context, message)
        )
        worker.signals.item.connect(
            lambda app: self._apk_asset_loaded(token, context, services, app)
        )
        worker.signals.result.connect(
            lambda updated: self._apk_assets_loaded(token, context, services, updated)
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
        if not self._start_page_worker(worker, token):
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

    def _bridge_progress_adapter(
        self,
        progress_callback,
        progress_text,
        cached_label_count: int,
        cached_icon_count: int,
    ):
        callback = progress_callback.emit if progress_callback is not None else None
        return AppAssetLoader._bridge_progress_adapter(
            callback,
            progress_text,
            cached_label_count,
            cached_icon_count,
        )

    def _asset_progress_percent(self, total: int, labels: int, icons: int) -> int:
        return asset_progress_percent(total, labels, icons)

    def _asset_progress_text(
        self,
        total: int,
        labels: int,
        icons: int,
        processed: int,
        phase: str,
        percent_override: int | None = None,
    ) -> str:
        return asset_progress_text(
            total,
            labels,
            icons,
            processed,
            phase,
            percent_override=percent_override,
        )

    def _has_known_size(self, app: AppInfo) -> bool:
        return has_known_size(app)

    def _metadata_has_size(self, metadata: dict[str, str]) -> bool:
        return metadata_has_size(metadata)

    def _size_text_from_metadata(self, metadata: dict[str, str]) -> str:
        return size_text_from_metadata(metadata)

    def _cached_icon_path(
        self,
        app: AppInfo,
        device_serial: str = "",
        icon_extractor: IconExtractor | None = None,
    ) -> Path | None:
        return cached_icon_path(
            app,
            device_serial or str(getattr(self.adb, "serial", "") or "device"),
            icon_extractor or self.icon_extractor,
        )

    def _cached_acbridge_icon_path(
        self,
        app: AppInfo,
        device_serial: str = "",
        icon_extractor: IconExtractor | None = None,
    ) -> Path | None:
        return cached_acbridge_icon_path(
            app,
            device_serial or str(getattr(self.adb, "serial", "") or "device"),
            icon_extractor or self.icon_extractor,
        )

    def _cached_display_label(
        self,
        app: AppInfo,
        apk_metadata: APKMetadataExtractor | None = None,
    ) -> str:
        return LABEL_FORMATTER.cached_display_label(app, apk_metadata or self.apk_metadata)

    def _is_placeholder_label(self, label: str, package_name: str) -> bool:
        return LABEL_FORMATTER.is_placeholder(label, package_name)

    def _normalize_display_label(
        self,
        label: str,
        package_name: str,
        apk_paths: list[str] | None = None,
    ) -> str:
        return LABEL_FORMATTER.normalize(label, package_name, apk_paths)

    def _looks_like_internal_label(self, label: str, package_name: str) -> bool:
        return LABEL_FORMATTER.looks_like_internal(label, package_name)

    def _looks_like_generated_package_label(self, label: str, package_name: str) -> bool:
        return LABEL_FORMATTER.looks_like_generated(label, package_name)

    def _compact_display_label(self, label: str, package_name: str) -> str:
        return LABEL_FORMATTER.compact(label, package_name)

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

    def _fallback_label_from_package(
        self,
        package_name: str,
        apk_paths: list[str] | None = None,
    ) -> str:
        return LABEL_FORMATTER.fallback(package_name, apk_paths)

    def _label_from_package_tokens(self, package_name: str) -> str:
        return LABEL_FORMATTER.label_from_package_tokens(package_name)

    def _looks_like_publisher_token(self, publisher: str, product: str) -> bool:
        return LABEL_FORMATTER.looks_like_publisher_token(publisher, product)

    def _label_from_apk_paths(self, apk_paths: list[str]) -> str:
        return LABEL_FORMATTER.label_from_apk_paths(apk_paths)

    def _overlay_label_source(self, package_name: str, apk_paths: list[str]) -> str:
        return LABEL_FORMATTER.overlay_label_source(package_name, apk_paths)

    def _label_token(self, token: str) -> str:
        return LABEL_FORMATTER.label_token(token)

    def _split_identifier(self, value: str) -> str:
        return LABEL_FORMATTER.split_identifier(value)
