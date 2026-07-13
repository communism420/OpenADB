from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

from openadb.models.command_result import CommandResult

from .adb import ADBClient
from .device_context import DeviceContext, DeviceContextUnavailable

if TYPE_CHECKING:
    from .device import DeviceManager


class BoundFileTransferManager:
    """File-transfer facade permanently pinned to one captured device."""

    def __init__(self, adb: ADBClient, context: DeviceContext) -> None:
        self._adb = adb.for_context(context)
        self.context = context

    def push(
        self,
        source: str | Path,
        android_destination: str,
        *,
        timeout: int | float | None = 300,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return self._adb.push(
            source,
            android_destination,
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def pull(
        self,
        android_source: str,
        pc_destination: str | Path,
        *,
        timeout: int | float | None = 300,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return self._adb.pull(
            android_source,
            pc_destination,
            timeout=timeout,
            cancel_event=cancel_event,
        )


class FileTransferManager:
    def __init__(self, adb: ADBClient, device_manager: DeviceManager | None = None) -> None:
        self.adb = adb
        self.device_manager = device_manager

    def for_context(self, context: DeviceContext) -> BoundFileTransferManager:
        return BoundFileTransferManager(self.adb, context)

    def _capture_context(self) -> DeviceContext:
        if self.device_manager is None:
            raise DeviceContextUnavailable(
                "A DeviceContext is required for this file transfer"
            )
        return self.device_manager.require_context(allowed_modes={"ADB", "Recovery"})

    def push(
        self,
        source: str | Path,
        android_destination: str,
        *,
        context: DeviceContext | None = None,
        timeout: int | float | None = 300,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return self.for_context(context or self._capture_context()).push(
            source,
            android_destination,
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def pull(
        self,
        android_source: str,
        pc_destination: str | Path,
        *,
        context: DeviceContext | None = None,
        timeout: int | float | None = 300,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        return self.for_context(context or self._capture_context()).pull(
            android_source,
            pc_destination,
            timeout=timeout,
            cancel_event=cancel_event,
        )
