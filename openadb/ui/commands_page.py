from __future__ import annotations

import re
import shlex
import threading
import time
from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from openadb.core.adb import ADBClient
from openadb.core.command_catalog import COMMAND_CATEGORIES, command_specs
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.fastboot import FastbootClient
from openadb.core.file_manager_errors import (
    command_contains_sensitive_data,
    redact_command_arguments,
    redact_sensitive_text,
)
from openadb.core.operations import (
    OperationConflictError,
    OperationRegistry,
    OperationToken,
)
from openadb.core.privilege import (
    SHIZUKU_INVALIDATING_ERRORS,
    PrivilegeBackend,
    PrivilegeManager,
    PrivilegeStatus,
)
from openadb.core.safety import RiskInfo, analyze_command_risk
from openadb.core.settings_manager import (
    SettingsManager,
    read_privilege_backend_setting,
)
from openadb.models.command_result import CommandResult
from openadb.models.command_spec import CommandSpec
from openadb.models.device_info import DeviceInfo
from openadb.ui.design_system import configure_page_layout
from openadb.ui.widgets.elided_label import ElidedLabel
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox as QComboBox
from openadb.ui.widgets.privilege_selector import PrivilegeModeSelector
from openadb.ui.workers import Worker, start_worker


class CommandsPage(QWidget):
    open_logs_requested = Signal()
    status_message = Signal(str, int)
    settings_changed = Signal()
    check_privilege_requested = Signal()
    request_shizuku_permission_requested = Signal()
    open_shizuku_requested = Signal()
    privilege_status_invalidated = Signal()

    def __init__(
        self,
        adb: ADBClient,
        fastboot: FastbootClient,
        runner: CommandRunner,
        settings: SettingsManager,
        device_manager: DeviceManager,
        detect_tools_callback,
        parent=None,
        *,
        privilege_manager: PrivilegeManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.adb = adb
        self.fastboot = fastboot
        self.runner = runner
        self.settings = settings
        self.device_manager = device_manager
        self.privilege_manager = privilege_manager or PrivilegeManager(
            adb,
            settings,
            device_manager,
        )
        self.operations = getattr(device_manager, "operations", None) or OperationRegistry()
        self.detect_tools_callback = detect_tools_callback
        self.pool = QThreadPool.globalInstance()
        self.specs = command_specs()
        self.spec_by_key = {spec.key: spec for spec in self.specs}
        self._command_running = False
        self._cancel_event: threading.Event | None = None
        self._command_token: OperationToken | None = None
        self._selected_spec: CommandSpec | None = None
        self._running_spec_key = ""
        self._root_access_state = "unknown"
        self._root_access_serial = ""
        self._root_access_generation: int | None = None
        self._root_access_context: DeviceContext | None = None
        self._privilege_status: PrivilegeStatus | None = None
        self._privilege_busy = False

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        configure_page_layout(layout)
        title = QLabel("Commands")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Search structured ADB and fastboot operations, review requirements and consequences, then run one command at a time."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        toolbar = QFrame()
        toolbar.setObjectName("commandToolbar")
        toolbar_layout = QGridLayout(toolbar)
        toolbar_layout.setContentsMargins(10, 8, 10, 8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search name, command, description, or category…")
        self.search.setAccessibleName("Search commands")
        self.search.setAccessibleDescription(self.search.placeholderText())
        self.search.setToolTip(self.search.placeholderText())
        self.search.setClearButtonEnabled(True)
        self.view_mode = QComboBox()
        self.view_mode.addItems(["Basic", "Advanced"])
        saved_mode = str(settings.get("commands_view_mode", "Basic"))
        self.view_mode.setCurrentText(saved_mode if saved_mode in {"Basic", "Advanced"} else "Basic")
        self.category_filter = QComboBox()
        self.category_filter.addItems(["All categories", *COMMAND_CATEGORIES])
        self.command_count = ElidedLabel("", elide_mode=Qt.ElideRight)
        self.command_count.setObjectName("commandCount")
        self.command_count.setAccessibleName("Visible command count")
        toolbar_layout.addWidget(self.search, 0, 0, 1, 3)
        toolbar_layout.addWidget(self.view_mode, 1, 0)
        toolbar_layout.addWidget(self.category_filter, 1, 1)
        toolbar_layout.addWidget(self.command_count, 1, 2)
        toolbar_layout.setColumnStretch(0, 1)
        toolbar_layout.setColumnStretch(1, 1)
        toolbar_layout.setColumnStretch(2, 1)
        layout.addWidget(toolbar)

        self.page_tabs = QTabWidget()
        self.page_tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.page_tabs.addTab(self._build_catalog_page(), "Built-in commands")
        self.page_tabs.addTab(self._build_custom_page(), "Custom command")

        self.output_panel = self._build_output_panel()
        self.output_panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.main_splitter = QSplitter(Qt.Vertical)
        self.main_splitter.setObjectName("commandsMainSplitter")
        self.main_splitter.addWidget(self.page_tabs)
        self.main_splitter.addWidget(self.output_panel)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setStretchFactor(0, 3)
        self.main_splitter.setStretchFactor(1, 2)
        self.main_splitter.setSizes([390, 260])
        layout.addWidget(self.main_splitter, 1)

        self.search.textChanged.connect(self._rebuild_tree)
        self.view_mode.currentTextChanged.connect(self._view_mode_changed)
        self.category_filter.currentTextChanged.connect(self._rebuild_tree)
        self.tree.currentItemChanged.connect(self._tree_selection_changed)
        self.tree.itemDoubleClicked.connect(lambda _item, _column: self.run_selected())
        self.run_selected_button.clicked.connect(self.run_selected)
        self.history.currentTextChanged.connect(self.manual.setText)
        self.custom_run_button.clicked.connect(self.run_manual)
        self.privilege_selector.backend_changed.connect(
            self._privilege_backend_changed
        )
        # Hidden compatibility controls keep old integrations/tests functional
        # while the visible UI has one unambiguous three-way selector.
        self.root_shell.toggled.connect(self._root_shell_toggled)
        self.shizuku_shell.toggled.connect(self._shizuku_shell_toggled)
        self.check_privilege_button.clicked.connect(self.check_privilege_requested.emit)
        self.request_shizuku_button.clicked.connect(
            self.request_shizuku_permission_requested.emit
        )
        self.open_shizuku_button.clicked.connect(self.open_shizuku_requested.emit)
        self.cancel_button.clicked.connect(self.cancel_running_command)
        self.copy_button.clicked.connect(self.copy_result)
        self.clear_button.clicked.connect(self.clear_result)
        self.open_logs_button.clicked.connect(self.open_logs_requested.emit)
        self._rebuild_tree()
        self.clear_result()

    def _build_catalog_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("commandCatalogPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        browser = QSplitter(Qt.Horizontal)
        browser.setObjectName("commandsBrowserSplitter")
        self.tree = QTreeWidget()
        self.tree.setObjectName("commandTree")
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setAccessibleName("Built-in command catalog")
        browser.addWidget(self.tree)
        browser.addWidget(self._build_details_panel())
        browser.setChildrenCollapsible(False)
        browser.setStretchFactor(0, 3)
        browser.setStretchFactor(1, 2)
        browser.setSizes([560, 390])
        page_layout.addWidget(browser)
        return page

    def _build_details_panel(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("commandDetailsScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setAccessibleName("Selected command details")
        panel = QFrame()
        panel.setObjectName("commandDetailsPanel")
        panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setSizeConstraint(QLayout.SetMinimumSize)
        panel_layout.setContentsMargins(12, 11, 12, 12)
        panel_layout.setSpacing(8)
        self.detail_title = QLabel("Select a command")
        self.detail_title.setObjectName("commandDetailsTitle")
        self.detail_title.setWordWrap(True)
        self.detail_description = QLabel("Choose an item to review its exact command and requirements.")
        self.detail_description.setObjectName("sectionDescription")
        self.detail_description.setWordWrap(True)
        self.detail_command = QLabel("—")
        self.detail_command.setObjectName("commandActualText")
        self.detail_command.setWordWrap(True)
        self.detail_command.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail_metadata = QLabel("—")
        self.detail_metadata.setObjectName("commandMetadata")
        self.detail_metadata.setWordWrap(True)
        self.detail_risk = QLabel("Risk: —")
        self.detail_risk.setObjectName("commandRiskBadge")
        self.detail_availability = QLabel("Select a command.")
        self.detail_availability.setObjectName("commandAvailability")
        self.detail_availability.setWordWrap(True)
        self.run_selected_button = QPushButton("Run selected command")
        self.run_selected_button.setObjectName("primaryAction")
        self.run_selected_button.setEnabled(False)
        panel_layout.addWidget(self.detail_title)
        panel_layout.addWidget(self.detail_description)
        panel_layout.addWidget(QLabel("Actual command"))
        panel_layout.addWidget(self.detail_command)
        panel_layout.addWidget(self.detail_metadata)
        panel_layout.addWidget(self.detail_risk)
        panel_layout.addWidget(self.detail_availability)
        panel_layout.addStretch()
        panel_layout.addWidget(self.run_selected_button)
        scroll.setWidget(panel)
        self.details_scroll = scroll
        self.details_panel = panel
        return scroll

    def _build_custom_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("customCommandPage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(12, 12, 12, 12)
        page_layout.setSpacing(9)
        description = QLabel(
            "Enter a command beginning with adb or fastboot. It runs in the background, uses the active device when required, and is checked by the same risk analyzer as built-in commands."
        )
        description.setObjectName("sectionDescription")
        description.setWordWrap(True)
        page_layout.addWidget(description)
        history_row = QHBoxLayout()
        history_row.addWidget(QLabel("History"))
        self.history = QComboBox()
        self.history.setEditable(False)
        self.history.addItems(self.settings.get("command_history", []))
        history_row.addWidget(self.history, 1)
        page_layout.addLayout(history_row)
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("Example: adb shell dumpsys battery")
        self.manual.setAccessibleName("Custom adb or fastboot command")
        self.manual.setAccessibleDescription(self.manual.placeholderText())
        self.manual.setToolTip(self.manual.placeholderText())
        self.manual.setClearButtonEnabled(True)
        page_layout.addWidget(self.manual)
        backend = PrivilegeBackend.normalize(
            read_privilege_backend_setting(
                self.settings,
                profile_available=getattr(
                    self,
                    "_privilege_profile_available",
                    True,
                ),
            )
        )
        selector_row = QHBoxLayout()
        selector_label = QLabel("Android access")
        self.privilege_selector = PrivilegeModeSelector()
        self.privilege_selector.set_backend(backend)
        selector_label.setBuddy(self.privilege_selector)
        selector_row.addWidget(selector_label)
        selector_row.addWidget(self.privilege_selector, 1)
        page_layout.addLayout(selector_row)
        self.root_shell = QCheckBox()
        self.root_shell.setChecked(backend is PrivilegeBackend.ROOT)
        self.root_shell.hide()
        self.shizuku_shell = QCheckBox()
        self.shizuku_shell.setChecked(backend is PrivilegeBackend.SHIZUKU)
        self.shizuku_shell.hide()
        privilege_row = QGridLayout()
        privilege_row.setContentsMargins(0, 0, 0, 0)
        privilege_row.setHorizontalSpacing(8)
        privilege_row.setVerticalSpacing(6)
        self.privilege_status = QLabel("Privileged access has not been checked for this device.")
        self.privilege_status.setObjectName("commandAvailability")
        self.privilege_status.setWordWrap(True)
        privilege_row.addWidget(self.privilege_status, 0, 0, 1, 2)
        self.check_privilege_button = QPushButton("Check access")
        self.request_shizuku_button = QPushButton("Request permission")
        self.open_shizuku_button = QPushButton("Open Shizuku")
        for button in (
            self.check_privilege_button,
            self.request_shizuku_button,
            self.open_shizuku_button,
        ):
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        privilege_row.addWidget(self.check_privilege_button, 1, 0)
        privilege_row.addWidget(self.request_shizuku_button, 1, 1)
        privilege_row.addWidget(self.open_shizuku_button, 2, 0, 1, 2)
        privilege_row.setColumnStretch(0, 1)
        privilege_row.setColumnStretch(1, 1)
        page_layout.addLayout(privilege_row)
        self.custom_availability = QLabel("ADB and fastboot commands are validated before they run.")
        self.custom_availability.setObjectName("commandAvailability")
        self.custom_availability.setWordWrap(True)
        page_layout.addWidget(self.custom_availability)
        self.custom_run_button = QPushButton("Run custom command")
        self.custom_run_button.setObjectName("primaryAction")
        page_layout.addWidget(self.custom_run_button)
        page_layout.addStretch()
        self._update_privilege_controls()
        return page

    def _build_output_panel(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setObjectName("commandOutputScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setAccessibleName("Command result")
        panel = QFrame()
        panel.setObjectName("commandOutputPanel")
        panel.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setSizeConstraint(QLayout.SetMinimumSize)
        panel_layout.setContentsMargins(10, 9, 10, 10)
        panel_layout.setSpacing(7)
        top = QGridLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setHorizontalSpacing(8)
        top.setVerticalSpacing(3)
        title = QLabel("Command result")
        title.setObjectName("commandGroupTitle")
        self.output_status = ElidedLabel(
            "No command has run",
            elide_mode=Qt.ElideRight,
        )
        self.output_status.setObjectName("commandOutputStatus")
        self.output_status.setAccessibleName("Command result status")
        top.addWidget(title, 0, 0)
        top.addWidget(self.output_status, 0, 1)
        self.output_exit = QLabel("Exit code: —")
        self.output_duration = QLabel("Duration: —")
        top.addWidget(self.output_exit, 1, 0)
        top.addWidget(self.output_duration, 1, 1)
        top.setColumnStretch(1, 1)
        panel_layout.addLayout(top)
        self.output_command = QLineEdit()
        self.output_command.setReadOnly(True)
        self.output_command.setPlaceholderText("Executed command")
        panel_layout.addWidget(self.output_command)
        self.output_tabs = QTabWidget()
        self.stdout_output = QPlainTextEdit()
        self.stdout_output.setReadOnly(True)
        self.stdout_output.setPlaceholderText("stdout will appear here")
        self.stderr_output = QPlainTextEdit()
        self.stderr_output.setReadOnly(True)
        self.stderr_output.setPlaceholderText("stderr will appear here")
        self.output_tabs.addTab(self.stdout_output, "stdout")
        self.output_tabs.addTab(self.stderr_output, "stderr")
        self.output_empty_state = EmptyState(
            "Command has not been run",
            "Choose a built-in or custom command to see its result here.",
            "Choose a command",
        )
        self.output_content = QStackedWidget()
        self.output_content.addWidget(self.output_tabs)
        self.output_content.addWidget(self.output_empty_state)
        self.output_empty_state.action_requested.connect(self._focus_command_catalog)
        panel_layout.addWidget(self.output_content, 1)
        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copy")
        self.clear_button = QPushButton("Clear")
        self.open_logs_button = QPushButton("Open Logs")
        self.cancel_button = QPushButton("Cancel running command")
        self.cancel_button.setEnabled(False)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.open_logs_button)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        panel_layout.addLayout(actions)
        scroll.setWidget(panel)
        self.output_content_panel = panel
        self.output_scroll = scroll
        return scroll

    def reload_from_settings(self) -> None:
        stored_history = self.settings.get("command_history", [])
        raw_history = (
            [str(item) for item in stored_history]
            if isinstance(stored_history, (list, tuple))
            else []
        )
        safe_history = [
            item
            for item in raw_history
            if redact_sensitive_text(item) == item
        ]
        if safe_history != raw_history or not isinstance(stored_history, list):
            self.settings.set("command_history", safe_history)
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItems(safe_history)
        self.history.blockSignals(False)
        profile_available = getattr(self, "_privilege_profile_available", True)
        configured_backend = read_privilege_backend_setting(
            self.settings,
            profile_available=profile_available,
        )
        backend = PrivilegeBackend.normalize(configured_backend)
        if profile_available:
            self.privilege_selector.set_backend(backend)
        else:
            self.privilege_selector.set_pending_backend(configured_backend)
        self.root_shell.blockSignals(True)
        self.shizuku_shell.blockSignals(True)
        self.root_shell.setChecked(backend is PrivilegeBackend.ROOT)
        self.shizuku_shell.setChecked(backend is PrivilegeBackend.SHIZUKU)
        self.root_shell.blockSignals(False)
        self.shizuku_shell.blockSignals(False)
        self._update_privilege_controls()
        saved_mode = str(self.settings.get("commands_view_mode", "Basic"))
        self.view_mode.blockSignals(True)
        self.view_mode.setCurrentText(saved_mode if saved_mode in {"Basic", "Advanced"} else "Basic")
        self.view_mode.blockSignals(False)
        self._rebuild_tree()

    def update_device_state(self, _device: DeviceInfo | None = None) -> None:
        active = _device or self.device_manager.active
        context_current = (
            self._root_access_context is not None
            and hasattr(self.device_manager, "is_context_current")
            and self.device_manager.is_context_current(self._root_access_context)
        )
        if active.serial != self._root_access_serial or (
            self._root_access_context is not None and not context_current
        ):
            self._root_access_state = "unknown"
            self._root_access_serial = ""
            self._root_access_generation = None
            self._root_access_context = None
        privilege_status = self._privilege_status
        if privilege_status is not None and (
            privilege_status.device_serial != active.serial
            or not self._privilege_status_is_current(privilege_status)
        ):
            self._privilege_status = None
            self._set_privilege_status_text("Privileged access has not been checked for this device.")
        self._refresh_availability()

    def update_tools_state(self) -> None:
        self._refresh_availability()

    def _view_mode_changed(self, mode: str) -> None:
        self.settings.set("commands_view_mode", mode)
        self._rebuild_tree()

    def _filtered_specs(self) -> list[CommandSpec]:
        advanced = self.view_mode.currentText() == "Advanced"
        category = self.category_filter.currentText()
        terms = self.search.text().strip().casefold().split()
        result: list[CommandSpec] = []
        for spec in self.specs:
            if not advanced and not spec.basic:
                continue
            if category != "All categories" and spec.category != category:
                continue
            if terms and not all(term in spec.search_text for term in terms):
                continue
            result.append(spec)
        return result

    def _rebuild_tree(self, *_args) -> None:
        selected_key = self._selected_spec.key if self._selected_spec else ""
        self.tree.blockSignals(True)
        self.tree.clear()
        visible_specs = self._filtered_specs()
        selected_item: QTreeWidgetItem | None = None
        first_item: QTreeWidgetItem | None = None
        for category in COMMAND_CATEGORIES:
            specs = [spec for spec in visible_specs if spec.category == category]
            if not specs:
                continue
            group = QTreeWidgetItem([category])
            group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
            group.setExpanded(True)
            group.setToolTip(0, category)
            group.setData(0, Qt.AccessibleTextRole, category)
            self.tree.addTopLevelItem(group)
            for spec in specs:
                available, reason = self._availability(spec)
                suffix = "" if available else " — Unavailable"
                visible_label = spec.label + suffix
                item = QTreeWidgetItem([visible_label])
                item.setData(0, Qt.UserRole, spec.key)
                item.setData(0, Qt.AccessibleTextRole, visible_label)
                item.setToolTip(
                    0,
                    f"{visible_label}\n\n{spec.actual_command}\n{reason}",
                )
                group.addChild(item)
                first_item = first_item or item
                if spec.key == selected_key:
                    selected_item = item
        self.tree.blockSignals(False)
        self.command_count.setText(f"Showing {len(visible_specs)} of {len(self.specs)}")
        self.command_count.setAccessibleDescription(self.command_count.full_text())
        target = selected_item or first_item
        if target is not None:
            self.tree.setCurrentItem(target)
            self._show_spec(self.spec_by_key[str(target.data(0, Qt.UserRole))])
        else:
            self._selected_spec = None
            self._show_empty_details()

    def _tree_selection_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if current is None:
            self._show_empty_details()
            return
        key = current.data(0, Qt.UserRole)
        if key and str(key) in self.spec_by_key:
            self._show_spec(self.spec_by_key[str(key)])

    def _show_spec(self, spec: CommandSpec) -> None:
        self._selected_spec = spec
        risk = spec.risk
        available, reason = self._availability(spec)
        modes = ", ".join(spec.required_modes) if spec.required_modes else "No device required"
        requirements = [
            f"Category: {spec.category}",
            f"Tool: {spec.required_tool}",
            f"Device mode: {modes}",
            f"File: {'Yes' if spec.requires_file else 'No'}",
            f"Input: {'Yes' if spec.requires_input else 'No'}",
            f"Root: {'Required' if spec.requires_root else 'No'}",
        ]
        self.detail_title.setText(spec.label)
        self.detail_description.setText(spec.description)
        self.detail_command.setText(spec.actual_command)
        self.detail_metadata.setText("  •  ".join(requirements))
        self.detail_risk.setText(f"Risk: {risk.level}")
        self.detail_risk.setProperty("riskLevel", risk.level)
        self.detail_risk.style().unpolish(self.detail_risk)
        self.detail_risk.style().polish(self.detail_risk)
        consequence = f" {risk.description}" if risk.description else ""
        self.detail_availability.setText(("Available." if available else f"Unavailable: {reason}") + consequence)
        self.run_selected_button.setText("Run selected command")
        self.run_selected_button.setEnabled(available)
        self.run_selected_button.setToolTip(reason)

    def _show_empty_details(self) -> None:
        self.detail_title.setText("No matching command")
        self.detail_description.setText("Change the search, category, or Basic/Advanced mode.")
        self.detail_command.setText("—")
        self.detail_metadata.setText("—")
        self.detail_risk.setText("Risk: —")
        self.detail_risk.setProperty("riskLevel", "Safe")
        self.detail_risk.style().unpolish(self.detail_risk)
        self.detail_risk.style().polish(self.detail_risk)
        self.detail_availability.setText("No command is selected.")
        self.run_selected_button.setText("Clear command filters")
        self.run_selected_button.setEnabled(bool(self.search.text() or self.category_filter.currentIndex()))

    def _availability(self, spec: CommandSpec) -> tuple[bool, str]:
        if self._command_running:
            return False, "Another command is already running. Cancel it or wait for completion."
        if self._privilege_busy and (spec.required_modes or spec.use_serial):
            return False, "The selected access mode is still being prepared for this device."
        if spec.required_tool == "ADB" and not self.adb.platform_tools.active.has_adb:
            return False, "ADB is unavailable. Find or choose Android Platform Tools in Settings."
        if spec.required_tool == "fastboot" and not self.fastboot.platform_tools.active.has_fastboot:
            return False, "fastboot is unavailable. Find or choose Android Platform Tools in Settings."
        mode = self.device_manager.active.mode
        if spec.required_modes and mode not in spec.required_modes:
            if mode in {"No device", "Checking"}:
                return False, f"Connect a device in one of these modes: {', '.join(spec.required_modes)}."
            if mode == "Unauthorized":
                return False, "Authorize ADB on the Android device first."
            if mode == "Offline":
                return False, "The selected device is offline. Reconnect it first."
            return False, f"Current mode is {mode}; required: {', '.join(spec.required_modes)}."
        backend = self._selected_shell_backend()
        if spec.kind == "adb_root_check" and backend is not PrivilegeBackend.ROOT:
            return False, "Select Root as the access mode before probing su/root access."
        if spec.requires_root:
            if backend is PrivilegeBackend.STANDARD:
                return False, "Select Root or a UID 0 Shizuku session for this command."
            if backend is PrivilegeBackend.SHIZUKU:
                status = self._current_privilege_status()
                if status is None or not status.root:
                    return False, self._shizuku_unavailable_reason(require_root=True)
            else:
                root_status = self._current_privilege_status()
                root_confirmed = bool(root_status and root_status.root) or self._root_access_is_confirmed()
                if not root_confirmed:
                    if self._root_access_state == "unavailable":
                        return False, "Root access was not granted. Check the device's su/root configuration."
                    return False, "Run Check root access for the active device first."
        if (
            backend is PrivilegeBackend.SHIZUKU
            and spec.kind in {"adb_shell", "adb_shell_input"}
        ):
            status = self._current_privilege_status()
            if status is None or not status.available:
                return False, self._shizuku_unavailable_reason()
        return True, "Ready to run."

    def _refresh_availability(self) -> None:
        self._rebuild_tree()
        self.custom_run_button.setEnabled(
            not self._command_running and not self._privilege_busy
        )
        self.custom_availability.setText(
            "Another command is running."
            if self._command_running
            else (
                "The selected access mode is still being prepared for this device."
                if self._privilege_busy
                else "ADB and fastboot commands are validated before they run."
            )
        )

    def run_selected(self) -> None:
        if self._selected_spec is not None:
            self.run_spec(self._selected_spec)
            return
        self.search.clear()
        self.category_filter.setCurrentIndex(0)
        self.view_mode.setCurrentText("Advanced")

    def run_spec(self, spec: CommandSpec) -> None:
        available, reason = self._availability(spec)
        if not available:
            self.detail_availability.setText(f"Unavailable: {reason}")
            self.status_message.emit(reason, 6000)
            return
        if spec.kind == "callback":
            self.detect_tools_callback()
            self.status_message.emit("Platform Tools search opened.", 4000)
            return
        privilege_lease = (
            self._capture_privilege_operation_lease()
            if spec.kind
            in {"adb_root_check", "adb_shell", "adb_shell_input", "adb_root_shell_input"}
            else None
        )
        context: DeviceContext | None = None
        if spec.use_serial:
            try:
                context = self._capture_context(spec.required_modes)
            except DeviceContextUnavailable as exc:
                self.status_message.emit(str(exc), 6000)
                self._refresh_availability()
                return
        selected_backend = (
            privilege_lease.backend
            if privilege_lease is not None
            else self._selected_shell_backend()
        )
        use_shizuku = (
            selected_backend is PrivilegeBackend.SHIZUKU
            and spec.kind in {"adb_shell", "adb_shell_input", "adb_root_shell_input"}
        )
        shizuku_expected_uid: int | None = None
        if use_shizuku:
            status = self._current_privilege_status()
            shizuku_expected_uid = getattr(status, "uid", None)
            if shizuku_expected_uid not in {0, 2000}:
                self.status_message.emit(
                    "Check Shizuku access and review its UID before running this command.",
                    7000,
                )
                return
        deferred_shell_risk = spec.kind in {"adb_shell_input", "adb_root_shell_input"}
        risk = spec.risk
        risk_command = spec.actual_command
        if (
            spec.kind == "adb_shell"
            and (
                selected_backend is PrivilegeBackend.ROOT
                or (use_shizuku and shizuku_expected_uid == 0)
            )
        ):
            elevated_shell = self.adb.root_shell_script(" ".join(spec.args))
            risk_parts = ["adb", "shell", elevated_shell]
            risk = analyze_command_risk(risk_parts)
            risk_command = self.runner.command_text(risk_parts)
        if not deferred_shell_risk and risk.needs_confirmation and not self._confirm_risk(
            spec.label, risk_command, risk
        ):
            self.status_message.emit("Command cancelled before execution.", 4000)
            return
        args = list(spec.args)
        if not self._collect_spec_arguments(spec, args):
            return
        if (
            spec.kind in {"adb_shell_input", "adb_root_shell_input"}
            and args
            and self._explicit_su_requested(args[-1])
        ):
            self.status_message.emit(
                "Do not put su in the command text. Select global Root mode and run "
                "the command without su so elevation is verified and applied exactly once.",
                7000,
            )
            return
        if deferred_shell_risk:
            shell_command = args[-1]
            if (
                selected_backend is PrivilegeBackend.ROOT
                or spec.kind == "adb_root_shell_input"
                or (
                    use_shizuku and shizuku_expected_uid == 0
                )
            ):
                shell_command = self.adb.root_shell_script(shell_command)
            actual_command = self.runner.command_text(["adb", "shell", shell_command])
            resolved_risk = analyze_command_risk(["adb", "shell", shell_command])
            if resolved_risk.needs_confirmation and not self._confirm_risk(
                spec.label, actual_command, resolved_risk
            ):
                self.status_message.emit("Command cancelled before execution.", 4000)
                return

        adb = self.adb
        fastboot = self.fastboot
        if context is not None:
            if hasattr(self.device_manager, "is_context_current") and not self.device_manager.is_context_current(context):
                self.status_message.emit(
                    "The active device changed while the command was being prepared. Review it and try again.",
                    7000,
                )
                return
            if spec.required_tool == "ADB" and hasattr(self.adb, "for_context"):
                adb = self.adb.for_context(context)
            elif spec.required_tool == "fastboot" and hasattr(self.fastboot, "for_context"):
                fastboot = self.fastboot.for_context(context)

        if (
            spec.kind in {"adb_shell", "adb_shell_input", "adb_root_shell_input"}
            and context is None
        ):
            self._show_worker_error("Android shell execution requires an active ADB device context.")
            return

        if spec.kind == "adb_root_check":
            fn = (
                partial(
                    self._check_prepared_root_access,
                    context,
                    privilege_lease,
                )
                if privilege_lease is not None
                else partial(self._check_root_access, adb)
            )
        elif spec.kind == "adb_root_shell_input":
            command = args[-1]
            fn = partial(
                self._execute_prepared_shell,
                context,
                command,
                timeout=spec.timeout,
                require_root=True,
                privilege_lease=privilege_lease,
            )
        elif spec.kind == "adb_shell_input":
            command = args[-1]
            fn = partial(
                self._execute_prepared_shell,
                context,
                command,
                timeout=spec.timeout,
                privilege_lease=privilege_lease,
            )
        elif spec.kind == "adb_shell":
            command = " ".join(args)
            fn = partial(
                self._execute_prepared_shell,
                context,
                command,
                timeout=spec.timeout,
                privilege_lease=privilege_lease,
            )
        elif spec.kind == "adb":
            fn = partial(adb.run_raw, args, timeout=spec.timeout, use_serial=spec.use_serial)
        elif spec.kind == "fastboot":
            fn = partial(fastboot.run_raw, args, timeout=spec.timeout, use_serial=spec.use_serial)
        else:
            self._show_worker_error(f"Unsupported command kind: {spec.kind}")
            return
        conflict_group = (
            "device-command"
            if context is not None or spec.key in {"adb_start_server", "adb_kill_server"}
            else "commands-page"
        )
        self._start_command(
            fn,
            spec.actual_command,
            spec.key,
            context=context,
            conflict_group=conflict_group,
            privilege_lease=privilege_lease,
        )

    def _capture_context(self, required_modes: tuple[str, ...] | None = None) -> DeviceContext:
        if hasattr(self.device_manager, "require_context"):
            return self.device_manager.require_context(required_modes or None)
        active = self.device_manager.active
        if not active.serial:
            raise DeviceContextUnavailable("No active Android device is available")
        root = Path(self.settings.config_dir)
        return DeviceContext(
            serial=active.serial,
            mode=active.mode,
            transport_id=active.transport_id,
            profile_key=active.serial,
            profile_kind=active.form_factor or "Phone",
            profile_path=root,
            backups_path=Path(self.settings.backups_folder),
            temp_path=Path(self.settings.temp_folder),
            logs_path=Path(self.settings.logs_folder),
            generation=0,
        )

    def _collect_spec_arguments(self, spec: CommandSpec, args: list[str]) -> bool:
        if spec.file_requirement == "append_file":
            path, _ = QFileDialog.getOpenFileName(self, spec.label, "", spec.file_filter)
            if not path:
                return False
            args.append(path)
        elif spec.file_requirement == "append_folder":
            folder = QFileDialog.getExistingDirectory(self, spec.label, str(Path.home()))
            if not folder:
                return False
            args.append(folder)
        elif spec.file_requirement == "push_pair":
            source = QFileDialog.getExistingDirectory(self, "Choose folder to copy", str(Path.home()))
            if not source:
                source, _ = QFileDialog.getOpenFileName(self, "Choose file to copy")
            if not source:
                return False
            destination, ok = QInputDialog.getText(
                self, "Android destination", "Destination path:", text="/sdcard/"
            )
            if not ok or not destination.strip():
                return False
            args[:] = ["push", source, destination.strip()]
        elif spec.file_requirement == "pull_pair":
            source, ok = QInputDialog.getText(self, "Android source", "Source path:", text="/sdcard/")
            if not ok or not source.strip():
                return False
            destination = QFileDialog.getExistingDirectory(self, "PC destination", str(Path.home()))
            if not destination:
                return False
            args[:] = ["pull", source.strip(), destination]
        if spec.input_prompt:
            value, ok = QInputDialog.getText(self, spec.label, spec.input_prompt)
            if not ok or not value.strip():
                return False
            args.append(value.strip())
        return True

    def run_manual(self) -> None:
        text = self.manual.text().strip()
        if not text:
            self.custom_availability.setText("Enter an adb or fastboot command first.")
            return
        try:
            parts = [part.strip('"') for part in shlex.split(text, posix=False)]
        except ValueError as exc:
            self.custom_availability.setText(str(exc))
            return
        available, reason = self._manual_availability(parts)
        if not available:
            self.custom_availability.setText(f"Unavailable: {reason}")
            self.status_message.emit(reason, 6000)
            return
        operation = self._first_operation(parts[1:])
        shell_command = self._manual_shell_command(parts)
        lease_guarded_operation = operation in {
            "exec-in",
            "exec-out",
            "root",
            "unroot",
            "remount",
            "disable-verity",
            "enable-verity",
        }
        privilege_lease = (
            self._capture_privilege_operation_lease()
            if shell_command or lease_guarded_operation
            else None
        )
        selected_backend = (
            privilege_lease.backend
            if privilege_lease is not None
            else self._selected_shell_backend()
        )
        use_shizuku = self._manual_uses_shizuku(
            parts,
            backend=selected_backend,
        )
        shizuku_expected_uid: int | None = None
        if use_shizuku:
            status = self._current_privilege_status()
            shizuku_expected_uid = getattr(status, "uid", None)
            if shizuku_expected_uid not in {0, 2000}:
                self.custom_availability.setText(
                    "Unavailable: check Shizuku access and review its UID first."
                )
                return
        risk_parts = list(parts)
        risk_parts[0] = "adb" if risk_parts[0].lower() in {"adb", "adb.exe"} else "fastboot"
        risk_parts = self._rootify_adb_shell_parts(
            risk_parts,
            backend=selected_backend,
        )
        if (
            use_shizuku and shizuku_expected_uid == 0
        ):
            risk_parts = self._rootify_adb_shell_parts(risk_parts, force=True)
        risk = analyze_command_risk(risk_parts)
        context: DeviceContext | None = None
        if not self._manual_is_global(parts):
            tool = parts[0].lower()
            modes = ("Fastboot",) if tool in {"fastboot", "fastboot.exe"} else ("ADB", "Recovery", "Sideload")
            try:
                context = self._capture_context(modes)
            except DeviceContextUnavailable as exc:
                self.custom_availability.setText(f"Unavailable: {exc}")
                self.status_message.emit(str(exc), 6000)
                return
        command = self._resolve_manual_command(parts, context=context)
        pairing_target = ""
        pairing_secret = ""
        if operation == "pair":
            pairing_target, pairing_secret = self._manual_pairing_values(parts)
            if not pairing_target or not pairing_secret:
                self.custom_availability.setText(
                    "Unavailable: adb pair requires a target and one-time pairing code."
                )
                self.status_message.emit(
                    "Enter both the Wireless debugging pairing target and code.",
                    6000,
                )
                return
        safe_command = redact_command_arguments(command)
        safe_command_text = self.runner.command_text(safe_command)
        contains_sensitive_data = command_contains_sensitive_data(command)
        if contains_sensitive_data and operation != "pair" and not use_shizuku:
            self.manual.clear()
            message = (
                "Commands containing credentials cannot be run from the custom command field "
                "because Windows process arguments are visible to other local processes."
            )
            self.custom_availability.setText(f"Unavailable: {message}")
            self.status_message.emit(message, 7000)
            return
        operation_index = self._manual_operation_index(parts)
        android_command_text = shell_command
        if operation in {"exec-in", "exec-out"} and operation_index >= 0:
            android_command_text = " ".join(parts[operation_index + 1 :]).strip()
        if android_command_text and self._explicit_su_requested(android_command_text):
            message = (
                "Do not put su in the command text. Select global Root mode and run the "
                "command without su so OpenADB can verify and apply elevation exactly once."
            )
            self.custom_availability.setText(f"Unavailable: {message}")
            self.status_message.emit(message, 7000)
            return
        if risk.needs_confirmation and not self._confirm_risk(
            "Custom command",
            safe_command_text,
            risk,
        ):
            if contains_sensitive_data:
                self.manual.clear()
            self.status_message.emit("Custom command cancelled before execution.", 4000)
            return
        if not contains_sensitive_data:
            self.settings.append_command_history(text)
        self.reload_from_settings()
        if contains_sensitive_data:
            self.manual.clear()
            self.status_message.emit(
                "Sensitive command values were hidden and were not saved to history.",
                6000,
            )
        else:
            self.manual.setText(text)
        runner = self.runner.for_context(context) if context is not None else self.runner
        conflict_group = (
            "device-command"
            if context is not None or operation in {"start-server", "kill-server"}
            else "wireless-connection"
            if operation in {"connect", "pair", "disconnect"}
            else "commands-page"
        )
        command_fn = partial(runner.run_streaming, command, timeout=300)
        if shell_command:
            if context is None:
                self.custom_availability.setText(
                    "Unavailable: Android shell execution requires an active device."
                )
                return
            command_fn = partial(
                self._execute_prepared_shell,
                context,
                shell_command,
                timeout=300,
                privilege_lease=privilege_lease,
            )
        elif operation in {"exec-in", "exec-out"}:
            if context is None or privilege_lease is None:
                self.custom_availability.setText(
                    "Unavailable: adb exec requires an active device and captured access mode."
                )
                return
            command_fn = partial(
                self._execute_prepared_exec,
                context,
                operation,
                parts[operation_index + 1 :],
                timeout=300,
                privilege_lease=privilege_lease,
            )
        elif operation in {
            "root",
            "unroot",
            "remount",
            "disable-verity",
            "enable-verity",
        }:
            command_fn = partial(
                self._execute_guarded_direct_command,
                runner,
                command,
                privilege_lease,
                invalidate_privilege=operation in {"root", "unroot"},
            )
        if operation == "connect":
            target = self._manual_operation_argument(parts, "connect")
            command_fn = partial(self._run_manual_wireless_connect, runner, command, target)
        elif operation == "pair":
            command_fn = partial(
                self._run_manual_wireless_pair,
                pairing_target,
                pairing_secret,
            )
        self._start_command(
            command_fn,
            safe_command_text,
            context=context,
            conflict_group=conflict_group,
            privilege_lease=privilege_lease,
        )

    def _manual_availability(self, parts: list[str]) -> tuple[bool, str]:
        if self._command_running:
            return False, "Another command is already running."
        if self._privilege_busy:
            return False, "The selected access mode is still being prepared for this device."
        if not parts:
            return False, "Command is empty."
        tool = parts[0].lower()
        if tool not in {"adb", "adb.exe", "fastboot", "fastboot.exe"}:
            return False, "Custom commands must begin with adb or fastboot."
        selector_error = self._manual_selector_error(parts)
        if selector_error:
            return False, selector_error
        if tool in {"adb", "adb.exe"}:
            if not self.adb.platform_tools.active.has_adb:
                return False, "ADB is unavailable. Select Platform Tools in Settings."
            operation = self._first_operation(parts[1:])
            no_device = operation in {
                "devices", "version", "start-server", "kill-server", "connect", "disconnect", "pair", "mdns",
            }
            if not no_device and self.device_manager.active.mode not in {"ADB", "Recovery", "Sideload"}:
                return False, f"Current device mode is {self.device_manager.active.mode}; an ADB device is required."
            operation = self._first_operation(parts[1:])
            if operation == "shell" and not self._manual_shell_command(parts):
                return False, "Enter a non-empty Android shell command."
            if (
                self._selected_shell_backend() is PrivilegeBackend.SHIZUKU
                and operation in {"shell", "exec-in", "exec-out"}
            ):
                if operation in {"exec-in", "exec-out"}:
                    return (
                        False,
                        (
                            "Shizuku cannot preserve adb exec-in/exec-out byte streaming. "
                            "Use an adb shell command or select Standard/Root for this transport."
                        ),
                    )
                status = self._current_privilege_status()
                if status is None or not status.available:
                    return False, self._shizuku_unavailable_reason()
            if operation in {
                "root",
                "unroot",
                "remount",
                "disable-verity",
                "enable-verity",
            } and self._selected_shell_backend() is not PrivilegeBackend.ROOT:
                return False, "Select global Root mode before running this root-control ADB operation."
        else:
            if not self.fastboot.platform_tools.active.has_fastboot:
                return False, "fastboot is unavailable. Select Platform Tools in Settings."
            operation = self._first_operation(parts[1:])
            if operation not in {"devices", "--version", "version"} and self.device_manager.active.mode != "Fastboot":
                return False, f"Current device mode is {self.device_manager.active.mode}; Fastboot is required."
        return True, "Ready to run."

    @staticmethod
    def _manual_selector_error(parts: list[str]) -> str:
        """Reject CLI selectors that could escape the immutable active target."""

        blocked_flags = {"-s", "-t", "-d", "-e", "-H", "-P", "-L", "-a"}
        blocked_long = {"--serial", "--transport-id", "--one-device"}
        operations = {
            "--version", "-w", "boot", "bugreport", "connect", "devices", "disconnect",
            "emu", "erase", "exec-in", "exec-out", "features", "fetch", "flash", "flashing",
            "format", "forward", "get-state", "get-serialno", "get-devpath", "getvar", "help",
            "host-features", "install", "install-multiple", "install-multi-package", "jdwp",
            "keygen", "kill-server", "logcat", "mdns", "oem", "pair", "pull", "push", "reboot",
            "reboot-bootloader", "reconnect", "remount", "reverse", "root", "server-status",
            "shell", "sideload", "start-server", "sync", "tcpip", "track-devices", "uninstall",
            "unroot", "usb", "version", "wait-for-device",
        }
        for option in parts[1:]:
            lowered = option.casefold()
            if lowered in operations or lowered.startswith("--set-active"):
                break
            if option in blocked_flags or lowered in blocked_long or any(
                lowered.startswith(prefix + "=") for prefix in blocked_long
            ):
                return (
                    "Custom device/server selectors (-s, -t, -d, -e, -H, -P, -L, "
                    "--serial, --transport-id, --one-device) are not allowed. "
                    "Choose the target in OpenADB so the command remains bound to its device profile."
                )
        return ""

    @staticmethod
    def _first_operation(parts: list[str]) -> str:
        index = 0
        while index < len(parts):
            part = parts[index].lower()
            if part in {"--exit-on-write-error"}:
                index += 1
                continue
            if part == "-s" and index + 1 < len(parts):
                index += 2
                continue
            return part
        return ""

    @staticmethod
    def _manual_operation_argument(parts: list[str], operation: str) -> str:
        operation = operation.casefold()
        for index, value in enumerate(parts[1:], start=1):
            if value.casefold() == operation:
                return parts[index + 1].strip() if index + 1 < len(parts) else ""
        return ""

    @staticmethod
    def _manual_pairing_values(parts: list[str]) -> tuple[str, str]:
        for index, value in enumerate(parts[1:], start=1):
            if value.casefold() != "pair":
                continue
            target = parts[index + 1].strip() if index + 1 < len(parts) else ""
            secret = parts[index + 2].strip() if index + 2 < len(parts) else ""
            return target, secret
        return "", ""

    def _run_manual_wireless_pair(
        self,
        target: str,
        pairing_secret: str,
        *,
        cancel_event: threading.Event,
    ) -> CommandResult:
        return self.adb.pair_wireless_target(
            target,
            pairing_secret,
            cancel_event=cancel_event,
        )

    def _run_manual_wireless_connect(
        self,
        runner,
        command: list[str],
        target: str,
        *,
        cancel_event: threading.Event,
    ) -> CommandResult:
        result = runner.run_streaming(command, timeout=300, cancel_event=cancel_event)
        if cancel_event.is_set() or not target:
            return result
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline and not cancel_event.is_set():
            if self._manual_wireless_target_ready(target):
                result.success = True
                result.error_type = ""
                result.status = f"Wireless ADB connection ready: {target}"
                return result
            cancel_event.wait(0.35)
        if not cancel_event.is_set():
            result.success = False
            result.error_type = "connection_not_ready"
            result.status = (
                "ADB connect finished, but the requested Wireless debugging transport "
                f"did not become ready: {target}"
            )
        return result

    def _manual_wireless_target_ready(self, target: str) -> bool:
        values = {str(target or "").strip().casefold()}
        target_key = str(target or "").strip().rstrip(".")
        if target_key.casefold().startswith("adb-"):
            if not target_key.casefold().endswith("._adb-tls-connect._tcp"):
                target_key += "._adb-tls-connect._tcp"
            values.update({target_key.casefold(), (target_key + ".").casefold()})
        return any(
            device.state == "device" and str(device.serial or "").strip().casefold() in values
            for device in self.adb.list_devices()
        )

    def _manual_is_global(self, parts: list[str]) -> bool:
        if not parts:
            return False
        operation = self._first_operation(parts[1:])
        if parts[0].lower() in {"adb", "adb.exe"}:
            return operation in {
                "devices", "version", "start-server", "kill-server", "connect", "disconnect", "pair", "mdns",
            }
        return operation in {"devices", "--version", "version"}

    def _resolve_manual_command(
        self,
        parts: list[str],
        context: DeviceContext | None = None,
    ) -> list[str]:
        first = parts[0].lower()
        if first in {"adb", "adb.exe"} and self.adb.platform_tools.adb_path:
            resolved = [str(self.adb.platform_tools.adb_path), *parts[1:]]
            if context is not None and context.serial and "-s" not in resolved:
                resolved[1:1] = ["-s", context.serial]
            return resolved
        if first in {"fastboot", "fastboot.exe"} and self.fastboot.platform_tools.fastboot_path:
            resolved = [str(self.fastboot.platform_tools.fastboot_path), *parts[1:]]
            if context is not None and context.serial and "-s" not in resolved:
                resolved[1:1] = ["-s", context.serial]
            return resolved
        return parts

    def _rootify_adb_shell_parts(
        self,
        parts: list[str],
        *,
        force: bool = False,
        backend: PrivilegeBackend | str | None = None,
    ) -> list[str]:
        selected = (
            PrivilegeBackend.normalize(backend)
            if backend is not None
            else self._selected_shell_backend()
        )
        if not force and selected is not PrivilegeBackend.ROOT:
            return parts
        shell_index = self._manual_operation_index(parts)
        if shell_index < 0 or parts[shell_index].casefold() != "shell":
            return parts
        if shell_index >= len(parts) - 1:
            return parts
        shell_command = " ".join(parts[shell_index + 1 :]).strip()
        if not shell_command:
            return parts
        return [*parts[: shell_index + 1], self.adb.root_shell_script(shell_command)]

    def _root_shell_toggled(self, checked: bool) -> None:
        current = self._selected_shell_backend()
        if checked:
            self.privilege_selector.set_backend(PrivilegeBackend.ROOT)
            self._privilege_backend_changed(PrivilegeBackend.ROOT.value)
        elif current is PrivilegeBackend.ROOT:
            self.privilege_selector.set_backend(PrivilegeBackend.STANDARD)
            self._privilege_backend_changed(PrivilegeBackend.STANDARD.value)

    def _shizuku_shell_toggled(self, checked: bool) -> None:
        current = self._selected_shell_backend()
        if checked:
            self.privilege_selector.set_backend(PrivilegeBackend.SHIZUKU)
            self._privilege_backend_changed(PrivilegeBackend.SHIZUKU.value)
        elif current is PrivilegeBackend.SHIZUKU:
            self.privilege_selector.set_backend(PrivilegeBackend.STANDARD)
            self._privilege_backend_changed(PrivilegeBackend.STANDARD.value)

    def _privilege_backend_changed(self, value: str) -> None:
        backend = PrivilegeBackend.normalize(value)
        self.root_shell.blockSignals(True)
        self.shizuku_shell.blockSignals(True)
        self.root_shell.setChecked(backend is PrivilegeBackend.ROOT)
        self.shizuku_shell.setChecked(backend is PrivilegeBackend.SHIZUKU)
        self.root_shell.blockSignals(False)
        self.shizuku_shell.blockSignals(False)
        self.settings.select_privilege_backend(
            backend.value,
            profile_available=getattr(
                self,
                "_privilege_profile_available",
                True,
            ),
        )
        self._root_access_state = "unknown"
        self._root_access_serial = ""
        self._root_access_generation = None
        self._root_access_context = None
        self._privilege_status = None
        self._update_privilege_controls()
        self._refresh_availability()
        self.settings_changed.emit()

    def _selected_shell_backend(self) -> PrivilegeBackend:
        return self.privilege_selector.backend()

    def _capture_privilege_operation_lease(self):
        capture = getattr(
            self.privilege_manager,
            "capture_operation_lease",
            None,
        )
        return capture() if callable(capture) else None

    def _current_privilege_status(self) -> PrivilegeStatus | None:
        status = self._privilege_status
        if status is not None and self._privilege_status_is_current(status):
            if status.backend is self._selected_shell_backend():
                return status
        cached = self.privilege_manager.cached_status()
        if cached is not None and cached.backend is self._selected_shell_backend():
            self._privilege_status = cached
            return cached
        return None

    def _privilege_status_is_current(self, status: PrivilegeStatus) -> bool:
        active = self.device_manager.active
        if status.device_serial != str(getattr(active, "serial", "") or ""):
            return False
        current_generation = getattr(self.device_manager, "current_generation", None)
        return not (
            status.device_generation is not None
            and current_generation is not None
            and status.device_generation != current_generation
        )

    def _shizuku_unavailable_reason(self, *, require_root: bool = False) -> str:
        status = self._current_privilege_status()
        if status is None:
            return "Run Check Shizuku access for the active device first."
        if require_root and status.available and not status.root:
            return (
                "This command requires UID 0. ADB-started Shizuku provides Android shell "
                "access (UID 2000), not root."
            )
        state = status.state
        if state in {"permission_required", "permission_denied"}:
            return "Grant OpenADB Bridge permission in Shizuku on the Android device."
        if state in {"stopped", "binder_dead"}:
            return "Start Shizuku on the Android device, then check access again."
        if state == "not_installed":
            return "Install and start Shizuku on Android; OpenADB does not install it automatically."
        return status.message or "Shizuku is unavailable for the active device."

    def set_privilege_status(self, status: PrivilegeStatus | None) -> None:
        self._privilege_status = status
        if status is None:
            self._root_access_state = "unknown"
            self._root_access_serial = ""
            self._root_access_generation = None
            self._root_access_context = None
            profile_available = getattr(self, "_privilege_profile_available", True)
            if profile_available:
                text = "Privileged access has not been checked for this device."
            elif self.privilege_selector.has_backend():
                text = "The selected access mode will be applied to the next active device."
            else:
                text = "Choose an access mode to apply to the next active device."
            self._set_privilege_status_text(text)
        else:
            label = {
                PrivilegeBackend.STANDARD: "Standard ADB",
                PrivilegeBackend.ROOT: "Root",
                PrivilegeBackend.SHIZUKU: "Shizuku",
            }.get(status.backend, "Privileged access")
            self._set_privilege_status_text(f"{label}: {status.message}")
            if status.backend is PrivilegeBackend.ROOT:
                self._root_access_state = "available" if status.root else "unavailable"
                self._root_access_serial = status.device_serial
                self._root_access_generation = status.device_generation
                self._root_access_context = None
            else:
                self._root_access_state = "unknown"
                self._root_access_serial = ""
                self._root_access_generation = None
                self._root_access_context = None
        self._update_privilege_controls()
        self._refresh_availability()

    def set_privilege_busy(self, busy: bool, message: str = "") -> None:
        self._privilege_busy = bool(busy)
        if busy:
            self._set_privilege_status_text(message or "Checking privileged access…")
        self._update_privilege_controls()
        self._refresh_availability()

    def _set_privilege_status_text(self, text: str) -> None:
        text = str(text or "Privileged access is unavailable.")
        self.privilege_status.setText(text)
        self.privilege_status.setToolTip(text)
        self.privilege_status.setAccessibleDescription(text)

    def _update_privilege_controls(self) -> None:
        backend = self._selected_shell_backend()
        busy = self._privilege_busy
        profile_available = getattr(self, "_privilege_profile_available", True)
        self.check_privilege_button.setEnabled(profile_available and not busy)
        self.check_privilege_button.setText(
            "Check Shizuku" if backend is PrivilegeBackend.SHIZUKU else (
                "Check root" if backend is PrivilegeBackend.ROOT else "Check access"
            )
        )
        shizuku = backend is PrivilegeBackend.SHIZUKU
        self.request_shizuku_button.setVisible(shizuku)
        self.open_shizuku_button.setVisible(shizuku)
        self.request_shizuku_button.setEnabled(profile_available and shizuku and not busy)
        self.open_shizuku_button.setEnabled(profile_available and shizuku and not busy)

    def set_privilege_profile_available(self, available: bool) -> None:
        """Keep selection available; gate only actions requiring a device."""

        self._privilege_profile_available = bool(available)
        self.privilege_selector.setEnabled(True)
        self.privilege_selector.set_profile_available(
            self._privilege_profile_available
        )
        configured_backend = read_privilege_backend_setting(
            self.settings,
            profile_available=self._privilege_profile_available,
        )
        if self._privilege_profile_available:
            self.privilege_selector.set_backend(configured_backend)
        else:
            self.privilege_selector.set_pending_backend(configured_backend)
        self.set_privilege_status(None)
        self._update_privilege_controls()

    def _manual_uses_shizuku(
        self,
        parts: list[str],
        *,
        backend: PrivilegeBackend | str | None = None,
    ) -> bool:
        operation_index = self._manual_operation_index(parts)
        selected = (
            PrivilegeBackend.normalize(backend)
            if backend is not None
            else self._selected_shell_backend()
        )
        return bool(
            parts
            and parts[0].lower() in {"adb", "adb.exe"}
            and operation_index >= 0
            and parts[operation_index].casefold() == "shell"
            and selected is PrivilegeBackend.SHIZUKU
        )

    @classmethod
    def _manual_shell_command(cls, parts: list[str]) -> str:
        shell_index = cls._manual_operation_index(parts)
        if shell_index < 0 or parts[shell_index].casefold() != "shell":
            return ""
        return " ".join(parts[shell_index + 1 :]).strip()

    @staticmethod
    def _explicit_su_requested(shell_command: str) -> bool:
        """Detect explicit su routing that would bypass or double-wrap global mode."""

        return bool(
            re.search(
                r"(?<![\w.-])(?:/(?:system/(?:xbin|bin)|sbin)/)?su(?=\s|$|[;&|])",
                str(shell_command or ""),
                flags=re.IGNORECASE,
            )
        )

    @staticmethod
    def _manual_operation_index(parts: list[str]) -> int:
        """Return the real adb/fastboot operation index, never an argument match."""

        index = 1
        while index < len(parts):
            part = parts[index].casefold()
            if part == "--exit-on-write-error":
                index += 1
                continue
            if part == "-s" and index + 1 < len(parts):
                index += 2
                continue
            return index
        return -1

    def _root_access_is_confirmed(self) -> bool:
        serial_matches = bool(
            self._root_access_state == "available"
            and self._root_access_serial
            and self._root_access_serial == self.device_manager.active.serial
        )
        if not serial_matches:
            return False
        if self._root_access_context is not None and hasattr(
            self.device_manager,
            "is_context_current",
        ):
            return bool(self.device_manager.is_context_current(self._root_access_context))
        current_generation = getattr(self.device_manager, "current_generation", None)
        if self._root_access_generation is not None and current_generation is not None:
            return self._root_access_generation == current_generation
        return True

    @staticmethod
    def _check_root_access(adb, cancel_event: threading.Event) -> CommandResult:
        direct = adb.run_shell("id -u", timeout=8, cancel_event=cancel_event)
        if cancel_event.is_set() or direct.stdout.strip() == "0":
            return direct
        return adb.run_root_shell(
            "id -u; id; getprop ro.debuggable; getprop ro.secure",
            timeout=20,
            cancel_event=cancel_event,
        )

    def _check_prepared_root_access(
        self,
        context: DeviceContext,
        privilege_lease,
        *,
        cancel_event: threading.Event,
    ) -> CommandResult:
        if context is None or privilege_lease is None:
            raise RuntimeError("Root access checking requires a captured device and access mode.")
        prepare_kwargs = {"cancel_event": cancel_event}
        if privilege_lease is not None:
            prepare_kwargs["privilege_lease"] = privilege_lease
        prepared = self.privilege_manager.prepare_adb(context, **prepare_kwargs)
        effective = PrivilegeBackend.normalize(
            getattr(
                prepared,
                "effective_privilege_backend",
                PrivilegeBackend.STANDARD,
            )
        )
        if effective is not PrivilegeBackend.ROOT:
            raise RuntimeError(
                "Root access was denied or unavailable for the captured Root operation."
            )
        return prepared.run_shell(
            "id -u; id; getprop ro.debuggable; getprop ro.secure",
            timeout=20,
            cancel_event=cancel_event,
        )

    def _execute_prepared_shell(
        self,
        context: DeviceContext,
        shell_command: str,
        *,
        timeout: float | None = 120,
        require_root: bool = False,
        privilege_lease=None,
        cancel_event: threading.Event,
    ) -> CommandResult:
        """Execute one shell command through the globally selected backend."""

        prepare_kwargs = {"cancel_event": cancel_event}
        if privilege_lease is not None:
            prepare_kwargs["privilege_lease"] = privilege_lease
        prepared = self.privilege_manager.prepare_adb(context, **prepare_kwargs)
        if hasattr(self.device_manager, "is_context_current") and not self.device_manager.is_context_current(context):
            raise DeviceContextUnavailable(
                "The active device changed while privileged access was being prepared."
            )
        if require_root:
            effective = PrivilegeBackend.normalize(
                getattr(
                    prepared,
                    "effective_privilege_backend",
                    PrivilegeBackend.STANDARD.value,
                )
            )
            root_ready = effective is PrivilegeBackend.ROOT or (
                effective is PrivilegeBackend.SHIZUKU
                and getattr(prepared, "verified_uid", None) == 0
            )
            if not root_ready:
                raise RuntimeError(
                    "This command requires UID 0, but the selected access mode did not provide root."
                )
            return prepared.run_root_shell(
                shell_command,
                timeout=timeout,
                cancel_event=cancel_event,
            )
        return prepared.run_shell(
            shell_command,
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def _execute_prepared_exec(
        self,
        context: DeviceContext,
        operation: str,
        payload: list[str],
        *,
        timeout: float | None,
        privilege_lease,
        cancel_event: threading.Event,
    ) -> CommandResult:
        """Keep adb exec streaming while enforcing the captured Standard/Root mode."""

        if privilege_lease is None:
            raise RuntimeError("adb exec requires a captured access mode.")
        if privilege_lease.backend is PrivilegeBackend.SHIZUKU:
            raise RuntimeError(
                "Shizuku cannot preserve adb exec-in/exec-out byte streaming."
            )
        prepared = self.privilege_manager.prepare_adb(
            context,
            cancel_event=cancel_event,
            privilege_lease=privilege_lease,
        )
        raw_payload = list(payload)
        if privilege_lease.backend is PrivilegeBackend.ROOT:
            effective = PrivilegeBackend.normalize(
                getattr(prepared, "effective_privilege_backend", PrivilegeBackend.STANDARD)
            )
            if effective is not PrivilegeBackend.ROOT:
                raise RuntimeError("Root is unavailable for this adb exec operation.")
            strategy = getattr(prepared, "root_strategy", None)
            if str(getattr(strategy, "value", strategy) or "").casefold() == "su":
                command = " ".join(raw_payload).strip()
                if not command:
                    raise RuntimeError("adb exec requires a non-empty Android command.")
                raw_payload = ["sh", "-c", self.adb.root_shell_script(command)]
        return prepared.run_raw_streaming(
            [operation, *raw_payload],
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def _execute_guarded_direct_command(
        self,
        runner,
        command: list[str],
        privilege_lease,
        *,
        invalidate_privilege: bool,
        cancel_event: threading.Event,
    ) -> CommandResult:
        """Run an ADB daemon/root-control command only for its captured mode."""

        if privilege_lease is None or privilege_lease.backend is not PrivilegeBackend.ROOT:
            raise RuntimeError("Select Root mode before running this ADB root-control command.")
        linked = self.privilege_manager.linked_cancellation_event(
            privilege_lease,
            cancel_event,
        )
        result = runner.run_streaming(command, timeout=300, cancel_event=linked)
        self.privilege_manager.validate_operation_lease(privilege_lease)
        if invalidate_privilege:
            self.privilege_manager.reset()
        return result

    def _start_command(
        self,
        fn,
        planned_command: str,
        spec_key: str = "",
        *,
        context: DeviceContext | None = None,
        conflict_group: str | None = None,
        privilege_lease=None,
    ) -> None:
        planned_command = redact_sensitive_text(planned_command)
        if self._command_running:
            self.status_message.emit("Another command is already running.", 5000)
            return
        if (
            context is not None
            and hasattr(self.device_manager, "is_context_current")
            and not self.device_manager.is_context_current(context)
        ):
            self.status_message.emit(
                "The active device changed before the command could start. Review it and try again.",
                7000,
            )
            return
        try:
            token = self.operations.register(
                "commands-page",
                device_context=context,
                conflict_group=conflict_group
                or ("device-command" if context is not None else "commands-page"),
                conflict_groups=(
                    (
                        f"device-exclusive:{context.serial}",
                        f"acbridge-maintenance:{context.serial}",
                    )
                    if context is not None
                    else ()
                ),
            )
        except (OperationConflictError, RuntimeError) as exc:
            self.status_message.emit(str(exc), 6000)
            return
        if (
            context is not None
            and hasattr(self.device_manager, "is_context_current")
            and not self.device_manager.is_context_current(context)
        ):
            token.cancel("device context changed before command registration completed")
            self.operations.finish(token)
            self.status_message.emit(
                "The active device changed before the command could start. Review it and try again.",
                7000,
            )
            return
        token.privilege_lease = privilege_lease
        self._command_running = True
        self._command_token = token
        self._running_spec_key = spec_key
        self._cancel_event = token.cancel_event
        self._set_output_status_text("Running…")
        self.output_status.setProperty("resultState", "running")
        self.output_command.setText(planned_command)
        self.output_command.setToolTip(planned_command)
        self.output_exit.setText("Exit code: —")
        self.output_duration.setText("Duration: —")
        self.stdout_output.clear()
        self.stderr_output.clear()
        self.output_content.setCurrentWidget(self.output_tabs)
        self.cancel_button.setEnabled(True)
        self.copy_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self._refresh_availability()
        worker = Worker(
            lambda: self._run_registered_command(token, context, fn, planned_command)
        )
        worker.signals.result.connect(lambda result: self._show_result(result, token))
        worker.signals.error.connect(lambda message, _trace: self._show_worker_error(message, token))
        worker.signals.finished.connect(lambda: self._command_finished(token))
        started = start_worker(
            self,
            self.pool,
            worker,
            operation_registry=self.operations,
            operation_token=token,
        )
        if started is False:
            self._command_finished(token)

    def _run_registered_command(
        self,
        token: OperationToken,
        context: DeviceContext | None,
        fn,
        planned_command: str,
    ) -> CommandResult:
        if token.cancelled:
            return self._cancelled_before_execution_result(planned_command)
        if (
            context is not None
            and hasattr(self.device_manager, "is_context_current")
            and not self.device_manager.is_context_current(context)
        ):
            token.cancel("device context changed before worker execution")
            return self._cancelled_before_execution_result(planned_command)
        return fn(cancel_event=token.cancel_event)

    @staticmethod
    def _cancelled_before_execution_result(planned_command: str) -> CommandResult:
        started = datetime.now()
        return CommandResult(
            command=[planned_command],
            exit_code=None,
            stdout="",
            stderr="",
            duration=0.0,
            started_at=started,
            finished_at=started,
            success=False,
            status="Cancelled before execution",
            error_type="cancelled",
        )

    def cancel_running_command(self) -> None:
        if self._cancel_event is None or not self._command_running:
            return
        if self._command_token is not None:
            self._command_token.cancel("user cancelled")
        else:
            self._cancel_event.set()
        self.cancel_button.setEnabled(False)
        self._set_output_status_text("Cancelling…")
        self.status_message.emit("Cancellation requested.", 4000)

    def _set_output_status_text(self, text: str, details: str = "") -> None:
        text = str(text or "Command status unavailable.")
        details = str(details or "").strip()
        full_tooltip = text if not details else f"{text}\n\n{details}"
        self.output_status.setText(text)
        self.output_status.setToolTip(full_tooltip)
        self.output_status.setAccessibleDescription(full_tooltip)

    def _show_result(self, result: CommandResult, token: OperationToken | None = None) -> None:
        if token is not None and not self._command_callback_is_current(token):
            self.status_message.emit(
                "Command finished for a device that is no longer active; its result was not applied.",
                7000,
            )
            return
        if (
            result.command
            and str(result.command[0]).casefold() == "shizuku"
            and result.error_type in SHIZUKU_INVALIDATING_ERRORS
        ):
            self.set_privilege_status(None)
            self.privilege_status_invalidated.emit()
        if self._running_spec_key == "root_check":
            self._root_access_state = "available" if self._result_confirms_root(result) else "unavailable"
            context = token.device_context if token is not None else None
            self._root_access_context = context
            self._root_access_serial = context.serial if context is not None else self.device_manager.active.serial
            self._root_access_generation = (
                context.generation
                if context is not None
                else getattr(self.device_manager, "current_generation", None)
            )
        safe_status = redact_sensitive_text(
            result.status or ("Success" if result.success else "Command failed")
        )
        safe_command_text = self.runner.command_text(redact_command_arguments(result.command))
        safe_stdout = redact_sensitive_text(result.stdout)
        safe_stderr = redact_sensitive_text(result.stderr)
        log_warning = redact_sensitive_text(
            str(getattr(result, "log_warning", "") or "").strip()
        )
        self._set_output_status_text(safe_status, log_warning)
        state = "success" if result.success else ("cancelled" if result.error_type == "cancelled" else "error")
        self.output_status.setProperty("resultState", state)
        self.output_status.style().unpolish(self.output_status)
        self.output_status.style().polish(self.output_status)
        self.output_command.setText(safe_command_text)
        self.output_command.setToolTip(safe_command_text)
        self.output_exit.setText(f"Exit code: {result.exit_code if result.exit_code is not None else '—'}")
        self.output_duration.setText(f"Duration: {result.duration:.2f} s")
        self.stdout_output.setPlainText(safe_stdout)
        self.stderr_output.setPlainText(safe_stderr)
        self.output_content.setCurrentWidget(self.output_tabs)
        if safe_stderr and not safe_stdout:
            self.output_tabs.setCurrentWidget(self.stderr_output)
        else:
            self.output_tabs.setCurrentWidget(self.stdout_output)
        status_message = self.output_status.full_text()
        if log_warning:
            status_message = f"{status_message}. {log_warning}"
        self.status_message.emit(status_message, 7000 if log_warning else 5000)
        self.copy_button.setEnabled(True)
        self.clear_button.setEnabled(True)

    def _show_worker_error(self, message: str, token: OperationToken | None = None) -> None:
        message = redact_sensitive_text(message)
        if token is not None and not self._command_callback_is_current(token):
            self.status_message.emit(
                "A command for a device that is no longer active ended with an error; the current view was not changed.",
                7000,
            )
            return
        self._set_output_status_text("Command worker failed", message)
        self.output_status.setProperty("resultState", "error")
        self.output_status.style().unpolish(self.output_status)
        self.output_status.style().polish(self.output_status)
        self.stderr_output.setPlainText(message)
        self.output_content.setCurrentWidget(self.output_tabs)
        self.output_tabs.setCurrentWidget(self.stderr_output)
        self.status_message.emit(message, 7000)
        self.copy_button.setEnabled(True)
        self.clear_button.setEnabled(True)

    def _command_finished(self, token: OperationToken | None = None) -> None:
        if token is not None and self._command_token is not token:
            return
        self._command_running = False
        self._command_token = None
        self._running_spec_key = ""
        self._cancel_event = None
        self.cancel_button.setEnabled(False)
        self._refresh_availability()

    def _command_callback_is_current(self, token: OperationToken) -> bool:
        if self._command_token is not token or getattr(self, "_workers_shutting_down", False):
            return False
        if token.cancelled and token.cancellation_reason != "user cancelled":
            return False
        context = token.device_context
        return context is None or not hasattr(self.device_manager, "is_context_current") or bool(
            self.device_manager.is_context_current(context)
        )

    @staticmethod
    def _result_confirms_root(result: CommandResult) -> bool:
        lines = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
        return result.success and any(line == "0" or "uid=0" in line for line in lines)

    def clear_result(self) -> None:
        if self._command_running:
            return
        self._set_output_status_text("No command has run")
        self.output_status.setProperty("resultState", "empty")
        self.output_status.style().unpolish(self.output_status)
        self.output_status.style().polish(self.output_status)
        self.output_command.clear()
        self.output_exit.setText("Exit code: —")
        self.output_duration.setText("Duration: —")
        self.stdout_output.clear()
        self.stderr_output.clear()
        self.output_content.setCurrentWidget(self.output_empty_state)
        self.copy_button.setEnabled(False)
        self.clear_button.setEnabled(False)

    def _focus_command_catalog(self) -> None:
        self.page_tabs.setCurrentIndex(0)
        self.tree.setFocus(Qt.OtherFocusReason)

    def copy_result(self) -> None:
        text = "\n".join(
            [
                self.output_status.full_text(),
                f"$ {self.output_command.text()}" if self.output_command.text() else "",
                self.output_exit.text(),
                self.output_duration.text(),
                "stdout:",
                self.stdout_output.toPlainText(),
                "stderr:",
                self.stderr_output.toPlainText(),
            ]
        ).strip()
        QApplication.clipboard().setText(text)
        self.status_message.emit("Command result copied.", 3000)

    def _confirm_risk(self, title: str, actual_command: str, risk: RiskInfo) -> bool:
        consequence = risk.description or "This command can change device state or data."
        message = (
            f"Risk level: {risk.level}\n\n{consequence}\n\nCommand:\n{actual_command}"
        )
        if risk.typed_confirmation:
            token = risk.typed_confirmation
            value, ok = QInputDialog.getText(
                self,
                title,
                message + f"\n\nType {token} to continue:",
            )
            return bool(ok and value.strip() == token)
        answer = QMessageBox.warning(
            self,
            title,
            message + "\n\nContinue?",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return answer == QMessageBox.Ok
