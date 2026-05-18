from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from openadb.core.settings_manager import SettingsManager
from openadb.models.platform_tools_info import PlatformToolsInfo


class SettingsPage(QWidget):
    detect_tools_requested = Signal()
    choose_tools_requested = Signal()
    theme_changed = Signal(str)
    settings_changed = Signal()
    clear_icon_cache_requested = Signal()

    def __init__(self, settings: SettingsManager, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        layout = QVBoxLayout(self)
        title = QLabel("Settings")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        layout.addLayout(form)

        self.platform_path = QLineEdit()
        self.platform_path.setReadOnly(True)
        self.adb_path = QLineEdit()
        self.adb_path.setReadOnly(True)
        self.fastboot_path = QLineEdit()
        self.fastboot_path.setReadOnly(True)
        self.platform_status = QLabel("Not found")
        self.adb_version = QLabel("Unknown")
        self.fastboot_version = QLabel("Unknown")

        detect_row = QHBoxLayout()
        self.detect_button = QPushButton("Detect Platform Tools")
        self.change_button = QPushButton("Change Platform Tools Path")
        self.check_button = QPushButton("Check platform-tools")
        detect_row.addWidget(self.detect_button)
        detect_row.addWidget(self.change_button)
        detect_row.addWidget(self.check_button)
        detect_row.addStretch()

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

        maintenance = QHBoxLayout()
        self.clear_icons = QPushButton("Clear icon cache")
        self.clear_temp = QPushButton("Clear temporary APK files")
        maintenance.addWidget(self.clear_icons)
        maintenance.addWidget(self.clear_temp)
        maintenance.addStretch()
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
        self.clear_icons.clicked.connect(self.clear_icon_cache_requested.emit)
        self.clear_temp.clicked.connect(self._clear_temp)
        layout.addStretch()

    def _folder_row(self, key: str, form: QFormLayout, label: str) -> QLineEdit:
        row = QHBoxLayout()
        edit = QLineEdit(str(self.settings.get(key, "")))
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
