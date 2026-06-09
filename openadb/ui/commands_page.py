from __future__ import annotations

import shlex
from pathlib import Path

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from openadb.core.adb import ADBClient
from openadb.core.command_runner import CommandRunner
from openadb.core.fastboot import FastbootClient
from openadb.core.safety import analyze_command_risk
from openadb.core.settings_manager import SettingsManager
from openadb.ui.widgets.command_button import CommandButton
from openadb.ui.workers import Worker, start_worker


class CommandsPage(QScrollArea):
    def __init__(
        self,
        adb: ADBClient,
        fastboot: FastbootClient,
        runner: CommandRunner,
        settings: SettingsManager,
        detect_tools_callback,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.adb = adb
        self.fastboot = fastboot
        self.runner = runner
        self.settings = settings
        self.detect_tools_callback = detect_tools_callback
        self.pool = QThreadPool.globalInstance()
        self.setWidgetResizable(True)
        root = QWidget()
        self.setWidget(root)
        layout = QVBoxLayout(root)

        layout.addWidget(self._group("ADB", self._adb_specs(), columns=3))
        layout.addWidget(self._group("Fastboot", self._fastboot_specs(), columns=3))
        layout.addWidget(self._group("Presets", self._preset_specs(), columns=3))

        advanced = QGroupBox("Advanced Commands")
        adv_layout = QVBoxLayout(advanced)
        row = QHBoxLayout()
        self.history = QComboBox()
        self.history.setEditable(True)
        self.history.addItems(self.settings.get("command_history", []))
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("Example: adb shell dumpsys battery")
        self.run_button = QPushButton("Run")
        row.addWidget(self.history, 1)
        row.addWidget(self.manual, 2)
        row.addWidget(self.run_button)
        adv_layout.addLayout(row)
        self.root_shell = QCheckBox("Run adb shell commands through root when available")
        self.root_shell.setChecked(bool(self.settings.get("root_mode_enabled", False)))
        self.root_shell.setToolTip("Only affects adb shell commands. Fastboot and non-shell adb commands stay unchanged.")
        adv_layout.addWidget(self.root_shell)
        self.history.currentTextChanged.connect(self.manual.setText)
        self.run_button.clicked.connect(self.run_manual)
        self.root_shell.toggled.connect(lambda checked: self.settings.set("root_mode_enabled", checked))
        layout.addWidget(advanced)
        layout.addStretch()

    def reload_from_settings(self) -> None:
        self.history.blockSignals(True)
        self.history.clear()
        self.history.addItems(self.settings.get("command_history", []))
        self.history.blockSignals(False)
        self.root_shell.blockSignals(True)
        self.root_shell.setChecked(bool(self.settings.get("root_mode_enabled", False)))
        self.root_shell.blockSignals(False)

    def _group(self, title: str, specs: list[dict], columns: int) -> QGroupBox:
        group = QGroupBox(title)
        grid = QGridLayout(group)
        for index, spec in enumerate(specs):
            risk = analyze_command_risk(spec.get("risk_command", spec.get("label", "")))
            button = CommandButton(spec["label"], spec, dangerous=risk.needs_confirmation or spec.get("danger", False))
            button.triggered.connect(self.run_spec)
            grid.addWidget(button, index // columns, index % columns)
        return group

    def run_spec(self, spec: dict) -> None:
        if spec.get("kind") == "callback":
            spec["callback"]()
            return
        risk = analyze_command_risk(spec.get("risk_command", spec.get("label", "")))
        if risk.needs_confirmation and not self._confirm_risk(spec["label"], risk.description):
            return
        kind = spec["kind"]
        args = list(spec.get("args", []))
        timeout = spec.get("timeout", 120)

        if spec.get("file"):
            path, _ = QFileDialog.getOpenFileName(self, spec["label"], "", spec.get("filter", "All files (*.*)"))
            if not path:
                return
            args.append(path)
        if spec.get("file_insert") is not None:
            path, _ = QFileDialog.getOpenFileName(self, spec["label"], "", spec.get("filter", "All files (*.*)"))
            if not path:
                return
            args.insert(int(spec["file_insert"]), path)
        if spec.get("folder"):
            folder = QFileDialog.getExistingDirectory(self, spec["label"], str(Path.home()))
            if not folder:
                return
            args.append(folder)
        if spec.get("input"):
            value, ok = QInputDialog.getText(self, spec["label"], spec.get("prompt", "Value:"))
            if not ok or not value.strip():
                return
            args.append(value.strip())
        if spec.get("path_pair") == "push":
            source = QFileDialog.getExistingDirectory(self, "Choose folder to push", str(Path.home()))
            if not source:
                path, _ = QFileDialog.getOpenFileName(self, "Choose file to push")
                source = path
            if not source:
                return
            dest, ok = QInputDialog.getText(self, "Android destination", "Destination path:", text="/sdcard/")
            if not ok or not dest.strip():
                return
            args = ["push", source, dest.strip()]
        if spec.get("path_pair") == "pull":
            src, ok = QInputDialog.getText(self, "Android source", "Source path:", text="/sdcard/")
            if not ok or not src.strip():
                return
            dest = QFileDialog.getExistingDirectory(self, "PC destination", str(Path.home()))
            if not dest:
                return
            args = ["pull", src.strip(), dest]

        if kind == "adb_root_check":
            self._run_worker(self._check_root_access, spec["label"])
        elif kind == "adb_root_shell_input":
            command, ok = QInputDialog.getText(self, spec["label"], "Root shell command:")
            if not ok or not command.strip():
                return
            self._run_worker(lambda: self.adb.run_root_shell(command.strip(), timeout=timeout), spec["label"])
        elif kind == "adb_shell_input":
            command, ok = QInputDialog.getText(self, spec["label"], "Shell command:")
            if not ok or not command.strip():
                return
            self._run_worker(lambda: self._run_shell_maybe_root(command.strip(), timeout), spec["label"])
        elif kind == "adb_shell":
            self._run_worker(lambda: self._run_shell_maybe_root(" ".join(args), timeout), spec["label"])
        elif kind == "adb":
            self._run_worker(lambda: self.adb.run_raw(args, timeout=timeout, use_serial=spec.get("serial", True)), spec["label"])
        elif kind == "fastboot":
            self._run_worker(lambda: self.fastboot.run_raw(args, timeout=timeout, use_serial=spec.get("serial", True)), spec["label"])

    def run_manual(self) -> None:
        text = self.manual.text().strip()
        if not text:
            QMessageBox.warning(self, "Manual command", "Command is empty.")
            return
        risk = analyze_command_risk(text)
        if risk.needs_confirmation and not self._confirm_risk("Manual command", risk.description):
            return
        try:
            parts = [part.strip('"') for part in shlex.split(text, posix=False)]
        except ValueError as exc:
            QMessageBox.warning(self, "Manual command", str(exc))
            return
        if not parts:
            return
        command = self._resolve_manual_command(parts)
        self.settings.append_command_history(text)
        self.history.clear()
        self.history.addItems(self.settings.get("command_history", []))
        self._run_worker(lambda: self.runner.run(command, timeout=300), "Manual command")

    def _resolve_manual_command(self, parts: list[str]) -> list[str]:
        first = parts[0].lower()
        if first in {"adb", "adb.exe"} and self.adb.platform_tools.adb_path:
            parts = self._rootify_adb_shell_parts(parts)
            resolved = [str(self.adb.platform_tools.adb_path), *parts[1:]]
            if self.adb.serial and "-s" not in resolved:
                resolved[1:1] = ["-s", self.adb.serial]
            return resolved
        if first in {"fastboot", "fastboot.exe"} and self.fastboot.platform_tools.fastboot_path:
            resolved = [str(self.fastboot.platform_tools.fastboot_path), *parts[1:]]
            if self.fastboot.serial and "-s" not in resolved:
                resolved[1:1] = ["-s", self.fastboot.serial]
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

    def _run_shell_maybe_root(self, command: str, timeout: int | float | None):
        if self.root_shell.isChecked():
            return self.adb.run_root_shell(command, timeout=timeout)
        return self.adb.run_shell(command, timeout=timeout)

    def _check_root_access(self):
        if self.adb.root_available():
            return self.adb.run_root_shell("id; getprop ro.debuggable; getprop ro.secure", timeout=20)
        return self.adb.run_shell("echo Root access was not granted by su", timeout=10)

    def _run_worker(self, fn, title: str) -> None:
        worker = Worker(fn)
        worker.signals.result.connect(lambda result: QMessageBox.information(self, title, self._result_message(result)))
        worker.signals.error.connect(lambda message, _trace: QMessageBox.critical(self, title, message))
        start_worker(self, self.pool, worker)

    def _result_message(self, result) -> str:
        parts = [result.status]
        if result.stdout:
            parts.append(result.stdout.strip())
        if result.stderr:
            parts.append("stderr:\n" + result.stderr.strip())
        return "\n\n".join(part for part in parts if part)

    def _confirm_risk(self, title: str, description: str) -> bool:
        text = description or "This command can change device state or data."
        answer = QMessageBox.warning(self, title, text + "\n\nContinue?", QMessageBox.Ok | QMessageBox.Cancel)
        return answer == QMessageBox.Ok

    def _adb_specs(self) -> list[dict]:
        return [
            {"label": "adb devices", "kind": "adb", "args": ["devices", "-l"], "serial": False},
            {"label": "adb kill-server", "kind": "adb", "args": ["kill-server"], "serial": False},
            {"label": "adb start-server", "kind": "adb", "args": ["start-server"], "serial": False},
            {"label": "adb reboot", "kind": "adb", "args": ["reboot"]},
            {"label": "adb reboot recovery", "kind": "adb", "args": ["reboot", "recovery"]},
            {"label": "adb reboot bootloader", "kind": "adb", "args": ["reboot", "bootloader"]},
            {"label": "adb reboot sideload", "kind": "adb", "args": ["reboot", "sideload"], "danger": True},
            {"label": "adb shell", "kind": "adb_shell_input", "args": [], "description": "Run a typed adb shell command."},
            {"label": "adb shell settings", "kind": "adb_shell", "args": ["settings", "list", "global"]},
            {"label": "pm list packages", "kind": "adb_shell", "args": ["pm", "list", "packages"]},
            {"label": "dumpsys battery", "kind": "adb_shell", "args": ["dumpsys", "battery"]},
            {"label": "wm size", "kind": "adb_shell", "args": ["wm", "size"]},
            {"label": "wm density", "kind": "adb_shell", "args": ["wm", "density"]},
            {"label": "getprop", "kind": "adb_shell", "args": ["getprop"], "timeout": 60},
            {"label": "logcat snapshot", "kind": "adb_shell", "args": ["logcat", "-d", "-t", "300"], "timeout": 60},
            {"label": "bugreport", "kind": "adb", "args": ["bugreport"], "folder": True, "timeout": 600},
            {"label": "adb install APK", "kind": "adb", "args": ["install"], "file": True, "filter": "APK files (*.apk)"},
            {"label": "adb uninstall package", "kind": "adb", "args": ["uninstall"], "input": True, "prompt": "Package name:"},
            {"label": "adb push", "kind": "adb", "args": [], "path_pair": "push", "timeout": None},
            {"label": "adb pull", "kind": "adb", "args": [], "path_pair": "pull", "timeout": None},
            {"label": "adb sideload ZIP", "kind": "adb", "args": ["sideload"], "file": True, "filter": "ZIP files (*.zip)", "danger": True},
            {"label": "check root access", "kind": "adb_root_check", "args": []},
            {"label": "root shell command", "kind": "adb_root_shell_input", "args": [], "danger": True},
        ]

    def _fastboot_specs(self) -> list[dict]:
        return [
            {"label": "fastboot devices", "kind": "fastboot", "args": ["devices"], "serial": False},
            {"label": "fastboot reboot", "kind": "fastboot", "args": ["reboot"]},
            {"label": "fastboot reboot bootloader", "kind": "fastboot", "args": ["reboot-bootloader"]},
            {"label": "fastboot getvar all", "kind": "fastboot", "args": ["getvar", "all"], "timeout": 60},
            {"label": "fastboot flashing unlock", "kind": "fastboot", "args": ["flashing", "unlock"], "danger": True},
            {"label": "fastboot flashing lock", "kind": "fastboot", "args": ["flashing", "lock"], "danger": True},
            {"label": "fastboot oem unlock", "kind": "fastboot", "args": ["oem", "unlock"], "danger": True},
            {"label": "fastboot oem lock", "kind": "fastboot", "args": ["oem", "lock"], "danger": True},
            {"label": "fastboot boot image", "kind": "fastboot", "args": ["boot"], "file": True, "filter": "Image files (*.img)"},
            {
                "label": "flash boot image",
                "kind": "fastboot",
                "args": ["flash", "boot"],
                "file": True,
                "filter": "Image files (*.img)",
                "danger": True,
            },
            {
                "label": "flash init_boot image",
                "kind": "fastboot",
                "args": ["flash", "init_boot"],
                "file": True,
                "filter": "Image files (*.img)",
                "danger": True,
            },
            {
                "label": "flash recovery image",
                "kind": "fastboot",
                "args": ["flash", "recovery"],
                "file": True,
                "filter": "Image files (*.img)",
                "danger": True,
            },
            {
                "label": "flash vbmeta image",
                "kind": "fastboot",
                "args": ["flash", "vbmeta"],
                "file": True,
                "filter": "Image files (*.img)",
                "danger": True,
            },
            {"label": "erase userdata", "kind": "fastboot", "args": ["erase", "userdata"], "danger": True},
            {"label": "erase cache", "kind": "fastboot", "args": ["erase", "cache"], "danger": True},
            {"label": "format userdata", "kind": "fastboot", "args": ["format", "userdata"], "danger": True},
        ]

    def _preset_specs(self) -> list[dict]:
        return [
            {"label": "Reboot to System", "kind": "adb", "args": ["reboot"]},
            {"label": "Reboot to Recovery", "kind": "adb", "args": ["reboot", "recovery"]},
            {"label": "Reboot to Bootloader", "kind": "adb", "args": ["reboot", "bootloader"]},
            {"label": "Check ADB Devices", "kind": "adb", "args": ["devices", "-l"], "serial": False},
            {"label": "Check Fastboot Devices", "kind": "fastboot", "args": ["devices"], "serial": False},
            {"label": "Install APK", "kind": "adb", "args": ["install"], "file": True, "filter": "APK files (*.apk)"},
            {"label": "Pull Folder", "kind": "adb", "args": [], "path_pair": "pull", "timeout": None},
            {"label": "Push Folder", "kind": "adb", "args": [], "path_pair": "push", "timeout": None},
            {"label": "Start Logcat", "kind": "adb_shell", "args": ["logcat", "-d", "-t", "300"], "timeout": 60},
            {"label": "Save Bugreport", "kind": "adb", "args": ["bugreport"], "folder": True, "timeout": 600},
            {"label": "Get Device Info", "kind": "adb_shell", "args": ["getprop"], "timeout": 60},
            {"label": "Get Battery Info", "kind": "adb_shell", "args": ["dumpsys", "battery"]},
            {"label": "Get Display Info", "kind": "adb_shell", "args": ["wm", "size", ";", "wm", "density"]},
            {"label": "List Installed Packages", "kind": "adb_shell", "args": ["pm", "list", "packages"]},
            {"label": "Detect Platform Tools", "kind": "callback", "callback": self.detect_tools_callback},
            {"label": "Show ADB Version", "kind": "adb", "args": ["version"], "serial": False},
            {"label": "Show Fastboot Version", "kind": "fastboot", "args": ["--version"], "serial": False},
        ]
