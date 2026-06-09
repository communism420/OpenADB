from __future__ import annotations

import ctypes
import os
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QWidget


if sys.platform == "win32":
    from ctypes import wintypes
else:  # pragma: no cover - Windows-only widget
    wintypes = None


from openadb.ui.widgets.file_panel import ANDROID_MIME


HRESULT = ctypes.c_long
CLSIDCTX_INPROC_SERVER = 0x1
FVM_DETAILS = 4
SVGIO_SELECTION = 0x1
EBO_SHOWFRAMES = 0x2
SIGDN_FILESYSPATH = 0x80058000
SIGDN_DESKTOPABSOLUTEPARSING = 0x80028000


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> "GUID":
        parsed = uuid.UUID(value)
        data4 = (ctypes.c_ubyte * 8)(*parsed.bytes[8:])
        return cls(parsed.time_low, parsed.time_mid, parsed.time_hi_version, data4)


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class FOLDERSETTINGS(ctypes.Structure):
    _fields_ = [
        ("ViewMode", ctypes.c_uint),
        ("fFlags", ctypes.c_uint),
    ]


CLSID_ExplorerBrowser = GUID.from_string("{71F96385-DDD6-48D3-A0C1-AE06E8B055FB}")
IID_IExplorerBrowser = GUID.from_string("{DFD3B6B5-C10C-4BE9-85F6-A66969F402F6}")
IID_IFolderView = GUID.from_string("{CDE725B0-CCC9-4519-917E-325D72FAB4CE}")
IID_IShellItem = GUID.from_string("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")
IID_IShellItemArray = GUID.from_string("{B63EA76D-1F85-456F-A19C-48159EFA858B}")


class _NativeExplorerBrowser:
    def __init__(self, hwnd: int, path: str) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Native ExplorerBrowser is only available on Windows.")
        self._ole32 = ctypes.OleDLL("ole32")
        self._shell32 = ctypes.OleDLL("shell32")
        self._browser = ctypes.c_void_p()
        self._ole_initialized = False
        self._setup_prototypes()
        hr = self._ole32.OleInitialize(None)
        if _failed(hr):
            raise OSError(f"OleInitialize failed: 0x{hr & 0xFFFFFFFF:08X}")
        self._ole_initialized = True
        hr = self._ole32.CoCreateInstance(
            ctypes.byref(CLSID_ExplorerBrowser),
            None,
            CLSIDCTX_INPROC_SERVER,
            ctypes.byref(IID_IExplorerBrowser),
            ctypes.byref(self._browser),
        )
        if _failed(hr) or not self._browser.value:
            self.close()
            raise OSError(f"CoCreateInstance(CLSID_ExplorerBrowser) failed: 0x{hr & 0xFFFFFFFF:08X}")
        hr = self._method(11, ctypes.c_uint)(self._browser, EBO_SHOWFRAMES)
        if _failed(hr):
            self.close()
            raise OSError(f"IExplorerBrowser.SetOptions(EBO_SHOWFRAMES) failed: 0x{hr & 0xFFFFFFFF:08X}")

        rect = RECT(0, 0, 1, 1)
        settings = FOLDERSETTINGS(FVM_DETAILS, 0)
        hr = self._method(3, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.POINTER(FOLDERSETTINGS))(
            self._browser,
            ctypes.c_void_p(hwnd),
            ctypes.byref(rect),
            ctypes.byref(settings),
        )
        if _failed(hr):
            self.close()
            raise OSError(f"IExplorerBrowser.Initialize failed: 0x{hr & 0xFFFFFFFF:08X}")
        self.browse(path)

    def _setup_prototypes(self) -> None:
        self._ole32.OleInitialize.argtypes = [ctypes.c_void_p]
        self._ole32.OleInitialize.restype = HRESULT
        self._ole32.OleUninitialize.argtypes = []
        self._ole32.OleUninitialize.restype = None
        self._ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(GUID),
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ole32.CoCreateInstance.restype = HRESULT
        self._ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self._ole32.CoTaskMemFree.restype = None
        self._shell32.SHParseDisplayName.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        self._shell32.SHParseDisplayName.restype = HRESULT

    def _method(self, index: int, *argtypes):
        vtable = ctypes.cast(self._browser, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        prototype = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)
        return prototype(vtable[index])

    def _com_method(self, pointer: ctypes.c_void_p, index: int, restype, *argtypes):
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return prototype(vtable[index])

    def resize(self, width: int, height: int) -> None:
        if not self._browser.value:
            return
        rect = RECT(0, 0, max(1, int(width)), max(1, int(height)))
        self._method(5, ctypes.c_void_p, ctypes.POINTER(RECT))(self._browser, None, ctypes.byref(rect))

    def browse(self, path: str) -> bool:
        if not self._browser.value:
            return False
        normalized = str(Path(path).expanduser())
        pidl = ctypes.c_void_p()
        attrs = ctypes.c_ulong()
        hr = self._shell32.SHParseDisplayName(normalized, None, ctypes.byref(pidl), 0, ctypes.byref(attrs))
        if _failed(hr) or not pidl.value:
            return False
        try:
            hr = self._method(13, ctypes.c_void_p, ctypes.c_uint)(self._browser, pidl, 0)
            return not _failed(hr)
        finally:
            self._ole32.CoTaskMemFree(pidl)

    def current_path(self) -> str:
        folder_view = self._current_folder_view()
        if not folder_view:
            return ""
        try:
            shell_item = ctypes.c_void_p()
            hr = self._com_method(folder_view, 5, HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                folder_view,
                ctypes.byref(IID_IShellItem),
                ctypes.byref(shell_item),
            )
            if _failed(hr) or not shell_item.value:
                return ""
            try:
                return self._shell_item_path(shell_item)
            finally:
                self._release(shell_item)
        finally:
            self._release(folder_view)

    def selected_paths(self) -> list[str]:
        folder_view = self._current_folder_view()
        if not folder_view:
            return []
        try:
            shell_array = ctypes.c_void_p()
            hr = self._com_method(folder_view, 8, HRESULT, ctypes.c_uint, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                folder_view,
                SVGIO_SELECTION,
                ctypes.byref(IID_IShellItemArray),
                ctypes.byref(shell_array),
            )
            if _failed(hr) or not shell_array.value:
                return []
            try:
                return self._shell_item_array_paths(shell_array)
            finally:
                self._release(shell_array)
        finally:
            self._release(folder_view)

    def _current_folder_view(self) -> ctypes.c_void_p:
        view = ctypes.c_void_p()
        hr = self._method(17, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
            self._browser,
            ctypes.byref(IID_IFolderView),
            ctypes.byref(view),
        )
        if _failed(hr) or not view.value:
            return ctypes.c_void_p()
        return view

    def _shell_item_array_paths(self, shell_array: ctypes.c_void_p) -> list[str]:
        count = ctypes.c_ulong()
        hr = self._com_method(shell_array, 7, HRESULT, ctypes.POINTER(ctypes.c_ulong))(shell_array, ctypes.byref(count))
        if _failed(hr):
            return []
        paths: list[str] = []
        for index in range(int(count.value)):
            item = ctypes.c_void_p()
            hr = self._com_method(shell_array, 8, HRESULT, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))(
                shell_array,
                index,
                ctypes.byref(item),
            )
            if _failed(hr) or not item.value:
                continue
            try:
                path = self._shell_item_path(item)
                if path:
                    paths.append(path)
            finally:
                self._release(item)
        return paths

    def _shell_item_path(self, shell_item: ctypes.c_void_p) -> str:
        for sigdn in (SIGDN_FILESYSPATH, SIGDN_DESKTOPABSOLUTEPARSING):
            raw = ctypes.c_void_p()
            hr = self._com_method(shell_item, 5, HRESULT, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                shell_item,
                sigdn,
                ctypes.byref(raw),
            )
            if _failed(hr) or not raw.value:
                continue
            try:
                value = ctypes.wstring_at(raw.value)
                if value and not value.startswith("::"):
                    return value
            finally:
                self._ole32.CoTaskMemFree(raw)
        return ""

    def _release(self, pointer: ctypes.c_void_p) -> None:
        if pointer and pointer.value:
            self._com_method(pointer, 2, ctypes.c_ulong)(pointer)

    def close(self) -> None:
        if self._browser.value:
            try:
                self._method(4)(self._browser)
            except Exception:
                pass
            self._release(self._browser)
            self._browser = ctypes.c_void_p()
        if self._ole_initialized:
            self._ole32.OleUninitialize()
            self._ole_initialized = False


class NativeExplorerPanel(QWidget):
    navigate_requested = Signal(str)
    up_requested = Signal()
    refresh_requested = Signal()
    new_folder_requested = Signal()
    delete_requested = Signal()
    rename_requested = Signal()
    transfer_requested = Signal()
    copy_path_requested = Signal()
    properties_requested = Signal()
    open_external_requested = Signal()
    dropped = Signal(list)
    path_changed = Signal(str)
    focused = Signal()

    def __init__(self, start_path: str | Path, parent=None) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Native ExplorerBrowser is Windows-only.")
        app = QApplication.instance()
        if app is not None and app.platformName().lower() == "offscreen":
            raise RuntimeError("Native ExplorerBrowser is unavailable on the offscreen Qt platform.")
        super().__init__(parent)
        self.kind = "windows"
        self.current_path = str(Path(start_path).expanduser())
        self._browser: _NativeExplorerBrowser | None = None
        self._pending_path = self.current_path
        self.setAcceptDrops(True)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors, False)
        self._path_timer = QTimer(self)
        self._path_timer.setInterval(500)
        self._path_timer.timeout.connect(self._poll_current_path)
        self._path_timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._ensure_browser()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._browser:
            self._browser.resize(self.width(), self.height())

    def focusInEvent(self, event) -> None:
        self.focused.emit()
        super().focusInEvent(event)

    def closeEvent(self, event) -> None:
        self._destroy_browser()
        super().closeEvent(event)

    def set_path(self, path: str) -> None:
        target = Path(path).expanduser()
        if not target.exists() or not target.is_dir():
            return
        self.current_path = str(target)
        self._pending_path = self.current_path
        if self._browser:
            if self._browser.browse(self.current_path):
                self.path_changed.emit(self.current_path)

    def refresh(self) -> None:
        self.set_path(self.current_path)

    def set_items(self, _items) -> None:
        self.refresh()

    def selected_paths(self) -> list[str]:
        if not self._browser:
            return []
        return self._browser.selected_paths()

    def selected_path(self) -> str:
        paths = self.selected_paths()
        return paths[0] if paths else ""

    def selected_is_dir(self) -> bool:
        path = self.selected_path()
        return Path(path).is_dir() if path else False

    def focus_tree(self) -> None:
        self.setFocus()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(ANDROID_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(ANDROID_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        mime: QMimeData = event.mimeData()
        if not mime.hasFormat(ANDROID_MIME):
            event.ignore()
            return
        data = bytes(mime.data(ANDROID_MIME)).decode("utf-8", "replace")
        paths = [line for line in data.splitlines() if line]
        if paths:
            self.dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _ensure_browser(self) -> None:
        if self._browser:
            return
        hwnd = int(self.winId())
        self._browser = _NativeExplorerBrowser(hwnd, self._pending_path)
        self._browser.resize(self.width(), self.height())
        self.path_changed.emit(self.current_path)

    def _destroy_browser(self) -> None:
        if self._browser:
            self._browser.close()
            self._browser = None

    def _poll_current_path(self) -> None:
        if not self._browser:
            return
        path = self._browser.current_path()
        if path and os.path.normcase(path) != os.path.normcase(self.current_path):
            self.current_path = path
            self.path_changed.emit(path)


def _failed(hr: int) -> bool:
    return int(hr) < 0
