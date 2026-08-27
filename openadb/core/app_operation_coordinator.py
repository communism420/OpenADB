"""Device-bound application workflows without GUI dependencies."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from openadb.models.app_info import AppInfo
from openadb.models.device_info import DeviceInfo

from .device_context import DeviceContext
from .privilege import PrivilegeBackend

if TYPE_CHECKING:
    from .privilege import PrivilegeManager


class AppOperationCoordinator:
    """Run one bulk workflow against one immutable Android target.

    The page owns confirmation and presentation. This coordinator owns the
    ordered device work and revalidates the captured context before every
    device-changing step, including the destructive half of backup+uninstall.
    """

    def __init__(
        self,
        *,
        context: DeviceContext,
        adb,
        backup_manager,
        device: DeviceInfo,
        cancel_event: threading.Event,
        require_current: Callable[[DeviceContext], None],
        root_enabled: bool = False,
        privilege_manager: PrivilegeManager | None = None,
        privilege_lease=None,
    ) -> None:
        self.context = context
        self.adb = adb
        self.backup_manager = backup_manager
        self.device = device
        self.cancel_event = cancel_event
        self.require_current = require_current
        self.root_enabled = bool(root_enabled)
        self.privilege_manager = privilege_manager
        capture_lease = getattr(privilege_manager, "capture_operation_lease", None)
        self.privilege_lease = (
            privilege_lease
            if privilege_lease is not None
            else (capture_lease() if callable(capture_lease) else None)
        )
        self._prepared_adb = None

    def backup(self, apps: Iterable[AppInfo]) -> list[str]:
        messages: list[str] = []
        if not self._continue():
            return messages
        adb = self._prepare_workflow_adb()
        use_root = self._resolve_root(adb)
        if use_root:
            messages.append(
                "Root mode: APK backups use su/root streaming when normal adb pull is blocked."
            )
        for app in tuple(apps):
            if not self._continue():
                break
            ok, _backup, message = self._create_backup(
                app,
                adb=adb,
                use_root=use_root,
            )
            if self.cancel_event.is_set():
                break
            messages.append(
                f"{app.package_name}: {'OK' if ok else 'FAILED'} - {message}"
            )
        return messages

    def uninstall(
        self,
        apps: Iterable[AppInfo],
        *,
        require_backup: bool,
    ) -> list[str]:
        messages: list[str] = []
        if not self._continue():
            return messages
        adb = self._prepare_workflow_adb()
        use_root = self._resolve_root(adb)
        for app in tuple(apps):
            if not self._continue():
                break
            if require_backup:
                ok, _backup, message = self._create_backup(
                    app,
                    adb=adb,
                    use_root=use_root,
                )
                if self.cancel_event.is_set():
                    break
                if not ok:
                    messages.append(
                        f"{app.package_name}: skipped, backup failed - {message}"
                    )
                    continue

            # The safety backup and uninstall are one immutable-context
            # transaction. A switch at this boundary must stop destruction.
            if not self._continue():
                break
            result = adb.uninstall_package(
                app.package_name,
                system_app=app.is_system,
                use_root=use_root,
                cancel_event=self.cancel_event,
            )
            if self.cancel_event.is_set():
                break
            messages.append(f"{app.package_name}: {result.status}")
        return messages

    def set_enabled(
        self,
        apps: Iterable[AppInfo],
        *,
        enabled: bool,
    ) -> list[str]:
        messages: list[str] = []
        if not self._continue():
            return messages
        adb = self._prepare_workflow_adb()
        for app in tuple(apps):
            if not self._continue():
                break
            result = (
                adb.enable_package(
                    app.package_name,
                    cancel_event=self.cancel_event,
                )
                if enabled
                else adb.disable_package(
                    app.package_name,
                    cancel_event=self.cancel_event,
                )
            )
            if self.cancel_event.is_set():
                break
            messages.append(f"{app.package_name}: {result.status}")
        return messages

    def install_existing(self, apps: Iterable[AppInfo]) -> list[str]:
        messages: list[str] = []
        if not self._continue():
            return messages
        adb = self._prepare_workflow_adb()
        for app in tuple(apps):
            if not self._continue():
                break
            result = adb.restore_existing_package(
                app.package_name,
                cancel_event=self.cancel_event,
            )
            if self.cancel_event.is_set():
                break
            messages.append(f"{app.package_name}: {result.status}")
        return messages

    def _continue(self) -> bool:
        if self.cancel_event.is_set():
            return False
        self.require_current(self.context)
        return not self.cancel_event.is_set()

    def _prepare_workflow_adb(self):
        if self._prepared_adb is not None:
            return self._prepared_adb
        if not self._continue():
            return self.adb
        if self.privilege_manager is None:
            prepared = self.adb
        else:
            prepare_kwargs = {"cancel_event": self.cancel_event}
            if self.privilege_lease is not None:
                prepare_kwargs["privilege_lease"] = self.privilege_lease
            prepared = self.privilege_manager.prepare_adb(
                self.context,
                **prepare_kwargs,
            )
        if prepared is None:
            raise RuntimeError("The privileged Android execution backend was not prepared.")
        if not self._continue():
            return prepared
        self._prepared_adb = prepared
        return prepared

    def _resolve_root(self, adb) -> bool:
        if not self._continue():
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
        elif not self.root_enabled:
            # Compatibility for tests/plugins that still construct the
            # coordinator without the application-wide PrivilegeManager.
            return False
        available = bool(
            adb.root_available(cancel_event=self.cancel_event)
        )
        return available and self._continue()

    def _create_backup(self, app: AppInfo, *, adb, use_root: bool):
        return self.backup_manager.create_backup(
            app,
            adb,
            self.device,
            self.uninstall_method(app),
            app.icon_path,
            use_root=use_root,
            cancel_event=self.cancel_event,
        )

    @staticmethod
    def uninstall_method(app: AppInfo) -> str:
        return "pm uninstall --user 0" if app.is_system else "pm uninstall"
