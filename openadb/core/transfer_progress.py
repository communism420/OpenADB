"""Transport-neutral progress accounting for File Manager transfers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from operator import index
from typing import Any

from .file_manager_errors import redact_sensitive_text


class TransferProgressStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TransferItemFailure:
    item: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "item", redact_sensitive_text(self.item))
        object.__setattr__(self, "message", redact_sensitive_text(self.message))


@dataclass(frozen=True, slots=True)
class TransferProgressSnapshot:
    """One immutable, secret-safe view of a transfer's progress."""

    status: TransferProgressStatus
    done_bytes: int
    total_bytes: int
    done_files: int
    total_files: int
    current_file: str
    activity: str
    message: str
    elapsed_seconds: float
    bytes_per_second: float
    failures: tuple[TransferItemFailure, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", TransferProgressStatus(self.status))
        for field_name in ("done_bytes", "total_bytes", "done_files", "total_files"):
            object.__setattr__(
                self,
                field_name,
                _nonnegative_int(getattr(self, field_name), name=field_name),
            )
        for field_name in ("current_file", "activity", "message"):
            object.__setattr__(
                self,
                field_name,
                redact_sensitive_text(getattr(self, field_name)),
            )
        object.__setattr__(self, "elapsed_seconds", max(0.0, float(self.elapsed_seconds)))
        object.__setattr__(self, "bytes_per_second", max(0.0, float(self.bytes_per_second)))
        object.__setattr__(self, "failures", tuple(self.failures))

    @property
    def is_terminal(self) -> bool:
        return self.status is not TransferProgressStatus.RUNNING

    @property
    def is_success(self) -> bool:
        return self.status is TransferProgressStatus.SUCCEEDED

    @property
    def is_partial(self) -> bool:
        return self.status is TransferProgressStatus.PARTIAL

    @property
    def percent(self) -> int:
        if self.total_bytes > 0:
            return round(min(1.0, self.done_bytes / self.total_bytes) * 100)
        if self.total_files > 0:
            return round(min(1.0, self.done_files / self.total_files) * 100)
        return 100 if self.is_success else 0

    def to_update(self) -> dict[str, object]:
        """Return the common dictionary format consumed by the progress dialog."""

        event_type = "progress" if not self.is_terminal else "done"
        return {
            "type": event_type,
            "status": self.status.value,
            "success": self.is_success,
            "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes,
            "done_files": self.done_files,
            "total_files": self.total_files,
            "current_file": self.current_file,
            "activity": self.activity,
            "message": self.message,
            "output": self.message,
            "elapsed_seconds": self.elapsed_seconds,
            "bytes_per_second": self.bytes_per_second,
            "speed": _format_speed(self.bytes_per_second),
            "percent": self.percent,
            "failed_items": tuple(failure.item for failure in self.failures),
        }


class TransferProgressTracker:
    """Thread-safe cumulative accounting shared by ADB and P2P strategies.

    Absolute progress never moves backwards, which makes parallel P2P updates
    and late subprocess observations deterministic.  Text is sanitized at the
    boundary so snapshots can safely be sent to the UI or logs.
    """

    def __init__(
        self,
        *,
        total_bytes: int = 0,
        total_files: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._started_at = float(clock())
        self._finished_at: float | None = None
        self._status = TransferProgressStatus.RUNNING
        self._done_bytes = 0
        self._total_bytes = _nonnegative_int(total_bytes, name="total_bytes")
        self._done_files = 0
        self._total_files = _nonnegative_int(total_files, name="total_files")
        self._current_file = ""
        self._activity = ""
        self._message = ""
        self._failures: list[TransferItemFailure] = []

    def snapshot(self) -> TransferProgressSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def update(
        self,
        *,
        done_bytes: int | None = None,
        total_bytes: int | None = None,
        done_files: int | None = None,
        total_files: int | None = None,
        byte_delta: int | None = None,
        file_delta: int | None = None,
        current_file: object | None = None,
        activity: object | None = None,
        message: object | None = None,
    ) -> TransferProgressSnapshot:
        """Apply an absolute or incremental update and return a new snapshot."""

        if done_bytes is not None and byte_delta is not None:
            raise ValueError("Specify done_bytes or byte_delta, not both")
        if done_files is not None and file_delta is not None:
            raise ValueError("Specify done_files or file_delta, not both")

        with self._lock:
            if self._status is not TransferProgressStatus.RUNNING:
                return self._snapshot_locked()

            if total_bytes is not None:
                self._total_bytes = max(
                    self._total_bytes,
                    _nonnegative_int(total_bytes, name="total_bytes"),
                )
            if total_files is not None:
                self._total_files = max(
                    self._total_files,
                    _nonnegative_int(total_files, name="total_files"),
                )

            if done_bytes is not None:
                self._done_bytes = max(
                    self._done_bytes,
                    _nonnegative_int(done_bytes, name="done_bytes"),
                )
            elif byte_delta is not None:
                self._done_bytes += _nonnegative_int(byte_delta, name="byte_delta")

            if done_files is not None:
                self._done_files = max(
                    self._done_files,
                    _nonnegative_int(done_files, name="done_files"),
                )
            elif file_delta is not None:
                self._done_files += _nonnegative_int(file_delta, name="file_delta")

            self._total_bytes = max(self._total_bytes, self._done_bytes)
            self._total_files = max(self._total_files, self._done_files)
            if current_file is not None:
                self._current_file = redact_sensitive_text(current_file)
            if activity is not None:
                self._activity = redact_sensitive_text(activity)
            if message is not None:
                self._message = redact_sensitive_text(message)
            return self._snapshot_locked()

    def ingest(self, update: Mapping[str, Any]) -> TransferProgressSnapshot:
        """Normalize a legacy ADB or P2P update dictionary."""

        event_type = str(update.get("type", "progress") or "progress").casefold()
        message = update.get("message", update.get("output"))
        snapshot = self.update(
            done_bytes=_optional_nonnegative_int(update.get("done_bytes"), "done_bytes"),
            total_bytes=_optional_nonnegative_int(update.get("total_bytes"), "total_bytes"),
            done_files=_optional_nonnegative_int(update.get("done_files"), "done_files"),
            total_files=_optional_nonnegative_int(update.get("total_files"), "total_files"),
            current_file=update.get("current_file"),
            activity=update.get("activity"),
            message=message,
        )
        if event_type in {"cancelled", "canceled"} or bool(
            update.get("cancelled", False)
        ):
            return self.cancel(message or "Transfer cancelled by user")
        if event_type in {"failed", "failure", "error"}:
            return self.fail(message or "Transfer failed")
        if event_type in {"complete", "completed", "success", "done"}:
            if update.get("success") is False:
                return self.fail(message or "Transfer failed")
            return self.finish(message=message)
        return snapshot

    def record_failure(self, item: object, error: object) -> TransferProgressSnapshot:
        with self._lock:
            if self._status is not TransferProgressStatus.RUNNING:
                return self._snapshot_locked()
            self._failures.append(
                TransferItemFailure(
                    item=redact_sensitive_text(item),
                    message=redact_sensitive_text(error),
                )
            )
            return self._snapshot_locked()

    def finish(self, *, message: object | None = None) -> TransferProgressSnapshot:
        """Finish successfully only when every known item and byte completed."""

        with self._lock:
            if self._status is not TransferProgressStatus.RUNNING:
                return self._snapshot_locked()
            if message is not None:
                self._message = redact_sensitive_text(message)
            incomplete = (
                self._done_bytes < self._total_bytes
                or self._done_files < self._total_files
                or bool(self._failures)
            )
            if incomplete:
                has_progress = self._done_bytes > 0 or self._done_files > 0
                self._status = (
                    TransferProgressStatus.PARTIAL
                    if has_progress
                    else TransferProgressStatus.FAILED
                )
                if not self._message:
                    self._message = "The transfer did not complete every planned item."
            else:
                self._status = TransferProgressStatus.SUCCEEDED
            self._finished_at = float(self._clock())
            return self._snapshot_locked()

    def fail(self, error: object) -> TransferProgressSnapshot:
        with self._lock:
            if self._status is not TransferProgressStatus.RUNNING:
                return self._snapshot_locked()
            self._message = redact_sensitive_text(error)
            has_progress = self._done_bytes > 0 or self._done_files > 0
            self._status = (
                TransferProgressStatus.PARTIAL
                if has_progress
                else TransferProgressStatus.FAILED
            )
            self._finished_at = float(self._clock())
            return self._snapshot_locked()

    def cancel(self, message: object = "Transfer cancelled by user") -> TransferProgressSnapshot:
        with self._lock:
            if self._status is not TransferProgressStatus.RUNNING:
                return self._snapshot_locked()
            self._message = redact_sensitive_text(message)
            self._status = TransferProgressStatus.CANCELLED
            self._finished_at = float(self._clock())
            return self._snapshot_locked()

    def _snapshot_locked(self) -> TransferProgressSnapshot:
        observed_at = self._finished_at
        if observed_at is None:
            observed_at = float(self._clock())
        elapsed = max(0.0, observed_at - self._started_at)
        speed = self._done_bytes / elapsed if elapsed > 0 else 0.0
        return TransferProgressSnapshot(
            status=self._status,
            done_bytes=self._done_bytes,
            total_bytes=self._total_bytes,
            done_files=self._done_files,
            total_files=self._total_files,
            current_file=self._current_file,
            activity=self._activity,
            message=self._message,
            elapsed_seconds=elapsed,
            bytes_per_second=speed,
            failures=tuple(self._failures),
        )


TransferProgress = TransferProgressTracker


def _nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        result = index(value)  # type: ignore[arg-type]
    except TypeError:
        if isinstance(value, str) and value.strip().isdecimal():
            result = int(value.strip())
        else:
            raise ValueError(f"{name} must be a non-negative integer") from None
    if result < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return result


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    if value is None or value == "":
        return None
    return _nonnegative_int(value, name=name)


def _format_speed(bytes_per_second: float) -> str:
    value = max(0.0, float(bytes_per_second))
    for unit in ("B/s", "KB/s", "MB/s", "GB/s", "TB/s"):
        if value < 1024.0 or unit == "TB/s":
            return f"{value:.0f} {unit}" if unit == "B/s" else f"{value:.1f} {unit}"
        value /= 1024.0
    return "0 B/s"
