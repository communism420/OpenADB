from __future__ import annotations

from PySide6.QtCore import QMimeData, Qt, QUrl, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openadb.models.file_item import FileItem
from openadb.ui.performance import optimize_table


ANDROID_MIME = "application/x-openadb-android-paths"


class FileTable(QTableWidget):
    dropped = Signal(list)

    def __init__(self, kind: str, parent=None) -> None:
        super().__init__(0, 4, parent)
        self.kind = kind
        self.setHorizontalHeaderLabels(["Name", "Size", "Modified", "Type"])
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        optimize_table(self)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.CopyAction)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)

    def selected_paths(self) -> list[str]:
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        paths: list[str] = []
        for row in rows:
            item = self.item(row, 0)
            if item:
                paths.append(str(item.data(Qt.UserRole)))
        return paths

    def startDrag(self, supportedActions: Qt.DropActions) -> None:
        paths = self.selected_paths()
        if not paths:
            return
        mime = QMimeData()
        if self.kind == "windows":
            mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        else:
            mime.setData(ANDROID_MIME, "\n".join(paths).encode("utf-8"))
            mime.setText("\n".join(paths))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)

    def dragEnterEvent(self, event) -> None:
        if self._accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._accepts(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        paths: list[str] = []
        if self.kind == "android" and mime.hasUrls():
            paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        elif self.kind == "windows" and mime.hasFormat(ANDROID_MIME):
            data = bytes(mime.data(ANDROID_MIME)).decode("utf-8", "replace")
            paths = [line for line in data.splitlines() if line]
        if paths:
            self.dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _accepts(self, mime: QMimeData) -> bool:
        return (self.kind == "android" and mime.hasUrls()) or (self.kind == "windows" and mime.hasFormat(ANDROID_MIME))


class FilePanel(QWidget):
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

    def __init__(self, title: str, kind: str, parent=None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.current_path = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.returnPressed.connect(lambda: self.navigate_requested.emit(self.path_edit.text()))
        top.addWidget(self.path_edit)
        self.up_button = QToolButton()
        self.up_button.setText("Up")
        self.up_button.clicked.connect(self.up_requested.emit)
        top.addWidget(self.up_button)
        self.refresh_button = QToolButton()
        self.refresh_button.setText("Refresh")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)

        button_row = QHBoxLayout()
        self.new_button = QPushButton("New folder")
        self.delete_button = QPushButton("Delete")
        self.rename_button = QPushButton("Rename")
        self.transfer_button = QPushButton("Pull to PC" if kind == "android" else "Push to device")
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
        ]:
            button.clicked.connect(signal.emit)
            button_row.addWidget(button)
        if kind == "windows":
            self.external_button.clicked.connect(self.open_external_requested.emit)
            button_row.addWidget(self.external_button)
        layout.addLayout(button_row)

        self.table = FileTable(kind)
        self.table.dropped.connect(self.dropped.emit)
        self.table.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self.table, 1)

    def set_path(self, path: str) -> None:
        self.current_path = path
        self.path_edit.setText(path)

    def set_items(self, items: list[FileItem]) -> None:
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(items))
        dir_icon = self.style().standardIcon(QStyle.SP_DirIcon)
        file_icon = self.style().standardIcon(QStyle.SP_FileIcon)
        for row, item in enumerate(items):
            name = QTableWidgetItem(item.name)
            name.setIcon(dir_icon if item.is_dir else file_icon)
            name.setData(Qt.UserRole, item.path)
            name.setData(Qt.UserRole + 1, item.is_dir)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(item.size_text))
            self.table.setItem(row, 2, QTableWidgetItem(item.modified))
            self.table.setItem(row, 3, QTableWidgetItem(item.item_type or ("Folder" if item.is_dir else "File")))
        self.table.resizeColumnToContents(1)
        self.table.resizeColumnToContents(2)
        self.table.resizeColumnToContents(3)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setUpdatesEnabled(True)

    def selected_paths(self) -> list[str]:
        return self.table.selected_paths()

    def selected_path(self) -> str:
        paths = self.selected_paths()
        return paths[0] if paths else ""

    def selected_is_dir(self) -> bool:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return False
        item = self.table.item(rows[0].row(), 0)
        return bool(item.data(Qt.UserRole + 1)) if item else False

    def _open_item(self, item: QTableWidgetItem) -> None:
        row = item.row()
        name = self.table.item(row, 0)
        if name and bool(name.data(Qt.UserRole + 1)):
            self.navigate_requested.emit(str(name.data(Qt.UserRole)))
