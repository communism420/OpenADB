from __future__ import annotations

import html

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from openadb.core.safety import is_dangerous_package
from openadb.models.app_info import AppInfo
from openadb.ui.performance import optimize_table


class AppTable(QTableWidget):
    selection_changed = Signal()

    COLUMNS = ["", "Icon", "App label", "Package name", "Type", "State", "Version", "Code", "APK path", "Size"]
    DETAIL_COLUMNS = {
        "app_label": 2,
        "package_name": 3,
        "app_type": 4,
        "state": 5,
        "version_name": 6,
        "version_code": 7,
        "apk_path": 8,
        "size": 9,
    }

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.apps: list[AppInfo] = []
        self._app_by_package: dict[str, AppInfo] = {}
        self.setObjectName("appsTable")
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setShowGrid(False)
        optimize_table(self)
        self.setIconSize(QSize(30, 30))
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setMinimumSectionSize(44)
        self.setColumnWidth(0, 44)
        self.setColumnWidth(1, 52)
        self.setColumnWidth(4, 82)
        self.setColumnWidth(5, 92)
        self.setColumnWidth(6, 120)
        self.setColumnWidth(7, 110)
        self.setColumnWidth(8, 260)
        self.setColumnWidth(9, 90)
        self.setSortingEnabled(True)
        self.itemChanged.connect(lambda _item: self.selection_changed.emit())

    def set_apps(self, apps: list[AppInfo]) -> None:
        self.setUpdatesEnabled(False)
        self.setSortingEnabled(False)
        self.blockSignals(True)
        self.apps = list(apps)
        for app in self.apps:
            app.app_label = self._clean_label_text(app.app_label)
        self._app_by_package = {app.package_name: app for app in self.apps}
        self.setRowCount(len(apps))
        for row, app in enumerate(apps):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Unchecked)
            check.setData(Qt.UserRole, app.package_name)
            self.setItem(row, 0, check)
            icon_item = QTableWidgetItem()
            icon_item.setData(Qt.UserRole, app.package_name)
            icon_item.setIcon(QIcon(app.icon_path) if app.icon_path else self._fallback_icon(app))
            self.setItem(row, 1, icon_item)
            values = [
                self._label_text(app),
                app.package_name,
                app.app_type,
                app.state,
                app.version_name,
                app.version_code,
                app.apk_path_text,
                app.size,
            ]
            for col, value in enumerate(values, start=2):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, app.package_name)
                if is_dangerous_package(app.package_name):
                    item.setForeground(QColor("#c42b1c"))
                    item.setToolTip("Potentially dangerous Android package")
                elif col == self.DETAIL_COLUMNS["app_label"] and not app.app_label.strip():
                    item.setForeground(QColor("#8f98a3"))
                    item.setToolTip("Real app label has not been extracted yet.")
                if col in {4, 5, 6, 7, 9}:
                    item.setTextAlignment(Qt.AlignCenter)
                self.setItem(row, col, item)
            self.setRowHeight(row, 42)
        self.blockSignals(False)
        self.setSortingEnabled(True)
        self.sortItems(2, Qt.AscendingOrder)
        self.setUpdatesEnabled(True)

    def checked_apps(self) -> list[AppInfo]:
        selected: list[AppInfo] = []
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item and item.checkState() == Qt.Checked and not self.isRowHidden(row):
                app = self._app_by_package.get(str(item.data(Qt.UserRole) or ""))
                if app:
                    selected.append(app)
        return selected

    def select_all_visible(self) -> None:
        for row in range(self.rowCount()):
            if not self.isRowHidden(row):
                self.item(row, 0).setCheckState(Qt.Checked)

    def unselect_all(self) -> None:
        for row in range(self.rowCount()):
            self.item(row, 0).setCheckState(Qt.Unchecked)

    def set_icon_for_package(self, package_name: str, icon_path: str) -> None:
        app = self._app_by_package.get(package_name)
        if app:
            app.icon_path = icon_path
        for row in self._rows_for_package(package_name):
            item = self.item(row, 1)
            if item:
                item.setIcon(QIcon(icon_path) if icon_path else self._fallback_icon(app))

    def update_app_details(self, updated: AppInfo) -> None:
        app = self._app_by_package.get(updated.package_name)
        if not app:
            return
        app.app_label = self._clean_label_text(updated.app_label) or self._clean_label_text(app.app_label)
        app.version_name = updated.version_name or app.version_name
        app.version_code = updated.version_code or app.version_code
        app.apk_paths = updated.apk_paths or app.apk_paths
        app.size = updated.size or app.size
        app.icon_path = updated.icon_path or app.icon_path
        for row in self._rows_for_package(updated.package_name):
            values = {
                "app_label": self._label_text(app),
                "version_name": app.version_name,
                "version_code": app.version_code,
                "apk_path": app.apk_path_text,
                "size": app.size,
            }
            for key, value in values.items():
                col = self.DETAIL_COLUMNS[key]
                item = self.item(row, col)
                if item:
                    item.setText(value)
                    if key == "app_label":
                        if app.app_label.strip():
                            item.setForeground(self.palette().text())
                            item.setToolTip(app.app_label)
                        else:
                            item.setForeground(QColor("#8f98a3"))
                            item.setToolTip("Real app label has not been extracted.")
            if app.icon_path:
                icon_item = self.item(row, 1)
                if icon_item:
                    icon_item.setIcon(QIcon(app.icon_path))

    def _label_text(self, app: AppInfo) -> str:
        label = self._clean_label_text(app.app_label)
        return label if label else "Not extracted"

    def _clean_label_text(self, value: str) -> str:
        text = html.unescape(value or "").strip()
        if not text:
            return ""
        lowered = text.lower()
        if text.startswith("@") or lowered.startswith("0x"):
            return ""
        if any(token in lowered for token in ("<", ">", "type 0x", "0x", "resource id")):
            return ""
        if len(text) > 72:
            return ""
        return " ".join(text.split())

    def apply_filter(self, text: str, mode: str) -> None:
        query = text.strip().lower()
        for row in range(self.rowCount()):
            package_item = self.item(row, 3)
            app = self._app_by_package.get(str(package_item.data(Qt.UserRole) or "")) if package_item else None
            if app is None:
                self.setRowHidden(row, True)
                continue
            visible = True
            if query and query not in app.package_name.lower() and query not in app.display_name.lower():
                visible = False
            if mode == "User apps" and app.app_type != "user":
                visible = False
            elif mode == "System apps" and app.app_type != "system":
                visible = False
            elif mode == "Enabled" and app.state != "enabled":
                visible = False
            elif mode == "Disabled" and app.state != "disabled":
                visible = False
            self.setRowHidden(row, not visible)

    def sort_by_label(self) -> None:
        self.sortItems(2, Qt.AscendingOrder)

    def _rows_for_package(self, package_name: str) -> list[int]:
        rows = []
        for row in range(self.rowCount()):
            item = self.item(row, 3)
            if item and item.data(Qt.UserRole) == package_name:
                rows.append(row)
        return rows

    def _fallback_icon(self, app: AppInfo | None) -> QIcon:
        text = ""
        if app:
            text = app.app_label.strip() or app.package_name.rsplit(".", 1)[-1] or app.package_name
        letter = (text[:1] or "?").upper()
        colors = ["#2563eb", "#0f766e", "#7c3aed", "#c2410c", "#be123c", "#4d7c0f", "#0369a1"]
        index = sum(ord(ch) for ch in (app.package_name if app else text)) % len(colors)
        pixmap = QPixmap(30, 30)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor(colors[index]))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(1, 1, 28, 28, 7, 7)
        font = QFont("Segoe UI", 12, QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(pixmap.rect(), Qt.AlignCenter, letter)
        painter.end()
        return QIcon(pixmap)
