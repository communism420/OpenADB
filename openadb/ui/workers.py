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

    @Slot()
    def run(self) -> None:
        try:
            code = getattr(self.fn, "__code__", None)
            if code is None and hasattr(self.fn, "__func__"):
                code = getattr(self.fn.__func__, "__code__", None)
            if code is not None and "progress_callback" in code.co_varnames:
                self.kwargs["progress_callback"] = self.signals.progress
            if code is not None and "item_callback" in code.co_varnames:
                self.kwargs["item_callback"] = self.signals.item
            result = self.fn(*self.args, **self.kwargs)
            self.signals.result.emit(result)
        except Exception as exc:
            self.signals.error.emit(str(exc), traceback.format_exc())
        finally:
            self.signals.finished.emit()


def start_worker(owner: QObject, pool: QThreadPool, worker: Worker) -> None:
    """Start a QRunnable and keep Python references alive until it finishes.

    PySide can crash without a Python traceback if a QRunnable or its signal
    object is garbage-collected while C++ code still runs it. Storing the worker
    on a long-lived QObject makes background tasks stable in normal Python,
    IDLE, and packaged Windows builds.
    """
    active = getattr(owner, "_active_workers", None)
    if active is None:
        active = set()
        setattr(owner, "_active_workers", active)
    active.add(worker)

    def cleanup() -> None:
        active.discard(worker)

    worker.signals.finished.connect(cleanup)
    pool.start(worker)
