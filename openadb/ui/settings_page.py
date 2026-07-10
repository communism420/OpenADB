from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Signal
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
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox as QComboBox
from openadb.ui.widgets.no_wheel_widgets import NoWheelSpinBox as QSpinBox


class SettingsPage(QScrollArea):
    detect_tools_requested = Signal()
    choose_tools_requested = Signal()
    theme_changed = Signal(str)
    settings_changed = Signal()
    clear_icon_cache_requested = Signal()
    reset_settings_and_caches_requested = Signal()

    def __init__(self, settings: SettingsManager, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        root = QWidget()
        self.setWidget(root)
        layout = QVBoxLayout(root)
        title = QLabel("Settings")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        layout.addLayout(form)

        self.platform_path = QLineEdit()
        self.platform_path.setReadOnly(True)
        self.adb_path = QLineEdit()
        self.adb_path.setReadOnly(True)
        self.fastboot_path = QLineEdit()
        self.fastboot_path.setReadOnly(True)
        for path_edit in [self.platform_path, self.adb_path, self.fastboot_path]:
            path_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.platform_status = QLabel("Not found")
        self.adb_version = QLabel("Unknown")
        self.fastboot_version = QLabel("Unknown")

        detect_row = QGridLayout()
        self.detect_button = QPushButton("Detect Platform Tools")
        self.change_button = QPushButton("Change Platform Tools Path")
        self.check_button = QPushButton("Check platform-tools")
        detect_row.addWidget(self.detect_button, 0, 0)
        detect_row.addWidget(self.change_button, 0, 1)
        detect_row.addWidget(self.check_button, 1, 0, 1, 2)
        detect_row.setColumnStretch(0, 1)
        detect_row.setColumnStretch(1, 1)

        form.addRow("Active platform-tools path", self.platform_path)
        form.addRow("adb.exe path", self.adb_path)
        form.addRow("fastboot.exe path", self.fastboot_path)
        form.addRow("Platform Tools status", self.platform_status)
        form.addRow("ADB version", self.adb_version)
        form.addRow("Fastboot version", self.fastboot_version)
        form.addRow("", detect_row)

        self.backups_folder = self._folder_row("backups_folder", form, "Backups folder")
        self.temp_folder = self._folder_row("temp_folder", form, "Temp folder")
        self.logs_folder = self._folder_row("logs_folder", form, "Logs folder")

        self.theme = QComboBox()
        self.theme.addItems(["System", "Light", "Dark"])
        self.theme.setCurrentText(str(settings.get("theme", "System")))
        form.addRow("Theme", self.theme)

        self.auto_refresh = QCheckBox("Auto-refresh device status")
        self.auto_refresh.setChecked(bool(settings.get("auto_refresh_device", True)))
        form.addRow("", self.auto_refresh)

        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(3, 300)
        self.refresh_interval.setValue(int(settings.get("refresh_interval_seconds", 8)))
        self.refresh_interval.setSuffix(" s")
        form.addRow("Refresh interval", self.refresh_interval)

        self.show_system_apps = QCheckBox("Show system apps")
        self.show_system_apps.setChecked(bool(settings.get("show_system_apps", True)))
        form.addRow("", self.show_system_apps)

        self.show_warnings = QCheckBox("Show warnings")
        self.show_warnings.setChecked(bool(settings.get("show_warnings", True)))
        form.addRow("", self.show_warnings)

        self.require_backup = QCheckBox("Require backup before uninstall")
        self.require_backup.setChecked(bool(settings.get("require_backup_before_uninstall", True)))
        form.addRow("", self.require_backup)

        self.root_mode = QCheckBox("Enable root features when su/root is available")
        self.root_mode.setChecked(bool(settings.get("root_mode_enabled", False)))
        self.root_mode.setToolTip(
            "Used for protected file browsing/transfers, APK backup reads, and optional root shell commands. "
            "OpenADB still asks before dangerous operations."
        )
        form.addRow("Root", self.root_mode)

        maintenance = QGridLayout()
        self.clear_icons = QPushButton("Clear icon cache")
        self.clear_temp = QPushButton("Clear temporary APK files")
        self.reset_all = QPushButton("Reset all settings and caches")
        self.reset_all.setProperty("danger", True)
        maintenance.addWidget(self.clear_icons, 0, 0)
        maintenance.addWidget(self.clear_temp, 0, 1)
        maintenance.addWidget(self.reset_all, 1, 0, 1, 2)
        maintenance.setColumnStretch(0, 1)
        maintenance.setColumnStretch(1, 1)
        form.addRow("Maintenance", maintenance)

        self.detect_button.clicked.connect(self.detect_tools_requested.emit)
        self.change_button.clicked.connect(self.choose_tools_requested.emit)
        self.check_button.clicked.connect(self.detect_tools_requested.emit)
        self.theme.currentTextChanged.connect(self._theme_changed)
        self.auto_refresh.toggled.connect(self._save)
        self.refresh_interval.valueChanged.connect(self._save)
        self.show_system_apps.toggled.connect(self._save)
        self.show_warnings.toggled.connect(self._save)
        self.require_backup.toggled.connect(self._save)
        self.root_mode.toggled.connect(self._save)
        self.clear_icons.clicked.connect(self.clear_icon_cache_requested.emit)
        self.clear_temp.clicked.connect(self._clear_temp)
        self.reset_all.clicked.connect(self.reset_settings_and_caches_requested.emit)
        layout.addStretch()

    def _folder_row(self, key: str, form: QFormLayout, label: str) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit(str(self.settings.get(key, "")))
        edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        button = QPushButton("Browse")
        row.addWidget(edit, 1)
        row.addWidget(button)
        form.addRow(label, row)

        def browse() -> None:
            folder = QFileDialog.getExistingDirectory(self, label, edit.text())
            if folder:
                edit.setText(folder)
                self.settings.set(key, folder)
                self.settings_changed.emit()

        button.clicked.connect(browse)
        edit.editingFinished.connect(lambda: (self.settings.set(key, edit.text()), self.settings_changed.emit()))
        return edit

    def update_tools(self, tools: PlatformToolsInfo) -> None:
        self.platform_path.setText(tools.folder_text)
        self.adb_path.setText(str(tools.adb_path) if tools.adb_path else "")
        self.fastboot_path.setText(str(tools.fastboot_path) if tools.fastboot_path else "")
        self.platform_status.setText(tools.status)
        self.adb_version.setText(tools.adb_version)
        self.fastboot_version.setText(tools.fastboot_version)

    def reload_from_settings(self) -> None:
        self.backups_folder.setText(str(self.settings.get("backups_folder", "")))
        self.temp_folder.setText(str(self.settings.get("temp_folder", "")))
        self.logs_folder.setText(str(self.settings.get("logs_folder", "")))

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

    def _theme_changed(self, theme: str) -> None:
        self.settings.set("theme", theme)
        self.theme_changed.emit(theme)
        self.settings_changed.emit()

    def _save(self) -> None:
        self.settings.set("auto_refresh_device", self.auto_refresh.isChecked(), save=False)
        self.settings.set("refresh_interval_seconds", self.refresh_interval.value(), save=False)
        self.settings.set("show_system_apps", self.show_system_apps.isChecked(), save=False)
        self.settings.set("show_warnings", self.show_warnings.isChecked(), save=False)
        self.settings.set("require_backup_before_uninstall", self.require_backup.isChecked(), save=False)
        self.settings.set("root_mode_enabled", self.root_mode.isChecked(), save=False)
        self.settings.save()
        self.settings_changed.emit()

    def _clear_temp(self) -> None:
        temp = Path(str(self.settings.get("temp_folder", "")))
        if temp.exists():
            for item in temp.iterdir():
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                except OSError:
                    continue
