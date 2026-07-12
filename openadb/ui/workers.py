from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


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

    @staticmethod
    def _safe_emit(signal, *args: Any) -> bool:
        """Emit unless Qt already destroyed the signal source during shutdown."""
        try:
            signal.emit(*args)
            return True
        except RuntimeError:
            return False

    @Slot()
    def run(self) -> None:
        try:
            code = getattr(self.fn, "__code__", None)
            if code is None and hasattr(self.fn, "__func__"):
                code = getattr(self.fn.__func__, "__code__", None)
            if code is not None and "progress_callback" in code.co_varnames:
                self.kwargs["progress_callback"] = _SafeSignalProxy(self.signals.progress)
            if code is not None and "item_callback" in code.co_varnames:
                self.kwargs["item_callback"] = _SafeSignalProxy(self.signals.item)
            result = self.fn(*self.args, **self.kwargs)
            self._safe_emit(self.signals.result, result)
        except Exception as exc:
            self._safe_emit(self.signals.error, str(exc), traceback.format_exc())
        finally:
            self._safe_emit(self.signals.finished)


class _SafeSignalProxy:
    """Small emit-only proxy safe to use from nested reader threads."""

    def __init__(self, signal) -> None:
        self._signal = signal

    def emit(self, *args: Any) -> bool:
        return Worker._safe_emit(self._signal, *args)


def start_worker(owner: QObject, pool: QThreadPool, worker: Worker) -> bool:
    """Start a QRunnable and keep Python references alive until it finishes.

    PySide can crash without a Python traceback if a QRunnable or its signal
    object is garbage-collected while C++ code still runs it. Storing the worker
    on a long-lived QObject makes background tasks stable in normal Python,
    IDLE, and packaged Windows builds.
    """
    if getattr(owner, "_workers_shutting_down", False):
        return False
    active = getattr(owner, "_active_workers", None)
    if active is None:
        active = set()
        setattr(owner, "_active_workers", active)
    active.add(worker)

    def cleanup() -> None:
        active.discard(worker)

    worker.signals.finished.connect(cleanup)
    pool.start(worker)
    return True
