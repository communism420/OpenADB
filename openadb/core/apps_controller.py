"""State and immutable-context services for the Applications page."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from openadb.models.device_info import DeviceInfo

from .adb import ADBClient
from .apk_metadata import APKMetadataExtractor
from .app_cache import AppInfoCache
from .backup_manager import BackupManager
from .device_context import DeviceContext, DeviceContextUnavailable, StaleDeviceContext
from .icon_extractor import IconExtractor
from .operations import OperationRegistry, OperationToken
from .path_utils import safe_filename


@dataclass(frozen=True, slots=True)
class CapturedProfileSettings:
    """Minimal settings view permanently attached to one device profile."""

    config_dir: Path
    backups_folder: Path
    temp_folder: Path
    logs_folder: Path


@dataclass(frozen=True, slots=True)
class AppsProfileServices:
    settings: CapturedProfileSettings
    app_cache: AppInfoCache
    apk_metadata: APKMetadataExtractor
    icon_extractor: IconExtractor
    include_system: bool


@dataclass(slots=True)
class AppsViewIdentity:
    serial: str = ""
    profile_path: Path = Path()
    context: DeviceContext | None = None


class AppsController:
    """Own Applications state that is independent of concrete Qt widgets."""

    OWNER_KEYS = ("apps.list", "apps.metadata", "apps.assets", "apps.bulk")

    def __init__(
        self,
        adb: ADBClient | None,
        device_manager,
        settings,
        operations: OperationRegistry | None = None,
    ) -> None:
        self.adb = adb
        self.device_manager = device_manager
        self.settings = settings
        manager_operations = getattr(device_manager, "operations", None)
        self.operations = (
            operations
            or (
                manager_operations
                if isinstance(manager_operations, OperationRegistry)
                else OperationRegistry()
            )
        )
        self.view = AppsViewIdentity(
            profile_path=Path(getattr(settings, "config_dir", Path.cwd()))
        )

    @staticmethod
    def captured_settings(context: DeviceContext) -> CapturedProfileSettings:
        return CapturedProfileSettings(
            config_dir=context.profile_path,
            backups_folder=context.backups_path,
            temp_folder=context.temp_path,
            logs_folder=context.logs_path,
        )

    def profile_services(
        self,
        context: DeviceContext,
        include_system: bool | None = None,
    ) -> AppsProfileServices:
        if include_system is None:
            # A default may only be read while the captured profile is still
            # current. Revalidate after the settings read as it may cross a
            # profile switch in lightweight integrations without an atomic
            # settings snapshot API.
            self.require_current(context)
            include_system = bool(self.settings.get("show_system_apps", True))
            self.require_current(context)
        captured = self.captured_settings(context)
        return AppsProfileServices(
            settings=captured,
            app_cache=AppInfoCache(captured),  # type: ignore[arg-type]
            apk_metadata=APKMetadataExtractor(captured),  # type: ignore[arg-type]
            icon_extractor=IconExtractor(captured),  # type: ignore[arg-type]
            include_system=bool(include_system),
        )

    def backup_manager(self, context: DeviceContext) -> BackupManager:
        return BackupManager(self.captured_settings(context))  # type: ignore[arg-type]

    def require_context(self) -> DeviceContext:
        require_context = getattr(self.device_manager, "require_context", None)
        if callable(require_context):
            context = require_context({"ADB", "Recovery"})
            if isinstance(context, DeviceContext):
                return context

        # Lightweight test doubles may not implement DeviceManager's context
        # API. Production never reaches this compatibility branch.
        context = self._fallback_context()
        if context is None or context.mode not in {"ADB", "Recovery"}:
            raise DeviceContextUnavailable(
                "An authorized ADB or Recovery device is required"
            )
        return context

    def bound_adb(self, context: DeviceContext):
        for_context = getattr(self.adb, "for_context", None)
        if callable(for_context):
            self.require_current(context)
            bound = for_context(context)
            self.require_current(context)
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
        raise DeviceContextUnavailable(
            "ADB client does not support immutable device-context binding"
        )

    def is_current(self, context: DeviceContext) -> bool:
        is_current = getattr(self.device_manager, "is_context_current", None)
        if callable(is_current):
            return bool(is_current(context))
        fallback = self._fallback_context()
        return fallback is not None and fallback == context

    def require_current(self, context: DeviceContext) -> None:
        require_current = getattr(self.device_manager, "require_current", None)
        if callable(require_current):
            require_current(context)
            return
        if not self.is_current(context):
            raise StaleDeviceContext("The active device or profile changed")

    def can_apply(self, token: OperationToken, context: DeviceContext) -> bool:
        return (
            self.operations.contains(token)
            and not token.cancelled
            and self.is_current(context)
        )

    def register_operation(
        self,
        context: DeviceContext,
        suffix: str,
        conflict: str,
        *,
        additional_conflicts: tuple[str, ...] = (),
    ) -> OperationToken:
        token = self.operations.register(
            f"apps.{suffix}",
            device_context=context,
            conflict_group=f"{conflict}:{context.serial}",
            conflict_groups=additional_conflicts,
        )
        if not self.is_current(context):
            token.cancel("device context changed during operation registration")
            self.operations.finish(token)
            raise StaleDeviceContext(
                "The active device changed before the application operation could start"
            )
        return token

    def device_snapshot(self, context: DeviceContext) -> DeviceInfo:
        self.require_current(context)
        active_snapshot = getattr(self.device_manager, "active_snapshot", None)
        if callable(active_snapshot):
            active, generation = active_snapshot()
            if int(generation) != context.generation:
                raise StaleDeviceContext(
                    "The active device changed while its details were captured"
                )
        else:
            before = self._fallback_context()
            if before != context:
                raise StaleDeviceContext(
                    "The active device changed before its details were captured"
                )
            active = getattr(self.device_manager, "active", DeviceInfo())

        defaults = DeviceInfo()
        values = {
            field.name: getattr(active, field.name, getattr(defaults, field.name))
            for field in fields(DeviceInfo)
        }
        snapshot = DeviceInfo(**values)

        if (
            snapshot.serial != context.serial
            or snapshot.mode != context.mode
            or snapshot.transport_id != context.transport_id
        ):
            raise StaleDeviceContext(
                "The captured device details do not match the immutable target"
            )
        self.require_current(context)
        return snapshot

    def _fallback_context(self) -> DeviceContext | None:
        """Build a complete legacy identity or fail closed when it is invalid."""

        active = getattr(self.device_manager, "active", None)
        raw_serial = getattr(active, "serial", "")
        raw_mode = getattr(active, "mode", "No device")
        serial = raw_serial if isinstance(raw_serial, str) else ""
        mode = raw_mode if isinstance(raw_mode, str) else "No device"
        if not serial:
            return None

        raw_generation = getattr(self.device_manager, "current_generation", None)
        if raw_generation is None:
            return None
        try:
            generation = int(raw_generation)
        except (TypeError, ValueError):
            return None

        profile_serial = str(
            getattr(self.settings, "active_profile_serial", "") or ""
        ).strip()
        if profile_serial and profile_serial != serial:
            return None
        try:
            profile_path = Path(
                getattr(self.settings, "config_dir", Path.cwd())
            ).expanduser()
        except (TypeError, ValueError, OSError):
            return None

        def profile_path_for(key: str, fallback: str) -> Path | None:
            value = str(self.settings.get(key, "") or "").strip()
            try:
                return Path(value).expanduser() if value else profile_path / fallback
            except (TypeError, ValueError, OSError):
                return None

        backups_path = profile_path_for("backups_folder", "backups")
        temp_path = profile_path_for("temp_folder", "temp")
        logs_path = profile_path_for("logs_folder", "logs")
        if backups_path is None or temp_path is None or logs_path is None:
            return None

        return DeviceContext(
            serial=serial,
            mode=mode,
            transport_id=str(getattr(active, "transport_id", "") or ""),
            profile_key=safe_filename(profile_serial or serial),
            profile_kind=str(
                getattr(self.settings, "active_profile_kind", "") or "Phone"
            ),
            profile_path=profile_path,
            backups_path=backups_path,
            temp_path=temp_path,
            logs_path=logs_path,
            generation=generation,
        )

    def set_view_identity(
        self,
        serial: str,
        context: DeviceContext | None = None,
    ) -> None:
        self.view = AppsViewIdentity(
            serial=str(serial or ""),
            profile_path=(
                context.profile_path
                if context is not None
                else Path(getattr(self.settings, "config_dir", Path.cwd()))
            ),
            context=context,
        )

    def view_matches(self, context: DeviceContext) -> bool:
        if (
            self.view.serial != context.serial
            or self.view.profile_path != context.profile_path
        ):
            return False
        return self.view.context is None or (
            self.view.context == context and self.is_current(self.view.context)
        )

    def cancel_profile_operations(self, reason: str) -> None:
        for owner_key in self.OWNER_KEYS:
            self.operations.cancel_owner(owner_key, reason)
        self.set_view_identity("")
