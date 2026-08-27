"""Best-effort native Windows taskbar progress for File Manager transfers."""

from __future__ import annotations

import ctypes
import sys
import uuid
from collections.abc import Callable, Mapping
from enum import IntEnum
from typing import Protocol


class TaskbarProgressState(IntEnum):
    NONE = 0
    INDETERMINATE = 1
    NORMAL = 2
    ERROR = 4
    PAUSED = 8


class _TaskbarApi(Protocol):
    def set_state(self, hwnd: int, state: TaskbarProgressState) -> bool: ...

    def set_value(self, hwnd: int, completed: int, total: int) -> bool: ...

    def close(self) -> None: ...


class _NullTaskbarApi:
    def set_state(self, hwnd: int, state: TaskbarProgressState) -> bool:
        return False

    def set_value(self, hwnd: int, completed: int, total: int) -> bool:
        return False

    def close(self) -> None:
        return None


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, value: str) -> _Guid:
        parsed = uuid.UUID(value)
        return cls(
            parsed.time_low,
            parsed.time_mid,
            parsed.time_hi_version,
            (ctypes.c_ubyte * 8)(*parsed.bytes[8:]),
        )


class _NativeWindowsTaskbarApi:
    """Small ctypes wrapper around ITaskbarList3.

    The COM object is initialized lazily on the GUI thread. Any native failure
    disables this cosmetic integration for the current process without
    affecting the transfer itself.
    """

    _CLSID_TASKBAR_LIST = _Guid.parse("56FDF344-FD6D-11D0-958A-006097C9A090")
    _IID_TASKBAR_LIST3 = _Guid.parse("EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF")
    _COINIT_APARTMENTTHREADED = 0x2
    _CLSCTX_INPROC_SERVER = 0x1
    _RPC_E_CHANGED_MODE = 0x80010106

    def __init__(self) -> None:
        self._ole32 = None
        self._interface = ctypes.c_void_p()
        self._methods: dict[tuple[int, tuple[object, ...]], object] = {}
        self._com_initialized = False
        self._ready = False
        self._disabled = False

    @staticmethod
    def _failed(hresult: int) -> bool:
        return int(hresult) < 0

    @staticmethod
    def _unsigned_hresult(hresult: int) -> int:
        return int(hresult) & 0xFFFFFFFF

    def _ensure_ready(self) -> bool:
        if self._disabled:
            return False
        if self._ready:
            return True
        if sys.platform != "win32":
            self._disabled = True
            return False
        try:
            self._ole32 = ctypes.WinDLL("ole32")
            self._ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self._ole32.CoInitializeEx.restype = ctypes.c_long
            initialize_result = int(
                self._ole32.CoInitializeEx(
                    None,
                    self._COINIT_APARTMENTTHREADED,
                )
            )
            if initialize_result in {0, 1}:
                self._com_initialized = True
            elif self._unsigned_hresult(initialize_result) != self._RPC_E_CHANGED_MODE:
                raise OSError(
                    f"CoInitializeEx failed: 0x{self._unsigned_hresult(initialize_result):08X}"
                )

            self._ole32.CoCreateInstance.argtypes = [
                ctypes.POINTER(_Guid),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(_Guid),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            self._ole32.CoCreateInstance.restype = ctypes.c_long
            create_result = int(
                self._ole32.CoCreateInstance(
                    ctypes.byref(self._CLSID_TASKBAR_LIST),
                    None,
                    self._CLSCTX_INPROC_SERVER,
                    ctypes.byref(self._IID_TASKBAR_LIST3),
                    ctypes.byref(self._interface),
                )
            )
            if self._failed(create_result) or not self._interface.value:
                raise OSError(
                    f"CoCreateInstance failed: 0x{self._unsigned_hresult(create_result):08X}"
                )
            initialize = self._method(3, ctypes.c_long)
            taskbar_result = int(initialize(self._interface))
            if self._failed(taskbar_result):
                raise OSError(
                    f"ITaskbarList3.HrInit failed: 0x{self._unsigned_hresult(taskbar_result):08X}"
                )
            self._ready = True
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            self._disable()
            return False

    def _method(self, index: int, restype, *argtypes):
        key = (index, (restype, *argtypes))
        cached = self._methods.get(key)
        if cached is not None:
            return cached
        if not self._interface.value:
            raise OSError("ITaskbarList3 is unavailable")
        vtable = ctypes.cast(
            self._interface,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ).contents
        address = vtable[index]
        if not address:
            raise OSError(f"ITaskbarList3 method {index} is unavailable")
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        method = prototype(address)
        self._methods[key] = method
        return method

    def set_state(self, hwnd: int, state: TaskbarProgressState) -> bool:
        if not hwnd or not self._ensure_ready():
            return False
        try:
            set_progress_state = self._method(
                10,
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_uint32,
            )
            result = int(
                set_progress_state(
                    self._interface,
                    ctypes.c_void_p(int(hwnd)),
                    int(state),
                )
            )
            if self._failed(result):
                raise OSError(
                    f"ITaskbarList3.SetProgressState failed: "
                    f"0x{self._unsigned_hresult(result):08X}"
                )
            return True
        except (OSError, TypeError, ValueError):
            self._disable()
            return False

    def set_value(self, hwnd: int, completed: int, total: int) -> bool:
        if not hwnd or not self._ensure_ready():
            return False
        maximum = max(1, int(total))
        value = max(0, min(maximum, int(completed)))
        try:
            set_progress_value = self._method(
                9,
                ctypes.c_long,
                ctypes.c_void_p,
                ctypes.c_ulonglong,
                ctypes.c_ulonglong,
            )
            result = int(
                set_progress_value(
                    self._interface,
                    ctypes.c_void_p(int(hwnd)),
                    value,
                    maximum,
                )
            )
            if self._failed(result):
                raise OSError(
                    f"ITaskbarList3.SetProgressValue failed: "
                    f"0x{self._unsigned_hresult(result):08X}"
                )
            return True
        except (OSError, TypeError, ValueError):
            self._disable()
            return False

    def _release_interface(self) -> None:
        if not self._interface.value:
            return
        try:
            release = self._method(2, ctypes.c_ulong)
            release(self._interface)
        except (OSError, TypeError, ValueError):
            pass
        finally:
            self._interface = ctypes.c_void_p()
            self._methods.clear()
            self._ready = False

    def _disable(self) -> None:
        self._release_interface()
        if self._com_initialized and self._ole32 is not None:
            try:
                self._ole32.CoUninitialize()
            except (AttributeError, OSError):
                pass
        self._com_initialized = False
        self._disabled = True

    def close(self) -> None:
        if self._disabled and not self._interface.value and not self._com_initialized:
            return
        self._release_interface()
        if self._com_initialized and self._ole32 is not None:
            try:
                self._ole32.CoUninitialize()
            except (AttributeError, OSError):
                pass
        self._com_initialized = False
        self._disabled = True


class WindowsTaskbarProgress:
    """Map normalized transfer updates to the main Windows taskbar button."""

    SCALE = 1000

    def __init__(
        self,
        hwnd_provider: Callable[[], int],
        *,
        native_api: _TaskbarApi | None = None,
        platform: str | None = None,
    ) -> None:
        self._hwnd_provider = hwnd_provider
        target_platform = platform or sys.platform
        self._native = (
            native_api
            if native_api is not None
            else (
                _NativeWindowsTaskbarApi()
                if _native_taskbar_available(target_platform)
                else _NullTaskbarApi()
            )
        )
        self._active_operation_id = ""
        self._last_value = 0
        self._determinate = False
        self._cancel_requested = False
        self._closed = False

    @property
    def active_operation_id(self) -> str:
        return self._active_operation_id

    def begin(self, operation_id: str) -> None:
        if self._closed:
            return
        operation_id = str(operation_id or "")
        if not operation_id:
            return
        self._active_operation_id = operation_id
        self._last_value = 0
        self._determinate = False
        self._cancel_requested = False
        self._set_state(TaskbarProgressState.INDETERMINATE)

    def apply_update(self, operation_id: str, update: Mapping[str, object]) -> None:
        if (
            self._closed
            or str(operation_id or "") != self._active_operation_id
            or not isinstance(update, Mapping)
        ):
            return
        kind = str(update.get("type", "progress") or "progress").casefold()
        status = str(update.get("status", "") or "").casefold()
        if kind in {"cancelled", "canceled"} or status == "cancelled":
            self._cancel_requested = True
            self._show_terminal_state(TaskbarProgressState.PAUSED)
            return
        if kind in {"failed", "failure", "error"}:
            self._show_terminal_state(TaskbarProgressState.ERROR)
            return
        if kind in {"done", "complete", "completed", "success"}:
            if bool(update.get("success", False)):
                self._last_value = self.SCALE
                self._determinate = True
                self._set_value(self.SCALE)
                self._set_state(TaskbarProgressState.NORMAL)
            elif self._cancel_requested or status == "cancelled":
                self._show_terminal_state(TaskbarProgressState.PAUSED)
            else:
                self._show_terminal_state(TaskbarProgressState.ERROR)
            return

        value = self._progress_value(update)
        if value is None:
            if not self._determinate:
                self._set_state(TaskbarProgressState.INDETERMINATE)
            return
        self._last_value = max(self._last_value, min(self.SCALE - 1, value))
        self._determinate = True
        self._set_value(self._last_value)
        self._set_state(
            TaskbarProgressState.PAUSED
            if self._cancel_requested
            else TaskbarProgressState.NORMAL
        )

    def finish(self, operation_id: str) -> None:
        if str(operation_id or "") != self._active_operation_id:
            return
        self.clear()

    def clear(self) -> None:
        if self._closed:
            return
        if self._active_operation_id:
            self._set_state(TaskbarProgressState.NONE)
        self._active_operation_id = ""
        self._last_value = 0
        self._determinate = False
        self._cancel_requested = False

    def close(self) -> None:
        if self._closed:
            return
        self.clear()
        self._closed = True
        try:
            self._native.close()
        except Exception:  # noqa: BLE001 - taskbar integration is cosmetic
            return

    def _progress_value(self, update: Mapping[str, object]) -> int | None:
        done_bytes = _nonnegative_int(update.get("done_bytes"))
        total_bytes = _nonnegative_int(update.get("total_bytes"))
        if total_bytes > 0:
            return int(min(1.0, done_bytes / total_bytes) * self.SCALE)
        done_files = _nonnegative_int(update.get("done_files"))
        total_files = _nonnegative_int(update.get("total_files"))
        if total_files > 0:
            return int(min(1.0, done_files / total_files) * self.SCALE)
        percent = _nonnegative_int(update.get("percent"))
        if percent > 0:
            return min(100, percent) * 10
        return None

    def _show_terminal_state(self, state: TaskbarProgressState) -> None:
        self._determinate = True
        self._last_value = max(1, self._last_value)
        self._set_value(self._last_value)
        self._set_state(state)

    def _hwnd(self) -> int:
        try:
            return max(0, int(self._hwnd_provider() or 0))
        except (RuntimeError, TypeError, ValueError):
            return 0

    def _set_state(self, state: TaskbarProgressState) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            self._native.set_state(hwnd, state)
        except Exception:  # noqa: BLE001 - taskbar integration is cosmetic
            return

    def _set_value(self, value: int) -> None:
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            self._native.set_value(hwnd, value, self.SCALE)
        except Exception:  # noqa: BLE001 - taskbar integration is cosmetic
            return


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _native_taskbar_available(platform: str) -> bool:
    if platform != "win32":
        return False
    try:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is None:
            return True
        platform_name = str(app.platformName() or "").casefold().partition(":")[0]
        return platform_name in {"windows", "win32"}
    except (ImportError, RuntimeError):
        return True


__all__ = ["TaskbarProgressState", "WindowsTaskbarProgress"]
