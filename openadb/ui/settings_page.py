from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from openadb.core.settings_manager import SettingsManager
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.design_system import configure_page_layout, set_button_role
from openadb.ui.widgets.empty_state import EmptyState
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox as QComboBox
from openadb.ui.widgets.no_wheel_widgets import NoWheelSpinBox as QSpinBox


class SettingsPage(QScrollArea):
    detect_tools_requested = Signal()
    choose_tools_requested = Signal()
    verify_tools_requested = Signal()
    theme_changed = Signal(str)
    settings_changed = Signal()
    clear_icon_cache_requested = Signal()
    clear_temp_requested = Signal()
    reset_ui_settings_requested = Signal()
    reset_settings_and_caches_requested = Signal()

    def __init__(self, settings: SettingsManager, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        root = QWidget()
        root.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setWidget(root)
        layout = QVBoxLayout(root)
        configure_page_layout(layout)

        title = QLabel("Settings")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = self._description(
            "Configure Android tools, monitoring, storage, safety preferences, and maintenance."
        )
        subtitle.setObjectName("pageSubtitle")
        layout.addWidget(subtitle)

        platform_card, platform_form = self._section(
            "Platform Tools",
            "Find, select, and verify the adb and fastboot installation used by OpenADB.",
        )
        self.platform_tools_empty_state = EmptyState(
            "Platform Tools not found",
            "OpenADB needs adb and fastboot before it can communicate with Android devices.",
            "Find Platform Tools",
        )
        self.platform_tools_empty_state.setMaximumHeight(180)
        platform_form.addRow(self.platform_tools_empty_state)
        self.platform_path = self._readonly_path()
        self.adb_path = self._readonly_path()
        self.fastboot_path = self._readonly_path()
        self.platform_status = QLabel("Not found")
        self.platform_status.setObjectName("settingsStatusValue")
        self.platform_source = QLabel("Unknown")
        self.adb_version = QLabel("Unknown")
        self.fastboot_version = QLabel("Unknown")
        self.last_verification = self._description("Not verified in this session.")
        self.last_verification.setObjectName("settingsVerificationResult")

        platform_form.addRow("Active folder", self.platform_path)
        platform_form.addRow("adb path", self.adb_path)
        platform_form.addRow("fastboot path", self.fastboot_path)
        platform_form.addRow("Detection source", self.platform_source)
        platform_form.addRow("Status", self.platform_status)
        platform_form.addRow("ADB version", self.adb_version)
        platform_form.addRow("Fastboot version", self.fastboot_version)
        platform_form.addRow("Last check", self.last_verification)

        tools_actions = QGridLayout()
        tools_actions.setContentsMargins(0, 4, 0, 0)
        self.detect_button = QPushButton("Find Platform Tools")
        set_button_role(self.detect_button, "primary")
        self.detect_button.setToolTip("Search saved, bundled, PATH, SDK, registry, and common installations.")
        self.change_button = QPushButton("Choose folder")
        self.change_button.setToolTip("Select a platform-tools folder manually.")
        self.check_button = QPushButton("Verify selected installation")
        self.check_button.setToolTip("Check only the active folder shown above.")
        self.check_button.setEnabled(False)
        tools_actions.addWidget(self.detect_button, 0, 0)
        tools_actions.addWidget(self.change_button, 0, 1)
        tools_actions.addWidget(self.check_button, 1, 0, 1, 2)
        tools_actions.setColumnStretch(0, 1)
        tools_actions.setColumnStretch(1, 1)
        platform_form.addRow(tools_actions)
        layout.addWidget(platform_card)

        appearance_card, appearance_form = self._section(
            "Appearance",
            "System follows the Windows app theme; Light and Dark select an OpenADB theme explicitly.",
        )
        self.theme = QComboBox()
        self.theme.addItems(["System", "Light", "Dark"])
        self.theme.setCurrentText(str(settings.get("theme", "System")))
        appearance_form.addRow("Theme", self.theme)
        layout.addWidget(appearance_card)

        monitoring_card, monitoring_form = self._section(
            "Device monitoring",
            "Periodically refresh the connected-device state without reloading page data.",
        )
        self.auto_refresh = QCheckBox("Automatically refresh device status")
        self.auto_refresh.setChecked(bool(settings.get("auto_refresh_device", True)))
        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(3, 300)
        self.refresh_interval.setValue(int(settings.get("refresh_interval_seconds", 8)))
        self.refresh_interval.setSuffix(" s")
        monitoring_form.addRow(self.auto_refresh)
        monitoring_form.addRow("Refresh interval", self.refresh_interval)
        layout.addWidget(monitoring_card)

        apps_card, apps_form = self._section(
            "Applications and backups",
            "Control application visibility, safety prompts, and backup-before-uninstall behaviour.",
        )
        self.show_system_apps = QCheckBox("Show system applications")
        self.show_system_apps.setChecked(bool(settings.get("show_system_apps", True)))
        self.show_warnings = QCheckBox("Show safety warnings")
        self.show_warnings.setChecked(bool(settings.get("show_warnings", True)))
        self.require_backup = QCheckBox("Require backup before uninstall")
        self.require_backup.setChecked(bool(settings.get("require_backup_before_uninstall", True)))
        apps_form.addRow(self.show_system_apps)
        apps_form.addRow(self.show_warnings)
        apps_form.addRow(self.require_backup)
        layout.addWidget(apps_card)

        root_card, root_form = self._section(
            "Root and advanced features",
            (
                "Root mode allows protected file browsing and transfers, APK backup reads, and optional root "
                "shell commands when su/root is already available on the device. OpenADB does not obtain, "
                "install, or unlock root access."
            ),
        )
        self.root_mode = QCheckBox("Enable root-assisted features when root is available")
        self.root_mode.setChecked(bool(settings.get("root_mode_enabled", False)))
        self.root_mode.setToolTip(
            "Uses existing su/root access for protected browsing, transfers, APK reads, and optional shell commands."
        )
        root_form.addRow(self.root_mode)
        layout.addWidget(root_card)

        storage_card, storage_form = self._section(
            "Storage paths",
            "Folders may be changed independently. Long paths remain available in each field tooltip.",
        )
        self.backups_folder = self._folder_row("backups_folder", storage_form, "APK backups")
        self.temp_folder = self._folder_row("temp_folder", storage_form, "Temporary files")
        self.logs_folder = self._folder_row("logs_folder", storage_form, "Logs")
        layout.addWidget(storage_card)

        maintenance_card, maintenance_form = self._section(
            "Maintenance",
            "Cache cleanup never removes APK backups. Full reset affects global settings and every device profile.",
        )
        self.clear_icons = QPushButton("Clear icon cache")
        self.clear_temp = QPushButton("Clear temporary files")
        self.reset_ui = QPushButton("Reset UI settings")
        self.reset_all = QPushButton("Reset all settings and caches")
        self.reset_all.setProperty("danger", True)
        set_button_role(self.reset_all, "danger")
        maintenance_form.addRow("Downloaded app artwork", self.clear_icons)
        maintenance_form.addRow("Active profile temporary folder", self.clear_temp)
        maintenance_form.addRow("Layout, theme, filters, and view state", self.reset_ui)
        maintenance_form.addRow("All profiles, preferences, and caches", self.reset_all)
        layout.addWidget(maintenance_card)
        layout.addStretch()

        self.detect_button.clicked.connect(self.detect_tools_requested.emit)
        self.platform_tools_empty_state.action_requested.connect(self.detect_tools_requested.emit)
        self.change_button.clicked.connect(self.choose_tools_requested.emit)
        self.check_button.clicked.connect(self.verify_tools_requested.emit)
        self.theme.currentTextChanged.connect(self._theme_changed)
        self.auto_refresh.toggled.connect(self._auto_refresh_toggled)
        self.refresh_interval.valueChanged.connect(self._save)
        self.show_system_apps.toggled.connect(self._save)
        self.show_warnings.toggled.connect(self._save)
        self.require_backup.toggled.connect(self._save)
        self.root_mode.toggled.connect(self._save)
        self.clear_icons.clicked.connect(self.clear_icon_cache_requested.emit)
        self.clear_temp.clicked.connect(self.clear_temp_requested.emit)
        self.reset_ui.clicked.connect(self.reset_ui_settings_requested.emit)
        self.reset_all.clicked.connect(self.reset_settings_and_caches_requested.emit)
        self._update_refresh_interval_state()

    def _section(self, title: str, description: str) -> tuple[QFrame, QFormLayout]:
        card = QFrame()
        card.setObjectName("settingsSection")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 14)
        card_layout.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("settingsSectionTitle")
        card_layout.addWidget(heading)
        card_layout.addWidget(self._description(description))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(9)
        card_layout.addLayout(form)
        return card, form

    @staticmethod
    def _description(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionDescription")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _readonly_path() -> QLineEdit:
        edit = QLineEdit()
        edit.setReadOnly(True)
        edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        edit.setPlaceholderText("Not available")
        return edit

    def _folder_row(self, key: str, form: QFormLayout, label: str) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit(str(self.settings.get(key, "")))
        edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        edit.setToolTip(edit.text())
        button = QPushButton("Browse")
        row.addWidget(edit, 1)
        row.addWidget(button)
        form.addRow(label, row)

        def browse() -> None:
            folder = QFileDialog.getExistingDirectory(self, label, edit.text())
            if folder:
                edit.setText(folder)
                edit.setToolTip(folder)
                self.settings.set(key, folder)
                self.settings_changed.emit()

        def save_path() -> None:
            edit.setToolTip(edit.text())
            self.settings.set(key, edit.text())
            self.settings_changed.emit()

        button.clicked.connect(browse)
        edit.editingFinished.connect(save_path)
        return edit

    def update_tools(self, tools: PlatformToolsInfo) -> None:
        self._set_path(self.platform_path, tools.folder_text)
        self._set_path(self.adb_path, str(tools.adb_path) if tools.adb_path else "")
        self._set_path(self.fastboot_path, str(tools.fastboot_path) if tools.fastboot_path else "")
        self.platform_status.setText(tools.status)
        self.platform_status.setToolTip(tools.status)
        self.platform_source.setText(tools.source or "Unknown")
        self.platform_source.setToolTip(tools.source or "Unknown")
        self.adb_version.setText(tools.adb_version)
        self.adb_version.setToolTip(tools.adb_version)
        self.fastboot_version.setText(tools.fastboot_version)
        self.fastboot_version.setToolTip(tools.fastboot_version)
        self.check_button.setEnabled(bool(tools.folder))
        self.platform_tools_empty_state.setVisible(not tools.has_adb and not tools.has_fastboot)

    @staticmethod
    def _set_path(edit: QLineEdit, value: str) -> None:
        edit.setText(value)
        edit.setToolTip(value)
        edit.setCursorPosition(0)

    def set_verification_result(self, message: str) -> None:
        self.last_verification.setText(message)
        self.last_verification.setToolTip(message)

    def reload_from_settings(self) -> None:
        for edit, key in [
            (self.backups_folder, "backups_folder"),
            (self.temp_folder, "temp_folder"),
            (self.logs_folder, "logs_folder"),
        ]:
            value = str(self.settings.get(key, ""))
            edit.setText(value)
            edit.setToolTip(value)

        self.theme.blockSignals(True)
        self.theme.setCurrentText(str(self.settings.get("theme", "System")))
        self.theme.blockSignals(False)

        for widget, value in [
            (self.auto_refresh, bool(self.settings.get("auto_refresh_device", True))),
            (self.show_system_apps, bool(self.settings.get("show_system_apps", True))),
            (self.show_warnings, bool(self.settings.get("show_warnings", True))),
            (self.require_backup, bool(self.settings.get("require_backup_before_uninstall", True))),
            (self.root_mode, bool(self.settings.get("root_mode_enabled", False))),
        ]:
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)

        self.refresh_interval.blockSignals(True)
        self.refresh_interval.setValue(int(self.settings.get("refresh_interval_seconds", 8)))
        self.refresh_interval.blockSignals(False)
        self._update_refresh_interval_state()

    def _theme_changed(self, theme: str) -> None:
        self.settings.set("theme", theme)
        self.theme_changed.emit(theme)
        self.settings_changed.emit()

    def _auto_refresh_toggled(self, _checked: bool) -> None:
        self._update_refresh_interval_state()
        self._save()

    def _update_refresh_interval_state(self) -> None:
        enabled = self.auto_refresh.isChecked()
        self.refresh_interval.setEnabled(enabled)
        if enabled:
            self.refresh_interval.setToolTip("Time between automatic device status refreshes.")
        else:
            self.refresh_interval.setToolTip("Enable automatic refresh to change this interval.")

    def _save(self) -> None:
        self.settings.set("auto_refresh_device", self.auto_refresh.isChecked(), save=False)
        self.settings.set("refresh_interval_seconds", self.refresh_interval.value(), save=False)
        self.settings.set("show_system_apps", self.show_system_apps.isChecked(), save=False)
        self.settings.set("show_warnings", self.show_warnings.isChecked(), save=False)
        self.settings.set("require_backup_before_uninstall", self.require_backup.isChecked(), save=False)
        self.settings.set("root_mode_enabled", self.root_mode.isChecked(), save=False)
        self.settings.save()
        self.settings_changed.emit()
