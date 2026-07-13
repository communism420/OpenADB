from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.adb import ADBClient
from openadb.core.app_cache import AppInfoCache
from openadb.core.apps_controller import (
    AppsController,
    AppsProfileServices as _AppsProfileServices,
    CapturedProfileSettings as _CapturedProfileSettings,
)
from openadb.core.apk_metadata import APKMetadataExtractor
from openadb.core.backup_manager import BackupManager
from openadb.core.bloatware_db import BloatwareDatabase
from openadb.core.device import DeviceManager
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.icon_extractor import IconExtractor
from openadb.core.operations import OperationToken
from openadb.core.safety import is_dangerous_package
from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.ui.app_selection_model import AppSelectionModel, VisibleSelectionState
from openadb.ui.apps_action_workflow import AppsActionWorkflow
from openadb.ui.apps_data_workflow import AppsDataWorkflow
from openadb.ui.apps_filter_controller import AppsFilterController
from openadb.ui.design_system import configure_page_layout, set_button_role
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.widgets.app_list_widget import AppFilterState, AppTable
from openadb.ui.widgets.elided_label import ElidedLabel
from openadb.ui.workers import Worker, start_worker


class VisibleSelectionCheckBox(QCheckBox):
    """Two-state user control that can display a computed partial state."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setTristate(True)

    def nextCheckState(self) -> None:  # noqa: N802 - Qt API name
        self.setCheckState(Qt.Unchecked if self.checkState() == Qt.Checked else Qt.Checked)


class AppsPage(AppsDataWorkflow, AppsActionWorkflow, QWidget):
    refresh_device_requested = Signal()
    COMPACT_CONTROLS_MAX_WIDTH = 700

    def __init__(
        self,
        adb: ADBClient,
        backup_manager: BackupManager,
        device_manager: DeviceManager,
        icon_extractor: IconExtractor,
        settings: SettingsManager,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.adb = adb
        self.backup_manager = backup_manager
        self.device_manager = device_manager
        self.icon_extractor = icon_extractor
        self.apk_metadata = APKMetadataExtractor(settings)
        self.app_cache = AppInfoCache(settings)
        self.bloatware_db = BloatwareDatabase()
        self.settings = settings
        self.controller = AppsController(adb, device_manager, settings)
        self.operations = self.controller.operations
        self.filter_controller = AppsFilterController(settings)
        self.selection_model = AppSelectionModel()
        self.pool = QThreadPool.globalInstance()
        self.apps: list[AppInfo] = []
        self._apps_loading = False
        self._assets_loading = False
        self._metadata_cache_updates_since_flush = 0
        self._asset_cache_updates_since_flush = 0
        self._asset_progress_status = ""
        self._suppress_cache_save = False
        self._sort_mode = self.filter_controller.state.sort_mode
        self._bulk_operation_busy = False
        self._bulk_operation_name = ""
        self._refresh_after_bulk = False
        self._apps_load_token: OperationToken | None = None
        self._metadata_token: OperationToken | None = None
        self._assets_token: OperationToken | None = None
        self._bulk_token: OperationToken | None = None
        self._compact_controls = False
        self._device_mode = str(
            getattr(getattr(self.device_manager, "active", None), "mode", "No device") or "No device"
        )
        self._search_filter_timer = QTimer(self)
        self._search_filter_timer.setSingleShot(True)
        self._search_filter_timer.setInterval(120)
        self._focus_restore_timer = QTimer(self)
        self._focus_restore_timer.setSingleShot(True)
        self._focus_restore_timer.setInterval(0)
        self._focus_restore_timer.timeout.connect(self._restore_apps_focus)
        layout = QVBoxLayout(self)
        configure_page_layout(layout)

        header = QHBoxLayout()
        title = QLabel("Applications")
        title.setObjectName("pageTitle")
        self.total_label = ElidedLabel("Showing 0 of 0 applications")
        self.total_label.setObjectName("appCountLabel")
        self.active_filters_label = ElidedLabel("No active filters")
        self.active_filters_label.setObjectName("appFilterSummary")
        header.addWidget(title)
        header.addWidget(self.total_label, 1)
        layout.addLayout(header)

        toolbar = QFrame()
        toolbar.setObjectName("appsTopBar")
        toolbar_layout = QVBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 8, 8, 8)
        toolbar_layout.setSpacing(6)

        controls = QHBoxLayout()
        self.refresh_button = QPushButton("Load applications")
        set_button_role(self.refresh_button, "primary", compact=True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search application name or package...")
        self.sort_button = QPushButton("Sort: name")
        set_button_role(self.sort_button, "secondary", compact=True)
        self.sort_button.setToolTip("Choose application size sorting")
        self.page_actions_button = QToolButton()
        self.page_actions_button.setObjectName("appsPageActionsButton")
        self.page_actions_button.setText("Page actions")
        self.page_actions_button.setAccessibleName("Page actions")
        set_button_role(self.page_actions_button, "secondary", compact=True)
        self.page_actions_button.setPopupMode(QToolButton.InstantPopup)
        self.page_actions_menu = QMenu(self.page_actions_button)
        self.export_action = QAction("Export package list", self)
        self.clear_cache_action = QAction("Clear apps cache…", self)
        self.page_actions_menu.addAction(self.export_action)
        self.page_actions_menu.addAction(self.clear_cache_action)
        self.page_actions_button.setMenu(self.page_actions_menu)
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.search, 1)
        controls.addWidget(self.sort_button)
        controls.addWidget(self.page_actions_button)
        toolbar_layout.addLayout(controls)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.filters_button = QToolButton()
        self.filters_button.setObjectName("appsFiltersButton")
        self.filters_button.setText("Filters")
        self.filters_button.setPopupMode(QToolButton.InstantPopup)
        self.filters_menu = QMenu(self.filters_button)
        self.filters_button.setMenu(self.filters_menu)
        self._filter_values = {"type": "all", "state": "any", "uad": "any"}
        self._filter_action_groups: dict[str, QActionGroup] = {}
        self._filter_actions: dict[str, dict[str, QAction]] = {}
        self._add_filter_menu_group("type", "Type", [("All", "all"), ("User", "user"), ("System", "system")])
        self._add_filter_menu_group(
            "state",
            "State",
            [("Any", "any"), ("Enabled", "enabled"), ("Disabled", "disabled")],
        )
        self._add_filter_menu_group(
            "uad",
            "UAD category",
            [
                ("Any", "any"),
                ("Recommended", "recommended"),
                ("Advanced", "advanced"),
                ("Expert", "expert"),
                ("Unsafe", "unsafe"),
                ("Not listed", "not listed"),
            ],
        )
        self.reset_filters_button = QPushButton("Reset filters")
        self.reset_filters_button.setObjectName("appsResetFilters")
        self.select_all_check = VisibleSelectionCheckBox("Select visible")
        self.select_all_check.setObjectName("appsSelectVisible")
        self.select_all_check.setAccessibleName("Select visible applications")
        filters.addWidget(self.filters_button)
        filters.addWidget(self.reset_filters_button)
        filters.addWidget(self.select_all_check)
        filters.addWidget(self.active_filters_label, 1)
        toolbar_layout.addLayout(filters)

        layout.addWidget(toolbar)

        self.bulk_action_bar = QFrame()
        self.bulk_action_bar.setObjectName("appsBulkActionBar")
        self.bulk_action_bar.setAccessibleName("Selected applications actions")
        bulk_layout = QVBoxLayout(self.bulk_action_bar)
        bulk_layout.setContentsMargins(8, 8, 8, 8)
        bulk_layout.setSpacing(4)
        summary_layout = QHBoxLayout()
        summary_layout.setSpacing(8)
        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)
        self.selection_summary_label = ElidedLabel("0 selected")
        self.selection_summary_label.setObjectName("appsSelectionSummary")
        self.selection_summary_label.setFocusPolicy(Qt.NoFocus)
        self.selection_state_label = ElidedLabel("")
        self.selection_state_label.setObjectName("appsSelectionState")
        self.selection_state_label.setProperty("uiRole", "secondary")
        self.selection_state_label.setFocusPolicy(Qt.NoFocus)
        self.clear_selection_button = QPushButton("Clear")
        self.backup_button = QPushButton("Backup")
        self.uninstall_button = QPushButton("Uninstall")
        self.uninstall_button.setProperty("danger", True)
        set_button_role(self.uninstall_button, "danger", compact=True)
        self.disable_button = QPushButton("Disable")
        self.enable_button = QPushButton("Enable")
        for button in (
            self.clear_selection_button,
            self.backup_button,
            self.disable_button,
            self.enable_button,
        ):
            set_button_role(button, "secondary", compact=True)
        self.more_button = QToolButton()
        self.more_button.setObjectName("appsMoreButton")
        self.more_button.setText("More")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        set_button_role(self.more_button, "secondary", compact=True)
        self.more_menu = QMenu(self.more_button)
        self.install_existing_action = self.more_menu.addAction("Install existing")
        self.more_menu.addAction(self.export_action)
        self.more_menu.addSeparator()
        self.more_menu.addAction(self.clear_cache_action)
        self.more_button.setMenu(self.more_menu)

        summary_layout.addWidget(self.selection_summary_label, 1)
        summary_layout.addWidget(self.clear_selection_button)
        action_layout.addWidget(self.backup_button)
        action_layout.addWidget(self.enable_button)
        action_layout.addWidget(self.disable_button)
        action_layout.addWidget(self.uninstall_button)
        action_layout.addWidget(self.more_button)
        action_layout.addStretch(1)
        bulk_layout.addLayout(summary_layout)
        bulk_layout.addLayout(action_layout)
        bulk_layout.addWidget(self.selection_state_label)
        self.selection_state_label.hide()
        self.bulk_action_bar.hide()
        layout.addWidget(self.bulk_action_bar)

        self.table = AppTable()
        self.apps_empty_state = EmptyState(
            "No applications loaded",
            "Load applications from the active Android device to begin.",
            "Load applications",
        )
        self.apps_content = QStackedWidget()
        self.apps_content.addWidget(self.table)
        self.apps_content.addWidget(self.apps_empty_state)
        layout.addWidget(self.apps_content, 1)

        self.status_label = QLabel("Press Load applications to read packages from the connected device.")
        self.status_label.setObjectName("hintLabel")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.refresh_button.clicked.connect(self.refresh_apps)
        self.search.textChanged.connect(self._schedule_search_filter)
        self.sort_button.clicked.connect(self._show_sort_menu_from_button)
        self.reset_filters_button.clicked.connect(self.reset_filters)
        self._search_filter_timer.timeout.connect(self.apply_filter)
        self.select_all_check.stateChanged.connect(self._select_visible_state_changed)
        self.table.selection_changed.connect(self._selection_changed)
        self.clear_selection_button.clicked.connect(self._clear_selection)
        self.backup_button.clicked.connect(self.backup_selected)
        self.uninstall_button.clicked.connect(self.uninstall_selected)
        self.disable_button.clicked.connect(lambda: self.set_enabled_selected(False))
        self.enable_button.clicked.connect(lambda: self.set_enabled_selected(True))
        self.install_existing_action.triggered.connect(self.install_existing_selected)
        self.export_action.triggered.connect(self.export_packages)
        self.clear_cache_action.triggered.connect(self.clear_apps_cache)
        self.apps_empty_state.action_requested.connect(self._handle_empty_state_action)
        self._configure_tab_order()
        self.reload_filter_state()
        self._load_cached_apps_for_saved_device()
        self._update_action_states()

    def _captured_profile_settings(self, context: DeviceContext) -> _CapturedProfileSettings:
        return self.controller.captured_settings(context)

    def _profile_services(
        self,
        context: DeviceContext,
        include_system: bool | None = None,
    ) -> _AppsProfileServices:
        return self.controller.profile_services(context, include_system)

    def _backup_manager_for_context(self, context: DeviceContext) -> BackupManager:
        return self.controller.backup_manager(context)

    def _require_apps_context(self) -> DeviceContext:
        return self.controller.require_context()

    def _bound_adb_for_context(self, context: DeviceContext):
        return self.controller.bound_adb(context)

    def _is_context_current(self, context: DeviceContext) -> bool:
        return self.controller.is_current(context)

    def _require_current_context(self, context: DeviceContext) -> None:
        self.controller.require_current(context)

    def _can_apply_operation(self, token: OperationToken, context: DeviceContext) -> bool:
        return self.controller.can_apply(token, context)

    def _set_apps_view_identity(
        self,
        serial: str,
        context: DeviceContext | None = None,
    ) -> None:
        self.controller.set_view_identity(serial, context)

    def _apps_view_matches_context(self, context: DeviceContext) -> bool:
        return self.controller.view_matches(context)

    def _register_operation(
        self,
        context: DeviceContext,
        suffix: str,
        conflict: str,
        *,
        additional_conflicts: tuple[str, ...] = (),
    ) -> OperationToken:
        return self.controller.register_operation(
            context,
            suffix,
            conflict,
            additional_conflicts=additional_conflicts,
        )

    def _device_snapshot(self, context: DeviceContext):
        return self.controller.device_snapshot(context)

    def _start_page_worker(self, worker: Worker, token: OperationToken) -> bool:
        """Start a worker while preserving the AppsPage monkeypatch seam."""

        try:
            return start_worker(
                self,
                self.pool,
                worker,
                operation_registry=self.operations,
                operation_token=token,
            )
        except Exception as exc:
            self.status_label.setText(f"Could not start the background operation: {exc}")
            return False

    def refresh_storage_roots(self) -> None:
        self.app_cache.refresh_root()
        self.apk_metadata.refresh_root()

    def _set_table_apps(self, apps: list[AppInfo]) -> None:
        """Rebuild the table without losing package-keyed selection."""

        available_packages = {app.package_name for app in apps}
        self.selection_model.retain(available_packages)
        self.table.set_apps_sorted(
            apps,
            self._sort_mode,
            checked_packages=set(self.selection_model.selected_packages),
        )

    def reset_for_device_profile(self) -> None:
        self.controller.cancel_profile_operations("application profile changed")
        self._apps_load_token = None
        self._metadata_token = None
        self._assets_token = None
        self._bulk_token = None
        self._apps_loading = False
        self._assets_loading = False
        self._bulk_operation_busy = False
        self._bulk_operation_name = ""
        self._refresh_after_bulk = False
        self._search_filter_timer.stop()
        self.apps = []
        self.selection_model.clear()
        self._set_table_apps([])
        self._asset_progress_status = ""
        self._suppress_cache_save = False
        self.reload_filter_state()
        self.status_label.setText("Press Load applications to read packages from the active device profile.")
        self._update_app_count()

    def _add_filter_menu_group(
        self,
        kind: str,
        title: str,
        options: list[tuple[str, str]],
    ) -> None:
        if self.filters_menu.actions():
            self.filters_menu.addSeparator()
        self.filters_menu.addSection(title)
        group = QActionGroup(self)
        group.setExclusive(True)
        actions: dict[str, QAction] = {}
        for text, value in options:
            action = self.filters_menu.addAction(text)
            action.setCheckable(True)
            action.setData(value)
            group.addAction(action)
            action.triggered.connect(
                lambda checked=False, filter_kind=kind, filter_value=value: self._filter_action_triggered(
                    filter_kind,
                    filter_value,
                    checked,
                )
            )
            actions[value] = action
        self._filter_action_groups[kind] = group
        self._filter_actions[kind] = actions
        if options:
            actions[options[0][1]].setChecked(True)

    def _filter_action_triggered(self, kind: str, value: str, checked: bool) -> None:
        if not checked:
            return
        self._filter_values[kind] = value
        self.apply_filter()

    def apply_filter(self, save_state: bool = True) -> None:
        filter_state = self._current_filter_state()
        self.filter_controller.set_filters(filter_state, persist=False)
        self.table.apply_filters(filter_state)
        self._update_filter_summary(filter_state)
        self._update_app_count()
        if save_state:
            self.filter_controller.persist()

    def reload_filter_state(self) -> None:
        self._search_filter_timer.stop()
        view_state = self.filter_controller.reload()
        filter_state = view_state.filters
        self._set_filter_menu_value("type", filter_state.app_type)
        self._set_filter_menu_value("state", filter_state.app_state)
        self._set_filter_menu_value("uad", filter_state.uad_category)
        self.search.blockSignals(True)
        self.search.setText(filter_state.search_text)
        self.search.blockSignals(False)
        self._sort_mode = view_state.sort_mode
        self._update_sort_button_text()
        self.table.apply_sort(self._sort_mode)
        self.apply_filter(save_state=False)

    def reset_filters(self) -> None:
        self._search_filter_timer.stop()
        filter_state = self.filter_controller.reset_filters(persist=False).filters
        self._set_filter_menu_value("type", filter_state.app_type)
        self._set_filter_menu_value("state", filter_state.app_state)
        self._set_filter_menu_value("uad", filter_state.uad_category)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.apply_filter()

    def _schedule_search_filter(self, _text: str) -> None:
        self._search_filter_timer.start()

    def _current_filter_state(self) -> AppFilterState:
        return AppFilterState.from_values(
            search_text=self.search.text(),
            app_type=self._filter_values["type"],
            app_state=self._filter_values["state"],
            uad_category=self._filter_values["uad"],
        )

    def _set_filter_menu_value(self, kind: str, value: str) -> None:
        actions = self._filter_actions[kind]
        defaults = {"type": "all", "state": "any", "uad": "any"}
        normalized = value if value in actions else defaults[kind]
        self._filter_values[kind] = normalized
        actions[normalized].setChecked(True)

    def _save_filter_state(self, filter_state: AppFilterState) -> None:
        self.filter_controller.set_filters(filter_state, persist=False)
        self.filter_controller.set_sort_mode(self._sort_mode, persist=False)
        self.filter_controller.persist()

    def _update_filter_summary(self, filter_state: AppFilterState) -> None:
        self.filter_controller.set_filters(filter_state, persist=False)
        summary = self.filter_controller.summary()
        self.active_filters_label.setText(summary.active_text)
        self.filters_button.setText(summary.filter_button_text)
        self.filters_button.setToolTip(summary.tooltip)
        self.reset_filters_button.setEnabled(summary.has_active_filters)
        self.active_filters_label.setVisible(not self._compact_controls)

    def _show_sort_menu_from_button(self) -> None:
        self._show_sort_context_menu(self.sort_button.mapToGlobal(self.sort_button.rect().bottomLeft()))

    def _show_sort_context_menu(self, global_position) -> None:
        menu = QMenu(self)
        name_action = menu.addAction("Sort by name")
        name_action.setCheckable(True)
        name_action.setChecked(self._sort_mode == "name")
        menu.addSeparator()
        heavy_action = menu.addAction("Size: largest to smallest")
        heavy_action.setCheckable(True)
        heavy_action.setChecked(self._sort_mode == "size_desc")
        light_action = menu.addAction("Size: smallest to largest")
        light_action.setCheckable(True)
        light_action.setChecked(self._sort_mode == "size_asc")

        selected = menu.exec(global_position)
        if selected is heavy_action:
            self._set_sort_mode("size_desc")
        elif selected is light_action:
            self._set_sort_mode("size_asc")
        elif selected is name_action:
            self._set_sort_mode("name")

    def _set_sort_mode(self, mode: str) -> None:
        view_state = self.filter_controller.set_sort_mode(mode, persist=False)
        self._sort_mode = view_state.sort_mode
        self._update_sort_button_text()
        self.table.apply_sort(self._sort_mode)
        self.apply_filter(save_state=False)
        self.filter_controller.persist()

    def _update_sort_button_text(self) -> None:
        full_labels = {
            "name": "Sort: name",
            "size_desc": "Size: largest first",
            "size_asc": "Size: smallest first",
        }
        compact_labels = {
            "name": "Sort: name",
            "size_desc": "Sort: largest",
            "size_asc": "Sort: smallest",
        }
        labels = compact_labels if self._compact_controls else full_labels
        self.sort_button.setText(labels.get(self._sort_mode, labels["name"]))
        self.sort_button.setAccessibleName(full_labels.get(self._sort_mode, full_labels["name"]))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        super().resizeEvent(event)
        compact = event.size().width() <= self.COMPACT_CONTROLS_MAX_WIDTH
        if compact == self._compact_controls:
            return
        self._compact_controls = compact
        self._update_responsive_control_text()

    def _update_responsive_control_text(self) -> None:
        has_apps = self.table.rowCount() > 0
        full_refresh = "Refresh applications" if has_apps else "Load applications"
        compact_refresh = "Refresh" if has_apps else "Load apps"
        self.refresh_button.setText(compact_refresh if self._compact_controls else full_refresh)
        self.refresh_button.setAccessibleName(full_refresh)
        self.page_actions_button.setText("Page" if self._compact_controls else "Page actions")
        self.select_all_check.setText("Visible" if self._compact_controls else "Select visible")
        self.active_filters_label.setVisible(not self._compact_controls)
        self._update_application_count_label(
            self.table.visible_count(),
            self.table.rowCount(),
        )
        self._update_sort_button_text()

    def _update_application_count_label(self, visible: int, total: int) -> None:
        full_text = f"Showing {visible} of {total} applications"
        display_text = f"{visible} / {total} apps" if self._compact_controls else full_text
        self.total_label.setText(display_text)
        self.total_label.setToolTip(full_text)
        self.total_label.setAccessibleName(full_text)

    def update_device_state(self, device=None) -> None:
        active = device if device is not None else getattr(self.device_manager, "active", None)
        self._device_mode = str(getattr(active, "mode", "No device") or "No device")
        serial = str(getattr(active, "serial", "") or "")
        if self.apps and serial and not self.controller.view.serial:
            context: DeviceContext | None = None
            try:
                candidate = self._require_apps_context()
                if candidate.serial == serial:
                    context = candidate
            except DeviceContextUnavailable:
                pass
            self._set_apps_view_identity(serial, context)
        self._update_action_states()
        self._update_app_count()

    def _device_available_for_apps(self) -> bool:
        return self._device_mode in {"ADB", "Recovery"}

    def _select_visible_state_changed(self, state: int) -> None:
        check_state = Qt.CheckState(state)
        if check_state == Qt.Checked:
            self.table.select_all_visible()
        elif check_state == Qt.Unchecked:
            self.table.unselect_all_visible()
        self._update_app_count()

    def _selection_changed(self) -> None:
        self.selection_model.replace(self.table.checked_package_names())
        self._update_app_count()

    def _clear_selection(self) -> None:
        self.selection_model.clear()
        self.table.unselect_all()

    def _configure_tab_order(self) -> None:
        """Keep keyboard navigation stable when the contextual bar appears."""

        order = (
            self.refresh_button,
            self.search,
            self.sort_button,
            self.page_actions_button,
            self.filters_button,
            self.reset_filters_button,
            self.select_all_check,
            self.table,
            self.backup_button,
            self.enable_button,
            self.disable_button,
            self.uninstall_button,
            self.more_button,
            self.clear_selection_button,
        )
        for current, following in zip(order, order[1:]):
            QWidget.setTabOrder(current, following)

    def _sync_contextual_action_bar(self, has_selection: bool) -> None:
        """Show bulk actions only for a selection without stealing focus."""

        was_visible = not self.bulk_action_bar.isHidden()
        focus_widget = QApplication.focusWidget()
        focus_was_in_bar = bool(
            focus_widget
            and (focus_widget is self.bulk_action_bar or self.bulk_action_bar.isAncestorOf(focus_widget))
        )
        self.bulk_action_bar.setVisible(has_selection)
        if was_visible and not has_selection and (
            focus_was_in_bar or focus_widget is self.select_all_check
        ):
            self._focus_restore_timer.start()

    def _restore_apps_focus(self) -> None:
        target = (
            self.table
            if self.apps_content.currentWidget() is self.table and self.table.visible_count() > 0
            else self.search
        )
        target.setFocus(Qt.OtherFocusReason)

    def _update_app_count(self) -> None:
        total = self.table.rowCount()
        visible = self.table.visible_count()
        self.selection_model.replace(self.table.checked_package_names())
        selection = self.selection_model.summary(self.table.visible_package_names())
        self._update_application_count_label(visible, total)
        self.selection_summary_label.setText(selection.text)
        check_state = {
            VisibleSelectionState.UNCHECKED: Qt.Unchecked,
            VisibleSelectionState.PARTIALLY_CHECKED: Qt.PartiallyChecked,
            VisibleSelectionState.CHECKED: Qt.Checked,
        }[selection.visible_state]
        self.select_all_check.blockSignals(True)
        self.select_all_check.setCheckState(check_state)
        self.select_all_check.blockSignals(False)
        self._update_action_states()
        self._update_apps_empty_state(total, visible)

    def _update_apps_empty_state(self, total: int, visible: int) -> None:
        if total > 0 and visible > 0:
            self.apps_content.setCurrentWidget(self.table)
            return
        if self._apps_loading and total == 0:
            self.apps_empty_state.set_content(
                "Loading applications",
                "OpenADB is reading the package list from the active device.",
            )
        elif total > 0:
            self.apps_empty_state.set_content(
                "Search returned no results",
                "No applications match the current search and filters.",
                "Reset search and filters",
            )
        elif not self._device_available_for_apps():
            self.apps_empty_state.set_content(
                "No device connected",
                "Connect and authorize an ADB device, then refresh its status.",
                "Refresh device status",
                kind="warning",
            )
        else:
            self.apps_empty_state.set_content(
                "No applications loaded",
                "Load applications from the active Android device to begin.",
                "Load applications",
            )
        self.apps_content.setCurrentWidget(self.apps_empty_state)

    def _handle_empty_state_action(self) -> None:
        if self.table.rowCount() > 0 and self.table.visible_count() == 0:
            self.reset_filters()
        elif self._device_available_for_apps():
            self.refresh_apps()
        else:
            self.refresh_device_requested.emit()

    def _update_action_states(self) -> None:
        selected_apps = self.table.checked_apps(include_hidden=True)
        has_selection = bool(selected_apps)
        self._sync_contextual_action_bar(has_selection)
        has_apps = self.table.rowCount() > 0
        device_ready = self._device_available_for_apps()
        risky_selection = any(app.is_system or is_dangerous_package(app.package_name) for app in selected_apps)

        busy_reason = ""
        if self._bulk_operation_busy:
            operation = self._bulk_operation_name or "another application operation"
            busy_reason = f"Wait for {operation} to finish."
        elif self._apps_loading:
            busy_reason = "Wait for the application list to finish loading."
        elif self._assets_loading:
            busy_reason = "Wait for application labels and icons to finish loading."

        device_reason = (
            ""
            if device_ready
            else f"Requires an authorized ADB or Recovery device (current mode: {self._device_mode})."
        )
        selection_reason = "" if has_selection else "Select one or more applications first."
        common_reason = busy_reason or device_reason or selection_reason

        self._update_responsive_control_text()
        self._set_available(
            self.refresh_button,
            not bool(busy_reason or device_reason),
            "Load the application list from the active device.",
            busy_reason or device_reason,
        )
        self._set_available(
            self.backup_button,
            not bool(common_reason),
            "Back up the selected applications.",
            common_reason,
        )

        danger_note = " Selection includes system or protected packages; an additional confirmation is required."
        self._set_available(
            self.uninstall_button,
            not bool(common_reason),
            "Uninstall the selected applications." + (danger_note if risky_selection else ""),
            common_reason,
        )

        states = {str(app.state or "").strip().casefold() for app in selected_apps}
        mixed_or_unknown_state = False
        if not selected_apps:
            state_explanation = ""
        elif states == {"enabled"}:
            state_explanation = "Enabled apps selected; Disable is available."
        elif states == {"disabled"}:
            state_explanation = "Disabled apps selected; Enable is available."
        elif "enabled" in states and "disabled" in states:
            mixed_or_unknown_state = True
            state_explanation = (
                "Selection mixes enabled and disabled states; select one state to use Enable or Disable."
            )
        else:
            mixed_or_unknown_state = True
            state_explanation = (
                "Enable and Disable need applications with one known state; adjust the selection first."
            )
        self.selection_state_label.setText(state_explanation)
        self.selection_state_label.setVisible(has_selection and mixed_or_unknown_state)
        self.bulk_action_bar.setToolTip(state_explanation)
        enable_reason = common_reason
        disable_reason = common_reason
        enable_allowed = not bool(common_reason)
        disable_allowed = not bool(common_reason)
        if not common_reason:
            if states == {"disabled"}:
                disable_allowed = False
                disable_reason = "All selected applications are already disabled."
            elif states == {"enabled"}:
                enable_allowed = False
                enable_reason = "All selected applications are already enabled."
            else:
                enable_allowed = False
                disable_allowed = False
                enable_reason = disable_reason = state_explanation
        self._set_available(
            self.enable_button,
            enable_allowed,
            "Enable the selected disabled applications.",
            enable_reason,
        )
        self._set_available(
            self.disable_button,
            disable_allowed,
            "Disable the selected enabled applications." + (danger_note if risky_selection else ""),
            disable_reason,
        )

        self._set_available(
            self.install_existing_action,
            not bool(common_reason),
            "Ask Android to install an existing system package for the current user.",
            common_reason,
        )
        export_reason = busy_reason or ("Load applications before exporting." if not has_apps else "")
        self._set_available(
            self.export_action,
            not bool(export_reason),
            "Export the current application list to CSV.",
            export_reason,
        )
        self._set_available(
            self.clear_cache_action,
            not bool(busy_reason),
            "Delete cached application metadata, labels and icons.",
            busy_reason,
        )

        visible = self.table.visible_count()
        self.select_all_check.setEnabled(visible > 0 and not self._bulk_operation_busy)
        if self._bulk_operation_busy:
            select_tooltip = busy_reason
        elif visible > 0:
            select_tooltip = "Select or clear all applications currently visible after filtering."
        else:
            select_tooltip = "No visible applications to select."
        self.select_all_check.setToolTip(select_tooltip)
        self._set_available(
            self.clear_selection_button,
            has_selection and not self._bulk_operation_busy,
            "Clear all selected applications, including rows hidden by filters.",
            "No applications are selected." if not has_selection else busy_reason,
        )
        self.more_button.setToolTip("Additional application actions")
        self.page_actions_button.setToolTip("Export the application list or clear cached app data")

    def _set_available(self, control, enabled: bool, available_tooltip: str, reason: str) -> None:
        control.setEnabled(enabled)
        tooltip = available_tooltip if enabled else (reason or "This action is currently unavailable.")
        control.setToolTip(tooltip)
        if isinstance(control, QAction):
            control.setStatusTip(tooltip)
