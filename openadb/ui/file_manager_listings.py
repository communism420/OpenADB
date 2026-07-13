"""Qt worker orchestration for Android File Manager listings."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from openadb.core.device_context import DeviceContextUnavailable
from openadb.core.file_listing_controller import (
    AndroidListingResult,
    PreparedAndroidListing,
    PreparedStorageVolumes,
    StaleFileListing,
    StorageVolumesResult,
)
from openadb.core.file_manager_state import (
    StaleFileManagerProfile,
    normalize_android_path as normalize_android_path_value,
)
from openadb.core.operations import OperationToken
from openadb.core.path_utils import format_bytes, parent_android_path
from openadb.ui.workers import Worker


class FileManagerListings:
    """Keep asynchronous listing state out of the widget composition page."""

    def __init__(self, host: Any) -> None:
        self.host = host
    def refresh_android_storage_roots(self) -> None:
        self.host.invalidate_stale_device_view()
        if self.host._android_storage_loading:
            self.host._android_storage_refresh_pending = True
            return
        if self.host.device_manager.active.mode not in {"ADB", "Recovery"}:
            self.host._android_storage_context = None
            self.host._set_android_storage_combo([])
            return
        use_root_requested = self.host._file_manager_root_requested()
        try:
            prepared = self.host.listing_controller.begin_storage_volumes(
                use_root=use_root_requested,
            )
        except (DeviceContextUnavailable, StaleFileListing) as exc:
            self.host.status_label.setText(self.host._friendly_error("Storage unavailable", str(exc)))
            return
        operation = self.host._capture_device_operation(
            "file-manager.storage-volumes",
            "file-manager.storage-volumes",
            expected_context=prepared.request.device_context,
        )
        if operation is None:
            return
        context, _adb, token = operation
        if (
            self.host._android_storage_context is not None
            and self.host._android_storage_context != context
        ):
            self.host._android_storage_context = None
            self.host._set_android_storage_combo([])
        self.host._android_storage_token = token
        self.host._android_storage_request = prepared.request
        self.host._android_storage_loading = True
        self.host.android_storage_refresh_button.setEnabled(False)

        def load_storage_volumes():
            self.host._require_operation_preflight(token)
            use_root = self.host._root_available_for_worker(
                prepared.adb,
                use_root_requested,
                token.cancel_event,
            )
            self.host._require_operation_preflight(token)
            resolved = PreparedStorageVolumes(
                request=replace(prepared.request, use_root=use_root),
                adb=prepared.adb,
            )
            return self.host.listing_controller.load_storage_volumes(
                resolved,
                cancel_event=token.cancel_event,
            )

        worker = Worker(load_storage_volumes)
        worker.signals.result.connect(
            lambda volumes, current=token: self.host._android_storage_roots_loaded(current, volumes)
        )
        worker.signals.error.connect(
            lambda message, _trace, current=token: self.host._android_storage_roots_failed(current, message)
        )
        worker.signals.finished.connect(
            lambda current=token: self.host._android_storage_refresh_finished(current)
        )
        if not self.host._start_operation_worker(worker, token):
            self.host._android_storage_refresh_finished(token)

    def android_storage_refresh_finished(self, token: OperationToken) -> None:
        self.host.operations.finish(token)
        if token is not self.host._android_storage_token:
            return
        self.host._android_storage_token = None
        self.host._android_storage_request = None
        self.host._android_storage_loading = False
        self.host.android_storage_refresh_button.setEnabled(True)
        if self.host._android_storage_refresh_pending:
            self.host._android_storage_refresh_pending = False
            self.host.refresh_android_storage_roots()

    def android_storage_roots_loaded(
        self,
        token: OperationToken,
        result: StorageVolumesResult | list,
    ) -> None:
        if token is not self.host._android_storage_token or not self.host._operation_is_current(token):
            return
        if isinstance(result, StorageVolumesResult):
            try:
                result = self.host.listing_controller.accept_storage_volumes(result)
            except (DeviceContextUnavailable, StaleFileListing):
                return
            volumes = list(result.volumes)
        else:
            volumes = result
        self.host._android_storage_context = token.device_context
        self.host._set_android_storage_combo(volumes)
        self.host._select_storage_combo_for_path(self.host.android_path)

    def android_storage_roots_failed(self, token: OperationToken, message: str) -> None:
        if token is not self.host._android_storage_token or not self.host._operation_is_current(token):
            return
        request = self.host._android_storage_request
        if request is not None and not self.host.listing_controller.is_storage_current(request):
            return
        self.host.status_label.setText(self.host._friendly_error("Storage unavailable", message))

    def set_android_storage_combo(self, volumes: list) -> None:
        self.host._android_storage_volumes = list(volumes or [])
        self.host._syncing_android_storage_combo = True
        try:
            self.host.android_storage_combo.clear()
            if not self.host._android_storage_volumes:
                self.host.android_storage_combo.addItem("Internal storage", "/sdcard/")
                return
            for volume in self.host._android_storage_volumes:
                self.host.android_storage_combo.addItem(self.host._android_storage_volume_label(volume), getattr(volume, "path", ""))
        finally:
            self.host._syncing_android_storage_combo = False

    def android_storage_volume_label(self, volume) -> str:
        label = getattr(volume, "label", "") or getattr(volume, "path", "") or "Android storage"
        path = getattr(volume, "path", "")
        free = getattr(volume, "free_bytes", None)
        state = getattr(volume, "state", "")
        extras: list[str] = []
        if isinstance(free, int) and free >= 0:
            extras.append(f"{format_bytes(free)} free")
        if state and state != "mounted":
            extras.append(state)
        suffix = f" ({', '.join(extras)})" if extras else ""
        return f"{label}{suffix} - {path}"

    def android_storage_selected(self, index: int) -> None:
        if self.host._syncing_android_storage_combo or index < 0:
            return
        path = self.host.android_storage_combo.itemData(index)
        if path and self.host._normalize_android_path(str(path)) != self.host._normalize_android_path(self.host.android_path):
            self.host.navigate_android(str(path))

    def select_storage_combo_for_path(self, path: str) -> None:
        current = self.host._normalize_android_path(path).rstrip("/") or "/"
        best_index = -1
        best_length = -1
        for index in range(self.host.android_storage_combo.count()):
            raw = self.host.android_storage_combo.itemData(index)
            if not raw:
                continue
            volume_path = self.host._normalize_android_path(str(raw)).rstrip("/") or "/"
            if current == volume_path or current.startswith(volume_path + "/"):
                if len(volume_path) > best_length:
                    best_index = index
                    best_length = len(volume_path)
        if best_index >= 0 and self.host.android_storage_combo.currentIndex() != best_index:
            self.host._syncing_android_storage_combo = True
            try:
                self.host.android_storage_combo.setCurrentIndex(best_index)
            finally:
                self.host._syncing_android_storage_combo = False

    def refresh_android(self) -> None:
        self.host.invalidate_stale_device_view()
        if self.host._android_loading:
            self.host._android_refresh_pending = True
            return
        if self.host.device_manager.active.mode not in {"ADB", "Recovery"}:
            self.host._android_view_context = None
            self.host._android_view_path = ""
            self.host.android_panel.set_path(self.host.android_path)
            self.host.android_path_edit.setText(self.host.android_path)
            self.host.android_panel.set_items([])
            self.host.android_space_label.setText("Free space: -")
            self.host.status_label.setText("Connect an authorized ADB device to browse Android files.")
            return
        path = self.host.android_path
        use_root_requested = self.host._file_manager_root_requested()
        try:
            prepared = self.host.listing_controller.begin_android_listing(
                path,
                use_root=use_root_requested,
            )
        except (DeviceContextUnavailable, StaleFileListing) as exc:
            self.host.status_label.setText(self.host._friendly_error("Android files", str(exc)))
            return
        operation = self.host._capture_device_operation(
            "file-manager.listing",
            "file-manager.listing",
            expected_context=prepared.request.device_context,
        )
        if operation is None:
            return
        context, _adb, token = operation
        if (
            self.host._android_view_context != context
            or self.host._normalize_android_path(self.host._android_view_path)
            != self.host._normalize_android_path(path)
        ):
            self.host._clear_android_listing()
        self.host._android_refresh_token = token
        self.host._android_listing_request = prepared.request
        self.host._android_loading = True
        self.host.android_panel.set_path(self.host.android_path)
        self.host.android_path_edit.setText(self.host.android_path)
        self.host.android_space_label.setText("Free space: checking...")
        self.host.status_label.setText(f"Loading Android files: {self.host.android_path}")
        worker = Worker(
            lambda: self.host._load_android_files(
                prepared,
                use_root_requested,
                token.cancel_event,
            )
        )
        worker.signals.result.connect(
            lambda result, current=token: self.host._android_items_loaded(current, result)
        )
        worker.signals.error.connect(
            lambda message, _trace, current=token: self.host._android_refresh_failed(current, message)
        )
        worker.signals.finished.connect(lambda current=token: self.host._android_refresh_finished(current))
        if not self.host._start_operation_worker(worker, token):
            self.host._android_refresh_finished(token)

    def android_refresh_finished(self, token: OperationToken) -> None:
        self.host.operations.finish(token)
        if token is not self.host._android_refresh_token:
            return
        self.host._android_refresh_token = None
        self.host._android_listing_request = None
        self.host._android_loading = False
        if self.host._android_refresh_pending:
            self.host._android_refresh_pending = False
            self.host.refresh_android()

    def load_android_files(
        self,
        prepared: PreparedAndroidListing,
        use_root_requested: bool,
        cancel_event=None,
    ) -> tuple[AndroidListingResult, bool]:
        use_root = self.host._root_available_for_worker(
            prepared.adb,
            use_root_requested,
            cancel_event,
        )
        resolved = PreparedAndroidListing(
            request=replace(prepared.request, use_root=use_root),
            adb=prepared.adb,
        )
        result = self.host.listing_controller.load_android_listing(
            resolved,
            cancel_event=cancel_event,
        )
        return result, use_root

    def android_items_loaded(
        self,
        token: OperationToken,
        result: tuple[AndroidListingResult, bool] | tuple[str, list, dict] | tuple[str, list, dict, bool],
    ) -> None:
        if token is not self.host._android_refresh_token or not self.host._operation_is_current(token):
            return
        if len(result) == 2 and isinstance(result[0], AndroidListingResult):
            listing_result = result[0]
            try:
                listing_result = self.host.listing_controller.accept_android_listing(listing_result)
            except (DeviceContextUnavailable, StaleFileListing):
                return
            path = listing_result.request.requested_path
            items = list(listing_result.items)
            storage = dict(listing_result.storage)
            use_root = bool(result[1])
        else:
            path, items, storage = result[:3]
            use_root = bool(result[3]) if len(result) > 3 else False
        if path == self.host.android_path:
            self.host._android_view_context = token.device_context
            self.host._android_view_path = path
            self.host.android_panel.set_items(items)
            storage_text = self.host._android_storage_text(storage)
            self.host.android_space_label.setText(storage_text)
            self.host._select_storage_combo_for_path(path)
            prefix = "Android root" if use_root else "Android"
            self.host.status_label.setText(f"{prefix}: {path} - {len(items)} item(s) - {storage_text}")

    def android_storage_text(self, storage: dict) -> str:
        free = storage.get("free_bytes")
        total = storage.get("total_bytes")
        used = storage.get("used_bytes")
        used_percent = storage.get("used_percent")
        if not isinstance(free, int) or free < 0:
            return "Free space: Unknown"
        parts = [f"Free space: {format_bytes(free)}"]
        if isinstance(total, int) and total >= 0:
            parts.append(f"Total: {format_bytes(total)}")
        if isinstance(used, int) and used >= 0:
            parts.append(f"Used: {format_bytes(used)}")
        if isinstance(used_percent, int) and used_percent >= 0:
            parts.append(f"{used_percent}% used")
        return " | ".join(parts)

    def navigate_android(self, path: str) -> None:
        normalized = self.host._normalize_android_path(path)
        if normalized != self.host._normalize_android_path(self.host.android_path):
            self.host._clear_android_listing()
        try:
            saved_path = self.host.file_manager_state.save_android_path(normalized)
        except StaleFileManagerProfile:
            self.host.listing_controller.invalidate_android()
            state = self.host.file_manager_state.reload()
            self.host.android_path = self.host.listing_controller.set_android_path(
                state.android_path
            )
            self.host.android_panel.set_path(self.host.android_path)
            self.host.android_path_edit.setText(self.host.android_path)
            self.host.status_label.setText(
                "Android navigation stopped because the active device profile changed."
            )
            return
        self.host.android_path = self.host.listing_controller.set_android_path(saved_path)
        self.host.android_path_edit.setText(self.host.android_path)
        self.host._select_storage_combo_for_path(self.host.android_path)
        self.host.refresh_android()

    def android_parent_path(self, path: str) -> str:
        normalized = self.host._normalize_android_path(path)
        clean = normalized.rstrip("/") or "/"
        return self.host._normalize_android_path(parent_android_path(clean))

    def normalize_android_path(self, path: str) -> str:
        return normalize_android_path_value(path)
