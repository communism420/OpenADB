"""Device-context-safe coordinator for File Manager transfer strategies."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .adb import ADBClient
from .device_context import DeviceContext, StaleDeviceContext
from .file_manager_errors import map_file_manager_error, redact_sensitive_text
from .transfer_plan import (
    FIXED_PARALLELISM,
    P2P_TRANSFER,
    PULL_DIRECTION,
    TransferPlan,
)
from .transfer_progress import TransferProgressSnapshot, TransferProgressTracker


class TransferStrategyHost(Protocol):
    """Compatibility surface implemented by the extracted transfer strategies."""

    def _run_pull_transfer(
        self,
        adb: ADBClient,
        android_paths: list[str],
        destination: Path,
        cancel_event: threading.Event,
        item_callback: Any,
        use_root_requested: bool,
    ) -> dict: ...

    def _run_push_transfer(
        self,
        adb: ADBClient,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback: Any,
        use_root_requested: bool,
        *,
        transport: str,
        p2p_parallelism: int | None,
        p2p_parallelism_mode: str,
        temp_path: Path | None,
    ) -> dict: ...

    def _run_adb_push_transfer(
        self,
        adb: ADBClient,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback: Any,
        use_root_requested: bool,
    ) -> dict: ...

    def _run_p2p_push_transfer(
        self,
        adb: ADBClient,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback: Any,
        parallelism: int | None = 1,
        temp_path: Path | None = None,
        parallelism_mode: str = FIXED_PARALLELISM,
    ) -> dict: ...


class FileTransferExecutionError(RuntimeError):
    """Secret-safe strategy failure suitable for the worker/UI boundary."""


class _NormalizedProgressSink:
    """Qt-signal-compatible adapter sharing accounting across all transports."""

    _LEGACY_EVENT_TYPES = {"plan", "file_start", "progress", "heartbeat", "file_done"}
    _TERMINAL_EVENT_TYPES = {
        "cancelled",
        "canceled",
        "complete",
        "completed",
        "done",
        "error",
        "failed",
        "failure",
        "success",
    }

    def __init__(
        self, tracker: TransferProgressTracker, downstream: Any | None
    ) -> None:
        self._tracker = tracker
        self._downstream = downstream
        self._lock = threading.RLock()
        self._strategy_terminal_seen = False

    def emit(self, update: Mapping[str, Any]) -> None:
        if not isinstance(update, Mapping):
            raise TypeError("Transfer progress updates must be mappings")
        with self._lock:
            if self._tracker.snapshot().is_terminal or self._strategy_terminal_seen:
                return
            legacy_type = str(update.get("type", "progress") or "progress").casefold()
            strategy_terminal = legacy_type in self._TERMINAL_EVENT_TYPES or bool(
                update.get("cancelled", False)
            )
            accounting_update = dict(update)
            if strategy_terminal:
                # A strategy may publish a final counter update, but only its
                # returned result is authoritative for success/failure.  Keep
                # the accounting and suppress a premature terminal UI event.
                accounting_update["type"] = "progress"
                accounting_update.pop("cancelled", None)
                accounting_update.pop("success", None)
                accounting_update.pop("status", None)
            snapshot = self._tracker.ingest(accounting_update)
            normalized = _sanitize_payload(accounting_update)
            snapshot_update = snapshot.to_update()
            # The tracker intentionally retains the latest message, but a progress
            # event without text must stay text-free. Otherwise every P2P chunk
            # repeats the previous file-start message in the details view.
            for field_name in ("message", "output"):
                if field_name not in update:
                    snapshot_update.pop(field_name, None)
            normalized.update(snapshot_update)
            if strategy_terminal:
                normalized["type"] = "progress"
                self._strategy_terminal_seen = True
            elif legacy_type in self._LEGACY_EVENT_TYPES and not snapshot.is_terminal:
                normalized["type"] = legacy_type
            if self._downstream is not None:
                self._downstream.emit(normalized)


class FileTransferController:
    """Execute one immutable transfer plan against one bound ADB facade.

    The controller deliberately never reads the currently active device.  Its
    only target identity comes from ``plan.device_context`` and, when present,
    the context carried by ``BoundADBClient``.  This makes queued work immune
    to later serial, transport, destination, root, or stream-count UI changes.
    """

    def __init__(self, strategy_host: TransferStrategyHost) -> None:
        self._strategy_host = strategy_host

    def execute(
        self,
        plan: TransferPlan,
        *,
        adb: ADBClient,
        cancel_event: threading.Event,
        item_callback: Any = None,
    ) -> dict:
        self._require_bound_context(plan, adb)
        tracker = TransferProgressTracker()
        # Accounting remains active even in headless/tests callers that do not
        # provide a UI sink. This keeps the returned terminal snapshot honest.
        progress_sink = _NormalizedProgressSink(tracker, item_callback)
        try:
            if plan.direction == PULL_DIRECTION:
                result = self._strategy_host._run_pull_transfer(
                    adb,
                    list(plan.sources),
                    Path(plan.destination),
                    cancel_event,
                    progress_sink,
                    plan.use_root,
                )
            else:
                # Keep the historical page seam while dispatching the actual
                # transport in this controller through ``execute_push``.
                result = self._strategy_host._run_push_transfer(
                    adb,
                    list(plan.sources),
                    plan.destination,
                    cancel_event,
                    progress_sink,
                    plan.use_root,
                    transport=plan.transport,
                    p2p_parallelism=plan.requested_parallelism,
                    p2p_parallelism_mode=plan.parallelism_mode,
                    temp_path=plan.device_context.temp_path,
                )
        except Exception as exc:
            mapped = map_file_manager_error(exc, operation="File transfer")
            tracker.fail(mapped.message)
            raise FileTransferExecutionError(
                f"{mapped.title}: {mapped.message}"
            ) from None
        normalized = self._normalize_result(result, cancel_event=cancel_event)
        terminal = self._finish_tracker(tracker, normalized, cancel_event=cancel_event)
        normalized["success"] = terminal.is_success
        normalized["cancelled"] = terminal.status.value == "cancelled"
        normalized["progress"] = terminal
        normalized["summary"] = redact_sensitive_text(
            normalized.get("summary") or terminal.message or "Transfer finished."
        )
        return normalized

    def execute_push(
        self,
        *,
        adb: ADBClient,
        local_paths: list[str],
        android_destination: str,
        cancel_event: threading.Event,
        item_callback: Any,
        use_root_requested: bool,
        transport: str,
        p2p_parallelism: int | None = 1,
        p2p_parallelism_mode: str = FIXED_PARALLELISM,
        temp_path: Path | None,
    ) -> dict:
        """Compatibility dispatcher kept behind ``FileManagerPage._run_push_transfer``."""

        if transport == P2P_TRANSFER:
            return self._strategy_host._run_p2p_push_transfer(
                adb,
                local_paths,
                android_destination,
                cancel_event,
                item_callback,
                parallelism=p2p_parallelism,
                parallelism_mode=p2p_parallelism_mode,
                temp_path=temp_path,
            )
        return self._strategy_host._run_adb_push_transfer(
            adb,
            local_paths,
            android_destination,
            cancel_event,
            item_callback,
            use_root_requested,
        )

    @staticmethod
    def _require_bound_context(plan: TransferPlan, adb: ADBClient) -> None:
        bound_context = getattr(adb, "device_context", None)
        if not isinstance(bound_context, DeviceContext):
            raise StaleDeviceContext(
                "A device transfer requires an ADB client bound to its captured DeviceContext"
            )
        if bound_context != plan.device_context:
            raise StaleDeviceContext(
                "The transfer strategy received an ADB client bound to a different device context"
            )
        bound_serial = str(getattr(adb, "serial", "") or "")
        if bound_serial != plan.device_context.serial:
            raise StaleDeviceContext(
                "The transfer strategy received an ADB transport that does not match its captured context"
            )

    @staticmethod
    def _normalize_result(result: object, *, cancel_event: threading.Event) -> dict:
        if not isinstance(result, dict):
            raise TypeError("A transfer strategy must return a result dictionary")
        normalized = dict(result)
        normalized = _sanitize_payload(normalized)
        if "messages" in normalized:
            messages = normalized.get("messages")
            if isinstance(messages, (list, tuple)):
                normalized["messages"] = [
                    redact_sensitive_text(message) for message in messages
                ]
            else:
                normalized["messages"] = [redact_sensitive_text(messages)]
        normalized["success"] = bool(normalized.get("success", False))
        if cancel_event.is_set():
            normalized["success"] = False
            normalized["cancelled"] = True
            normalized.setdefault("summary", "Transfer cancelled by user.")
        else:
            normalized["cancelled"] = bool(normalized.get("cancelled", False))
        return normalized

    @staticmethod
    def _finish_tracker(
        tracker: TransferProgressTracker,
        result: dict,
        *,
        cancel_event: threading.Event,
    ) -> TransferProgressSnapshot:
        message = redact_sensitive_text(result.get("summary", ""))
        if cancel_event.is_set() or result.get("cancelled"):
            return tracker.cancel(message or "Transfer cancelled by user.")
        if result.get("success"):
            return tracker.finish(message=message)
        return tracker.fail(message or "Transfer failed.")


_SECRET_FIELD_NAMES = {
    "auth",
    "auth_key",
    "auth_token",
    "key",
    "pairing_code",
    "pairing_pin",
    "p2p_key",
    "p2p_secret",
    "p2p_token",
    "secret",
    "session_id",
    "session_key",
    "session_secret",
    "session_token",
    "token",
}


def _sanitize_payload(payload: Mapping[Any, Any]) -> dict[str, Any]:
    """Copy a strategy payload without allowing credential-shaped fields through."""

    sanitized: dict[str, Any] = {}
    for raw_key, value in payload.items():
        key = str(raw_key)
        normalized_key = key.casefold().replace("-", "_").replace(" ", "_")
        if _is_secret_field_name(normalized_key):
            sanitized[key] = "[REDACTED]"
        else:
            sanitized[key] = _sanitize_value(value)
    return sanitized


def _is_secret_field_name(normalized_key: str) -> bool:
    return normalized_key in _SECRET_FIELD_NAMES or normalized_key.endswith(
        ("_credential", "_password", "_secret", "_signature", "_token")
    )


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, Mapping):
        return _sanitize_payload(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item) for item in value)
    return value
