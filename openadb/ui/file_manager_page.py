from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QInputDialog,
    QLabel,
    QMessageBox,
    QWidget,
)

from openadb.core.acbridge_p2p import (
    ADB_TRANSPORT,
    P2P_TRANSPORT,
)
from openadb.core.adb import ADBClient
from openadb.core.adb_transfer_strategy import ADBTransferStrategy
from openadb.core.device import DeviceManager
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.file_listing_controller import (
    AndroidListingResult,
    FileListingController,
    PreparedAndroidListing,
    StorageVolumesResult,
)
from openadb.core.file_manager_controller import (
    FileManagerActionCoordinator,
    WindowsNavigationHistory,
)
from openadb.core.file_manager_errors import map_file_manager_error
from openadb.core.file_manager_state import FileManagerState
from openadb.core.file_transfer_controller import FileTransferController
from openadb.core.operations import OperationConflictError, OperationToken
from openadb.core.path_utils import is_probably_writable_android_path
from openadb.core.p2p_parallelism import (
    AUTO_PARALLELISM_MODE,
    P2PParallelismPreference,
    migrate_p2p_parallelism_setting,
)
from openadb.core.p2p_transfer_strategy import P2PTransferStrategy
from openadb.core.settings_manager import SettingsManager
from openadb.core.transfer_plan import (
    ADB_TRANSFER,
    AUTO_PARALLELISM,
    FIXED_PARALLELISM,
    PULL_DIRECTION,
    PUSH_DIRECTION,
    TransferPlan,
    TransferPlanError,
)
from openadb.ui.file_manager_view import build_file_manager_view
from openadb.ui.file_manager_actions import FileManagerActions
from openadb.ui.file_manager_listings import FileManagerListings
from openadb.ui.widgets.native_explorer_panel import NativeExplorerPanel
from openadb.ui.widgets.progress_dialog import TransferProgressDialog
from openadb.ui.widgets.windows_file_panel import WindowsFilePanel
from openadb.ui.workers import Worker, start_worker

__all__ = ["FileManagerPage", "QDesktopServices", "QGuiApplication", "QInputDialog"]


P2P_SECURITY_ACKNOWLEDGED_KEY = "file_manager_p2p_security_acknowledged"


class FileManagerPage(ADBTransferStrategy, P2PTransferStrategy, QWidget):
    def __init__(self, adb: ADBClient, device_manager: DeviceManager, settings: SettingsManager, parent=None) -> None:
        super().__init__(parent)
        self.adb = adb
        self.device_manager = device_manager
        self.settings = settings
        self.operations = device_manager.operations
        self.transfer_controller = FileTransferController(self)
        self.action_controller = FileManagerActionCoordinator(
            adb,
            device_manager,
            settings,
        )
        self.file_actions = FileManagerActions(self, self.action_controller)
        self.file_manager_state = FileManagerState(settings)
        self.listing_controller = FileListingController(
            adb,
            device_manager,
            android_path=self.file_manager_state.android_path,
        )
        self.file_listings = FileManagerListings(self)
        self.pool = QThreadPool.globalInstance()
        self.android_path = self.file_manager_state.android_path
        saved_windows_path = self.file_manager_state.windows_path
        saved_windows = Path(saved_windows_path).expanduser() if saved_windows_path else Path.home()
        self.windows_path = str(saved_windows if saved_windows.exists() and saved_windows.is_dir() else Path.home())
        self._active_side = "android"
        self.windows_history = WindowsNavigationHistory()
        self._syncing_windows_history = False
        self._android_loading = False
        self._android_refresh_pending = False
        self._android_refresh_token: OperationToken | None = None
        self._android_listing_request = None
        self._android_view_context: DeviceContext | None = None
        self._android_view_path = ""
        self._android_storage_loading = False
        self._android_storage_refresh_pending = False
        self._android_storage_token: OperationToken | None = None
        self._android_storage_request = None
        self._android_storage_context: DeviceContext | None = None
        self._syncing_android_storage_combo = False
        self._android_storage_volumes: list = []
        self._transfer_dialogs: list[TransferProgressDialog] = []
        self._transfer_cancel_events: set[threading.Event] = set()
        self._transfer_running = False
        self._transfer_token: OperationToken | None = None
        self._transfer_plan: TransferPlan | None = None
        self._stale_transfer_notifications: set[str] = set()
        self._stale_transfer_dialogs: dict[str, TransferProgressDialog] = {}
        self._root_check_running = False
        self._root_check_token: OperationToken | None = None
        self._root_status = "not checked"
        self._accepted_transfer_transport = ADB_TRANSPORT
        self._p2p_security_session_acknowledged: set[tuple[str, str, str]] = set()
        self._p2p_security_prompt_nonce = 0
        self._pending_p2p_security_prompt: tuple[tuple[str, str, str], int] | None = None
        self._p2p_security_dialog_active = False
        self._p2p_security_prompt_timer = QTimer(self)
        self._p2p_security_prompt_timer.setSingleShot(True)
        self._p2p_security_prompt_timer.timeout.connect(
            self._run_pending_p2p_security_prompt
        )

        build_file_manager_view(self)

    def _capture_device_operation(
        self,
        owner_key: str,
        conflict_group: str,
        *,
        cancel_event: threading.Event | None = None,
        exclusive: bool = False,
        expected_context: DeviceContext | None = None,
    ) -> tuple[DeviceContext, ADBClient, OperationToken] | None:
        """Atomically register an operation against one immutable ADB target."""

        try:
            context = expected_context or self.device_manager.require_context(
                {"ADB", "Recovery"}
            )
            if context.mode not in {"ADB", "Recovery"}:
                raise DeviceContextUnavailable(
                    f"Current device mode is {context.mode}; expected ADB or Recovery"
                )
            if not self.device_manager.is_context_current(context):
                raise DeviceContextUnavailable(
                    "The active device changed while the operation was being confirmed."
                )
            token = self.operations.register(
                owner_key,
                device_context=context,
                conflict_group=conflict_group,
                conflict_groups=(f"device-exclusive:{context.serial}",) if exclusive else (),
                cancel_event=cancel_event,
            )
        except (DeviceContextUnavailable, OperationConflictError, RuntimeError) as exc:
            self.status_label.setText(str(exc))
            return None

        if not self.device_manager.is_context_current(context):
            token.cancel("device context changed before the operation started")
            self.operations.finish(token)
            self.status_label.setText("The active Android device changed before the operation could start.")
            return None
        try:
            bound_adb = self.adb.for_context(context)
        except (RuntimeError, ValueError) as exc:
            token.cancel("could not bind ADB to the captured device")
            self.operations.finish(token)
            self.status_label.setText(f"Could not bind ADB to the selected device: {exc}")
            return None
        return context, bound_adb, token

    def _capture_android_action_context(
        self,
        action: str,
        *,
        require_current_view: bool = False,
    ) -> DeviceContext | None:
        if require_current_view and not self._require_current_android_view(action):
            return None
        try:
            context = self.device_manager.require_context({"ADB", "Recovery"})
        except DeviceContextUnavailable as exc:
            self.status_label.setText(str(exc))
            return None
        if require_current_view and self._android_view_context != context:
            self._clear_android_listing()
            self.status_label.setText(
                f"{action}: the Android folder view belongs to another device. Refresh it and try again."
            )
            return None
        return context

    def _require_operation_preflight(self, token: OperationToken) -> None:
        context = token.device_context
        if token.cancelled:
            raise DeviceContextUnavailable(
                token.cancellation_reason or "The operation was cancelled before it started."
            )
        if context is None or not self.device_manager.is_context_current(context):
            token.cancel("device context changed before the worker started")
            raise DeviceContextUnavailable(
                "The active device changed before the operation could start."
            )

    def _operation_is_current(self, token: OperationToken, *, allow_cancelled: bool = False) -> bool:
        if getattr(self, "_workers_shutting_down", False):
            return False
        if not self.operations.contains(token):
            return False
        if token.device_context is None or not self.device_manager.is_context_current(token.device_context):
            return False
        return allow_cancelled or not token.cancelled

    def _android_view_is_current(self) -> bool:
        context = self._android_view_context
        return bool(
            context is not None
            and self.device_manager.is_context_current(context)
            and self._normalize_android_path(self._android_view_path)
            == self._normalize_android_path(self.android_path)
        )

    def _clear_android_listing(self) -> None:
        self._android_view_context = None
        self._android_view_path = ""
        self.android_panel.set_items([])
        self.android_space_label.setText("Free space: -")

    def invalidate_stale_device_view(self) -> None:
        """Remove rows and volumes that no longer belong to the active context."""

        invalidate_requests = False
        if self._android_view_context is not None and not self._android_view_is_current():
            invalidate_requests = True
            self._clear_android_listing()
        storage_context = self._android_storage_context
        if storage_context is not None and not self.device_manager.is_context_current(storage_context):
            invalidate_requests = True
            self._android_storage_context = None
            self._set_android_storage_combo([])
        listing_request = self._android_listing_request
        if (
            listing_request is not None
            and not self.device_manager.is_context_current(listing_request.device_context)
        ):
            invalidate_requests = True
        storage_request = self._android_storage_request
        if (
            storage_request is not None
            and not self.device_manager.is_context_current(storage_request.device_context)
        ):
            invalidate_requests = True
        if invalidate_requests:
            self.listing_controller.invalidate_android()

    def _require_current_android_view(self, action: str) -> bool:
        if self._android_view_is_current():
            return True
        self._clear_android_listing()
        message = (
            f"{action}: the Android folder view is no longer current. "
            "Wait for the active device and folder to finish refreshing."
        )
        self.status_label.setText(message)
        QMessageBox.warning(self, action, message)
        return False

    def _start_operation_worker(self, worker: Worker, token: OperationToken) -> bool:
        return start_worker(
            self,
            self.pool,
            worker,
            operation_registry=self.operations,
            operation_token=token,
        )

    def _start_local_worker(self, worker: Worker) -> bool:
        """Start a Windows-only action through the historical page seam."""

        return start_worker(self, self.pool, worker)

    def reload_from_settings(self) -> None:
        state = self.file_manager_state.reload()
        self.root_boost_button.blockSignals(True)
        self.root_boost_button.setChecked(bool(self.settings.get("file_manager_root_transfer", False)))
        self.root_boost_button.blockSignals(False)
        root_state = "not checked" if self.device_manager.active.mode in {"ADB", "Recovery"} else "unavailable"
        self._set_root_status(root_state)
        self.android_path = self.listing_controller.set_android_path(state.android_path)
        self.android_panel.set_path(self.android_path)
        self.android_path_edit.setText(self.android_path)
        self._restore_transfer_transport()
        self._restore_p2p_parallelism()

    def _action_group_title(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("fileManagerActionGroupTitle")
        return label

    def _center_separator(self) -> QFrame:
        separator = QFrame()
        separator.setObjectName("fileManagerCenterSeparator")
        separator.setFrameShape(QFrame.HLine)
        separator.setFixedHeight(1)
        return separator

    def _restore_splitter_state(self) -> None:
        self.file_splitter.setSizes(list(self.file_manager_state.splitter_sizes))

    def _save_splitter_state(self) -> None:
        sizes = self.file_splitter.sizes()
        if len(sizes) == 3 and all(size > 0 for size in sizes):
            self.file_manager_state.save_splitter_sizes(sizes)

    def save_ui_state(self) -> None:
        self._splitter_save_timer.stop()
        self._save_splitter_state()

    def restore_ui_state(self) -> None:
        self._restore_splitter_state()

    def _root_transfer_toggled(self, checked: bool) -> None:
        if not checked:
            self.settings.set("file_manager_root_transfer", False)
            self._set_root_status("not checked")
            return
        if self.device_manager.active.mode not in {"ADB", "Recovery"}:
            self.root_boost_button.blockSignals(True)
            self.root_boost_button.setChecked(False)
            self.root_boost_button.blockSignals(False)
            self.settings.set("file_manager_root_transfer", False)
            self._set_root_status("unavailable")
            self.status_label.setText("Root unavailable: connect an authorized ADB or Recovery device first.")
            return
        self.settings.set("file_manager_root_transfer", True)
        self._check_root_availability()

    def _check_root_availability(self) -> None:
        if self._root_check_running:
            return
        operation = self._capture_device_operation("file-manager.root-check", "file-manager.root-check")
        if operation is None:
            self.root_boost_button.blockSignals(True)
            self.root_boost_button.setChecked(False)
            self.root_boost_button.blockSignals(False)
            self.settings.set("file_manager_root_transfer", False)
            self._set_root_status("unavailable")
            return
        _context, adb, token = operation
        self._root_check_token = token
        self._root_check_running = True
        self.root_boost_button.setEnabled(False)
        self.pull_button.setEnabled(False)
        self.push_button.setEnabled(False)
        self._set_root_status("checking")
        worker = Worker(
            lambda: adb.root_available(cancel_event=token.cancel_event)
        )
        worker.signals.result.connect(lambda granted, current=token: self._root_check_result(current, granted))
        worker.signals.error.connect(
            lambda message, _trace, current=token: self._root_check_failed(current, message)
        )
        worker.signals.finished.connect(lambda current=token: self._root_check_finished(current))
        if not self._start_operation_worker(worker, token):
            self._root_check_finished(token)

    def _root_check_result(self, token: OperationToken, granted: bool) -> None:
        if token is not self._root_check_token or not self._operation_is_current(token):
            return
        state = "granted" if granted else "denied"
        self._set_root_status(state)
        if granted:
            self.status_label.setText("Root granted by the device for File Manager transfers.")
        else:
            self.status_label.setText("Root denied or unavailable; transfers will use normal ADB.")

    def _root_check_failed(self, token: OperationToken, message: str) -> None:
        if token is not self._root_check_token or not self._operation_is_current(token):
            return
        self._set_root_status("denied")
        self.status_label.setText(self._friendly_error("Root check", message))

    def _root_check_finished(self, token: OperationToken) -> None:
        self.operations.finish(token)
        if token is not self._root_check_token:
            return
        self._root_check_token = None
        self._root_check_running = False
        self.pull_button.setEnabled(not self._transfer_running)
        self.push_button.setEnabled(not self._transfer_running)
        self._update_transfer_transport_ui()

    def _set_root_status(self, state: str) -> None:
        normalized = state if state in {"unavailable", "not checked", "checking", "granted", "denied"} else "not checked"
        self._root_status = normalized
        self.root_status_label.setText(f"Root: {normalized}")
        descriptions = {
            "unavailable": "No authorized ADB/Recovery device is available for a root check.",
            "not checked": "Root has not been checked. Enabling the transfer option performs a safe availability check.",
            "checking": "Checking whether the connected device grants su/root access.",
            "granted": "The connected device granted root access for transfers.",
            "denied": "Root was denied or unavailable. Transfers fall back to normal ADB.",
        }
        self.root_status_label.setToolTip(descriptions[normalized])
        self.root_status_label.setProperty("rootState", normalized.replace(" ", "-"))
        self.root_status_label.style().unpolish(self.root_status_label)
        self.root_status_label.style().polish(self.root_status_label)

    def _create_windows_panel(self) -> QWidget:
        try:
            return NativeExplorerPanel(self.windows_path)
        except Exception:
            return WindowsFilePanel(self.windows_path, show_path_bar=False, show_button_row=False)

    def _set_active_side(self, side: str) -> None:
        self._active_side = "windows" if side == "windows" else "android"

    def refresh_all(self) -> None:
        self.invalidate_stale_device_view()
        if self.device_manager.active.mode not in {"ADB", "Recovery"}:
            self._set_root_status("unavailable")
        elif not self.root_boost_button.isChecked():
            self._set_root_status("not checked")
        self.refresh_windows()
        self.refresh_android_storage_roots()
        self.refresh_android()

    def refresh_android_storage_roots(self) -> None:
        self.file_listings.refresh_android_storage_roots()

    def _android_storage_refresh_finished(self, token: OperationToken) -> None:
        self.file_listings.android_storage_refresh_finished(token)

    def _android_storage_roots_loaded(
        self,
        token: OperationToken,
        result: StorageVolumesResult | list,
    ) -> None:
        self.file_listings.android_storage_roots_loaded(token, result)

    def _android_storage_roots_failed(self, token: OperationToken, message: str) -> None:
        self.file_listings.android_storage_roots_failed(token, message)

    def _set_android_storage_combo(self, volumes: list) -> None:
        self.file_listings.set_android_storage_combo(volumes)

    def _android_storage_volume_label(self, volume) -> str:
        return self.file_listings.android_storage_volume_label(volume)

    def _android_storage_selected(self, index: int) -> None:
        self.file_listings.android_storage_selected(index)

    def _select_storage_combo_for_path(self, path: str) -> None:
        self.file_listings.select_storage_combo_for_path(path)

    def refresh_android(self) -> None:
        self.file_listings.refresh_android()

    def _android_refresh_finished(self, token: OperationToken) -> None:
        self.file_listings.android_refresh_finished(token)

    def _android_refresh_failed(self, token: OperationToken, message: str) -> None:
        if token is not self._android_refresh_token or not self._operation_is_current(token):
            return
        request = self._android_listing_request
        if request is not None and not self.listing_controller.is_listing_current(request):
            return
        friendly = self._friendly_error("Android files", message)
        self.status_label.setText(friendly)
        QMessageBox.warning(self, "Android files", friendly)

    def _load_android_files(
        self,
        prepared: PreparedAndroidListing,
        use_root_requested: bool,
        cancel_event=None,
    ) -> tuple[AndroidListingResult, bool]:
        return self.file_listings.load_android_files(
            prepared,
            use_root_requested,
            cancel_event,
        )

    def _android_items_loaded(
        self,
        token: OperationToken,
        result: tuple[AndroidListingResult, bool] | tuple[str, list, dict] | tuple[str, list, dict, bool],
    ) -> None:
        self.file_listings.android_items_loaded(token, result)

    def _android_storage_text(self, storage: dict) -> str:
        return self.file_listings.android_storage_text(storage)

    def navigate_android(self, path: str) -> None:
        self.file_listings.navigate_android(path)

    def _android_parent_path(self, path: str) -> str:
        return self.file_listings.android_parent_path(path)

    def _normalize_android_path(self, path: str) -> str:
        return self.file_listings.normalize_android_path(path)

    def refresh_windows(self) -> None:
        if hasattr(self.windows_panel, "refresh"):
            self.windows_panel.refresh()
        else:
            self.windows_panel.set_path(self.windows_path)
        self.windows_path_edit.setText(self.windows_path)

    def navigate_windows(self, path: str, record_history: bool = True) -> None:
        if not path:
            return
        try:
            resolved = self.listing_controller.navigate_windows(path)
            self.windows_path = resolved
            self.file_manager_state.save_windows_path(resolved)
            self.windows_path_edit.setText(resolved)
            self.windows_panel.set_path(resolved)
            if record_history and not self._syncing_windows_history:
                self._push_windows_history(resolved)
            self._sync_windows_history_buttons()
            self.status_label.setText(f"Windows: {resolved}")
        except (OSError, ValueError):
            QMessageBox.warning(self, "Windows path", f"Folder does not exist:\n{path}")

    def _windows_path_changed(self, path: str) -> None:
        if path:
            if os.path.normcase(path) != os.path.normcase(self.windows_path):
                self.windows_path = path
                self.file_manager_state.save_windows_path(path)
                self.windows_path_edit.setText(path)
                if not self._syncing_windows_history:
                    self._push_windows_history(path)
            self._sync_windows_history_buttons()

    def _push_windows_history(self, path: str) -> None:
        self.windows_history.push(path)

    def _sync_windows_history_buttons(self) -> None:
        snapshot = self.windows_history.snapshot
        self.windows_back_button.setEnabled(snapshot.can_go_back)
        self.windows_forward_button.setEnabled(snapshot.can_go_forward)

    def windows_back(self) -> None:
        path = self.windows_history.back()
        if path is None:
            return
        self._syncing_windows_history = True
        try:
            self.navigate_windows(path, record_history=False)
        finally:
            self._syncing_windows_history = False
        self._sync_windows_history_buttons()

    def windows_forward(self) -> None:
        path = self.windows_history.forward()
        if path is None:
            return
        self._syncing_windows_history = True
        try:
            self.navigate_windows(path, record_history=False)
        finally:
            self._syncing_windows_history = False
        self._sync_windows_history_buttons()

    def new_folder(self, kind: str) -> None:
        self.file_actions.new_folder(kind)

    def delete_selected(self, kind: str) -> None:
        self.file_actions.delete_selected(kind)

    def rename_selected(self, kind: str) -> None:
        self.file_actions.rename_selected(kind)

    def pull_selected(self) -> None:
        self.pull_paths(self.android_panel.selected_paths())

    def pull_paths(self, android_paths: list[str]) -> None:
        if not android_paths:
            return
        if not self._can_start_transfer():
            return
        if not self._ensure_android_available("Android → PC"):
            return
        expected_context = self._capture_android_action_context(
            "Android → PC",
            require_current_view=True,
        )
        if expected_context is None:
            return
        destination = Path(self.windows_path)
        cancel_event = threading.Event()
        operation = self._capture_device_operation(
            "file-manager.pull",
            "file-manager.transfer",
            cancel_event=cancel_event,
            exclusive=True,
            expected_context=expected_context,
        )
        if operation is None:
            return
        context, adb, token = operation
        android_sources = tuple(str(path) for path in android_paths)
        try:
            plan = TransferPlan(
                direction=PULL_DIRECTION,
                transport=ADB_TRANSFER,
                sources=android_sources,
                destination=str(destination),
                device_context=context,
                use_root=self._file_manager_root_requested(),
            )
        except TransferPlanError as exc:
            self.operations.finish(token)
            self.status_label.setText(f"Android → PC: {exc}")
            return
        self._transfer_token = token
        self._transfer_plan = plan
        self._transfer_cancel_events.add(cancel_event)
        dialog = self._create_transfer_dialog("Android → PC")
        if self._selected_transfer_transport() == P2P_TRANSPORT:
            self.status_label.setText(
                "P2P via ACBridge is selected for uploads. Android → PC uses Platform Tools in this version."
            )
        dialog.cancel_requested.connect(lambda: self._cancel_transfer(dialog, token))

        def run(item_callback=None) -> dict:
            self._require_operation_preflight(token)
            return self.transfer_controller.execute(
                plan,
                adb=adb,
                cancel_event=cancel_event,
                item_callback=item_callback,
            )

        worker = Worker(run)
        worker.signals.item.connect(
            lambda update, current=token: self._transfer_progress(current, dialog, update)
        )
        worker.signals.result.connect(
            lambda result, current=token: self._transfer_done(
                current, dialog, result, self.refresh_windows
            )
        )
        worker.signals.error.connect(
            lambda message, _trace, current=token: self._transfer_failed(
                current, dialog, "Android → PC", message
            )
        )
        worker.signals.finished.connect(
            lambda current=token: self._transfer_worker_finished(current, dialog)
        )
        self._set_transfer_running(True)
        if self._start_operation_worker(worker, token):
            dialog.show()
        else:
            self._transfer_worker_finished(token, dialog)

    def push_selected(self) -> None:
        self.push_paths(self.windows_panel.selected_paths())

    def push_paths(self, local_paths: list[str]) -> None:
        if not local_paths:
            return
        if not self._can_start_transfer():
            return
        if not self._ensure_android_available("PC → Android"):
            return
        expected_context = self._capture_android_action_context(
            "PC → Android",
            require_current_view=True,
        )
        if expected_context is None:
            return
        if self._offer_install_single_apk(
            local_paths,
            expected_context=expected_context,
        ):
            return
        if not self._ensure_p2p_security_consent():
            return
        if not self._warn_android_write(self.android_path):
            return
        android_destination = str(self.android_path)
        cancel_event = threading.Event()
        operation = self._capture_device_operation(
            "file-manager.push",
            "file-manager.transfer",
            cancel_event=cancel_event,
            exclusive=True,
            expected_context=expected_context,
        )
        if operation is None:
            return
        context, adb, token = operation
        local_sources = tuple(str(path) for path in local_paths)
        use_root = self._file_manager_root_requested()
        transport = self._selected_transfer_transport()
        if transport == P2P_TRANSPORT:
            parallelism_mode = self._selected_p2p_parallelism_mode()
            requested_parallelism = self._selected_p2p_parallelism()
        else:
            parallelism_mode = FIXED_PARALLELISM
            requested_parallelism = 1
        try:
            plan = TransferPlan(
                direction=PUSH_DIRECTION,
                transport=transport,
                sources=local_sources,
                destination=android_destination,
                device_context=context,
                use_root=use_root,
                parallelism_mode=parallelism_mode,
                requested_parallelism=requested_parallelism,
            )
        except TransferPlanError as exc:
            self.operations.finish(token)
            self.status_label.setText(f"PC → Android: {exc}")
            return
        self._transfer_token = token
        self._transfer_plan = plan
        self._transfer_cancel_events.add(cancel_event)
        dialog = self._create_transfer_dialog("PC → Android")
        dialog.cancel_requested.connect(lambda: self._cancel_transfer(dialog, token))

        def run(item_callback=None) -> dict:
            self._require_operation_preflight(token)
            return self.transfer_controller.execute(
                plan,
                adb=adb,
                cancel_event=cancel_event,
                item_callback=item_callback,
            )

        worker = Worker(run)
        worker.signals.item.connect(
            lambda update, current=token: self._transfer_progress(current, dialog, update)
        )
        worker.signals.result.connect(
            lambda result, current=token: self._transfer_done(
                current, dialog, result, self.refresh_android
            )
        )
        worker.signals.error.connect(
            lambda message, _trace, current=token: self._transfer_failed(
                current, dialog, "PC → Android", message
            )
        )
        worker.signals.finished.connect(
            lambda current=token: self._transfer_worker_finished(current, dialog)
        )
        self._set_transfer_running(True)
        if self._start_operation_worker(worker, token):
            dialog.show()
        else:
            self._transfer_worker_finished(token, dialog)

    def _offer_install_single_apk(
        self,
        local_paths: list[str],
        *,
        expected_context: DeviceContext | None = None,
    ) -> bool:
        return self.file_actions.offer_install_single_apk(
            local_paths,
            expected_context=expected_context,
        )

    def _single_local_apk_path(self, local_paths: list[str]) -> Path | None:
        return self.file_actions.single_local_apk_path(local_paths)

    def _install_local_apk(
        self,
        apk_path: Path,
        *,
        expected_context: DeviceContext | None = None,
    ) -> None:
        self.file_actions.install_local_apk(
            apk_path,
            expected_context=expected_context,
        )


    def copy_path(self, kind: str) -> None:
        self.file_actions.copy_path(kind)

    def properties(self, kind: str) -> None:
        self.file_actions.properties(kind)


    def open_explorer(self) -> None:
        self.file_actions.open_explorer()

    def _warn_android_write(self, path: str) -> bool:
        if is_probably_writable_android_path(path):
            return True
        answer = QMessageBox.warning(
            self,
            "Android path warning",
            (
                "This Android path may be protected or read-only. Root access must be explicitly granted by the "
                "device and is not guaranteed even when Use root for transfers is enabled. Continue?"
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        return answer == QMessageBox.Ok

    def _ensure_android_available(self, action: str) -> bool:
        if self.device_manager.active.mode in {"ADB", "Recovery"}:
            return True
        message = f"{action}: the Android device disconnected or is not available for ADB file operations."
        self.status_label.setText(message)
        QMessageBox.warning(self, action, message)
        return False

    def _file_manager_root_requested(self) -> bool:
        return bool(self.root_boost_button.isChecked())

    def _selected_transfer_transport(self) -> str:
        value = str(self.transfer_transport_combo.currentData() or ADB_TRANSPORT)
        return P2P_TRANSPORT if value == P2P_TRANSPORT else ADB_TRANSPORT

    def _restore_transfer_transport(self) -> None:
        self._p2p_security_prompt_nonce += 1
        self._p2p_security_prompt_timer.stop()
        self._pending_p2p_security_prompt = None
        value = str(self.settings.get("file_manager_transfer_transport", ADB_TRANSPORT) or ADB_TRANSPORT)
        restored = P2P_TRANSPORT if value == P2P_TRANSPORT else ADB_TRANSPORT
        index = self.transfer_transport_combo.findData(restored)
        self.transfer_transport_combo.blockSignals(True)
        self.transfer_transport_combo.setCurrentIndex(max(0, index))
        self.transfer_transport_combo.blockSignals(False)
        identity = self._settings_profile_identity()
        if restored == P2P_TRANSPORT and self._p2p_security_warning_required(identity):
            # An unacknowledged legacy P2P preference is visible while its
            # consent dialog is pending, but Cancel always has a safe ADB
            # transport to return to.
            self._accepted_transfer_transport = ADB_TRANSPORT
            nonce = self._p2p_security_prompt_nonce
            self._pending_p2p_security_prompt = identity, nonce
            self._p2p_security_prompt_timer.start(0)
        else:
            self._accepted_transfer_transport = restored
        self._update_transfer_transport_ui()

    def _transfer_transport_changed(self, _index: int) -> None:
        self._p2p_security_prompt_nonce += 1
        self._p2p_security_prompt_timer.stop()
        self._pending_p2p_security_prompt = None
        selected = self._selected_transfer_transport()
        previous = self._accepted_transfer_transport
        identity = self._settings_profile_identity()
        if selected == P2P_TRANSPORT and self._p2p_security_warning_required(identity):
            self._confirm_p2p_selection(previous, identity)
            return
        self.settings.set("file_manager_transfer_transport", selected)
        self._accepted_transfer_transport = selected
        self._update_transfer_transport_ui()

    def _ensure_p2p_security_consent(self) -> bool:
        if self._selected_transfer_transport() != P2P_TRANSPORT:
            return True
        identity = self._settings_profile_identity()
        if not self._p2p_security_warning_required(identity):
            return True
        self._p2p_security_prompt_nonce += 1
        self._p2p_security_prompt_timer.stop()
        self._pending_p2p_security_prompt = None
        return self._confirm_p2p_selection(
            self._accepted_transfer_transport,
            identity,
        )

    def _run_pending_p2p_security_prompt(self) -> None:
        pending = self._pending_p2p_security_prompt
        self._pending_p2p_security_prompt = None
        if pending is None or self._p2p_security_dialog_active:
            return
        self._confirm_restored_p2p(*pending)

    def _confirm_restored_p2p(
        self,
        expected_identity: tuple[str, str, str],
        expected_nonce: int,
    ) -> None:
        if expected_nonce != self._p2p_security_prompt_nonce:
            return
        if self._settings_profile_identity() != expected_identity:
            self._restore_transfer_transport()
            return
        if self._selected_transfer_transport() != P2P_TRANSPORT:
            return
        if not self._p2p_security_warning_required(expected_identity):
            self._accepted_transfer_transport = P2P_TRANSPORT
            return
        self._confirm_p2p_selection(ADB_TRANSPORT, expected_identity)

    def _confirm_p2p_selection(
        self,
        previous_transport: str,
        expected_identity: tuple[str, str, str],
    ) -> bool:
        if self._p2p_security_dialog_active:
            return False
        self._p2p_security_dialog_active = True
        try:
            accepted, do_not_show_again = self._show_p2p_security_warning()
        finally:
            self._p2p_security_dialog_active = False
        if self._settings_profile_identity() != expected_identity:
            # QMessageBox.exec() runs a nested event loop. A device refresh can
            # activate another profile while it is open, so the old answer must
            # never be written into that new profile.
            self._restore_transfer_transport()
            return False
        if not accepted:
            fallback = P2P_TRANSPORT if previous_transport == P2P_TRANSPORT else ADB_TRANSPORT
            self.transfer_transport_combo.blockSignals(True)
            self.transfer_transport_combo.setCurrentIndex(
                max(0, self.transfer_transport_combo.findData(fallback))
            )
            self.transfer_transport_combo.blockSignals(False)
            self.settings.set("file_manager_transfer_transport", fallback)
            self._accepted_transfer_transport = fallback
            self._update_transfer_transport_ui()
            return False

        self._p2p_security_session_acknowledged.add(expected_identity)
        if do_not_show_again:
            self.settings.set(P2P_SECURITY_ACKNOWLEDGED_KEY, True, save=False)
        self.settings.set("file_manager_transfer_transport", P2P_TRANSPORT)
        self._accepted_transfer_transport = P2P_TRANSPORT
        self._update_transfer_transport_ui()
        return True

    def _show_p2p_security_warning(self) -> tuple[bool, bool]:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("P2P transfer security")
        dialog.setAccessibleName("P2P transfer security")
        dialog.setAccessibleDescription(
            "Security warning for authenticated but unencrypted ACBridge P2P transfers"
        )
        dialog.setIcon(QMessageBox.Warning)
        dialog.setText("ACBridge P2P is authenticated and verifies file integrity, but data is not encrypted.")
        dialog.setInformativeText(
            "Use P2P only on a trusted private network. Do not use public, shared, guest, or untrusted Wi-Fi.\n\n"
            "Firewall rules or client isolation can block the transfer. Platform Tools (ADB) remains the safe "
            "default transfer method."
        )
        do_not_show_again = QCheckBox("Do not show this warning again", dialog)
        do_not_show_again.setAccessibleName("Do not show this P2P security warning again")
        dialog.setCheckBox(do_not_show_again)
        dialog.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        continue_button = dialog.button(QMessageBox.Ok)
        if continue_button is not None:
            continue_button.setText("Use P2P")
        dialog.setDefaultButton(QMessageBox.Cancel)
        dialog.setEscapeButton(QMessageBox.Cancel)
        accepted = dialog.exec() == QMessageBox.Ok
        return accepted, bool(do_not_show_again.isChecked())

    def _settings_profile_identity(self) -> tuple[str, str, str]:
        profile_key = str(
            getattr(self.settings, "active_profile_key", "")
            or getattr(self.settings, "active_profile_serial", "")
            or ""
        )
        profile_kind = str(getattr(self.settings, "active_profile_kind", "") or "")
        raw_path = getattr(self.settings, "path", None) or getattr(self.settings, "config_dir", "") or ""
        try:
            profile_path = str(Path(raw_path).expanduser().resolve(strict=False)) if raw_path else ""
        except (OSError, RuntimeError):
            profile_path = str(raw_path)
        return profile_key, profile_kind, profile_path

    def _p2p_security_warning_required(self, identity: tuple[str, str, str]) -> bool:
        return (
            self.settings.get(P2P_SECURITY_ACKNOWLEDGED_KEY, False) is not True
            and identity not in self._p2p_security_session_acknowledged
        )

    def _selected_p2p_parallelism_preference(self) -> P2PParallelismPreference:
        return migrate_p2p_parallelism_setting(self.p2p_parallelism_combo.currentData())

    def _selected_p2p_parallelism(self) -> int | None:
        return self._selected_p2p_parallelism_preference().manual_value

    def _selected_p2p_parallelism_mode(self) -> str:
        preference = self._selected_p2p_parallelism_preference()
        return (
            AUTO_PARALLELISM
            if preference.mode == AUTO_PARALLELISM_MODE
            else FIXED_PARALLELISM
        )

    def _restore_p2p_parallelism(self) -> None:
        raw_value = self.settings.get("file_manager_p2p_parallelism", AUTO_PARALLELISM_MODE)
        preference = migrate_p2p_parallelism_setting(raw_value)
        value = preference.to_setting_value()
        index = self.p2p_parallelism_combo.findData(value)
        self.p2p_parallelism_combo.blockSignals(True)
        self.p2p_parallelism_combo.setCurrentIndex(max(0, index))
        self.p2p_parallelism_combo.blockSignals(False)
        if raw_value != value:
            self.settings.set("file_manager_p2p_parallelism", value)

    def _p2p_parallelism_changed(self, _index: int) -> None:
        preference = self._selected_p2p_parallelism_preference()
        self.settings.set("file_manager_p2p_parallelism", preference.to_setting_value())

    def _update_transfer_transport_ui(self) -> None:
        p2p = self._selected_transfer_transport() == P2P_TRANSPORT
        self.p2p_security_status_label.setVisible(p2p)
        self.p2p_parallelism_row.setVisible(p2p)
        self.p2p_parallelism_combo.setEnabled(p2p and not self._transfer_running)
        self.root_boost_button.setEnabled(not p2p and not self._transfer_running and not self._root_check_running)
        if p2p:
            self.root_status_label.setText("Root: not used by P2P")
            self.push_button.setToolTip(
                "Upload directly over the local network to ACBridge. Platform Tools creates the one-time session; "
                "Android SAF writes to the granted MicroSD/USB folder without root."
            )
        else:
            self.root_status_label.setText(f"Root: {self._root_status}")
            self.push_button.setToolTip("Copy selected Windows files to the current Android folder through Platform Tools")

    def _command_done(self, title: str, result, refresh) -> None:
        message = result.status or result.stderr or result.stdout or f"{title} finished."
        if result.success:
            QMessageBox.information(self, title, message)
        else:
            friendly = self._friendly_error(title, message)
            self.status_label.setText(friendly)
            QMessageBox.warning(self, title, friendly)
        refresh()

    def _device_command_done(self, token: OperationToken, title: str, result, refresh) -> None:
        if self._operation_is_current(token):
            self._command_done(title, result, refresh)

    def _operation_failed(self, title: str, message: str) -> None:
        friendly = self._friendly_error(title, message)
        self.status_label.setText(friendly)
        QMessageBox.warning(self, title, friendly)

    def _device_operation_failed(self, token: OperationToken, title: str, message: str) -> None:
        if self._operation_is_current(token):
            self._operation_failed(title, message)

    def _apk_install_done(self, token: OperationToken, apk_path: Path, result) -> None:
        """Compatibility callback retained for integrations built before Stage 4."""

        if not self._operation_is_current(token):
            return
        status = result.status or result.stderr or result.stdout or "Install command finished."
        if result.success:
            self.status_label.setText(f"Installed APK: {apk_path.name}")
            QMessageBox.information(self, "Install APK", status)
        else:
            self.status_label.setText(f"APK install failed: {apk_path.name}")
            QMessageBox.warning(self, "Install APK", status)

    def _android_properties_done(self, token: OperationToken, result) -> None:
        """Compatibility callback retained for integrations built before Stage 4."""

        if not self._operation_is_current(token):
            return
        message = result.stdout or result.stderr or result.status or "No properties were returned."
        if result.success:
            QMessageBox.information(self, "Properties", message)
        else:
            self._operation_failed("Properties", message)

    def _messages_done(self, title: str, messages: list[str], refresh) -> None:
        text = "\n".join(messages[:80])
        lowered = text.lower()
        if any(marker in lowered for marker in ["failed", "refused", "permission denied", "read-only", "still reports"]):
            QMessageBox.warning(self, title, text)
        else:
            QMessageBox.information(self, title, text)
        refresh()

    def _device_messages_done(
        self,
        token: OperationToken,
        title: str,
        messages: list[str],
        refresh,
    ) -> None:
        if self._operation_is_current(token):
            self._messages_done(title, messages, refresh)

    def _create_transfer_dialog(self, title: str) -> TransferProgressDialog:
        dialog = TransferProgressDialog(title, self)
        self._transfer_dialogs.append(dialog)
        dialog.finished.connect(lambda _code, dlg=dialog: self._forget_transfer_dialog(dlg))
        dialog.destroyed.connect(lambda _object=None, dlg=dialog: self._forget_transfer_dialog(dlg))
        return dialog

    def _forget_transfer_dialog(self, dialog: TransferProgressDialog) -> None:
        if dialog in self._transfer_dialogs:
            self._transfer_dialogs.remove(dialog)
        stale_operation_ids = tuple(
            operation_id
            for operation_id, stale_dialog in self._stale_transfer_dialogs.items()
            if stale_dialog is dialog
        )
        for operation_id in stale_operation_ids:
            self._stale_transfer_dialogs.pop(operation_id, None)
            self._stale_transfer_notifications.discard(operation_id)

    def _cancel_transfer(self, dialog: TransferProgressDialog, token: OperationToken) -> None:
        token.cancel("Transfer cancelled by user.")
        self.status_label.setText("Transfer cancellation requested. Waiting for the active ADB operation to stop.")
        dialog.apply_update({"type": "cancelled"})

    def cancel_active_transfers(self) -> None:
        """Cancel active transfer and local filesystem work before application exit."""
        self.file_actions.cancel_active()
        if self._transfer_token is not None:
            self._transfer_token.cancel("Application shutdown requested.")
        for cancel_event in tuple(self._transfer_cancel_events):
            cancel_event.set()
        for dialog in tuple(self._transfer_dialogs):
            if dialog.isVisible():
                dialog.apply_update({"type": "cancelled"})

    def _can_start_transfer(self) -> bool:
        if not self._transfer_running:
            return True
        self.status_label.setText("Another file transfer is already running. Wait for it to finish or cancel it.")
        return False

    def _set_transfer_running(self, running: bool) -> None:
        self._transfer_running = bool(running)
        self.pull_button.setEnabled(not running and not self._root_check_running)
        self.push_button.setEnabled(not running and not self._root_check_running)
        self.transfer_transport_combo.setEnabled(not running)
        self.p2p_parallelism_combo.setEnabled(not running and self._selected_transfer_transport() == P2P_TRANSPORT)
        self._update_transfer_transport_ui()

    def _transfer_progress(
        self,
        token: OperationToken,
        dialog: TransferProgressDialog,
        update: dict,
    ) -> None:
        if token.operation_id in self._stale_transfer_notifications:
            return
        if self._operation_is_current(token):
            dialog.apply_update(update)
        elif not self.device_manager.is_context_current(token.device_context):
            self._mark_stale_transfer(dialog, token)

    def _mark_stale_transfer(self, dialog: TransferProgressDialog, token: OperationToken) -> None:
        if token.operation_id in self._stale_transfer_notifications:
            return
        self._stale_transfer_notifications.add(token.operation_id)
        self._stale_transfer_dialogs[token.operation_id] = dialog
        reason = token.cancellation_reason or "The active device changed during the transfer."
        dialog.apply_update({"type": "done", "success": False, "message": reason})

    def _transfer_worker_finished(
        self,
        token: OperationToken,
        dialog: TransferProgressDialog,
    ) -> None:
        self.operations.finish(token)
        self._transfer_cancel_events.discard(token.cancel_event)
        if not self.device_manager.is_context_current(token.device_context):
            self._mark_stale_transfer(dialog, token)
        if token is not self._transfer_token:
            return
        self._transfer_token = None
        self._transfer_plan = None
        self._set_transfer_running(False)

    def _transfer_done(
        self,
        token: OperationToken,
        dialog: TransferProgressDialog,
        result: dict,
        refresh,
    ) -> None:
        if token.operation_id in self._stale_transfer_notifications:
            return
        if not self._operation_is_current(token, allow_cancelled=True):
            if self.device_manager.is_context_current(token.device_context):
                return
            self._mark_stale_transfer(dialog, token)
            return
        success = bool(result.get("success", False)) and not token.cancelled
        raw_message = str(result.get("summary", "Transfer finished."))
        if token.cancelled:
            raw_message = token.cancellation_reason or "Transfer cancelled by user."
        message = raw_message if success else self._friendly_error("Transfer", raw_message)
        dialog.apply_update(
            {
                "type": "done",
                "success": success,
                "message": message,
            }
        )
        self.status_label.setText("Transfer completed successfully." if success else message)
        refresh()

    def _transfer_failed(
        self,
        token: OperationToken,
        dialog: TransferProgressDialog,
        title: str,
        message: str,
    ) -> None:
        if token.operation_id in self._stale_transfer_notifications:
            return
        if not self._operation_is_current(token, allow_cancelled=True):
            if self.device_manager.is_context_current(token.device_context):
                return
            self._mark_stale_transfer(dialog, token)
            return
        if token.cancelled:
            message = token.cancellation_reason or message
        friendly = self._friendly_error(title, message)
        self.status_label.setText(friendly)
        dialog.apply_update({"type": "done", "success": False, "message": friendly})

    def _run_push_transfer(
        self,
        adb: ADBClient,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback,
        use_root_requested: bool,
        transport: str = ADB_TRANSPORT,
        p2p_parallelism: int | None = 1,
        temp_path: Path | None = None,
        p2p_parallelism_mode: str = FIXED_PARALLELISM,
    ) -> dict:
        """Compatibility seam delegating transport choice to the controller."""

        return self.transfer_controller.execute_push(
            adb=adb,
            local_paths=local_paths,
            android_destination=android_destination,
            cancel_event=cancel_event,
            item_callback=item_callback,
            use_root_requested=use_root_requested,
            transport=transport,
            p2p_parallelism_mode=p2p_parallelism_mode,
            p2p_parallelism=p2p_parallelism,
            temp_path=temp_path,
        )

    @staticmethod
    def _friendly_error(context: str, message: str) -> str:
        mapped = map_file_manager_error(message, operation=context)
        return f"{context}: {mapped.message}"
