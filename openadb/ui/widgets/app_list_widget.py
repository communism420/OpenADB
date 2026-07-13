from __future__ import annotations

import html
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
)

from openadb.core.safety import is_dangerous_package
from openadb.models.app_info import AppInfo
from openadb.ui.design_system import DARK_COLORS, LIGHT_COLORS
from openadb.ui.performance import optimize_table
from openadb.ui.material_icons import material_icon


PACKAGE_ROLE = Qt.UserRole
SORT_ROLE = Qt.UserRole + 2

APP_TYPE_FILTERS = {"all", "user", "system"}
APP_STATE_FILTERS = {"any", "enabled", "disabled"}
APP_UAD_FILTERS = {"any", "recommended", "advanced", "expert", "unsafe", "not listed"}
APP_SORT_MODES = {"name", "size_desc", "size_asc"}


@dataclass(frozen=True, slots=True)
class AppFilterState:
    """Independent, normalized local filters for the applications table."""

    search_text: str = ""
    app_type: str = "all"
    app_state: str = "any"
    uad_category: str = "any"

    @classmethod
    def from_values(
        cls,
        search_text: str = "",
        app_type: str = "all",
        app_state: str = "any",
        uad_category: str = "any",
    ) -> AppFilterState:
        normalized_type = str(app_type or "").strip().casefold()
        normalized_state = str(app_state or "").strip().casefold()
        normalized_uad = str(uad_category or "").strip().casefold()
        return cls(
            search_text=str(search_text or "").strip(),
            app_type=normalized_type if normalized_type in APP_TYPE_FILTERS else "all",
            app_state=normalized_state if normalized_state in APP_STATE_FILTERS else "any",
            uad_category=normalized_uad if normalized_uad in APP_UAD_FILTERS else "any",
        )

    def matches(self, app: AppInfo, uad_category: str) -> bool:
        query = self.search_text.casefold()
        if query and query not in app.display_name.casefold() and query not in app.package_name.casefold():
            return False
        if self.app_type != "all" and app.app_type.casefold() != self.app_type:
            return False
        if self.app_state != "any" and app.state.casefold() != self.app_state:
            return False
        return self.uad_category == "any" or uad_category.casefold() == self.uad_category


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
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(75)
        self._resize_timer.timeout.connect(self._resize_columns_to_content)
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
        self.itemChanged.connect(self._table_item_changed)

    def _table_item_changed(self, item: QTableWidgetItem) -> None:
        # Metadata, labels, sizes and semantic colors are updated in-place while
        # background loaders run. Only the checkbox column represents selection.
        if item.column() == 0:
            self.selection_changed.emit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_column_resize()
        QTimer.singleShot(50, self._resize_columns_to_content)
        QTimer.singleShot(200, self._resize_columns_to_content)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_column_resize()

    def set_apps(self, apps: list[AppInfo], sort_by_label: bool = True, checked_packages: set[str] | None = None) -> None:
        self.setUpdatesEnabled(False)
        self.setSortingEnabled(False)
        self.blockSignals(True)
        checked_packages = checked_packages or set()
        self.apps = list(apps)
        for app in self.apps:
            app.app_label = self._clean_label_text(app.app_label)
        self._app_by_package = {app.package_name: app for app in self.apps}
        self.setRowCount(len(apps))
        for row, app in enumerate(apps):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if app.package_name in checked_packages else Qt.Unchecked)
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
                name_item.setForeground(self._semantic_item_color("danger"))
            self.setItem(row, 2, name_item)

            values = [self._bloatware_text(app), app.app_type, app.state, app.size]
            for col, value in enumerate(values, start=3):
                item = QTableWidgetItem(value)
                item.setData(PACKAGE_ROLE, app.package_name)
                item.setData(SORT_ROLE, self._sort_value_for_column(col, app, value))
                item.setToolTip(self._details_tooltip(app))
                if col == self.DETAIL_COLUMNS["bloatware"]:
                    item.setForeground(self._bloatware_color(app))
                if is_dangerous_package(app.package_name):
                    item.setForeground(self._semantic_item_color("danger"))
                    item.setToolTip("Potentially dangerous Android package")
                item.setTextAlignment(Qt.AlignCenter)
                self.setItem(row, col, item)
            self.setRowHeight(row, 66)
        self.blockSignals(False)
        if sort_by_label:
            self.setSortingEnabled(True)
            self.sort_by_label()
        else:
            self.setSortingEnabled(False)
        self.setUpdatesEnabled(True)
        self._resize_columns_to_content()

    def set_apps_sorted(
        self,
        apps: list[AppInfo],
        sort_mode: str = "name",
        checked_packages: set[str] | None = None,
    ) -> None:
        mode = sort_mode if sort_mode in APP_SORT_MODES else "name"
        ordered_apps = list(apps)
        if mode in {"size_desc", "size_asc"}:
            descending = mode == "size_desc"
            ordered_apps.sort(key=lambda app: self._size_sort_key(app, descending))
        self.set_apps(
            ordered_apps,
            sort_by_label=mode == "name",
            checked_packages=checked_packages,
        )

    def checked_apps(self, include_hidden: bool = False) -> list[AppInfo]:
        selected: list[AppInfo] = []
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item and item.checkState() == Qt.Checked and (include_hidden or not self.isRowHidden(row)):
                app = self._app_by_package.get(str(item.data(PACKAGE_ROLE) or ""))
                if app:
                    selected.append(app)
        return selected

    def checked_package_names(self) -> set[str]:
        packages: set[str] = set()
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                package_name = str(item.data(PACKAGE_ROLE) or "")
                if package_name:
                    packages.add(package_name)
        return packages

    def select_all_visible(self) -> None:
        self._set_visible_check_state(Qt.Checked)

    def unselect_all_visible(self) -> None:
        self._set_visible_check_state(Qt.Unchecked)

    def unselect_all(self) -> None:
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item = self.item(row, 0)
                if item is not None:
                    item.setCheckState(Qt.Unchecked)
        finally:
            self.blockSignals(False)
        self.selection_changed.emit()

    def visible_count(self) -> int:
        return sum(1 for row in range(self.rowCount()) if not self.isRowHidden(row))

    def visible_package_names(self) -> set[str]:
        packages: set[str] = set()
        for row in range(self.rowCount()):
            if self.isRowHidden(row):
                continue
            item = self.item(row, 0)
            package_name = str(item.data(PACKAGE_ROLE) or "") if item else ""
            if package_name:
                packages.add(package_name)
        return packages

    def visible_checked_count(self) -> int:
        return sum(
            1
            for row in range(self.rowCount())
            if not self.isRowHidden(row)
            and self.item(row, 0) is not None
            and self.item(row, 0).checkState() == Qt.Checked
        )

    def _set_visible_check_state(self, state: Qt.CheckState) -> None:
        self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                if self.isRowHidden(row):
                    continue
                item = self.item(row, 0)
                if item is not None:
                    item.setCheckState(state)
        finally:
            self.blockSignals(False)
        self.selection_changed.emit()

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
                            item.setForeground(self._semantic_item_color("danger"))
                    elif key == "bloatware":
                        item.setForeground(
                            self._semantic_item_color("danger")
                            if is_dangerous_package(app.package_name)
                            else self._bloatware_color(app)
                        )
                    elif key == "size":
                        item.setData(SORT_ROLE, self._size_sort_value(app.size))
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

    def apply_filters(self, filters: AppFilterState) -> int:
        visible_count = 0
        for row in range(self.rowCount()):
            package_item = self.item(row, 2)
            app = self._app_by_package.get(str(package_item.data(PACKAGE_ROLE) or "")) if package_item else None
            visible = app is not None and filters.matches(app, self._bloatware_category(app))
            hidden = not visible
            if self.isRowHidden(row) != hidden:
                self.setRowHidden(row, hidden)
            if visible:
                visible_count += 1
        return visible_count

    def _schedule_column_resize(self) -> None:
        if self._resize_columns_pending:
            return
        self._resize_columns_pending = True
        self._resize_timer.start()

    def _resize_columns_to_content(self) -> None:
        self._resize_timer.stop()
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
        removal = self._bloatware_category(app)
        if removal == "Not listed":
            return "Not listed"
        source = (app.bloatware_list or "").strip()
        return f"{removal} / {source}" if source else removal

    def _bloatware_color(self, app: AppInfo) -> QColor:
        removal = self._bloatware_category(app)
        if removal == "Recommended":
            return self._semantic_item_color("success")
        if removal == "Advanced":
            return self._semantic_item_color("warning")
        if removal == "Expert":
            return self._semantic_item_color("expert")
        if removal == "Unsafe":
            return self._semantic_item_color("danger")
        return self._semantic_item_color("secondary")

    @staticmethod
    def _semantic_item_color(role: str) -> QColor:
        app = QApplication.instance()
        dark = app is not None and app.property("openadbResolvedTheme") == "Dark"
        colors = DARK_COLORS if dark else LIGHT_COLORS
        values = {
            "success": colors.success,
            "warning": colors.warning,
            "expert": "#ffd0a8" if dark else "#7a3e00",
            "danger": colors.danger,
            "secondary": colors.text_secondary,
        }
        return QColor(values[role])

    def refresh_semantic_colors(self) -> None:
        for row in range(self.rowCount()):
            package_item = self.item(row, 2)
            if package_item is None:
                continue
            app = self._app_by_package.get(str(package_item.data(PACKAGE_ROLE) or ""))
            if app is None:
                continue
            dangerous = is_dangerous_package(app.package_name)
            if dangerous:
                package_item.setForeground(self._semantic_item_color("danger"))
            bloatware_item = self.item(row, self.DETAIL_COLUMNS["bloatware"])
            if bloatware_item is not None:
                bloatware_item.setForeground(
                    self._semantic_item_color("danger") if dangerous else self._bloatware_color(app)
                )
            if dangerous:
                for column in range(4, self.columnCount()):
                    item = self.item(row, column)
                    if item is not None:
                        item.setForeground(self._semantic_item_color("danger"))
        self.viewport().update()

    def _bloatware_category(self, app: AppInfo) -> str:
        removal = (app.bloatware_removal or "").strip()
        return removal if removal in {"Recommended", "Advanced", "Expert", "Unsafe"} else "Not listed"

    def sort_by_label(self) -> None:
        self.setSortingEnabled(True)
        self.sortItems(2, Qt.AscendingOrder)

    def apply_sort(self, sort_mode: str) -> None:
        mode = sort_mode if sort_mode in APP_SORT_MODES else "name"
        if mode == "size_desc":
            self.sort_by_size(descending=True)
        elif mode == "size_asc":
            self.sort_by_size(descending=False)
        else:
            self.sort_by_label()

    def sort_by_size(self, descending: bool) -> None:
        checked = self.checked_package_names()
        self.set_apps_sorted(
            self.apps,
            sort_mode="size_desc" if descending else "size_asc",
            checked_packages=checked,
        )

    def _size_sort_key(self, app: AppInfo, descending: bool) -> tuple[bool, int, str, str]:
        size = self._size_sort_value(app.size)
        unknown = size < 0
        ordered_size = -size if descending and not unknown else size
        return (unknown, ordered_size, self._label_text(app).lower(), app.package_name.lower())

    def _sort_value_for_column(self, col: int, app: AppInfo, value: str) -> object:
        if col == self.DETAIL_COLUMNS["bloatware"]:
            return self._bloatware_text(app).lower()
        if col == self.DETAIL_COLUMNS["app_type"]:
            return app.app_type.lower()
        if col == self.DETAIL_COLUMNS["state"]:
            return app.state.lower()
        if col == self.DETAIL_COLUMNS["size"]:
            return self._size_sort_value(value)
        return str(value or "").lower()

    def _size_sort_value(self, value: str) -> int:
        text = str(value or "").strip().replace(",", ".")
        if not text or text.lower() == "unknown":
            return -1
        parts = text.split()
        try:
            number = float(parts[0])
        except (ValueError, IndexError):
            return -1
        unit = parts[1].lower() if len(parts) > 1 else "b"
        multipliers = {
            "b": 1,
            "byte": 1,
            "bytes": 1,
            "kb": 1024,
            "kib": 1024,
            "mb": 1024**2,
            "mib": 1024**2,
            "gb": 1024**3,
            "gib": 1024**3,
            "tb": 1024**4,
            "tib": 1024**4,
        }
        return int(number * multipliers.get(unit, 1))

    def _rows_for_package(self, package_name: str) -> list[int]:
        rows = []
        for row in range(self.rowCount()):
            item = self.item(row, 2)
            if item and item.data(PACKAGE_ROLE) == package_name:
                rows.append(row)
        return rows

    def _fallback_icon(self, app: AppInfo | None) -> QIcon:
        return material_icon("apps", "primary")
