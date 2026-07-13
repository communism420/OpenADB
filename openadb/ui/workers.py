from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from openadb.core.file_manager_errors import REDACTED, redact_sensitive_text


class WorkerSignals(QObject):
    result = Signal(object)
    item = Signal(object)
    error = Signal(str, str)
    finished = Signal()
    progress = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
        self._finalizers: list[Callable[[], None]] = []
        self._fallback_finalizers: list[Callable[[], None]] = []

    def add_finalizer(self, callback: Callable[[], None]) -> None:
        self._finalizers.append(callback)

    def add_fallback_finalizer(self, callback: Callable[[], None]) -> None:
        """Run cleanup only when the Qt ``finished`` signal cannot be emitted."""
        self._fallback_finalizers.append(callback)

    @staticmethod
    def _safe_emit(signal, *args: Any) -> bool:
        """Emit unless Qt already destroyed the signal source during shutdown."""
        if signal is None:
            return False
        try:
            signal.emit(*args)
            return True
        except (AttributeError, RuntimeError):
            return False

    @staticmethod
    def _signal(signals: WorkerSignals | None, name: str):
        if signals is None:
            return None
        try:
            return getattr(signals, name)
        except RuntimeError:
            return None

    def _run_finalizers(self, callbacks: tuple[Callable[[], None], ...], error_signal) -> None:
        for finalizer in callbacks:
            try:
                finalizer()
            except Exception as exc:
                self._safe_emit(
                    error_signal,
                    redact_sensitive_text(f"Worker cleanup failed: {exc}"),
                    redact_sensitive_text(traceback.format_exc()),
                )

    @Slot()
    def run(self) -> None:
        signals = getattr(self, "signals", None)
        error_signal = self._signal(signals, "error")
        try:
            code = getattr(self.fn, "__code__", None)
            if code is None and hasattr(self.fn, "__func__"):
                code = getattr(self.fn.__func__, "__code__", None)
            if code is not None and "progress_callback" in code.co_varnames:
                self.kwargs["progress_callback"] = _SafeSignalProxy(self._signal(signals, "progress"))
            if code is not None and "item_callback" in code.co_varnames:
                self.kwargs["item_callback"] = _SafeSignalProxy(self._signal(signals, "item"))
            result = self.fn(*self.args, **self.kwargs)
            self._safe_emit(self._signal(signals, "result"), result)
        except Exception as exc:
            self._safe_emit(
                error_signal,
                redact_sensitive_text(exc),
                redact_sensitive_text(traceback.format_exc()),
            )
        finally:
            self._run_finalizers(tuple(self._finalizers), error_signal)
            if not self._safe_emit(self._signal(signals, "finished")):
                self._run_finalizers(tuple(self._fallback_finalizers), error_signal)


class _SafeSignalProxy:
    """Small emit-only proxy safe to use from nested reader threads."""

    def __init__(self, signal) -> None:
        self._signal = signal

    def emit(self, *args: Any) -> bool:
        safe_args = tuple(_redact_signal_value(value) for value in args)
        return Worker._safe_emit(self._signal, *safe_args)


_SENSITIVE_PAYLOAD_KEYS = {
    "access_token",
    "auth_token",
    "authenticated_url",
    "hmac_key",
    "one_shot_request_secret",
    "pairing_code",
    "pairing_password",
    "pairing_secret",
    "qr_password",
    "request_secret",
    "session_id",
    "session_key",
    "session_secret",
}


def _redact_signal_value(value: Any, *, field_name: str = "") -> Any:
    """Sanitize UI callback payloads while preserving primitive containers."""

    if _is_sensitive_payload_key(field_name):
        if isinstance(value, bytes):
            return REDACTED.encode("utf-8")
        if isinstance(value, bytearray):
            return bytearray(REDACTED, "utf-8")
        return REDACTED
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
        redacted = redact_sensitive_text(decoded)
        return value if redacted == decoded else redacted.encode("utf-8")
    if isinstance(value, bytearray):
        redacted = _redact_signal_value(bytes(value))
        return value if redacted == bytes(value) else bytearray(redacted)
    if isinstance(value, list):
        return [_redact_signal_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_signal_value(item) for item in value)
    if isinstance(value, set):
        return {_redact_signal_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_redact_signal_value(item) for item in value)
    if isinstance(value, dict):
        return {
            key: _redact_signal_value(item, field_name=str(key))
            for key, item in value.items()
        }
    return value


def _is_sensitive_payload_key(value: str) -> bool:
    normalized = "_".join(
        segment
        for segment in "".join(
            character.casefold() if character.isalnum() else "_"
            for character in str(value or "")
        ).split("_")
        if segment
    )
    return normalized in _SENSITIVE_PAYLOAD_KEYS or normalized.endswith(
        ("_credential", "_password", "_secret", "_signature", "_token")
    )


def start_worker(
    owner: QObject,
    pool: QThreadPool,
    worker: Worker,
    *,
    operation_registry=None,
    operation_token=None,
) -> bool:
    """Start a QRunnable and keep Python references alive until it finishes.

    PySide can crash without a Python traceback if a QRunnable or its signal
    object is garbage-collected while C++ code still runs it. Storing the worker
    on a long-lived QObject makes background tasks stable in normal Python,
    IDLE, and packaged Windows builds.
    """
    if getattr(owner, "_workers_shutting_down", False):
        if operation_token is not None:
            operation_token.cancel("worker owner is shutting down")
        if operation_registry is not None and operation_token is not None:
            operation_registry.finish(operation_token)
        return False
    active = getattr(owner, "_active_workers", None)
    if active is None:
        active = set()
        setattr(owner, "_active_workers", active)
    active.add(worker)

    def cleanup() -> None:
        if operation_registry is not None and operation_token is not None:
            operation_registry.finish(operation_token)
        active.discard(worker)

    worker.signals.finished.connect(cleanup)
    worker.add_fallback_finalizer(cleanup)
    try:
        pool.start(worker)
    except Exception:
        active.discard(worker)
        if operation_token is not None:
            operation_token.cancel("worker could not be started")
        if operation_registry is not None and operation_token is not None:
            operation_registry.finish(operation_token)
        raise
    return True
