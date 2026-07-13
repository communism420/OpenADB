"""Device-bound application workflows without GUI dependencies."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable

from openadb.models.app_info import AppInfo
from openadb.models.device_info import DeviceInfo

from .device_context import DeviceContext


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
    ) -> None:
        self.context = context
        self.adb = adb
        self.backup_manager = backup_manager
        self.device = device
        self.cancel_event = cancel_event
        self.require_current = require_current
        self.root_enabled = bool(root_enabled)

    def backup(self, apps: Iterable[AppInfo]) -> list[str]:
        messages: list[str] = []
        if not self._continue():
            return messages
        use_root = self._resolve_root()
        if use_root:
            messages.append(
                "Root mode: APK backups use su/root streaming when normal adb pull is blocked."
            )
        for app in tuple(apps):
            if not self._continue():
                break
            ok, _backup, message = self._create_backup(app, use_root=use_root)
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
        use_root = self._resolve_root()
        for app in tuple(apps):
            if not self._continue():
                break
            if require_backup:
                ok, _backup, message = self._create_backup(app, use_root=use_root)
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
            result = self.adb.uninstall_package(
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
        for app in tuple(apps):
            if not self._continue():
                break
            result = (
                self.adb.enable_package(
                    app.package_name,
                    cancel_event=self.cancel_event,
                )
                if enabled
                else self.adb.disable_package(
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
        for app in tuple(apps):
            if not self._continue():
                break
            result = self.adb.restore_existing_package(
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

    def _resolve_root(self) -> bool:
        if not self.root_enabled or not self._continue():
            return False
        available = bool(
            self.adb.root_available(cancel_event=self.cancel_event)
        )
        return available and self._continue()

    def _create_backup(self, app: AppInfo, *, use_root: bool):
        return self.backup_manager.create_backup(
            app,
            self.adb,
            self.device,
            self.uninstall_method(app),
            app.icon_path,
            use_root=use_root,
            cancel_event=self.cancel_event,
        )

    @staticmethod
    def uninstall_method(app: AppInfo) -> str:
        return "pm uninstall --user 0" if app.is_system else "pm uninstall"
