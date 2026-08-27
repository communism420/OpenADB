from __future__ import annotations

import os
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThreadPool, QTimer, Signal
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
from openadb.core.p2p_parallelism import (
    AUTO_PARALLELISM_MODE,
    P2PParallelismPreference,
    migrate_p2p_parallelism_setting,
)
from openadb.core.p2p_transfer_strategy import P2PTransferStrategy
from openadb.core.path_utils import is_probably_writable_android_path
from openadb.core.privilege import PrivilegeBackend, PrivilegeStatus
from openadb.core.settings_manager import (
    SettingsManager,
    read_privilege_backend_setting,
)
from openadb.core.transfer_plan import (
    ADB_TRANSFER,
    AUTO_PARALLELISM,
    FIXED_PARALLELISM,
    PULL_DIRECTION,
    PUSH_DIRECTION,
    TransferPlan,
    TransferPlanError,
)
from openadb.ui.file_manager_actions import FileManagerActions
from openadb.ui.file_manager_listings import FileManagerListings
from openadb.ui.file_manager_view import build_file_manager_view
from openadb.ui.widgets.native_explorer_panel import NativeExplorerPanel
from openadb.ui.widgets.progress_dialog import TransferProgressDialog
from openadb.ui.widgets.windows_file_panel import WindowsFilePanel
from openadb.ui.workers import Worker, start_worker

__all__ = ["FileManagerPage", "QDesktopServices", "QGuiApplication", "QInputDialog"]


P2P_SECURITY_ACKNOWLEDGED_KEY = "file_manager_p2p_security_acknowledged"
PASSIVE_APPS_OPERATION_OWNERS = frozenset(
    {"apps.list", "apps.metadata", "apps.assets"}
)


@dataclass(frozen=True, slots=True)
class _PendingTransferStart:
    plan: TransferPlan
    title: str
    refresh: Callable[[], None]


class FileManagerPage(ADBTransferStrategy, P2PTransferStrategy, QWidget):
    passive_apps_preempted = Signal()
    transfer_started = Signal(str)
    transfer_progress_changed = Signal(str, object)
    transfer_finished = Signal(str)

    def __init__(
        self,
        adb: ADBClient,
        device_manager: DeviceManager,
        settings: SettingsManager,
        parent=None,
        *,
        privilege_manager=None,
    ) -> None:
        super().__init__(parent)
        self.adb = adb
        self.device_manager = device_manager
        self.settings = settings
        self.privilege_manager = privilege_manager
        self.operations = device_manager.operations
        self.transfer_controller = FileTransferController(self)
        self.action_controller = FileManagerActionCoordinator(
            adb,
            device_manager,
            settings,
            privilege_manager=privilege_manager,
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
        self._maintenance_refresh_context: DeviceContext | None = None
        self._maintenance_storage_refresh_pending = False
        self._maintenance_listing_refresh_pending = False
        self._maintenance_refresh_timer = QTimer(self)
        self._maintenance_refresh_timer.setSingleShot(True)
        self._maintenance_refresh_timer.setInterval(75)
        self._maintenance_refresh_timer.timeout.connect(
            self._retry_refresh_after_maintenance
        )
        self._transfer_dialogs: list[TransferProgressDialog] = []
        self._transfer_cancel_events: set[threading.Event] = set()
        self._transfer_running = False
        self._transfer_token: OperationToken | None = None
        self._transfer_plan: TransferPlan | None = None
        self._pending_transfer_start: _PendingTransferStart | None = None
        self._transfer_refresh_callbacks: dict[str, Callable[[], None]] = {}
        self._android_refresh_deferred_until_transfer = False
        self._stale_transfer_notifications: set[str] = set()
        self._stale_transfer_dialogs: dict[str, TransferProgressDialog] = {}
        self._dismissed_transfer_dialog_ids: deque[int] = deque(maxlen=256)
        self._root_check_running = False
        self._root_check_token: OperationToken | None = None
        self._privilege_refresh_pending = False
        self._root_status = "not checked"
        self._global_privilege_status: PrivilegeStatus | None = None
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

        context: DeviceContext | None = None
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
            capture_lease = getattr(
                self.privilege_manager,
                "capture_operation_lease",
                None,
            )
            privilege_lease = capture_lease() if callable(capture_lease) else None
            token = self.operations.register(
                owner_key,
                device_context=context,
                conflict_group=conflict_group,
                conflict_groups=(
                    f"acbridge-maintenance:{context.serial}",
                    *((f"device-exclusive:{context.serial}",) if exclusive else ()),
                ),
                cancel_event=cancel_event,
            )
            token.privilege_lease = privilege_lease
        except OperationConflictError as exc:
            maintenance_conflict = bool(
                context is not None
                and f"acbridge-maintenance:{context.serial}" in str(exc)
            )
            if (
                context is not None
                and self._defer_passive_refresh_after_maintenance(
                    owner_key,
                    context,
                    known_maintenance_conflict=maintenance_conflict,
                )
            ):
                return None
            self.status_label.setText(str(exc))
            return None
        except (DeviceContextUnavailable, RuntimeError) as exc:
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

    def _maintenance_blockers(
        self,
        context: DeviceContext,
    ) -> tuple[OperationToken, ...]:
        """Return operations that currently own this device's ACBridge gate."""

        maintenance_group = f"acbridge-maintenance:{context.serial}"
        return tuple(
            token
            for token in self.operations.active_tokens()
            if maintenance_group in token.conflict_groups
        )

    def _defer_passive_refresh_after_maintenance(
        self,
        owner_key: str,
        context: DeviceContext,
        *,
        known_maintenance_conflict: bool = False,
    ) -> bool:
        """Coalesce passive listings instead of exposing an expected conflict.

        Application labels/icons are background cache work, so a visible File
        Manager may cooperatively cancel that read. The registry token stays
        live until its worker really finishes; the retry timer therefore never
        overlaps two ACBridge clients. Mutations and maintenance operations are
        only waited for and are never cancelled here.
        """

        if owner_key not in {
            "file-manager.storage-volumes",
            "file-manager.listing",
        }:
            return False
        blockers = self._maintenance_blockers(context)
        if not blockers and not known_maintenance_conflict:
            return False

        if owner_key == "file-manager.storage-volumes":
            self._maintenance_storage_refresh_pending = True
        else:
            self._maintenance_listing_refresh_pending = True
        self._maintenance_refresh_context = context

        passive_apps_blocking = False
        for token in blockers:
            if token.owner_key not in PASSIVE_APPS_OPERATION_OWNERS:
                continue
            passive_apps_blocking = True
            token.cancel(
                "File Manager foreground refresh is waiting for passive application details to stop."
            )
        if passive_apps_blocking:
            self.passive_apps_preempted.emit()
            self.status_label.setText(
                "Preparing Android files. Stopping background application details first..."
            )
        elif blockers:
            blocker_names = ", ".join(
                sorted({token.owner_key for token in blockers})
            )
            self.status_label.setText(
                "Preparing Android files. Waiting for "
                f"{blocker_names} to finish..."
            )
        else:
            self.status_label.setText(
                "Preparing Android files. Waiting for the current device operation to finish..."
            )
        if not self._maintenance_refresh_timer.isActive():
            self._maintenance_refresh_timer.start()
        return True

    def _clear_maintenance_refresh_wait(self) -> None:
        self._maintenance_refresh_timer.stop()
        self._maintenance_refresh_context = None
        self._maintenance_storage_refresh_pending = False
        self._maintenance_listing_refresh_pending = False

    def _retry_refresh_after_maintenance(self) -> None:
        """Retry the latest passive refresh once the shared gate is released."""

        context = self._maintenance_refresh_context
        if context is None or getattr(self, "_workers_shutting_down", False):
            self._clear_maintenance_refresh_wait()
            return
        if not self.device_manager.is_context_current(context):
            self._clear_maintenance_refresh_wait()
            return

        window = self.window()
        stack = getattr(window, "stack", None)
        if stack is not None and stack.currentWidget() is not self:
            self._clear_maintenance_refresh_wait()
            return

        if self._maintenance_blockers(context):
            self._maintenance_refresh_timer.start()
            return

        refresh_storage = self._maintenance_storage_refresh_pending
        refresh_listing = self._maintenance_listing_refresh_pending
        self._clear_maintenance_refresh_wait()
        if refresh_storage:
            if refresh_listing:
                self._android_refresh_pending = True
            self.refresh_android_storage_roots()
        elif refresh_listing:
            self.refresh_android()

    def _active_passive_listing_tokens(
        self,
        device_context: DeviceContext,
    ) -> tuple[OperationToken, ...]:
        """Return page-owned reads that a confirmed operation may pre-empt."""

        return tuple(
            token
            for token in (
                self._android_refresh_token,
                self._android_storage_token,
            )
            if (
                token is not None
                and token.owner_key
                in {"file-manager.listing", "file-manager.storage-volumes"}
                and token.device_context == device_context
                and self.operations.contains(token)
            )
        )

    def _privileged_adb_for_worker(
        self,
        context: DeviceContext,
        direct_adb,
        cancel_event=None,
        privilege_lease=None,
    ):
        """Prepare the selected shell backend without changing binary transfers.

        The caller has already captured ``direct_adb`` for the immutable device
        context.  Shizuku preparation deliberately happens in the worker: its
        permission/status handshake must never block the Qt GUI thread.  Host
        ADB operations such as push, pull, install and P2P setup continue to use
        the original bound client.
        """

        manager = self.privilege_manager
        if manager is None:
            return direct_adb
        prepare_kwargs = {"cancel_event": cancel_event}
        if privilege_lease is not None:
            prepare_kwargs["privilege_lease"] = privilege_lease
        prepared = manager.prepare_adb(context, **prepare_kwargs)
        if getattr(prepared, "device_context", None) != context:
            raise DeviceContextUnavailable(
                "The privileged File Manager shell was bound to another device context."
            )
        return prepared

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

    def _capture_android_upload_context(self, action: str) -> DeviceContext | None:
        """Capture a safe upload target while its current view is refreshing.

        PC-to-Android uploads do not consume rows from the Android table.  A
        Shizuku/access refresh can therefore keep the immutable destination and
        wait behind the page-owned passive read, even though the last completed
        table snapshot has intentionally been cleared.  Other Android actions
        remain strict because their selected rows do depend on that snapshot.
        """

        try:
            context = self.device_manager.require_context({"ADB", "Recovery"})
        except DeviceContextUnavailable as exc:
            self.status_label.setText(str(exc))
            return None
        if (
            self._android_view_context == context
            and self._android_view_is_current()
        ):
            return context
        if self._android_upload_refresh_is_current(context):
            return context
        self._require_current_android_view(action)
        return None

    def _android_upload_refresh_is_current(self, context: DeviceContext) -> bool:
        """Return whether a same-device/path passive refresh can gate a push."""

        def active(token: OperationToken | None) -> bool:
            return bool(
                token is not None
                and not token.cancelled
                and token.device_context == context
                and self.operations.contains(token)
                and self.device_manager.is_context_current(context)
            )

        listing_token = self._android_refresh_token
        listing_request = self._android_listing_request
        if (
            active(listing_token)
            and listing_request is not None
            and listing_request.device_context == context
            and self._normalize_android_path(listing_request.requested_path)
            == self._normalize_android_path(self.android_path)
            and self.listing_controller.is_listing_current(listing_request)
        ):
            return True

        storage_token = self._android_storage_token
        storage_request = self._android_storage_request
        return bool(
            self._android_refresh_pending
            and active(storage_token)
            and storage_request is not None
            and storage_request.device_context == context
            and self.listing_controller.is_storage_current(storage_request)
            and self._normalize_android_path(
                self.listing_controller.requested_android_path
            )
            == self._normalize_android_path(self.android_path)
        )

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
        self._set_android_space_text("Free space: -")

    def invalidate_stale_device_view(self) -> None:
        """Remove rows and volumes that no longer belong to the active context."""

        invalidate_requests = False
        maintenance_context = self._maintenance_refresh_context
        if (
            maintenance_context is not None
            and not self.device_manager.is_context_current(maintenance_context)
        ):
            self._clear_maintenance_refresh_wait()
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
            self._cancel_pending_transfer_start()
            self.file_actions.cancel_pending_android_action()
            self.listing_controller.invalidate_android()

    def invalidate_privilege_backend_view(self) -> None:
        """Discard Android data and workers captured under the previous mode.

        The visible Windows side and P2P/SAF uploads are mode-independent and
        remain intact.  Android shell listings, actions and ADB transfers must
        not continue displaying or applying results from the old access mode.
        """

        reason = "selected access mode changed"
        self._clear_maintenance_refresh_wait()
        self._cancel_pending_transfer_start(privilege_only=True)
        self.file_actions.cancel_pending_android_action()
        for token in (
            self._android_refresh_token,
            self._android_storage_token,
            self._root_check_token,
            self._transfer_token,
        ):
            if token is not None and token.privilege_lease is not None:
                token.cancel(reason)
        self._android_refresh_pending = False
        self._android_storage_refresh_pending = False
        self._android_listing_request = None
        self._android_storage_request = None
        self.listing_controller.invalidate_android()
        self._clear_android_listing()
        self._android_storage_context = None
        self._set_android_storage_combo([])
        self.status_label.setText(
            "Access mode changed. Android files will refresh with the selected mode."
        )

    def request_privilege_backend_refresh(self) -> None:
        """Refresh after every old-backend File Manager worker has drained."""

        self._privilege_refresh_pending = True
        self._maybe_start_privilege_backend_refresh()

    def _maybe_start_privilege_backend_refresh(self) -> bool:
        if not self._privilege_refresh_pending:
            return False
        if getattr(self, "_workers_shutting_down", False):
            self._privilege_refresh_pending = False
            return False
        active_privilege_actions = any(
            token.privilege_lease is not None
            and token.owner_key.startswith("file-manager.")
            for token in self.operations.active_tokens()
        )
        if (
            active_privilege_actions
            or self._android_loading
            or self._android_storage_loading
            or self._root_check_running
            or (
                self._transfer_running
                and self._transfer_token is not None
                and self._transfer_token.privilege_lease is not None
            )
        ):
            return False
        if self.device_manager.active.mode not in {"ADB", "Recovery"}:
            return False
        self._privilege_refresh_pending = False
        self.refresh_all()
        return True

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
        root_state = (
            "not checked"
            if self._file_manager_root_requested()
            and self.device_manager.active.mode in {"ADB", "Recovery"}
            else (
                "unavailable"
                if self._file_manager_root_requested()
                else "not selected"
            )
        )
        self._set_root_status(root_state)
        self.android_path = self.listing_controller.set_android_path(state.android_path)
        self.android_panel.set_path(self.android_path)
        self._set_path_display(self.android_path_edit, self.android_path)
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

    def _check_root_availability(self) -> None:
        if self._root_check_running:
            return
        if not self._file_manager_root_requested():
            self._set_root_status("not selected")
            return
        operation = self._capture_device_operation("file-manager.root-check", "file-manager.root-check")
        if operation is None:
            self._set_root_status("unavailable")
            return
        context, adb, token = operation
        self._root_check_token = token
        self._root_check_running = True
        self.pull_button.setEnabled(False)
        self.push_button.setEnabled(False)
        self._set_root_status("checking")
        def check_root() -> bool:
            prepared = self._privileged_adb_for_worker(
                context,
                adb,
                token.cancel_event,
                token.privilege_lease,
            )
            return self._root_available_for_worker(
                prepared,
                True,
                token.cancel_event,
            )

        worker = Worker(check_root)
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
            self.status_label.setText("Global Root backend is ready for this device.")
        else:
            self.status_label.setText(
                "Global Root backend was denied or is unavailable; select Standard or Shizuku to use another backend."
            )

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
        self._maybe_start_privilege_backend_refresh()

    def _set_access_status_label(self, text: str, tooltip: str) -> None:
        """Show the complete access status without forcing the action panel wider."""

        text = str(text or "Access status unavailable.")
        tooltip = str(tooltip or text)
        full_tooltip = text if tooltip == text else f"{text}\n\n{tooltip}"
        self.root_status_label.setText(text)
        self.root_status_label.setToolTip(full_tooltip)
        self.root_status_label.setAccessibleDescription(full_tooltip)

    def _set_android_space_text(self, text: str) -> None:
        text = str(text or "Free space: -")
        self.android_space_label.setText(text)
        self.android_space_label.setToolTip(text)
        self.android_space_label.setAccessibleDescription(text)

    @staticmethod
    def _set_path_display(edit, text: str) -> None:
        text = str(text or "")
        edit.setText(text)
        edit.setToolTip(text)
        edit.setAccessibleDescription(text)
        edit.setCursorPosition(0)

    def _set_root_status(self, state: str) -> None:
        normalized = state if state in {"unavailable", "not selected", "not checked", "checking", "granted", "denied"} else "not checked"
        self._root_status = normalized
        descriptions = {
            "unavailable": "No authorized ADB/Recovery device is available for a root check.",
            "not selected": "Global privilege mode is Standard or Shizuku, so direct su/root is not used.",
            "not checked": "Global Root mode is selected, but access has not yet been verified for this device.",
            "checking": "Checking whether the connected device grants su/root access.",
            "granted": "The connected device granted root access for transfers.",
            "denied": "Global Root was denied or unavailable; no direct-root fallback is active.",
        }
        self._set_access_status_label(
            f"Root: {normalized}",
            descriptions[normalized],
        )
        self.root_status_label.setProperty("rootState", normalized.replace(" ", "-"))
        self.root_status_label.style().unpolish(self.root_status_label)
        self.root_status_label.style().polish(self.root_status_label)

    def set_privilege_status(
        self,
        status: PrivilegeStatus | None,
        *,
        backend: PrivilegeBackend | str | None = None,
        profile_available: bool = True,
        pending_queued: bool | None = None,
    ) -> None:
        """Mirror the one global access selection in File Manager."""

        self._global_privilege_status = status
        manager = self.privilege_manager
        configured_backend = backend if backend is not None else (
            manager.selected_backend
            if manager is not None and profile_available
            else read_privilege_backend_setting(
                self.settings,
                profile_available=profile_available,
            )
        )
        if pending_queued is None:
            pending_queued = profile_available or bool(
                str(
                    getattr(configured_backend, "value", configured_backend)
                    or ""
                ).strip()
            )
        backend = PrivilegeBackend.normalize(configured_backend)
        if self._selected_transfer_transport() == P2P_TRANSPORT:
            self._set_access_status_label(
                "P2P: Android SAF (no Root or Shizuku)",
                "P2P writes through ACBridge and Android Storage Access Framework; "
                "the global shell access mode is not used for file data.",
            )
            return
        if status is None or status.backend is not backend:
            if profile_available:
                text = {
                    PrivilegeBackend.STANDARD: "Standard ADB: not checked",
                    PrivilegeBackend.ROOT: "Root: not checked",
                    PrivilegeBackend.SHIZUKU: "Shizuku: not checked",
                }[backend]
                tooltip = "Access will be verified for the active device before a supported operation."
            else:
                if pending_queued:
                    text = {
                        PrivilegeBackend.STANDARD: "Next device: Standard ADB",
                        PrivilegeBackend.ROOT: "Next device: Root",
                        PrivilegeBackend.SHIZUKU: "Next device: Shizuku",
                    }[backend]
                    tooltip = (
                        "No device is active. This access mode will be applied once "
                        "to the next active device profile."
                    )
                else:
                    text = "Next device: choose an access mode"
                    tooltip = (
                        "No access-mode override is queued for the next device profile."
                    )
        else:
            text = str(status.message or f"{backend.value.title()}: {status.state}")
            tooltip = text
        self._set_access_status_label(text, tooltip)

    def _android_shell_backend_available(self) -> bool:
        """Return whether Android shell-backed browsing can start right now."""

        active = getattr(self.device_manager, "active", None)
        mode = str(getattr(active, "mode", "No device") or "No device")
        state = str(getattr(active, "state", "") or "").casefold()
        if mode not in {"ADB", "Recovery"} or state not in {"", "device"}:
            return False
        manager = self.privilege_manager
        backend = PrivilegeBackend.normalize(
            getattr(
                manager,
                "selected_backend",
                read_privilege_backend_setting(self.settings),
            )
        )
        if backend is not PrivilegeBackend.SHIZUKU:
            return True
        if mode != "ADB" or manager is None:
            return False
        cached_status = getattr(manager, "cached_status", None)
        if not callable(cached_status):
            return True
        status = cached_status()
        return bool(
            status is not None
            and status.backend is PrivilegeBackend.SHIZUKU
            and status.available
        )

    def _android_shell_backend_unavailable_message(self) -> str:
        active = getattr(self.device_manager, "active", None)
        mode = str(getattr(active, "mode", "No device") or "No device")
        manager = self.privilege_manager
        backend = PrivilegeBackend.normalize(
            getattr(
                manager,
                "selected_backend",
                read_privilege_backend_setting(self.settings),
            )
        )
        if backend is PrivilegeBackend.SHIZUKU:
            if mode != "ADB":
                return f"Shizuku cannot browse Android files in {mode} mode."
            status = manager.cached_status() if manager is not None else None
            if status is not None and status.message:
                return str(status.message)
            return "Grant and verify Shizuku access before browsing Android files."
        return (
            "The Android device is disconnected, unauthorized, or unavailable "
            "for ADB/Recovery file operations."
        )

    def _create_windows_panel(self) -> QWidget:
        try:
            return NativeExplorerPanel(self.windows_path)
        except Exception:  # noqa: BLE001 - optional native host must degrade safely
            return WindowsFilePanel(self.windows_path, show_path_bar=False, show_button_row=False)

    def _set_active_side(self, side: str) -> None:
        self._active_side = "windows" if side == "windows" else "android"

    def refresh_all(self) -> None:
        self.invalidate_stale_device_view()
        if self.device_manager.active.mode not in {"ADB", "Recovery"}:
            self._set_root_status("unavailable")
        elif not self._file_manager_root_requested():
            self.set_privilege_status(self._global_privilege_status)
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
        privilege_lease=None,
    ) -> tuple[AndroidListingResult, bool]:
        return self.file_listings.load_android_files(
            prepared,
            use_root_requested,
            cancel_event,
            privilege_lease,
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
        self._set_path_display(self.windows_path_edit, self.windows_path)

    def navigate_windows(self, path: str, record_history: bool = True) -> None:
        if not path:
            return
        try:
            resolved = self.listing_controller.navigate_windows(path)
            self.windows_path = resolved
            self.file_manager_state.save_windows_path(resolved)
            self._set_path_display(self.windows_path_edit, resolved)
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
                self._set_path_display(self.windows_path_edit, path)
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
        android_sources = tuple(str(path) for path in android_paths)
        try:
            plan = TransferPlan(
                direction=PULL_DIRECTION,
                transport=ADB_TRANSFER,
                sources=android_sources,
                destination=str(destination),
                device_context=expected_context,
                use_root=self._file_manager_root_requested(),
            )
        except TransferPlanError as exc:
            self.status_label.setText(f"Android → PC: {exc}")
            return
        if self._selected_transfer_transport() == P2P_TRANSPORT:
            self.status_label.setText(
                "P2P via ACBridge is selected for uploads. Android → PC uses Platform Tools in this version."
            )
        self._start_or_defer_transfer(
            _PendingTransferStart(plan, "Android → PC", self.refresh_windows)
        )

    def push_selected(self) -> None:
        self.push_paths(self.windows_panel.selected_paths())

    def push_paths(self, local_paths: list[str]) -> None:
        if not local_paths:
            return
        if not self._can_start_transfer():
            return
        if not self._ensure_android_available("PC → Android"):
            return
        expected_context = self._capture_android_upload_context("PC → Android")
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
        local_sources = tuple(str(path) for path in local_paths)
        transport = self._selected_transfer_transport()
        # ACBridge/SAF owns the P2P data path.  Global Root only affects the
        # direct Platform Tools streaming strategy.
        use_root = (
            self._file_manager_root_requested()
            if transport == ADB_TRANSPORT
            else False
        )
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
                device_context=expected_context,
                use_root=use_root,
                parallelism_mode=parallelism_mode,
                requested_parallelism=requested_parallelism,
            )
        except TransferPlanError as exc:
            self.status_label.setText(f"PC → Android: {exc}")
            return
        self._start_or_defer_transfer(
            _PendingTransferStart(plan, "PC → Android", self.refresh_android)
        )

    def _start_or_defer_transfer(self, pending: _PendingTransferStart) -> None:
        listing_tokens = self._active_passive_listing_tokens(
            pending.plan.device_context
        )
        if not listing_tokens:
            self._start_prepared_transfer(pending)
            return
        if self._pending_transfer_start is not None:
            self.status_label.setText(
                f"{pending.title}: another transfer is already waiting for the current Android refresh."
            )
            return
        self._pending_transfer_start = pending
        self._transfer_plan = pending.plan
        self._android_refresh_pending = False
        self._android_storage_refresh_pending = False
        self._set_transfer_running(True)
        for token in listing_tokens:
            token.cancel(
                f"{pending.title} is waiting for the passive Android folder refresh to stop."
            )
        self.status_label.setText(
            f"{pending.title}: waiting for the current Android folder refresh to stop..."
        )

    def _start_pending_transfer_if_ready(self) -> bool:
        pending = self._pending_transfer_start
        if pending is None:
            return False
        if getattr(self, "_workers_shutting_down", False):
            self._cancel_pending_transfer_start()
            return True
        if self._active_passive_listing_tokens(pending.plan.device_context):
            return False
        self._pending_transfer_start = None
        if not self.device_manager.is_context_current(pending.plan.device_context):
            self._clear_waiting_transfer_state()
            self._android_storage_refresh_pending = True
            self._android_refresh_pending = True
            return False
        started = self._start_prepared_transfer(pending)
        if not started:
            self._clear_waiting_transfer_state()
            self._android_refresh_pending = True
        return started

    def _start_prepared_transfer(self, pending: _PendingTransferStart) -> bool:
        plan = pending.plan
        context = plan.device_context
        cancel_event = threading.Event()
        owner_key = "file-manager.push" if plan.is_upload else "file-manager.pull"
        operation = self._capture_device_operation(
            owner_key,
            "file-manager.transfer",
            cancel_event=cancel_event,
            exclusive=True,
            expected_context=context,
        )
        if operation is None:
            return False
        _context, adb, token = operation
        if plan.is_p2p:
            # ACBridge writes P2P uploads through Android SAF and never uses
            # the selected shell backend. Keep it alive across access changes.
            token.privilege_lease = None
        self._pending_transfer_start = None
        self._transfer_token = token
        self._transfer_plan = plan
        self._transfer_cancel_events.add(cancel_event)
        dialog = self._create_transfer_dialog(pending.title)
        dialog.cancel_requested.connect(lambda: self._cancel_transfer(dialog, token))

        def run(item_callback=None) -> dict:
            self._require_operation_preflight(token)
            operation_adb = (
                adb
                if plan.is_p2p
                else self._privileged_adb_for_worker(
                    context,
                    adb,
                    cancel_event,
                    token.privilege_lease,
                )
            )
            self._require_operation_preflight(token)
            return self.transfer_controller.execute(
                plan,
                adb=operation_adb,
                cancel_event=cancel_event,
                item_callback=item_callback,
            )

        worker = Worker(run)
        worker.signals.item.connect(
            lambda update, current=token: self._transfer_progress(
                current, dialog, update
            )
        )
        worker.signals.result.connect(
            lambda result, current=token: self._transfer_done(
                current, dialog, result, pending.refresh
            )
        )
        worker.signals.error.connect(
            lambda message, _trace, current=token: self._transfer_failed(
                current, dialog, pending.title, message
            )
        )
        worker.signals.finished.connect(
            lambda current=token: self._transfer_worker_finished(current, dialog)
        )
        self._set_transfer_running(True)
        if self._start_operation_worker(worker, token):
            self.transfer_started.emit(token.operation_id)
            dialog.show()
            return True
        self._transfer_worker_finished(token, dialog)
        self._forget_transfer_dialog(dialog)
        return False

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
                "device and is not guaranteed even when global Root mode is selected. Continue?"
            ),
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        return answer == QMessageBox.Ok

    def _ensure_android_available(self, action: str) -> bool:
        active_mode = str(getattr(self.device_manager.active, "mode", "No device"))
        if (
            action == "PC → Android"
            and self._selected_transfer_transport() == P2P_TRANSPORT
            and active_mode == "ADB"
        ):
            return True
        if self._android_shell_backend_available():
            return True
        message = f"{action}: {self._android_shell_backend_unavailable_message()}"
        self.status_label.setText(message)
        QMessageBox.warning(self, action, message)
        return False

    def _file_manager_root_requested(self) -> bool:
        """Return whether the one global privilege selection requests Root.

        ``file_manager_root_transfer`` is intentionally ignored.  It remains a
        readable legacy setting so older profiles/plugins load safely, but it
        no longer creates a fourth privilege state beside Standard, Root and
        Shizuku.
        """

        manager = self.privilege_manager
        raw_backend = getattr(manager, "selected_backend", None)
        if raw_backend is None:
            raw_backend = self.settings.get("privilege_backend", None)
        if (raw_backend is None or raw_backend == "") and manager is None:
            # A minimal legacy settings object may not have run the normal
            # SettingsManager migration.  This fallback is never used when the
            # application-wide manager exists.
            raw_backend = (
                PrivilegeBackend.ROOT
                if bool(self.settings.get("root_mode_enabled", False))
                else PrivilegeBackend.STANDARD
            )
        return PrivilegeBackend.normalize(raw_backend) is PrivilegeBackend.ROOT

    def _root_available_for_worker(
        self,
        adb: ADBClient,
        requested: bool,
        cancel_event=None,
    ) -> bool:
        """Prevent stale/local flags from probing root outside global Root."""

        if not requested:
            return False
        if self.privilege_manager is not None:
            effective = PrivilegeBackend.normalize(
                getattr(
                    adb,
                    "effective_privilege_backend",
                    PrivilegeBackend.STANDARD,
                )
            )
            if effective is not PrivilegeBackend.ROOT:
                return False
        return bool(adb.root_available(cancel_event=cancel_event))

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
        if p2p:
            self._set_access_status_label(
                "P2P: Android SAF (no Root or Shizuku)",
                "P2P writes through ACBridge and Android Storage Access Framework; "
                "the global shell access mode is not used for file data.",
            )
            self.push_button.setToolTip(
                "Upload directly over the local network to ACBridge. Platform Tools creates the one-time session; "
                "Android SAF writes to the granted MicroSD/USB folder without root."
            )
        else:
            if self._global_privilege_status is None and self._file_manager_root_requested():
                self._set_root_status(self._root_status)
            else:
                self.set_privilege_status(self._global_privilege_status)
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
        if id(dialog) in self._dismissed_transfer_dialog_ids:
            self._dismissed_transfer_dialog_ids.remove(id(dialog))
        self._transfer_dialogs.append(dialog)
        dialog.finished.connect(lambda _code, dlg=dialog: self._forget_transfer_dialog(dlg))
        dialog.destroyed.connect(lambda _object=None, dlg=dialog: self._forget_transfer_dialog(dlg))
        return dialog

    def _forget_transfer_dialog(self, dialog: TransferProgressDialog) -> None:
        dialog_id = id(dialog)
        if dialog_id not in self._dismissed_transfer_dialog_ids:
            self._dismissed_transfer_dialog_ids.append(dialog_id)
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
        self._apply_transfer_update(token, dialog, {"type": "cancelled"})

    def cancel_active_transfers(self) -> None:
        """Cancel active transfer and local filesystem work before application exit."""
        self.file_actions.cancel_active()
        self._cancel_pending_transfer_start()
        if self._transfer_token is not None:
            self._transfer_token.cancel("Application shutdown requested.")
            self.transfer_progress_changed.emit(
                self._transfer_token.operation_id,
                {"type": "cancelled"},
            )
        for cancel_event in tuple(self._transfer_cancel_events):
            cancel_event.set()
        for dialog in tuple(self._transfer_dialogs):
            if dialog.isVisible():
                dialog.apply_update({"type": "cancelled"})

    def _cancel_pending_transfer_start(self, *, privilege_only: bool = False) -> bool:
        pending = self._pending_transfer_start
        if pending is None or (privilege_only and pending.plan.is_p2p):
            return False
        self._pending_transfer_start = None
        self._android_refresh_deferred_until_transfer = False
        self._clear_waiting_transfer_state()
        return True

    def _clear_waiting_transfer_state(self) -> None:
        if self._transfer_token is None:
            self._transfer_plan = None
            self._set_transfer_running(False)

    def _can_start_transfer(self) -> bool:
        if self.file_actions.has_pending_android_action:
            self.status_label.setText(
                "An Android file action is already waiting for the current folder refresh to stop."
            )
            return False
        if self._root_check_running:
            self.status_label.setText(
                "Wait for the current Root access check to finish before starting a file transfer."
            )
            return False
        if not self._transfer_running:
            return True
        self.status_label.setText(
            "Another file transfer is already running or waiting for the Android folder refresh to stop."
        )
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
            self._apply_transfer_update(token, dialog, update)
        elif not self.device_manager.is_context_current(token.device_context):
            self._mark_stale_transfer(dialog, token)

    def _apply_transfer_update(
        self,
        token: OperationToken,
        dialog: TransferProgressDialog,
        update: dict,
    ) -> None:
        dialog.apply_update(update)
        if token is self._transfer_token:
            self.transfer_progress_changed.emit(token.operation_id, dict(update))

    def _mark_stale_transfer(self, dialog: TransferProgressDialog, token: OperationToken) -> None:
        if id(dialog) in self._dismissed_transfer_dialog_ids:
            return
        if token.operation_id in self._stale_transfer_notifications:
            return
        self._stale_transfer_notifications.add(token.operation_id)
        self._stale_transfer_dialogs[token.operation_id] = dialog
        reason = token.cancellation_reason or "The active device changed during the transfer."
        self._apply_transfer_update(
            token,
            dialog,
            {"type": "done", "success": False, "message": reason},
        )

    def _transfer_worker_finished(
        self,
        token: OperationToken,
        dialog: TransferProgressDialog,
    ) -> None:
        is_current_transfer = token is self._transfer_token
        self.operations.finish(token)
        self._transfer_cancel_events.discard(token.cancel_event)
        context_is_current = self.device_manager.is_context_current(token.device_context)
        refresh = self._transfer_refresh_callbacks.pop(token.operation_id, None)
        deferred_android_refresh = False
        if is_current_transfer:
            deferred_android_refresh = self._android_refresh_deferred_until_transfer
            self._android_refresh_deferred_until_transfer = False
            self._transfer_token = None
            self._transfer_plan = None
            self._set_transfer_running(False)
            self.transfer_finished.emit(token.operation_id)
        if not context_is_current:
            self._mark_stale_transfer(dialog, token)
            if is_current_transfer and deferred_android_refresh:
                # The deferred request belongs to the newly active context,
                # not to the stale transfer.  Its old token is already gone,
                # so it is now safe to populate the new device view.
                try:
                    self.refresh_android_storage_roots()
                    self.refresh_android()
                except Exception as exc:  # noqa: BLE001 - never strand the page after worker teardown
                    self.status_label.setText(
                        self._friendly_error("Refresh after device change", str(exc))
                    )
            return
        if not is_current_transfer:
            return
        try:
            if deferred_android_refresh:
                # A connection/profile callback may request a full device-side
                # refresh while the transfer owns the shared ACBridge barrier.
                # Run it only after releasing that token so expected contention
                # never leaks into the status bar as an error.
                self.refresh_android_storage_roots()
                self.refresh_android()
            if refresh is not None:
                refresh()
        except Exception as exc:  # noqa: BLE001 - never strand the page after worker teardown
            self.status_label.setText(
                self._friendly_error("Refresh after transfer", str(exc))
            )
        self._maybe_start_privilege_backend_refresh()

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
        self._apply_transfer_update(
            token,
            dialog,
            {
                "type": "done",
                "success": success,
                "message": message,
            }
        )
        self.status_label.setText("Transfer completed successfully." if success else message)
        self._transfer_refresh_callbacks[token.operation_id] = refresh

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
        self._apply_transfer_update(
            token,
            dialog,
            {"type": "done", "success": False, "message": friendly},
        )

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
