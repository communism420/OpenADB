"""Qt orchestration for File Manager actions.

This composition object owns prompts, immutable request construction, worker
lifecycle, and result presentation.  The page remains responsible for its
panels and refresh/listing controller; actual filesystem work belongs to
``FileManagerActionCoordinator``.
"""

from __future__ import annotations

import threading
import sys
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from openadb.core.device_context import DeviceContext
from openadb.core.file_manager_controller import (
    AndroidActionRequest,
    FileManagerActionCoordinator,
    FileManagerActionResult,
    FileManagerRequestError,
    WindowsActionRequest,
    is_public_removable_android_path,
)
from openadb.core.file_manager_errors import (
    map_file_manager_error,
    redact_sensitive_text,
)
from openadb.core.operations import OperationToken
from openadb.ui.workers import Worker, start_worker


class FileManagerActionsHost(Protocol):
    """Page seams used by :class:`FileManagerActions`."""

    android_path: str
    windows_path: str
    android_panel: Any
    windows_panel: Any
    status_label: Any
    pool: Any
    device_manager: Any

    def _ensure_android_available(self, action: str) -> bool: ...

    def _require_current_android_view(self, action: str) -> bool: ...

    def _capture_android_action_context(
        self,
        action: str,
        *,
        require_current_view: bool = False,
    ) -> DeviceContext | None: ...

    def _capture_device_operation(
        self,
        owner_key: str,
        conflict_group: str,
        *,
        cancel_event: threading.Event | None = None,
        exclusive: bool = False,
        expected_context: DeviceContext | None = None,
    ) -> tuple[DeviceContext, Any, OperationToken] | None: ...

    def _file_manager_root_requested(self) -> bool: ...

    def _warn_android_write(self, path: str) -> bool: ...

    def _operation_is_current(
        self,
        token: OperationToken,
        *,
        allow_cancelled: bool = False,
    ) -> bool: ...

    def _start_operation_worker(self, worker: Worker, token: OperationToken) -> bool: ...

    def _device_operation_failed(
        self,
        token: OperationToken,
        title: str,
        message: str,
    ) -> None: ...

    def refresh_android(self) -> None: ...

    def refresh_windows(self) -> None: ...

    def _start_local_worker(self, worker: Worker) -> bool: ...


class FileManagerActions:
    """Prompt and run File Manager actions through immutable requests."""

    def __init__(
        self,
        host: FileManagerActionsHost,
        coordinator: FileManagerActionCoordinator,
    ) -> None:
        self.host = host
        self.coordinator = coordinator
        self._local_cancel_events: set[threading.Event] = set()

    @property
    def parent(self) -> QWidget:
        return self.host  # type: ignore[return-value]

    def _host_symbol(self, name: str, fallback):
        """Resolve Page aliases lazily so existing monkeypatch seams survive."""

        module = sys.modules.get(type(self.host).__module__)
        return getattr(module, name, fallback) if module is not None else fallback

    def new_folder(self, kind: str) -> None:
        expected_context = self._prepare_android_action(
            "New folder",
            kind,
            require_current_view=True,
        )
        if kind == "android" and expected_context is None:
            return
        input_dialog = self._host_symbol("QInputDialog", QInputDialog)
        name, accepted = input_dialog.getText(
            self.parent,
            "New folder",
            "Folder name:",
        )
        if not accepted or not name.strip():
            return
        try:
            if kind == "android":
                assert expected_context is not None
                request = AndroidActionRequest.create_folder(
                    expected_context,
                    self.host.android_path,
                    name,
                    use_root_requested=self.host._file_manager_root_requested(),
                )
                if not self.host._warn_android_write(request.target):
                    return
                self._start_android(
                    request,
                    title="New folder",
                    owner_key="file-manager.mkdir",
                    refresh=self.host.refresh_android,
                )
            else:
                request = WindowsActionRequest.create_folder(
                    self.host.windows_path,
                    name,
                )
                self._start_windows(
                    request,
                    title="New folder",
                    refresh=self.host.refresh_windows,
                )
        except FileManagerRequestError as exc:
            self._request_failed("New folder", exc)

    def delete_selected(self, kind: str) -> None:
        panel = self.host.android_panel if kind == "android" else self.host.windows_panel
        paths = tuple(str(path) for path in panel.selected_paths())
        if not paths:
            return
        expected_context = self._prepare_android_action(
            "Delete",
            kind,
            require_current_view=True,
        )
        if kind == "android" and expected_context is None:
            return
        message_box = self._host_symbol("QMessageBox", QMessageBox)
        answer = message_box.warning(
            self.parent,
            "Delete",
            "Delete selected item(s)?",
            message_box.Ok | message_box.Cancel,
        )
        if answer != message_box.Ok:
            return
        try:
            if kind == "android":
                assert expected_context is not None
                if any(not self.host._warn_android_write(path) for path in paths):
                    return
                removable = any(is_public_removable_android_path(path) for path in paths)
                if removable and not self._confirm_removable_delete():
                    return
                request = AndroidActionRequest.delete(
                    expected_context,
                    paths,
                    use_root_requested=self.host._file_manager_root_requested(),
                    allow_storage_grant=removable,
                )
                self._start_android(
                    request,
                    title="Delete",
                    owner_key="file-manager.delete",
                    refresh=self.host.refresh_android,
                )
            else:
                request = WindowsActionRequest.delete(paths)
                self._start_windows(
                    request,
                    title="Delete",
                    refresh=self.host.refresh_windows,
                )
        except FileManagerRequestError as exc:
            self._request_failed("Delete", exc)

    def rename_selected(self, kind: str) -> None:
        panel = self.host.android_panel if kind == "android" else self.host.windows_panel
        path = str(panel.selected_path() or "")
        if not path:
            return
        expected_context = self._prepare_android_action(
            "Rename",
            kind,
            require_current_view=True,
        )
        if kind == "android" and expected_context is None:
            return
        current_name = (
            path.rstrip("/").split("/")[-1]
            if kind == "android"
            else Path(path).name
        )
        input_dialog = self._host_symbol("QInputDialog", QInputDialog)
        new_name, accepted = input_dialog.getText(
            self.parent,
            "Rename",
            "New name:",
            text=current_name,
        )
        if not accepted or not new_name.strip():
            return
        try:
            if kind == "android":
                assert expected_context is not None
                request = AndroidActionRequest.rename(
                    expected_context,
                    path,
                    new_name,
                    use_root_requested=self.host._file_manager_root_requested(),
                )
                if not self.host._warn_android_write(path):
                    return
                self._start_android(
                    request,
                    title="Rename",
                    owner_key="file-manager.rename",
                    refresh=self.host.refresh_android,
                )
            else:
                request = WindowsActionRequest.rename(path, new_name)
                self._start_windows(
                    request,
                    title="Rename",
                    refresh=self.host.refresh_windows,
                )
        except FileManagerRequestError as exc:
            self._request_failed("Rename", exc)

    def properties(self, kind: str) -> None:
        panel = self.host.android_panel if kind == "android" else self.host.windows_panel
        selected = str(panel.selected_path() or "")
        if kind == "android" and selected:
            if not self.host._require_current_android_view("Properties"):
                return
        path = selected or str(panel.current_path)
        if not path:
            return
        if kind == "android":
            expected_context = self._prepare_android_action(
                "Properties",
                kind,
                require_current_view=True,
            )
            if expected_context is None:
                return
            try:
                request = AndroidActionRequest.properties(
                    expected_context,
                    path,
                    use_root_requested=self.host._file_manager_root_requested(),
                )
                self._start_android(
                    request,
                    title="Properties",
                    owner_key="file-manager.properties",
                    conflict_group="file-manager.properties",
                )
            except FileManagerRequestError as exc:
                self._request_failed("Properties", exc)
            return
        self._start_windows(
            WindowsActionRequest.properties(path),
            title="Properties",
        )

    def copy_path(self, kind: str) -> None:
        panel = self.host.android_panel if kind == "android" else self.host.windows_panel
        if (
            kind == "android"
            and panel.selected_path()
            and not self.host._require_current_android_view("Copy path")
        ):
            return
        path = str(panel.selected_path() or panel.current_path or "")
        if path:
            gui_application = self._host_symbol("QGuiApplication", QGuiApplication)
            gui_application.clipboard().setText(path)

    def open_explorer(self) -> None:
        desktop_services = self._host_symbol("QDesktopServices", QDesktopServices)
        if not desktop_services.openUrl(QUrl.fromLocalFile(self.host.windows_path)):
            self._request_failed(
                "Open in Explorer",
                FileManagerRequestError(
                    f"Folder is unavailable: {self.host.windows_path}"
                ),
            )

    def offer_install_single_apk(
        self,
        local_paths: list[str] | tuple[str, ...],
        *,
        expected_context: DeviceContext | None = None,
    ) -> bool:
        apk_path = self.single_local_apk_path(local_paths)
        if apk_path is None:
            return False
        if expected_context is None:
            expected_context = self._prepare_android_action("Install APK", "android")
        if expected_context is None:
            return True

        message_box = self._host_symbol("QMessageBox", QMessageBox)
        box = message_box(self.parent)
        box.setWindowTitle("APK file selected")
        box.setIcon(message_box.Question)
        box.setText("The selected file is an APK.")
        box.setInformativeText(
            "Do you want to install this application directly with adb install "
            "instead of copying the APK file to Android storage?"
        )
        box.setDetailedText(str(apk_path))
        install_button = box.addButton("Install APK", message_box.AcceptRole)
        copy_button = box.addButton("Copy anyway", message_box.ActionRole)
        box.addButton("Cancel", message_box.RejectRole)
        box.setDefaultButton(install_button)
        box.exec()

        clicked = box.clickedButton()
        if clicked is copy_button:
            return False
        if clicked is install_button:
            self.install_local_apk(apk_path, expected_context=expected_context)
        return True

    @staticmethod
    def single_local_apk_path(
        local_paths: list[str] | tuple[str, ...],
    ) -> Path | None:
        if len(local_paths) != 1:
            return None
        try:
            path = Path(local_paths[0]).expanduser()
            if path.is_file() and path.suffix.casefold() == ".apk":
                return path
        except OSError:
            pass
        return None

    def install_local_apk(
        self,
        apk_path: str | Path,
        *,
        expected_context: DeviceContext | None = None,
    ) -> None:
        context = expected_context or self._prepare_android_action(
            "Install APK",
            "android",
        )
        if context is None:
            return
        try:
            request = AndroidActionRequest.install_apk(context, apk_path)
        except FileManagerRequestError as exc:
            self._request_failed("Install APK", exc)
            return
        self.host.status_label.setText(f"Installing APK: {request.local_path.name}")
        self._start_android(
            request,
            title="Install APK",
            owner_key="file-manager.install-apk",
        )

    def _prepare_android_action(
        self,
        title: str,
        kind: str,
        *,
        require_current_view: bool = False,
    ) -> DeviceContext | None:
        if kind != "android":
            return None
        if not self.host._ensure_android_available(title):
            return None
        return self.host._capture_android_action_context(
            title,
            require_current_view=require_current_view,
        )

    def _start_android(
        self,
        request: AndroidActionRequest,
        *,
        title: str,
        owner_key: str,
        conflict_group: str = "file-manager.mutation",
        refresh=None,
    ) -> None:
        operation = self.host._capture_device_operation(
            owner_key,
            conflict_group,
            exclusive=conflict_group == "file-manager.mutation",
            expected_context=request.device_context,
        )
        if operation is None:
            return
        _context, _bound_adb, token = operation
        worker = Worker(
            lambda: self.coordinator.execute_android(
                request,
                cancel_event=token.cancel_event,
            )
        )
        worker.signals.result.connect(
            lambda result, current=token: self._android_done(
                current,
                title,
                result,
                refresh,
            )
        )
        worker.signals.error.connect(
            lambda message, _trace, current=token: self.host._device_operation_failed(
                current,
                title,
                message,
            )
        )
        self.host._start_operation_worker(worker, token)

    def _start_windows(
        self,
        request: WindowsActionRequest,
        *,
        title: str,
        refresh=None,
    ) -> None:
        cancel_event = threading.Event()
        self._local_cancel_events.add(cancel_event)
        worker = Worker(
            lambda: self.coordinator.execute_windows(
                request,
                cancel_event=cancel_event,
            )
        )
        worker.signals.result.connect(
            lambda result: self._windows_done(title, result, refresh)
        )
        worker.signals.error.connect(
            lambda message, _trace: self._windows_failed(title, message)
        )
        worker.signals.finished.connect(
            lambda current=cancel_event: self._local_cancel_events.discard(current)
        )
        starter = getattr(self.host, "_start_local_worker", None)
        if callable(starter):
            started = starter(worker)
        else:
            started = start_worker(self.parent, self.host.pool, worker)
        if not started:
            self._local_cancel_events.discard(cancel_event)

    def cancel_active(self) -> None:
        """Cancel every local File Manager action during application shutdown."""

        for cancel_event in tuple(self._local_cancel_events):
            cancel_event.set()

    def _windows_done(
        self,
        title: str,
        result: FileManagerActionResult,
        refresh,
    ) -> None:
        if not getattr(self.host, "_workers_shutting_down", False):
            self._present_result(title, result, refresh)

    def _windows_failed(self, title: str, message: str) -> None:
        if not getattr(self.host, "_workers_shutting_down", False):
            self._request_failed(title, FileManagerRequestError(message))

    def _android_done(
        self,
        token: OperationToken,
        title: str,
        result: FileManagerActionResult,
        refresh,
    ) -> None:
        if self.host._operation_is_current(token):
            self._present_result(title, result, refresh)

    def _present_result(
        self,
        title: str,
        result: FileManagerActionResult,
        refresh,
    ) -> None:
        if result.success or result.cancelled:
            messages = list(result.messages)
        else:
            failures = [item.message for item in result.items if not item.success]
            successes = [item.message for item in result.items if item.success]
            messages = failures + successes
        visible_messages = messages[:79]
        if len(messages) > len(visible_messages):
            visible_messages.append(
                f"... {len(messages) - len(visible_messages)} additional result(s) omitted"
            )
        text = redact_sensitive_text("\n".join(visible_messages))
        if result.cancelled:
            mapped = map_file_manager_error(
                "File Manager action was cancelled",
                operation=title,
            )
            self.host.status_label.setText(mapped.title)
        elif result.success:
            if title == "Install APK" and result.items:
                self.host.status_label.setText(
                    f"Installed APK: {Path(result.items[0].path).name}"
                )
            else:
                self.host.status_label.setText(f"{title}: completed")
            message_box = self._host_symbol("QMessageBox", QMessageBox)
            message_box.information(self.parent, title, text or f"{title} completed.")
        else:
            mapped = map_file_manager_error(text or f"{title} failed.", operation=title)
            self.host.status_label.setText(mapped.title)
            message_box = self._host_symbol("QMessageBox", QMessageBox)
            message_box.warning(self.parent, mapped.title, mapped.message)
        if refresh is not None:
            refresh()

    def _confirm_removable_delete(self) -> bool:
        message_box = self._host_symbol("QMessageBox", QMessageBox)
        answer = message_box.warning(
            self.parent,
            "Android TV storage access",
            (
                "You are deleting from removable MicroSD/USB storage.\n\n"
                "If Android TV asks for storage access, select the ROOT of this "
                "MicroSD/USB card on the TV screen and confirm it. OpenADB will wait "
                "and will not retry deletion until access is granted.\n\nContinue?"
            ),
            message_box.Ok | message_box.Cancel,
            message_box.Ok,
        )
        return answer == message_box.Ok

    def _request_failed(self, title: str, error: Exception) -> None:
        mapped = map_file_manager_error(error, operation=title)
        self.host.status_label.setText(mapped.title)
        message_box = self._host_symbol("QMessageBox", QMessageBox)
        message_box.warning(self.parent, mapped.title, mapped.message)
