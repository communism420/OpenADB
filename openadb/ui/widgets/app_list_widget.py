from __future__ import annotations

import html

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from openadb.core.safety import is_dangerous_package
from openadb.models.app_info import AppInfo
from openadb.ui.performance import optimize_table


PACKAGE_ROLE = Qt.UserRole
SORT_ROLE = Qt.UserRole + 2


class AppTable(QTableWidget):
    selection_changed = Signal()

    COLUMNS = ["", "", "Application", "Bloatware", "Type", "State", "Size"]
    COLUMN_MIN_WIDTHS = {
        0: 42,
        1: 64,
        2: 220,
        3: 128,
        4: 72,
        5: 82,
        6: 92,
    }
    COLUMN_MAX_WIDTHS = {
        0: 42,
        1: 64,
        2: 620,
        3: 260,
        4: 120,
        5: 130,
        6: 150,
    }
    DETAIL_COLUMNS = {
        "app_label": 2,
        "bloatware": 3,
        "app_type": 4,
        "state": 5,
        "size": 6,
    }

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.apps: list[AppInfo] = []
        self._app_by_package: dict[str, AppInfo] = {}
        self._resize_columns_pending = False
        self.setObjectName("appsTable")
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        optimize_table(self)
        self.setIconSize(QSize(42, 42))
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        for col in range(2, len(self.COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
        header.setMinimumSectionSize(44)
        for col, width in self.COLUMN_MIN_WIDTHS.items():
            self.setColumnWidth(col, width)
        self.setSortingEnabled(True)
        self.itemChanged.connect(lambda _item: self.selection_changed.emit())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_column_resize()
        QTimer.singleShot(50, self._resize_columns_to_content)
        QTimer.singleShot(200, self._resize_columns_to_content)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_column_resize()

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
            check.setData(PACKAGE_ROLE, app.package_name)
            self.setItem(row, 0, check)
            icon_item = QTableWidgetItem()
            icon_item.setData(PACKAGE_ROLE, app.package_name)
            icon_item.setIcon(QIcon(app.icon_path) if app.icon_path else self._fallback_icon(app))
            icon_item.setTextAlignment(Qt.AlignCenter)
            self.setItem(row, 1, icon_item)

            name_item = QTableWidgetItem(self._label_text(app))
            name_item.setData(PACKAGE_ROLE, app.package_name)
            name_item.setData(SORT_ROLE, self._label_text(app).lower())
            name_item.setData(Qt.UserRole + 10, is_dangerous_package(app.package_name))
            name_item.setToolTip(self._details_tooltip(app))
            name_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            if is_dangerous_package(app.package_name):
                name_item.setForeground(QColor("#ffb4ab"))
            self.setItem(row, 2, name_item)

            values = [self._bloatware_text(app), app.app_type, app.state, app.size]
            for col, value in enumerate(values, start=3):
                item = QTableWidgetItem(value)
                item.setData(PACKAGE_ROLE, app.package_name)
                item.setToolTip(self._details_tooltip(app))
                if col == self.DETAIL_COLUMNS["bloatware"]:
                    item.setForeground(self._bloatware_color(app))
                if is_dangerous_package(app.package_name):
                    item.setForeground(QColor("#c42b1c"))
                    item.setToolTip("Potentially dangerous Android package")
                item.setTextAlignment(Qt.AlignCenter)
                self.setItem(row, col, item)
            self.setRowHeight(row, 66)
        self.blockSignals(False)
        self.setSortingEnabled(True)
        self.sortItems(2, Qt.AscendingOrder)
        self.setUpdatesEnabled(True)
        self._resize_columns_to_content()

    def checked_apps(self) -> list[AppInfo]:
        selected: list[AppInfo] = []
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item and item.checkState() == Qt.Checked and not self.isRowHidden(row):
                app = self._app_by_package.get(str(item.data(PACKAGE_ROLE) or ""))
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

    def visible_count(self) -> int:
        return sum(1 for row in range(self.rowCount()) if not self.isRowHidden(row))

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
        if updated.size and updated.size.strip().lower() != "unknown":
            app.size = updated.size
        elif not app.size:
            app.size = "Unknown"
        app.icon_path = updated.icon_path or app.icon_path
        app.bloatware_removal = updated.bloatware_removal or app.bloatware_removal
        app.bloatware_list = updated.bloatware_list or app.bloatware_list
        app.bloatware_description = updated.bloatware_description or app.bloatware_description
        app.bloatware_labels = updated.bloatware_labels or app.bloatware_labels
        app.metadata_checked = bool(updated.metadata_checked or app.metadata_checked)
        app.assets_checked = bool(updated.assets_checked or app.assets_checked)
        for row in self._rows_for_package(updated.package_name):
            values = {
                "app_label": self._label_text(app),
                "bloatware": self._bloatware_text(app),
                "size": app.size,
            }
            for key, value in values.items():
                col = self.DETAIL_COLUMNS[key]
                item = self.item(row, col)
                if item:
                    item.setText(value)
                    item.setToolTip(self._details_tooltip(app))
                    if key == "app_label":
                        item.setData(SORT_ROLE, self._label_text(app).lower())
                        if is_dangerous_package(app.package_name):
                            item.setForeground(QColor("#ffb4ab"))
                    elif key == "bloatware":
                        item.setForeground(self._bloatware_color(app))
            if app.icon_path:
                icon_item = self.item(row, 1)
                if icon_item:
                    icon_item.setIcon(QIcon(app.icon_path))
        self._schedule_column_resize()

    def _label_text(self, app: AppInfo) -> str:
        label = self._clean_label_text(app.app_label)
        return label if label else app.package_name

    def _details_tooltip(self, app: AppInfo) -> str:
        lines = [
            f"Name: {self._label_text(app)}",
            f"Package: {app.package_name}",
            f"Type: {app.app_type}",
            f"State: {app.state}",
            f"Size: {app.size or 'Unknown'}",
        ]
        if app.bloatware_removal:
            lines.append(f"UAD: {self._bloatware_text(app)}")
            if app.bloatware_description:
                lines.append(app.bloatware_description)
        else:
            lines.append("UAD: Not listed")
        if app.version_name:
            lines.append(f"Version: {app.version_name}")
        if app.version_code:
            lines.append(f"Version code: {app.version_code}")
        if app.apk_path_text:
            lines.append(f"APK: {app.apk_path_text}")
        return "\n".join(lines)

    def _clean_label_text(self, value: str) -> str:
        text = html.unescape(value or "").strip()
        if not text:
            return ""
        lowered = text.lower()
        if lowered in {"not extracted", "unknown", "none", "null"}:
            return ""
        if text.startswith("@") or lowered.startswith("0x"):
            return ""
        if any(token in lowered for token in ("<", ">", "type 0x", "0x", "resource id")):
            return ""
        if len(text) > 72:
            return text[:69].rstrip() + "..."
        return " ".join(text.split())

    def apply_filter(self, text: str, mode: str) -> None:
        query = text.strip().lower()
        for row in range(self.rowCount()):
            package_item = self.item(row, 2)
            app = self._app_by_package.get(str(package_item.data(PACKAGE_ROLE) or "")) if package_item else None
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
            elif mode == "Bloatware" and app.bloatware_removal not in {"Recommended", "Advanced", "Expert"}:
                visible = False
            elif mode == "Unsafe" and app.bloatware_removal != "Unsafe":
                visible = False
            self.setRowHidden(row, not visible)
        self._schedule_column_resize()

    def _schedule_column_resize(self) -> None:
        if self._resize_columns_pending:
            return
        self._resize_columns_pending = True
        QTimer.singleShot(0, self._resize_columns_to_content)

    def _resize_columns_to_content(self) -> None:
        self._resize_columns_pending = False
        if self.columnCount() <= 0:
            return
        metrics = self.fontMetrics()
        widths: list[int] = []
        for col in range(self.columnCount()):
            width = metrics.horizontalAdvance(self.COLUMNS[col]) + 34
            for row in range(self.rowCount()):
                if self.isRowHidden(row):
                    continue
                item = self.item(row, col)
                if not item:
                    continue
                width = max(width, metrics.horizontalAdvance(item.text()) + 28)
            minimum = self.COLUMN_MIN_WIDTHS.get(col, 72)
            maximum = self.COLUMN_MAX_WIDTHS.get(col, 320)
            widths.append(min(max(width, minimum), maximum))

        available_width = max(0, self.viewport().width())
        extra_width = available_width - sum(widths)
        if extra_width > 0:
            for col in (2, 3, 6):
                maximum = self.COLUMN_MAX_WIDTHS.get(col, widths[col])
                growth = min(extra_width, max(0, maximum - widths[col]))
                widths[col] += growth
                extra_width -= growth
                if extra_width <= 0:
                    break
            if extra_width > 0:
                widths[2] += extra_width

        for col, width in enumerate(widths):
            self.setColumnWidth(col, width)

    def _bloatware_text(self, app: AppInfo) -> str:
        removal = (app.bloatware_removal or "").strip()
        if not removal:
            return "Not listed"
        source = (app.bloatware_list or "").strip()
        return f"{removal} / {source}" if source else removal

    def _bloatware_color(self, app: AppInfo) -> QColor:
        removal = (app.bloatware_removal or "").strip()
        if removal == "Recommended":
            return QColor("#80d47c")
        if removal == "Advanced":
            return QColor("#ffd166")
        if removal == "Expert":
            return QColor("#ffb86c")
        if removal == "Unsafe":
            return QColor("#ff8a80")
        return QColor("#9aa4af")

    def sort_by_label(self) -> None:
        self.sortItems(2, Qt.AscendingOrder)

    def _rows_for_package(self, package_name: str) -> list[int]:
        rows = []
        for row in range(self.rowCount()):
            item = self.item(row, 2)
            if item and item.data(PACKAGE_ROLE) == package_name:
                rows.append(row)
        return rows

    def _fallback_icon(self, app: AppInfo | None) -> QIcon:
        pixmap = QPixmap(42, 42)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QColor("#008577"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 38, 38)
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(11, 15, 20, 15, 3, 3)
        painter.drawRect(14, 29, 3, 4)
        painter.drawRect(25, 29, 3, 4)
        painter.setBrush(QColor("#008577"))
        painter.drawEllipse(15, 20, 3, 3)
        painter.drawEllipse(24, 20, 3, 3)
        painter.setPen(QPen(QColor("#ffffff"), 1.4))
        painter.drawLine(15, 15, 11, 10)
        painter.drawLine(27, 15, 31, 10)
        painter.end()
        return QIcon(pixmap)
