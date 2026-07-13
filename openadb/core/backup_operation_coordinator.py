"""Backup workflows that do not belong in Qt pages.

The coordinator deliberately distinguishes a local profile snapshot from an
immutable device target.  Local inspection and deletion therefore keep
working without Android, while install and backup operations can only use the
ADB client bound to the captured :class:`DeviceContext`.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openadb.models.app_info import AppInfo
from openadb.models.backup_info import BackupInfo
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo

from .adb import ADBClient
from .backup_manager import BackupManager
from .device import DeviceManager
from .device_context import DeviceContext, DeviceContextUnavailable, StaleDeviceContext


@dataclass(frozen=True, slots=True)
class BackupProfileContext:
    """Filesystem locations captured for one device profile."""

    config_dir: Path
    backups_folder: Path
    temp_folder: Path
    logs_folder: Path


@dataclass(frozen=True, slots=True)
class BoundBackupOperation:
    """Dependencies bound to exactly one immutable Android target."""

    context: DeviceContext
    profile: BackupProfileContext
    adb: Any
    manager: Any


BackupManagerFactory = Callable[[BackupProfileContext], Any]


class BackupOperationCoordinator:
    """Execute local and device-bound backup workflows without Qt dependencies."""

    def __init__(
        self,
        backup_manager: BackupManager,
        adb: ADBClient,
        device_manager: DeviceManager,
    ) -> None:
        self.backup_manager = backup_manager
        self.adb = adb
        self.device_manager = device_manager

    def capture_local_profile(self, root: Path | None = None) -> BackupProfileContext:
        """Capture current local paths without requiring a connected device."""

        settings = getattr(self.backup_manager, "settings", None) or getattr(
            self.device_manager,
            "settings",
            None,
        )
        configured_root = getattr(settings, "backups_folder", None)
        if root is None:
            if isinstance(configured_root, (str, Path)):
                root = Path(configured_root)
            else:
                root = Path(getattr(self.backup_manager, "root", Path.cwd() / "backups"))
        profile_path = getattr(settings, "config_dir", root.parent)
        if not isinstance(profile_path, (str, Path)):
            profile_path = root.parent

        def configured_path(name: str, fallback: Path) -> Path:
            value = getattr(settings, name, None)
            return Path(value) if isinstance(value, (str, Path)) else fallback

        profile_path = Path(profile_path)
        return BackupProfileContext(
            config_dir=profile_path,
            backups_folder=Path(root),
            temp_folder=configured_path("temp_folder", profile_path / "temp"),
            logs_folder=configured_path("logs_folder", profile_path / "logs"),
        )

    @staticmethod
    def profile_for_device(context: DeviceContext) -> BackupProfileContext:
        return BackupProfileContext(
            config_dir=context.profile_path,
            backups_folder=context.backups_path,
            temp_folder=context.temp_path,
            logs_folder=context.logs_path,
        )

    @staticmethod
    def path_identity(path: Path) -> str:
        try:
            return str(path.resolve(strict=False)).casefold()
        except OSError:
            return str(path.absolute()).casefold()

    @classmethod
    def backup_belongs_to_profile(
        cls,
        backup: BackupInfo,
        profile: BackupProfileContext,
    ) -> bool:
        try:
            backup.path.resolve(strict=False).relative_to(
                profile.backups_folder.resolve(strict=False)
            )
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def is_profile_current(self, profile: BackupProfileContext) -> bool:
        current = self.capture_local_profile()
        return self.path_identity(current.backups_folder) == self.path_identity(
            profile.backups_folder
        )

    def manager_for_profile(self, profile: BackupProfileContext) -> Any:
        """Return a manager pinned to captured paths, not mutable settings."""

        if isinstance(self.backup_manager, BackupManager):
            return BackupManager(profile)  # type: ignore[arg-type]
        return self.backup_manager

    def scan_backups(
        self,
        profile: BackupProfileContext,
        *,
        cancel_event: threading.Event | None = None,
        manager_factory: BackupManagerFactory | None = None,
    ) -> list[BackupInfo]:
        if cancel_event is not None and cancel_event.is_set():
            return []
        manager = (manager_factory or self.manager_for_profile)(profile)
        return manager.scan_backups(cancel_event=cancel_event)

    def delete_local_backup(
        self,
        profile: BackupProfileContext,
        backup: BackupInfo,
        *,
        cancel_event: threading.Event | None = None,
        manager_factory: BackupManagerFactory | None = None,
    ) -> bool:
        """Delete a profile-owned folder without consulting device state."""

        if cancel_event is not None and cancel_event.is_set():
            return False
        self._require_backup_in_profile(backup, profile)
        if not self.is_profile_current(profile):
            raise StaleDeviceContext("The backup profile changed before local deletion")
        manager = (manager_factory or self.manager_for_profile)(profile)
        if cancel_event is not None and cancel_event.is_set():
            return False
        if not self.is_profile_current(profile):
            raise StaleDeviceContext("The backup profile changed before local deletion")
        manager.delete_backup(backup)
        return True

    def metadata_text(
        self,
        profile: BackupProfileContext,
        backup: BackupInfo,
    ) -> str:
        """Read and format local metadata without any UI or device dependency."""

        self._require_backup_in_profile(backup, profile)
        metadata_path = backup.path / "metadata.json"
        raw = metadata_path.read_text(encoding="utf-8", errors="replace")
        try:
            metadata = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        return json.dumps(metadata, indent=2, ensure_ascii=False)

    def folder_to_open(
        self,
        profile: BackupProfileContext,
        backup: BackupInfo | None = None,
    ) -> Path:
        if backup is None:
            return profile.backups_folder
        self._require_backup_in_profile(backup, profile)
        return backup.path

    def capture_device_operation(
        self,
        allowed_modes: Iterable[str] = ("ADB", "Recovery"),
        *,
        manager_factory: BackupManagerFactory | None = None,
    ) -> BoundBackupOperation:
        """Capture context and bind all mutable collaborators to that target."""

        context = self._require_device_context(allowed_modes)
        profile = self.profile_for_device(context)
        adb = self._bound_adb(context)
        manager = (manager_factory or self.manager_for_profile)(profile)
        self.require_current(context)
        return BoundBackupOperation(
            context=context,
            profile=profile,
            adb=adb,
            manager=manager,
        )

    def is_context_current(self, context: DeviceContext) -> bool:
        is_current = getattr(self.device_manager, "is_context_current", None)
        if callable(is_current):
            return bool(is_current(context))
        try:
            current = self._require_device_context({context.mode})
        except (DeviceContextUnavailable, RuntimeError):
            return False
        return current == context

    def require_current(self, context: DeviceContext) -> None:
        require_current = getattr(self.device_manager, "require_current", None)
        if callable(require_current):
            require_current(context)
            return
        if not self.is_context_current(context):
            raise StaleDeviceContext("The active device or profile changed")

    def restore_backup(
        self,
        operation: BoundBackupOperation,
        backup: BackupInfo,
        *,
        prefer_install_existing: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        self._require_backup_in_profile(backup, operation.profile)
        if cancel_event is None or not cancel_event.is_set():
            self.require_current(operation.context)
        return operation.manager.restore_backup(
            backup,
            operation.adb,
            prefer_install_existing,
            cancel_event=cancel_event,
        )

    def install_backup(
        self,
        operation: BoundBackupOperation,
        backup: BackupInfo,
        *,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return self.restore_backup(
            operation,
            backup,
            prefer_install_existing=False,
            cancel_event=cancel_event,
        )

    def install_existing(
        self,
        operation: BoundBackupOperation,
        backup: BackupInfo,
        *,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return self.restore_backup(
            operation,
            backup,
            prefer_install_existing=True,
            cancel_event=cancel_event,
        )

    def create_backup(
        self,
        operation: BoundBackupOperation,
        app: AppInfo,
        device: DeviceInfo,
        uninstall_method: str,
        *,
        icon_path: str = "",
        use_root: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, BackupInfo | None, str]:
        """Create a backup using only the captured target and profile manager."""

        if device.serial != operation.context.serial:
            raise StaleDeviceContext("Backup device metadata does not match the captured target")
        if cancel_event is None or not cancel_event.is_set():
            self.require_current(operation.context)
        return operation.manager.create_backup(
            app,
            operation.adb,
            device,
            uninstall_method,
            icon_path=icon_path,
            use_root=use_root,
            cancel_event=cancel_event,
        )

    def _require_device_context(self, allowed_modes: Iterable[str]) -> DeviceContext:
        modes = {str(mode) for mode in allowed_modes}
        require_context = getattr(self.device_manager, "require_context", None)
        if not callable(require_context):
            raise DeviceContextUnavailable(
                "Device-bound backup operations require an immutable device context"
            )
        context = require_context(modes)
        if not isinstance(context, DeviceContext):
            raise DeviceContextUnavailable(
                "Device manager did not provide an immutable device context"
            )
        if not context.serial or context.mode not in modes:
            expected = ", ".join(sorted(modes)) or "an authorized device mode"
            raise DeviceContextUnavailable(
                f"Current device context is not valid for backup operations; expected {expected}"
            )
        return context

    def _bound_adb(self, context: DeviceContext) -> Any:
        for_context = getattr(self.adb, "for_context", None)
        if not callable(for_context):
            raise DeviceContextUnavailable(
                "ADB client cannot bind backup operations to an immutable device context"
            )
        bound = for_context(context)
        if bound is self.adb:
            raise DeviceContextUnavailable(
                "ADB context binding returned the mutable shared client"
            )
        bound_context = getattr(bound, "device_context", None)
        if not isinstance(bound_context, DeviceContext) or bound_context != context:
            raise DeviceContextUnavailable(
                "ADB client did not preserve the complete captured device identity"
            )
        if str(getattr(bound, "serial", "") or "") != context.serial:
            raise DeviceContextUnavailable(
                "ADB client was bound to a different device serial"
            )
        return bound

    @classmethod
    def _require_backup_in_profile(
        cls,
        backup: BackupInfo,
        profile: BackupProfileContext,
    ) -> None:
        if not cls.backup_belongs_to_profile(backup, profile):
            raise DeviceContextUnavailable(
                "The selected backup belongs to another device profile. "
                "Refresh backups before using it."
            )
