from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QUrl, Signal
from PySide6.QtGui import QDrag, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileIconProvider,
    QFileSystemModel,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from openadb.ui.widgets.file_panel import ANDROID_MIME
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.material_icons import material_icon


class MaterialFileIconProvider(QFileIconProvider):
    def icon(self, file_or_type):
        if hasattr(file_or_type, "isDir"):
            return material_icon("folder" if file_or_type.isDir() else "draft")
        mapping = {
            QFileIconProvider.Computer: "computer",
            QFileIconProvider.Drive: "folder",
            QFileIconProvider.Folder: "folder",
            QFileIconProvider.File: "draft",
            QFileIconProvider.Trashcan: "delete",
        }
        name = mapping.get(file_or_type)
        return material_icon(name) if name else super().icon(file_or_type)


class WindowsFileTree(QTreeView):
    dropped = Signal(list)
    up_requested = Signal()
    refresh_requested = Signal()
    open_current_requested = Signal()
    rename_requested = Signal()
    delete_requested = Signal()
    focused = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.setAnimated(False)
        self.setSortingEnabled(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerItem)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setTextElideMode(Qt.ElideRight)

    def selected_paths(self) -> list[str]:
        model = self.model()
        rows = self.selectionModel().selectedRows(0) if self.selectionModel() else []
        paths: list[str] = []
        for index in rows:
            path = model.filePath(index) if isinstance(model, QFileSystemModel) else ""
            if path:
                paths.append(path)
        return paths

    def selected_is_dir(self) -> bool:
        model = self.model()
        rows = self.selectionModel().selectedRows(0) if self.selectionModel() else []
        if not rows or not isinstance(model, QFileSystemModel):
            return False
        return model.isDir(rows[0])

    def focusInEvent(self, event) -> None:
        self.focused.emit()
        super().focusInEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.open_current_requested.emit()
            return
        if event.key() == Qt.Key_Backspace:
            self.up_requested.emit()
            return
        if event.key() == Qt.Key_F5:
            self.refresh_requested.emit()
            return
        if event.key() == Qt.Key_F2:
            self.rename_requested.emit()
            return
        if event.key() == Qt.Key_Delete:
            self.delete_requested.emit()
            return
        super().keyPressEvent(event)

    def startDrag(self, supportedActions: Qt.DropActions) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

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
        mime = event.mimeData()
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


class WindowsFilePanel(QWidget):
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

    def __init__(self, start_path: str | Path, parent=None, show_path_bar: bool = True, show_button_row: bool = True) -> None:
        super().__init__(parent)
        self.kind = "windows"
        self.current_path = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.path_bar = QWidget()
        top = QHBoxLayout(self.path_bar)
        top.setContentsMargins(0, 0, 0, 0)
        self.path_edit = QLineEdit()
        self.path_edit.returnPressed.connect(lambda: self.navigate_requested.emit(self.path_edit.text()))
        top.addWidget(self.path_edit, 1)
        self.up_button = QToolButton()
        self.up_button.setText("Up")
        self.up_button.clicked.connect(self.up_requested.emit)
        top.addWidget(self.up_button)
        self.refresh_button = QToolButton()
        self.refresh_button.setText("Refresh")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        top.addWidget(self.refresh_button)
        layout.addWidget(self.path_bar)
        self.path_bar.setVisible(show_path_bar)

        self.button_bar = QWidget()
        button_row = QHBoxLayout(self.button_bar)
        button_row.setContentsMargins(0, 0, 0, 0)
        self.new_button = QPushButton("New folder")
        self.delete_button = QPushButton("Delete")
        self.rename_button = QPushButton("Rename")
        self.transfer_button = QPushButton("Push to device")
        self.copy_button = QPushButton("Copy path")
        self.properties_button = QPushButton("Properties")
        self.external_button = QPushButton("Open in Explorer")
        for button, signal in [
            (self.new_button, self.new_folder_requested),
            (self.delete_button, self.delete_requested),
            (self.rename_button, self.rename_requested),
            (self.transfer_button, self.transfer_requested),
            (self.copy_button, self.copy_path_requested),
            (self.properties_button, self.properties_requested),
            (self.external_button, self.open_external_requested),
        ]:
            button.clicked.connect(signal.emit)
            button_row.addWidget(button)
        button_row.addStretch()
        layout.addWidget(self.button_bar)
        self.button_bar.setVisible(show_button_row)

        self.model = QFileSystemModel(self)
        self.file_icon_provider = MaterialFileIconProvider()
        self.model.setIconProvider(self.file_icon_provider)
        self.model.setReadOnly(False)
        self.model.setResolveSymlinks(False)
        self.tree = WindowsFileTree()
        self.tree.setModel(self.model)
        self.tree.dropped.connect(self.dropped.emit)
        self.tree.up_requested.connect(self.up_requested.emit)
        self.tree.refresh_requested.connect(self.refresh_requested.emit)
        self.tree.open_current_requested.connect(self.open_selected)
        self.tree.rename_requested.connect(self.rename_requested.emit)
        self.tree.delete_requested.connect(self.delete_requested.emit)
        self.tree.doubleClicked.connect(self._open_index)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.empty_state = EmptyState(
            "Empty folder",
            "This Windows folder does not contain any visible files.",
            "Refresh folder",
        )
        self.content = QStackedWidget()
        self.content.addWidget(self.tree)
        self.content.addWidget(self.empty_state)
        self.empty_state.action_requested.connect(self.refresh)
        self.model.directoryLoaded.connect(self._directory_loaded)
        layout.addWidget(self.content, 1)

        self.set_path(str(start_path))

    def set_path(self, path: str) -> None:
        target = Path(path).expanduser()
        if not target.exists() or not target.is_dir():
            return
        resolved = str(target)
        self.current_path = resolved
        self.path_edit.setText(resolved)
        index = self.model.setRootPath(resolved)
        self.tree.setRootIndex(index)
        self.content.setCurrentWidget(self.tree)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.path_changed.emit(resolved)

    def _directory_loaded(self, path: str) -> None:
        try:
            if Path(path).resolve() != Path(self.current_path).resolve():
                return
        except OSError:
            if str(path) != self.current_path:
                return
        root = self.tree.rootIndex()
        self.content.setCurrentWidget(self.empty_state if self.model.rowCount(root) == 0 else self.tree)

    def refresh(self) -> None:
        current = self.current_path
        self.model.setRootPath("")
        self.set_path(current)

    def set_items(self, _items) -> None:
        self.refresh()

    def selected_paths(self) -> list[str]:
        return self.tree.selected_paths()

    def selected_path(self) -> str:
        paths = self.selected_paths()
        return paths[0] if paths else ""

    def selected_is_dir(self) -> bool:
        return self.tree.selected_is_dir()

    def copy_current_path(self) -> None:
        QGuiApplication.clipboard().setText(self.selected_path() or self.current_path)

    def focus_tree(self) -> None:
        self.tree.setFocus()

    def open_selected(self) -> None:
        path = self.selected_path()
        if path and self.selected_is_dir():
            self.navigate_requested.emit(path)

    def _open_index(self, index) -> None:
        if self.model.isDir(index):
            self.navigate_requested.emit(self.model.filePath(index))
