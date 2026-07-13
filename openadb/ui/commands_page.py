from __future__ import annotations

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
    QLineEdit,
    QLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QSizePolicy,
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
from openadb.core.operations import OperationConflictError, OperationRegistry, OperationToken
from openadb.core.safety import RiskInfo, analyze_command_risk
from openadb.core.settings_manager import SettingsManager
from openadb.models.command_result import CommandResult
from openadb.models.command_spec import CommandSpec
from openadb.models.device_info import DeviceInfo
from openadb.ui.design_system import configure_page_layout
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox as QComboBox
from openadb.ui.workers import Worker, start_worker


class CommandsPage(QWidget):
    open_logs_requested = Signal()
    status_message = Signal(str, int)
    settings_changed = Signal()

    def __init__(
        self,
        adb: ADBClient,
        fastboot: FastbootClient,
        runner: CommandRunner,
        settings: SettingsManager,
        device_manager: DeviceManager,
        detect_tools_callback,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.adb = adb
        self.fastboot = fastboot
        self.runner = runner
        self.settings = settings
        self.device_manager = device_manager
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
        self._root_access_context: DeviceContext | None = None

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
        self.search.setClearButtonEnabled(True)
        self.view_mode = QComboBox()
        self.view_mode.addItems(["Basic", "Advanced"])
        saved_mode = str(settings.get("commands_view_mode", "Basic"))
        self.view_mode.setCurrentText(saved_mode if saved_mode in {"Basic", "Advanced"} else "Basic")
        self.category_filter = QComboBox()
        self.category_filter.addItems(["All categories", *COMMAND_CATEGORIES])
        self.command_count = QLabel()
        self.command_count.setObjectName("commandCount")
        toolbar_layout.addWidget(self.search, 0, 0, 1, 3)
        toolbar_layout.addWidget(self.view_mode, 1, 0)
        toolbar_layout.addWidget(self.category_filter, 1, 1)
        toolbar_layout.addWidget(self.command_count, 1, 2)
        toolbar_layout.setColumnStretch(0, 1)
        toolbar_layout.setColumnStretch(1, 1)
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
        self.root_shell.toggled.connect(self._root_shell_toggled)
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

    def _build_details_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("commandDetailsPanel")
        panel_layout = QVBoxLayout(panel)
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
        return panel

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
        self.manual.setClearButtonEnabled(True)
        page_layout.addWidget(self.manual)
        self.root_shell = QCheckBox("Run adb shell command through existing root/su access")
        self.root_shell.setChecked(bool(self.settings.get("root_mode_enabled", False)))
        self.root_shell.setToolTip(
            "Only affects adb shell commands. OpenADB does not obtain root; root commands require typed confirmation."
        )
        page_layout.addWidget(self.root_shell)
        self.custom_availability = QLabel("ADB and fastboot commands are validated before they run.")
        self.custom_availability.setObjectName("commandAvailability")
        self.custom_availability.setWordWrap(True)
        page_layout.addWidget(self.custom_availability)
        self.custom_run_button = QPushButton("Run custom command")
        self.custom_run_button.setObjectName("primaryAction")
        page_layout.addWidget(self.custom_run_button)
        page_layout.addStretch()
        return page

    def _build_output_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("commandOutputPanel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(10, 9, 10, 10)
        panel_layout.setSpacing(7)
        top = QHBoxLayout()
        title = QLabel("Command result")
        title.setObjectName("commandGroupTitle")
        self.output_status = QLabel("No command has run")
        self.output_status.setObjectName("commandOutputStatus")
        top.addWidget(title)
        top.addWidget(self.output_status)
        top.addStretch()
        self.output_exit = QLabel("Exit code: —")
        self.output_duration = QLabel("Duration: —")
        top.addWidget(self.output_exit)
        top.addWidget(self.output_duration)
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
        return panel

    def reload_from_settings(self) -> None:
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItems(self.settings.get("command_history", []))
        self.history.blockSignals(False)
        self.root_shell.blockSignals(True)
        self.root_shell.setChecked(bool(self.settings.get("root_mode_enabled", False)))
        self.root_shell.blockSignals(False)
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
            self._root_access_context = None
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
            self.tree.addTopLevelItem(group)
            for spec in specs:
                available, reason = self._availability(spec)
                suffix = "" if available else " — Unavailable"
                item = QTreeWidgetItem([spec.label + suffix])
                item.setData(0, Qt.UserRole, spec.key)
                item.setToolTip(0, f"{spec.actual_command}\n{reason}")
                group.addChild(item)
                first_item = first_item or item
                if spec.key == selected_key:
                    selected_item = item
        self.tree.blockSignals(False)
        self.command_count.setText(f"Showing {len(visible_specs)} of {len(self.specs)}")
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
        if spec.requires_root and not bool(self.settings.get("root_mode_enabled", False)):
            return False, "Enable root-assisted features in Settings; OpenADB does not obtain root access."
        if spec.requires_root and not self._root_access_is_confirmed():
            if self._root_access_state == "unavailable":
                return False, "Root access was not granted. Check the device's su/root configuration."
            return False, "Run Check root access for the active device first."
        return True, "Ready to run."

    def _refresh_availability(self) -> None:
        self._rebuild_tree()
        self.custom_run_button.setEnabled(not self._command_running)
        self.custom_availability.setText(
            "Another command is running." if self._command_running else "ADB and fastboot commands are validated before they run."
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
        context: DeviceContext | None = None
        if spec.use_serial:
            try:
                context = self._capture_context(spec.required_modes)
            except DeviceContextUnavailable as exc:
                self.status_message.emit(str(exc), 6000)
                self._refresh_availability()
                return
        deferred_shell_risk = spec.kind in {"adb_shell_input", "adb_root_shell_input"}
        risk = spec.risk
        if not deferred_shell_risk and risk.needs_confirmation and not self._confirm_risk(
            spec.label, spec.actual_command, risk
        ):
            self.status_message.emit("Command cancelled before execution.", 4000)
            return
        args = list(spec.args)
        if not self._collect_spec_arguments(spec, args):
            return
        if deferred_shell_risk:
            shell_command = args[-1]
            if spec.kind == "adb_root_shell_input":
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

        if spec.kind == "adb_root_check":
            fn = partial(self._check_root_access, adb)
        elif spec.kind == "adb_root_shell_input":
            command = args[-1]
            fn = partial(adb.run_root_shell, command, timeout=spec.timeout)
        elif spec.kind == "adb_shell_input":
            command = args[-1]
            fn = partial(adb.run_shell, command, timeout=spec.timeout)
        elif spec.kind == "adb_shell":
            command = " ".join(args)
            fn = partial(adb.run_shell, command, timeout=spec.timeout)
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
        risk_parts = list(parts)
        risk_parts[0] = "adb" if risk_parts[0].lower() in {"adb", "adb.exe"} else "fastboot"
        risk_parts = self._rootify_adb_shell_parts(risk_parts)
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
        if risk.needs_confirmation and not self._confirm_risk("Custom command", self.runner.command_text(command), risk):
            self.status_message.emit("Custom command cancelled before execution.", 4000)
            return
        self.settings.append_command_history(text)
        self.reload_from_settings()
        self.manual.setText(text)
        runner = self.runner.for_context(context) if context is not None else self.runner
        operation = self._first_operation(parts[1:])
        conflict_group = (
            "device-command"
            if context is not None or operation in {"start-server", "kill-server"}
            else "wireless-connection"
            if operation in {"connect", "pair", "disconnect"}
            else "commands-page"
        )
        command_fn = partial(runner.run_streaming, command, timeout=300)
        if operation == "connect":
            target = self._manual_operation_argument(parts, "connect")
            command_fn = partial(self._run_manual_wireless_connect, runner, command, target)
        self._start_command(
            command_fn,
            self.runner.command_text(command),
            context=context,
            conflict_group=conflict_group,
        )

    def _manual_availability(self, parts: list[str]) -> tuple[bool, str]:
        if self._command_running:
            return False, "Another command is already running."
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
            lowered = [part.lower() for part in parts]
            if self.root_shell.isChecked() and "shell" in lowered and not self._root_access_is_confirmed():
                return False, "Run Check root access for the active device before using root shell."
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
            parts = self._rootify_adb_shell_parts(parts)
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

    def _rootify_adb_shell_parts(self, parts: list[str]) -> list[str]:
        if not self.root_shell.isChecked():
            return parts
        lowered = [part.lower() for part in parts]
        if "shell" not in lowered:
            return parts
        shell_index = lowered.index("shell")
        if shell_index >= len(parts) - 1:
            return parts
        shell_command = " ".join(parts[shell_index + 1 :]).strip()
        if not shell_command:
            return parts
        return [*parts[: shell_index + 1], self.adb.root_shell_script(shell_command)]

    def _root_shell_toggled(self, checked: bool) -> None:
        self.settings.set("root_mode_enabled", checked)
        if not checked:
            self._root_access_state = "unknown"
            self._root_access_serial = ""
            self._root_access_context = None
        self._refresh_availability()
        self.settings_changed.emit()

    def _root_access_is_confirmed(self) -> bool:
        serial_matches = bool(
            self._root_access_state == "available"
            and self._root_access_serial
            and self._root_access_serial == self.device_manager.active.serial
        )
        if not serial_matches:
            return False
        if self._root_access_context is None or not hasattr(self.device_manager, "is_context_current"):
            return True
        return bool(self.device_manager.is_context_current(self._root_access_context))

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

    def _start_command(
        self,
        fn,
        planned_command: str,
        spec_key: str = "",
        *,
        context: DeviceContext | None = None,
        conflict_group: str | None = None,
    ) -> None:
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
                conflict_groups=(f"device-exclusive:{context.serial}",) if context is not None else (),
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
        self._command_running = True
        self._command_token = token
        self._running_spec_key = spec_key
        self._cancel_event = token.cancel_event
        self.output_status.setText("Running…")
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
        self.output_status.setText("Cancelling…")
        self.status_message.emit("Cancellation requested.", 4000)

    def _show_result(self, result: CommandResult, token: OperationToken | None = None) -> None:
        if token is not None and not self._command_callback_is_current(token):
            self.status_message.emit(
                "Command finished for a device that is no longer active; its result was not applied.",
                7000,
            )
            return
        if self._running_spec_key == "root_check":
            self._root_access_state = "available" if self._result_confirms_root(result) else "unavailable"
            context = token.device_context if token is not None else None
            self._root_access_context = context
            self._root_access_serial = context.serial if context is not None else self.device_manager.active.serial
        self.output_status.setText(result.status or ("Success" if result.success else "Command failed"))
        log_warning = str(getattr(result, "log_warning", "") or "").strip()
        self.output_status.setToolTip(log_warning)
        state = "success" if result.success else ("cancelled" if result.error_type == "cancelled" else "error")
        self.output_status.setProperty("resultState", state)
        self.output_status.style().unpolish(self.output_status)
        self.output_status.style().polish(self.output_status)
        self.output_command.setText(result.command_text)
        self.output_command.setToolTip(result.command_text)
        self.output_exit.setText(f"Exit code: {result.exit_code if result.exit_code is not None else '—'}")
        self.output_duration.setText(f"Duration: {result.duration:.2f} s")
        self.stdout_output.setPlainText(result.stdout)
        self.stderr_output.setPlainText(result.stderr)
        self.output_content.setCurrentWidget(self.output_tabs)
        if result.stderr and not result.stdout:
            self.output_tabs.setCurrentWidget(self.stderr_output)
        else:
            self.output_tabs.setCurrentWidget(self.stdout_output)
        status_message = self.output_status.text()
        if log_warning:
            status_message = f"{status_message}. {log_warning}"
        self.status_message.emit(status_message, 7000 if log_warning else 5000)
        self.copy_button.setEnabled(True)
        self.clear_button.setEnabled(True)

    def _show_worker_error(self, message: str, token: OperationToken | None = None) -> None:
        if token is not None and not self._command_callback_is_current(token):
            self.status_message.emit(
                "A command for a device that is no longer active ended with an error; the current view was not changed.",
                7000,
            )
            return
        self.output_status.setText("Command worker failed")
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
        self.output_status.setText("No command has run")
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
                self.output_status.text(),
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
