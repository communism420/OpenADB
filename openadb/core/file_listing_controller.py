"""UI-independent Android and Windows file listing orchestration."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .device_context import DeviceContext, DeviceContextUnavailable, StaleDeviceContext
from .file_manager_errors import TransferCancelled
from .file_manager_state import normalize_android_path


class StaleFileListing(StaleDeviceContext):
    """Raised when a late listing no longer belongs to the visible folder."""


@dataclass(frozen=True, slots=True)
class AndroidListingRequest:
    """Immutable identity and options for one Android directory listing."""

    device_context: DeviceContext
    generation: int
    requested_path: str
    use_root: bool = False


@dataclass(frozen=True, slots=True)
class PreparedAndroidListing:
    """A listing request bound to its captured ADB transport."""

    request: AndroidListingRequest
    adb: Any = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class AndroidListingResult:
    request: AndroidListingRequest
    items: tuple[Any, ...]
    storage_items: tuple[tuple[str, object], ...]

    @property
    def storage(self) -> Mapping[str, object]:
        return MappingProxyType(dict(self.storage_items))


@dataclass(frozen=True, slots=True)
class StorageVolumesRequest:
    """Immutable identity for a storage-volume enumeration."""

    device_context: DeviceContext
    generation: int
    use_root: bool = False


@dataclass(frozen=True, slots=True)
class PreparedStorageVolumes:
    request: StorageVolumesRequest
    adb: Any = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class StorageVolumesResult:
    request: StorageVolumesRequest
    volumes: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class WindowsFileEntry:
    path: str
    name: str
    is_dir: bool
    size: int | None
    modified_time: float | None


@dataclass(frozen=True, slots=True)
class WindowsListingResult:
    requested_path: str
    entries: tuple[WindowsFileEntry, ...]


class FileListingController:
    """Coordinate listings while keeping device identity out of the UI.

    Android work starts with :meth:`begin_android_listing` or
    :meth:`begin_storage_volumes`. Each method captures and binds exactly one
    device context. Windows methods deliberately do not access ADB or a device
    manager, so they remain usable in a no-device state.
    """

    ALLOWED_ANDROID_MODES = frozenset({"ADB", "Recovery"})

    def __init__(
        self,
        adb=None,
        device_manager=None,
        *,
        android_path: str = "/sdcard/",
    ) -> None:
        self.adb = adb
        self.device_manager = device_manager
        self._lock = threading.RLock()
        self._android_path = normalize_android_path(android_path)
        self._listing_generation = 0
        self._storage_generation = 0

    @property
    def requested_android_path(self) -> str:
        with self._lock:
            return self._android_path

    @property
    def listing_generation(self) -> int:
        with self._lock:
            return self._listing_generation

    @property
    def storage_generation(self) -> int:
        with self._lock:
            return self._storage_generation

    def set_android_path(self, path: str) -> str:
        """Change the visible path and invalidate any earlier listing."""

        normalized = normalize_android_path(path)
        with self._lock:
            if normalized != self._android_path:
                self._android_path = normalized
                self._listing_generation += 1
        return normalized

    def invalidate_android(self) -> None:
        """Invalidate Android folder and volume work after a target change."""

        with self._lock:
            self._listing_generation += 1
            self._storage_generation += 1

    def begin_android_listing(
        self,
        path: str | None = None,
        *,
        use_root: bool = False,
    ) -> PreparedAndroidListing:
        """Capture target/path identity and bind ADB before a worker starts."""

        requested_path = normalize_android_path(
            self.requested_android_path if path is None else path
        )
        with self._lock:
            self._android_path = requested_path
            self._listing_generation += 1
            generation = self._listing_generation
        context, bound_adb = self._capture_bound_context()
        request = AndroidListingRequest(
            device_context=context,
            generation=generation,
            requested_path=requested_path,
            use_root=bool(use_root),
        )
        self.require_listing_current(request)
        return PreparedAndroidListing(request=request, adb=bound_adb)

    def load_android_listing(
        self,
        prepared: PreparedAndroidListing,
        *,
        cancel_event=None,
    ) -> AndroidListingResult:
        """Run a captured Android listing and reject mid-flight changes."""

        request = prepared.request
        self._raise_if_cancelled(cancel_event, "Android folder refresh was cancelled")
        self.require_listing_current(request)
        self._require_bound_adb(request.device_context, prepared.adb)
        items = prepared.adb.list_files(
            request.requested_path,
            use_root=request.use_root,
            cancel_event=cancel_event,
        )
        self._raise_if_cancelled(cancel_event, "Android folder refresh was cancelled")
        self.require_listing_current(request)
        self._require_bound_adb(request.device_context, prepared.adb)
        storage = prepared.adb.storage_info(
            request.requested_path,
            use_root=request.use_root,
            cancel_event=cancel_event,
        )
        self._raise_if_cancelled(cancel_event, "Android folder refresh was cancelled")
        self.require_listing_current(request)
        self._require_bound_adb(request.device_context, prepared.adb)
        storage_items = tuple(dict(storage or {}).items())
        return AndroidListingResult(
            request=request,
            items=tuple(items or ()),
            storage_items=storage_items,
        )

    def accept_android_listing(
        self,
        result: AndroidListingResult,
    ) -> AndroidListingResult:
        """Validate a result immediately before applying it to widgets."""

        self.require_listing_current(result.request)
        return result

    def is_listing_current(self, request: AndroidListingRequest) -> bool:
        try:
            self.require_listing_current(request)
        except (StaleFileListing, DeviceContextUnavailable):
            return False
        return True

    def require_listing_current(self, request: AndroidListingRequest) -> None:
        with self._lock:
            generation_matches = request.generation == self._listing_generation
            path_matches = (
                normalize_android_path(request.requested_path) == self._android_path
            )
        if not generation_matches or not path_matches:
            raise StaleFileListing(
                "The Android folder changed while its file listing was loading"
            )
        self._require_context_current(request.device_context)

    def begin_storage_volumes(
        self,
        *,
        use_root: bool = False,
    ) -> PreparedStorageVolumes:
        with self._lock:
            self._storage_generation += 1
            generation = self._storage_generation
        context, bound_adb = self._capture_bound_context()
        request = StorageVolumesRequest(
            device_context=context,
            generation=generation,
            use_root=bool(use_root),
        )
        self.require_storage_current(request)
        return PreparedStorageVolumes(request=request, adb=bound_adb)

    def load_storage_volumes(
        self,
        prepared: PreparedStorageVolumes,
        *,
        cancel_event=None,
    ) -> StorageVolumesResult:
        request = prepared.request
        self._raise_if_cancelled(cancel_event, "Android storage refresh was cancelled")
        self.require_storage_current(request)
        self._require_bound_adb(request.device_context, prepared.adb)
        volumes = prepared.adb.storage_volumes(
            use_root=request.use_root,
            cancel_event=cancel_event,
        )
        self._raise_if_cancelled(cancel_event, "Android storage refresh was cancelled")
        self.require_storage_current(request)
        self._require_bound_adb(request.device_context, prepared.adb)
        return StorageVolumesResult(request=request, volumes=tuple(volumes or ()))

    def accept_storage_volumes(
        self,
        result: StorageVolumesResult,
    ) -> StorageVolumesResult:
        self.require_storage_current(result.request)
        return result

    def is_storage_current(self, request: StorageVolumesRequest) -> bool:
        try:
            self.require_storage_current(request)
        except (StaleFileListing, DeviceContextUnavailable):
            return False
        return True

    def require_storage_current(self, request: StorageVolumesRequest) -> None:
        with self._lock:
            generation_matches = request.generation == self._storage_generation
        if not generation_matches:
            raise StaleFileListing(
                "A newer Android storage-volume request replaced this result"
            )
        self._require_context_current(request.device_context)

    @staticmethod
    def navigate_windows(path: str | Path) -> str:
        """Validate and normalize a Windows directory without Android state."""

        target = Path(path).expanduser()
        if not target.exists():
            raise FileNotFoundError(f"Folder does not exist: {target}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a folder: {target}")
        return str(target.resolve())

    @classmethod
    def list_windows(cls, path: str | Path) -> WindowsListingResult:
        """List a local directory without touching ADB or DeviceManager."""

        requested_path = cls.navigate_windows(path)
        entries: list[WindowsFileEntry] = []
        with os.scandir(requested_path) as iterator:
            for item in iterator:
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                    stat = item.stat(follow_symlinks=False)
                    size = None if is_dir else stat.st_size
                    modified_time = stat.st_mtime
                except OSError:
                    try:
                        is_dir = item.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    size = None
                    modified_time = None
                entries.append(
                    WindowsFileEntry(
                        path=item.path,
                        name=item.name,
                        is_dir=is_dir,
                        size=size,
                        modified_time=modified_time,
                    )
                )
        entries.sort(key=lambda entry: (not entry.is_dir, entry.name.casefold()))
        return WindowsListingResult(
            requested_path=requested_path,
            entries=tuple(entries),
        )

    def _capture_bound_context(self) -> tuple[DeviceContext, Any]:
        if self.device_manager is None or self.adb is None:
            raise DeviceContextUnavailable(
                "Connect an authorized ADB or Recovery device to browse Android files"
            )
        context = self.device_manager.require_context(self.ALLOWED_ANDROID_MODES)
        self._require_context_current(context)
        binder = getattr(self.adb, "for_context", None)
        if not callable(binder):
            raise DeviceContextUnavailable(
                "ADB cannot bind file listings to the captured device context"
            )
        bound_adb = binder(context)
        self._require_bound_adb(context, bound_adb)
        self._require_context_current(context)
        return context, bound_adb

    def _require_bound_adb(self, context: DeviceContext, bound_adb: Any) -> None:
        """Fail closed if a prepared facade lost its immutable target binding."""

        if bound_adb is self.adb:
            raise DeviceContextUnavailable(
                "ADB context binding returned the mutable shared client"
            )
        bound_context = getattr(bound_adb, "device_context", None)
        if not isinstance(bound_context, DeviceContext) or bound_context != context:
            raise DeviceContextUnavailable(
                "ADB did not preserve the captured file-listing context"
            )
        if str(getattr(bound_adb, "serial", "") or "") != context.serial:
            raise DeviceContextUnavailable(
                "ADB file listing was bound to another device"
            )

    def _require_context_current(self, context: DeviceContext) -> None:
        manager = self.device_manager
        if manager is None or not manager.is_context_current(context):
            raise StaleFileListing(
                "The active Android device or profile changed while files were loading"
            )

    @staticmethod
    def _raise_if_cancelled(cancel_event, message: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise TransferCancelled(message)
