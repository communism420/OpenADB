"""Immutable File Manager actions outside the Qt page.

The controller intentionally has two execution paths.  Windows requests never
consult device state, while Android requests carry a complete
``DeviceContext`` and are rebound and revalidated at execution time.  This
keeps local file work available without a phone and prevents a selector change
from retargeting an already confirmed Android mutation.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .acbridge import ACBridgeClient
from .device_context import DeviceContext, DeviceContextUnavailable, StaleDeviceContext
from .file_manager_errors import map_file_manager_error
from .path_utils import join_android_path, parent_android_path, safe_filename


class FileManagerAction(str, Enum):
    CREATE_FOLDER = "create-folder"
    DELETE = "delete"
    RENAME = "rename"
    PROPERTIES = "properties"
    INSTALL_APK = "install-apk"


class FileManagerSide(str, Enum):
    WINDOWS = "windows"
    ANDROID = "android"


class FileManagerRequestError(ValueError):
    """Raised when an action request is incomplete or unsafe to execute."""


class FileManagerActionCancelled(RuntimeError):
    """Raised at a cancellation checkpoint before another action is started."""


def _clean_name(value: str) -> str:
    name = str(value or "").strip()
    if not name or name in {".", ".."}:
        raise FileManagerRequestError("A non-empty item name is required")
    if "/" in name or "\\" in name or "\x00" in name:
        raise FileManagerRequestError("Item names cannot contain path separators")
    return name


def _android_path(value: str) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path.startswith("/"):
        raise FileManagerRequestError("Android paths must be absolute")
    parts = tuple(part for part in path.split("/") if part)
    if any(part in {".", ".."} for part in parts):
        raise FileManagerRequestError("Android paths cannot contain traversal segments")
    return "/" + "/".join(parts) if parts else "/"


def is_public_removable_android_path(path: str) -> bool:
    """Return whether *path* names public MicroSD/USB storage."""

    text = str(path or "").replace("\\", "/").strip()
    return text.startswith("/storage/") and not text.startswith(
        ("/storage/emulated/", "/storage/self/")
    )


@dataclass(frozen=True, slots=True)
class WindowsActionRequest:
    """A device-independent local filesystem action."""

    action: FileManagerAction
    paths: tuple[Path, ...] = ()
    target: Path | None = None

    @classmethod
    def create_folder(cls, parent: str | Path, name: str) -> WindowsActionRequest:
        clean_name = _clean_name(name)
        return cls(
            FileManagerAction.CREATE_FOLDER,
            target=Path(parent).expanduser() / safe_filename(clean_name),
        )

    @classmethod
    def delete(cls, paths: tuple[str | Path, ...] | list[str | Path]) -> WindowsActionRequest:
        return cls(
            FileManagerAction.DELETE,
            paths=tuple(Path(path).expanduser() for path in paths),
        )

    @classmethod
    def rename(cls, source: str | Path, new_name: str) -> WindowsActionRequest:
        clean_name = _clean_name(new_name)
        source_path = Path(source).expanduser()
        return cls(
            FileManagerAction.RENAME,
            paths=(source_path,),
            target=source_path.with_name(clean_name),
        )

    @classmethod
    def properties(cls, path: str | Path) -> WindowsActionRequest:
        return cls(FileManagerAction.PROPERTIES, paths=(Path(path).expanduser(),))

    def __post_init__(self) -> None:
        if not isinstance(self.action, FileManagerAction):
            raise FileManagerRequestError("Unknown Windows action")
        if any(not isinstance(path, Path) for path in self.paths):
            raise FileManagerRequestError("Windows request paths must be Path objects")
        if self.action is FileManagerAction.CREATE_FOLDER:
            if self.paths or self.target is None:
                raise FileManagerRequestError("Create-folder requires only a target")
        elif self.action is FileManagerAction.DELETE:
            if not self.paths or self.target is not None:
                raise FileManagerRequestError("Delete requires one or more source paths")
        elif self.action is FileManagerAction.RENAME:
            if len(self.paths) != 1 or self.target is None:
                raise FileManagerRequestError("Rename requires one source and one target")
            if self.paths[0].parent != self.target.parent:
                raise FileManagerRequestError("Rename cannot move an item to another folder")
        elif self.action is FileManagerAction.PROPERTIES:
            if len(self.paths) != 1 or self.target is not None:
                raise FileManagerRequestError("Properties requires exactly one path")
        else:
            raise FileManagerRequestError(f"{self.action.value} is not a Windows action")


@dataclass(frozen=True, slots=True)
class AndroidActionRequest:
    """An Android action pinned to a complete immutable device identity."""

    action: FileManagerAction
    device_context: DeviceContext
    paths: tuple[str, ...] = ()
    target: str = ""
    local_path: Path | None = None
    use_root_requested: bool = False
    allow_storage_grant: bool = False

    @classmethod
    def create_folder(
        cls,
        context: DeviceContext,
        parent: str,
        name: str,
        *,
        use_root_requested: bool = False,
    ) -> AndroidActionRequest:
        target = _android_path(join_android_path(_android_path(parent), _clean_name(name)))
        return cls(
            FileManagerAction.CREATE_FOLDER,
            context,
            target=target,
            use_root_requested=use_root_requested,
        )

    @classmethod
    def delete(
        cls,
        context: DeviceContext,
        paths: tuple[str, ...] | list[str],
        *,
        use_root_requested: bool = False,
        allow_storage_grant: bool = False,
    ) -> AndroidActionRequest:
        return cls(
            FileManagerAction.DELETE,
            context,
            paths=tuple(_android_path(path) for path in paths),
            use_root_requested=use_root_requested,
            allow_storage_grant=allow_storage_grant,
        )

    @classmethod
    def rename(
        cls,
        context: DeviceContext,
        source: str,
        new_name: str,
        *,
        use_root_requested: bool = False,
    ) -> AndroidActionRequest:
        source_path = _android_path(source)
        target = _android_path(
            join_android_path(parent_android_path(source_path), _clean_name(new_name))
        )
        return cls(
            FileManagerAction.RENAME,
            context,
            paths=(source_path,),
            target=target,
            use_root_requested=use_root_requested,
        )

    @classmethod
    def properties(
        cls,
        context: DeviceContext,
        path: str,
        *,
        use_root_requested: bool = False,
    ) -> AndroidActionRequest:
        return cls(
            FileManagerAction.PROPERTIES,
            context,
            paths=(_android_path(path),),
            use_root_requested=use_root_requested,
        )

    @classmethod
    def install_apk(
        cls,
        context: DeviceContext,
        apk_path: str | Path,
    ) -> AndroidActionRequest:
        path = Path(apk_path).expanduser()
        if path.suffix.casefold() != ".apk":
            raise FileManagerRequestError("Only APK files can be installed")
        return cls(FileManagerAction.INSTALL_APK, context, local_path=path)

    def __post_init__(self) -> None:
        if not isinstance(self.action, FileManagerAction):
            raise FileManagerRequestError("Unknown Android action")
        if not isinstance(self.device_context, DeviceContext):
            raise FileManagerRequestError("Android actions require an immutable DeviceContext")
        if not self.device_context.serial or self.device_context.mode not in {"ADB", "Recovery"}:
            raise FileManagerRequestError("Android file actions require an ADB or Recovery context")
        for path in self.paths:
            if not isinstance(path, str) or _android_path(path) != path:
                raise FileManagerRequestError("Android source paths must be canonical")
        if self.target and (
            not isinstance(self.target, str) or _android_path(self.target) != self.target
        ):
            raise FileManagerRequestError("Android target paths must be canonical")
        if self.action is FileManagerAction.CREATE_FOLDER:
            if self.paths or not self.target or self.local_path is not None:
                raise FileManagerRequestError("Create-folder requires only an Android target")
        elif self.action is FileManagerAction.DELETE:
            if not self.paths or self.target or self.local_path is not None:
                raise FileManagerRequestError("Delete requires Android source paths")
        elif self.action is FileManagerAction.RENAME:
            if len(self.paths) != 1 or not self.target or self.local_path is not None:
                raise FileManagerRequestError("Rename requires one Android source and target")
        elif self.action is FileManagerAction.PROPERTIES:
            if len(self.paths) != 1 or self.target or self.local_path is not None:
                raise FileManagerRequestError("Properties requires one Android path")
        elif self.action is FileManagerAction.INSTALL_APK:
            if self.paths or self.target or self.local_path is None:
                raise FileManagerRequestError("Install requires one local APK path")
            if not isinstance(self.local_path, Path) or self.local_path.suffix.casefold() != ".apk":
                raise FileManagerRequestError("Install requires a local APK Path")


@dataclass(frozen=True, slots=True)
class FileActionItemResult:
    path: str
    success: bool
    message: str


@dataclass(frozen=True, slots=True)
class FileManagerActionResult:
    action: FileManagerAction
    side: FileManagerSide
    items: tuple[FileActionItemResult, ...]
    cancelled: bool = False

    @property
    def success(self) -> bool:
        return bool(self.items) and all(item.success for item in self.items) and not self.cancelled

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(item.message for item in self.items)


@dataclass(frozen=True, slots=True)
class WindowsNavigationSnapshot:
    entries: tuple[str, ...]
    index: int

    @property
    def current(self) -> str:
        if 0 <= self.index < len(self.entries):
            return self.entries[self.index]
        return ""

    @property
    def can_go_back(self) -> bool:
        return self.index > 0

    @property
    def can_go_forward(self) -> bool:
        return 0 <= self.index < len(self.entries) - 1


class WindowsNavigationHistory:
    """Small device-independent browser history for the Windows panel."""

    def __init__(self, initial_path: str | Path | None = None) -> None:
        self._entries: list[str] = []
        self._index = -1
        if initial_path is not None:
            self.push(initial_path)

    @property
    def snapshot(self) -> WindowsNavigationSnapshot:
        return WindowsNavigationSnapshot(tuple(self._entries), self._index)

    def push(self, path: str | Path) -> WindowsNavigationSnapshot:
        normalized = self._identity(path)
        current = self.snapshot.current
        if current and os.path.normcase(current) == os.path.normcase(normalized):
            return self.snapshot
        if self._index < len(self._entries) - 1:
            del self._entries[self._index + 1 :]
        self._entries.append(normalized)
        self._index = len(self._entries) - 1
        return self.snapshot

    def back(self) -> str | None:
        if self._index <= 0:
            return None
        self._index -= 1
        return self._entries[self._index]

    def forward(self) -> str | None:
        if self._index >= len(self._entries) - 1:
            return None
        self._index += 1
        return self._entries[self._index]

    @staticmethod
    def _identity(path: str | Path) -> str:
        candidate = Path(path).expanduser()
        try:
            return str(candidate.resolve(strict=False))
        except OSError:
            return str(candidate.absolute())


BridgeFactory = Callable[..., Any]


class FileManagerActionCoordinator:
    """Execute local or immutable-context File Manager requests without Qt."""

    def __init__(
        self,
        adb: Any,
        device_manager: Any,
        settings: Any = None,
        *,
        bridge_factory: BridgeFactory = ACBridgeClient,
    ) -> None:
        self._adb = adb
        self._device_manager = device_manager
        self._settings = settings
        self._bridge_factory = bridge_factory

    def execute_windows(
        self,
        request: WindowsActionRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> FileManagerActionResult:
        """Execute a local request without touching any Android collaborator."""

        event = cancel_event or threading.Event()
        if event.is_set():
            return self._cancelled_result(request.action, FileManagerSide.WINDOWS)
        if request.action is FileManagerAction.CREATE_FOLDER:
            assert request.target is not None
            try:
                request.target.mkdir(parents=True, exist_ok=False)
                item = self._item(request.target, True, f"{request.target}: created")
            except OSError as exc:
                item = self._item(request.target, False, f"{request.target}: {exc}")
            return self._result(request.action, FileManagerSide.WINDOWS, (item,), event)

        if request.action is FileManagerAction.DELETE:
            items: list[FileActionItemResult] = []
            for path in request.paths:
                if event.is_set():
                    break
                try:
                    self._delete_windows_path(path, event)
                    items.append(self._item(path, True, f"{path}: deleted"))
                except FileManagerActionCancelled:
                    break
                except OSError as exc:
                    items.append(self._item(path, False, f"{path}: {exc}"))
            return self._result(request.action, FileManagerSide.WINDOWS, tuple(items), event)

        if request.action is FileManagerAction.RENAME:
            source = request.paths[0]
            assert request.target is not None
            try:
                source.rename(request.target)
                item = self._item(request.target, True, f"{source}: renamed to {request.target.name}")
            except OSError as exc:
                item = self._item(source, False, f"{source}: {exc}")
            return self._result(request.action, FileManagerSide.WINDOWS, (item,), event)

        if request.action is FileManagerAction.PROPERTIES:
            path = request.paths[0]
            try:
                stat = path.stat()
                kind = "Folder" if path.is_dir() else "File"
                message = (
                    f"Path: {path}\nType: {kind}\nSize: {stat.st_size} bytes\n"
                    f"Modified: {stat.st_mtime}"
                )
                item = self._item(path, True, message)
            except OSError as exc:
                item = self._item(path, False, f"{path}: {exc}")
            return self._result(request.action, FileManagerSide.WINDOWS, (item,), event)

        raise FileManagerRequestError(f"Unsupported Windows action: {request.action.value}")

    @classmethod
    def _delete_windows_path(cls, path: Path, event: threading.Event) -> None:
        """Delete a local tree with cancellation checkpoints between entries."""

        if event.is_set():
            raise FileManagerActionCancelled("File Manager action was cancelled")
        if cls._is_windows_directory_reparse_point(path):
            path.rmdir()
            return
        if path.is_dir() and not path.is_symlink():
            with os.scandir(path) as entries:
                for entry in entries:
                    cls._delete_windows_path(Path(entry.path), event)
            if event.is_set():
                raise FileManagerActionCancelled("File Manager action was cancelled")
            path.rmdir()
            return
        path.unlink()

    @staticmethod
    def _is_windows_directory_reparse_point(path: Path) -> bool:
        """Recognize junctions without requiring ``Path.is_junction`` (3.12+)."""

        try:
            metadata = path.lstat()
        except OSError:
            return False
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return bool(attributes & reparse_flag) and stat.S_ISDIR(metadata.st_mode)

    def execute_android(
        self,
        request: AndroidActionRequest,
        *,
        cancel_event: threading.Event | None = None,
    ) -> FileManagerActionResult:
        """Execute only against the device identity captured in *request*."""

        event = cancel_event or threading.Event()
        self._checkpoint(request.device_context, event)
        adb = self._bound_adb(request.device_context)
        self._checkpoint(request.device_context, event)
        use_root = self._resolve_root(
            request.device_context,
            adb,
            request.use_root_requested,
            event,
        )

        if request.action is FileManagerAction.CREATE_FOLDER:
            result = adb.mkdir(
                request.target,
                use_root=use_root,
                cancel_event=event,
            )
            self._checkpoint(request.device_context, event)
            return self._command_result(request, request.target, result, event)

        if request.action is FileManagerAction.RENAME:
            source = request.paths[0]
            result = adb.rename(
                source,
                request.target,
                use_root=use_root,
                cancel_event=event,
            )
            self._checkpoint(request.device_context, event)
            return self._command_result(request, source, result, event)

        if request.action is FileManagerAction.PROPERTIES:
            path = request.paths[0]
            result = adb.stat(path, use_root=use_root, cancel_event=event)
            self._checkpoint(request.device_context, event)
            return self._command_result(request, path, result, event)

        if request.action is FileManagerAction.INSTALL_APK:
            assert request.local_path is not None
            if not request.local_path.is_file():
                raise FileManagerRequestError(f"APK file is unavailable: {request.local_path}")
            result = adb.install_apk(request.local_path, cancel_event=event)
            self._checkpoint(request.device_context, event)
            return self._command_result(request, str(request.local_path), result, event)

        if request.action is FileManagerAction.DELETE:
            items: list[FileActionItemResult] = []
            bridge = None
            for path in request.paths:
                try:
                    self._checkpoint(request.device_context, event)
                    command = adb.delete(
                        path,
                        recursive=True,
                        use_root=use_root,
                        cancel_event=event,
                    )
                    self._checkpoint(request.device_context, event)
                    if not command.success and is_public_removable_android_path(path):
                        bridge = bridge or self._create_bridge(adb, request.device_context)
                        if bridge is not None:
                            command = self._delete_through_bridge(
                                bridge,
                                request,
                                path,
                                command,
                                use_root,
                                event,
                            )
                            self._checkpoint(request.device_context, event)
                    items.append(self._command_item(path, command))
                except (
                    FileManagerActionCancelled,
                    DeviceContextUnavailable,
                    StaleDeviceContext,
                ):
                    raise
                except Exception as exc:
                    mapped = map_file_manager_error(exc, operation="Delete")
                    items.append(self._item(path, False, mapped.message))
                    break
            return self._result(
                request.action,
                FileManagerSide.ANDROID,
                tuple(items),
                event,
            )

        raise FileManagerRequestError(f"Unsupported Android action: {request.action.value}")

    def _bound_adb(self, context: DeviceContext) -> Any:
        binder = getattr(self._adb, "for_context", None)
        if not callable(binder):
            raise DeviceContextUnavailable(
                "ADB client cannot bind File Manager actions to a device context"
            )
        bound = binder(context)
        if bound is self._adb:
            raise DeviceContextUnavailable(
                "ADB context binding returned the mutable shared client"
            )
        bound_context = getattr(bound, "device_context", None)
        if not isinstance(bound_context, DeviceContext) or bound_context != context:
            raise DeviceContextUnavailable(
                "ADB client did not preserve the captured File Manager context"
            )
        if str(getattr(bound, "serial", "") or "") != context.serial:
            raise DeviceContextUnavailable("ADB client was bound to another device")
        return bound

    def _checkpoint(self, context: DeviceContext, event: threading.Event) -> None:
        if event.is_set():
            raise FileManagerActionCancelled("File Manager action was cancelled")
        require_current = getattr(self._device_manager, "require_current", None)
        if callable(require_current):
            require_current(context)
        else:
            is_current = getattr(self._device_manager, "is_context_current", None)
            if not callable(is_current) or not is_current(context):
                raise DeviceContextUnavailable(
                    "The active device changed during the File Manager action"
                )
        if event.is_set():
            raise FileManagerActionCancelled("File Manager action was cancelled")

    def _resolve_root(
        self,
        context: DeviceContext,
        adb: Any,
        requested: bool,
        event: threading.Event,
    ) -> bool:
        if not requested:
            return False
        self._checkpoint(context, event)
        granted = bool(adb.root_available(cancel_event=event))
        self._checkpoint(context, event)
        return granted

    def _create_bridge(self, adb: Any, context: DeviceContext) -> Any | None:
        if self._settings is None:
            return None
        return self._bridge_factory(
            adb,
            self._settings,
            temp_folder=context.temp_path,
        )

    def _delete_through_bridge(
        self,
        bridge: Any,
        request: AndroidActionRequest,
        path: str,
        adb_result: Any,
        use_root: bool,
        event: threading.Event,
    ) -> Any:
        bridge_result = bridge.delete_path(
            path,
            recursive=True,
            use_root=use_root,
            timeout=150,
            cancel_event=event,
        )
        self._checkpoint(request.device_context, event)
        if (
            not bridge_result.success
            and request.allow_storage_grant
            and self._bridge_needs_storage_grant(bridge_result)
        ):
            grant = bridge.grant_storage_access(
                path,
                timeout=600,
                cancel_event=event,
            )
            self._checkpoint(request.device_context, event)
            if grant.success:
                bridge_result = bridge.delete_path(
                    path,
                    recursive=True,
                    use_root=use_root,
                    timeout=150,
                    cancel_event=event,
                )
                self._checkpoint(request.device_context, event)
            else:
                grant_message = self._status_text(grant, "Storage access was not granted")
                bridge_message = self._status_text(bridge_result, "ACBridge delete failed")
                return self._failed_command_like(
                    adb_result,
                    f"{bridge_message}\nStorage permission request: {grant_message}",
                )
        if bridge_result.success:
            return bridge_result
        adb_message = self._status_text(adb_result, "ADB delete failed")
        bridge_message = self._status_text(bridge_result, "ACBridge delete failed")
        return self._failed_command_like(
            adb_result,
            f"{adb_message}\nACBridge fallback: {bridge_message}",
        )

    @staticmethod
    def _bridge_needs_storage_grant(result: Any) -> bool:
        text = "\n".join(
            str(getattr(result, field, "") or "")
            for field in ("status", "stderr", "stdout")
        ).casefold()
        return "saf_permission_required" in text or "grant microsd/usb access" in text

    @staticmethod
    def _status_text(result: Any, fallback: str) -> str:
        fields = (
            ("stdout", "status", "stderr")
            if bool(getattr(result, "success", False))
            else ("stderr", "stdout", "status")
        )
        return str(
            next(
                (
                    value
                    for field in fields
                    if (value := getattr(result, field, ""))
                ),
                fallback,
            )
        ).strip()

    @classmethod
    def _failed_command_like(cls, result: Any, message: str) -> Any:
        # CommandResult is mutable, but some tests and plugins provide immutable
        # command-like objects.  Prefer a small proxy instead of mutating either.
        class FailedResult:
            success = False
            status = message
            stderr = message
            stdout = ""

        return FailedResult()

    @classmethod
    def _command_item(cls, path: str, result: Any) -> FileActionItemResult:
        success = bool(getattr(result, "success", False))
        status = cls._status_text(result, "Command finished without a status")
        return FileActionItemResult(
            path=path,
            success=success,
            message=f"{path}: {status}" if path not in status else status,
        )

    @classmethod
    def _command_result(
        cls,
        request: AndroidActionRequest,
        path: str,
        result: Any,
        event: threading.Event,
    ) -> FileManagerActionResult:
        return cls._result(
            request.action,
            FileManagerSide.ANDROID,
            (cls._command_item(path, result),),
            event,
        )

    @staticmethod
    def _item(path: str | Path, success: bool, message: str) -> FileActionItemResult:
        return FileActionItemResult(str(path), success, message)

    @staticmethod
    def _result(
        action: FileManagerAction,
        side: FileManagerSide,
        items: tuple[FileActionItemResult, ...],
        event: threading.Event,
    ) -> FileManagerActionResult:
        return FileManagerActionResult(action, side, items, cancelled=event.is_set())

    @staticmethod
    def _cancelled_result(
        action: FileManagerAction,
        side: FileManagerSide,
    ) -> FileManagerActionResult:
        return FileManagerActionResult(action, side, (), cancelled=True)
