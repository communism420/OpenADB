from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from openadb.core.adb import ADBClient
from openadb.core.command_catalog import COMMAND_CATEGORIES, command_specs
from openadb.core.fastboot import FastbootClient
from openadb.core.safety import analyze_command_risk
from openadb.core.settings_manager import SettingsManager
from openadb.models.command_result import CommandResult, format_command
from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.commands_page import CommandsPage
from openadb.ui.style import apply_theme


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class FakeRunner:
    def __init__(self) -> None:
        self.run_streaming = MagicMock()

    @staticmethod
    def command_text(command) -> str:
        return format_command(list(command))


class FakeAdb:
    def __init__(self, platform_tools) -> None:
        self.platform_tools = platform_tools
        self.serial = "device-1"
        self.run_raw = MagicMock()
        self.run_shell = MagicMock()
        self.run_root_shell = MagicMock()

    @staticmethod
    def root_shell_script(command: str) -> str:
        return f"su -c '{command}'"


class FakeFastboot:
    def __init__(self, platform_tools) -> None:
        self.platform_tools = platform_tools
        self.serial = ""
        self.run_raw = MagicMock()


def make_result(
    command: list[str] | None = None,
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = 0,
    status: str = "Success",
    error_type: str = "",
) -> CommandResult:
    started = datetime.now()
    return CommandResult(
        command=command or ["adb", "devices", "-l"],
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration=1.25,
        started_at=started,
        finished_at=started + timedelta(seconds=1.25),
        success=exit_code == 0,
        status=status,
        error_type=error_type,
    )


class CommandCatalogTests(unittest.TestCase):
    def test_metadata_is_complete_unique_and_covers_every_category(self) -> None:
        specs = command_specs()
        self.assertEqual(len(specs), 43)
        self.assertEqual(len({spec.key for spec in specs}), len(specs))
        self.assertEqual({spec.category for spec in specs}, set(COMMAND_CATEGORIES))
        for spec in specs:
            self.assertTrue(spec.label)
            self.assertTrue(spec.description)
            self.assertTrue(spec.actual_command)
            self.assertIn(spec.required_tool, {"None", "ADB", "fastboot"})
            self.assertIn(spec.risk_level, {"Safe", "Changes device state", "May erase data", "Critical"})
            self.assertEqual(spec.requires_file, bool(spec.file_requirement))

    def test_catalog_preserves_existing_command_capabilities(self) -> None:
        commands = "\n".join(spec.actual_command for spec in command_specs())
        required = [
            "adb devices -l",
            "adb kill-server",
            "adb start-server",
            "adb reboot sideload",
            "adb shell settings list global",
            "adb shell pm list packages",
            "adb bugreport",
            "adb install",
            "adb uninstall",
            "adb push",
            "adb pull",
            "adb sideload",
            "adb shell su -c",
            "fastboot devices",
            "fastboot getvar all",
            "fastboot flashing unlock",
            "fastboot oem lock",
            "fastboot boot",
            "fastboot flash boot",
            "fastboot flash init_boot",
            "fastboot flash recovery",
            "fastboot flash vbmeta",
            "fastboot erase userdata",
            "fastboot erase cache",
            "fastboot format userdata",
        ]
        for command in required:
            self.assertIn(command, commands)

    def test_risk_analyzer_uses_required_levels_and_specific_consequences(self) -> None:
        matrix = {
            "adb devices -l": ("Safe", False, ""),
            "adb reboot recovery": ("Changes device state", True, ""),
            "adb uninstall com.example": ("Changes device state", True, ""),
            "adb sideload update.zip": ("Changes device state", True, ""),
            "fastboot erase userdata": ("May erase data", True, "ERASE"),
            "fastboot format userdata": ("May erase data", True, "ERASE"),
            "fastboot flashing unlock": ("Critical", True, "CONFIRM"),
            "fastboot flashing lock": ("Critical", True, "CONFIRM"),
            "fastboot flash boot boot.img": ("Critical", True, "CONFIRM"),
            "fastboot -s SERIAL flash boot boot.img": ("Critical", True, "CONFIRM"),
            "fastboot --slot all --disable-verification flash vbmeta vbmeta.img": (
                "Critical", True, "CONFIRM"
            ),
            "fastboot --set-active=a": ("Changes device state", True, ""),
            '"C:/Android SDK/platform-tools/fastboot.exe" -s SERIAL erase userdata': (
                "May erase data", True, "ERASE"
            ),
            "adb -s SERIAL uninstall com.example": ("Changes device state", True, ""),
            "adb shell su -c id": ("Critical", True, "CONFIRM"),
        }
        for command, (level, confirmation, typed) in matrix.items():
            risk = analyze_command_risk(command)
            self.assertEqual(risk.level, level, command)
            self.assertEqual(risk.needs_confirmation, confirmation, command)
            self.assertEqual(risk.typed_confirmation, typed, command)
            if confirmation:
                self.assertTrue(risk.description, command)


class CommandsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.settings = IsolatedSettings(self.config_dir)
        self.tools_dir = self.config_dir / "platform-tools"
        self.tools_dir.mkdir()
        self.adb_exe = self.tools_dir / "adb.exe"
        self.fastboot_exe = self.tools_dir / "fastboot.exe"
        self.adb_exe.touch()
        self.fastboot_exe.touch()
        info = PlatformToolsInfo(
            folder=self.tools_dir,
            adb_path=self.adb_exe,
            fastboot_path=self.fastboot_exe,
            source="Test",
        )
        self.platform_tools = SimpleNamespace(active=info, adb_path=self.adb_exe, fastboot_path=self.fastboot_exe)
        self.adb = FakeAdb(self.platform_tools)
        self.fastboot = FakeFastboot(self.platform_tools)
        self.runner = FakeRunner()
        self.device_manager = SimpleNamespace(
            active=DeviceInfo(serial="device-1", model="Test phone", mode="ADB", state="device")
        )
        self.detect_tools = MagicMock()
        self.page = CommandsPage(
            self.adb,
            self.fastboot,
            self.runner,
            self.settings,
            self.device_manager,
            self.detect_tools,
        )
        self.page.resize(900, 680)
        self.page.show()
        self.app.processEvents()
        self.assertEqual(self.settings.get("commands_view_mode"), "Basic")
        self.assertEqual(self.page.page_tabs.tabText(1), "Custom command")

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def _visible_keys(self) -> list[str]:
        keys: list[str] = []
        for group_index in range(self.page.tree.topLevelItemCount()):
            group = self.page.tree.topLevelItem(group_index)
            for child_index in range(group.childCount()):
                key = group.child(child_index).data(0, Qt.UserRole)
                if key:
                    keys.append(str(key))
        return keys

    def test_search_categories_and_basic_advanced_modes_are_local(self) -> None:
        basic_keys = self._visible_keys()
        self.assertLess(len(basic_keys), len(self.page.specs))
        self.assertNotIn("fastboot_flashing_unlock", basic_keys)

        with patch("openadb.ui.commands_page.start_worker") as start:
            self.page.view_mode.setCurrentText("Advanced")
            self.assertEqual(len(self._visible_keys()), 43)
            self.page.search.setText("dumpsys battery")
            self.assertEqual(self._visible_keys(), ["battery"])
            self.page.search.setText("temperature")
            self.assertEqual(self._visible_keys(), ["battery"])
            self.page.search.setText("applications")
            self.assertEqual(set(self._visible_keys()), {"list_packages", "install_apk", "uninstall_package"})
            self.page.search.clear()
            self.page.category_filter.setCurrentText("Fastboot")
            self.assertTrue(self._visible_keys())
            self.assertTrue(all(self.page.spec_by_key[key].category == "Fastboot" for key in self._visible_keys()))
            self.assertEqual(start.call_count, 0)
        self.assertEqual(self.settings.get("commands_view_mode"), "Advanced")

    def test_availability_explains_tools_modes_root_and_busy_state(self) -> None:
        adb_devices = self.page.spec_by_key["adb_devices"]
        battery = self.page.spec_by_key["battery"]
        fastboot_getvar = self.page.spec_by_key["fastboot_getvar"]
        root_shell = self.page.spec_by_key["root_shell"]

        self.platform_tools.active = PlatformToolsInfo()
        available, reason = self.page._availability(adb_devices)
        self.assertFalse(available)
        self.assertIn("ADB is unavailable", reason)

        self.platform_tools.active = PlatformToolsInfo(
            folder=self.tools_dir, adb_path=self.adb_exe, fastboot_path=self.fastboot_exe
        )
        self.device_manager.active = DeviceInfo(mode="No device")
        self.assertTrue(self.page._availability(adb_devices)[0])
        self.assertFalse(self.page._availability(battery)[0])
        self.assertIn("Connect a device", self.page._availability(battery)[1])

        self.device_manager.active = DeviceInfo(serial="adb-1", mode="ADB", state="device")
        self.assertTrue(self.page._availability(battery)[0])
        self.assertFalse(self.page._availability(fastboot_getvar)[0])
        self.assertIn("Current mode is ADB", self.page._availability(fastboot_getvar)[1])
        self.assertFalse(self.page._availability(root_shell)[0])
        self.assertIn("root-assisted", self.page._availability(root_shell)[1])
        self.settings.set("root_mode_enabled", True)
        self.assertFalse(self.page._availability(root_shell)[0])
        self.assertIn("Check root access", self.page._availability(root_shell)[1])
        self.page._root_access_state = "available"
        self.page._root_access_serial = "adb-1"
        self.assertTrue(self.page._availability(root_shell)[0])

        self.device_manager.active = DeviceInfo(serial="fb-1", mode="Fastboot", state="fastboot")
        self.assertTrue(self.page._availability(fastboot_getvar)[0])
        self.assertFalse(self.page._availability(battery)[0])
        self.page._command_running = True
        self.assertIn("already running", self.page._availability(adb_devices)[1])

    def test_every_risky_builtin_is_blocked_when_confirmation_is_cancelled(self) -> None:
        risky = [spec for spec in self.page.specs if spec.risk.needs_confirmation]
        self.assertTrue(risky)
        with (
            patch.object(self.page, "_availability", return_value=(True, "Ready")),
            patch.object(self.page, "_confirm_risk", return_value=False) as confirm,
            patch.object(self.page, "_start_command") as start,
            patch("openadb.ui.commands_page.QFileDialog") as file_dialog,
            patch("openadb.ui.commands_page.QInputDialog") as input_dialog,
        ):
            input_dialog.getText.return_value = ("reboot", True)
            for spec in risky:
                self.page.run_spec(spec)
        self.assertEqual(confirm.call_count, len(risky))
        start.assert_not_called()
        file_dialog.getOpenFileName.assert_not_called()
        self.assertEqual(input_dialog.getText.call_count, 2)

    def test_typed_shell_command_is_reanalyzed_after_input(self) -> None:
        shell = self.page.spec_by_key["shell"]
        with (
            patch.object(self.page, "_availability", return_value=(True, "Ready")),
            patch("openadb.ui.commands_page.QInputDialog.getText", return_value=("su -c 'rm -rf /data'", True)),
            patch.object(self.page, "_confirm_risk", return_value=False) as confirm,
            patch.object(self.page, "_start_command") as start,
        ):
            self.page.run_spec(shell)
        self.assertEqual(confirm.call_args.args[2].level, "Critical")
        self.assertIn("su -c", confirm.call_args.args[1])
        start.assert_not_called()

    def test_confirmation_uses_typed_tokens_for_destructive_and_critical_risks(self) -> None:
        critical = analyze_command_risk("fastboot flash boot boot.img")
        erase = analyze_command_risk("fastboot erase userdata")
        state_change = analyze_command_risk("adb reboot recovery")
        with patch("openadb.ui.commands_page.QInputDialog.getText", return_value=("confirm", True)):
            self.assertFalse(self.page._confirm_risk("Flash", "fastboot flash boot boot.img", critical))
        with patch("openadb.ui.commands_page.QInputDialog.getText", return_value=("CONFIRM", True)):
            self.assertTrue(self.page._confirm_risk("Flash", "fastboot flash boot boot.img", critical))
        with patch("openadb.ui.commands_page.QInputDialog.getText", return_value=("ERASE", True)):
            self.assertTrue(self.page._confirm_risk("Erase", "fastboot erase userdata", erase))
        with patch("openadb.ui.commands_page.QMessageBox.warning", return_value=QMessageBox.Cancel):
            self.assertFalse(self.page._confirm_risk("Reboot", "adb reboot recovery", state_change))

    def test_inline_result_handles_stdout_stderr_long_output_copy_clear_and_logs(self) -> None:
        long_stdout = "\n".join(f"line {index}" for index in range(1500))
        result = make_result(
            [str(self.adb_exe), "-s", "device-1", "shell", "getprop"],
            stdout=long_stdout,
            stderr="diagnostic warning",
        )
        self.page._show_result(result)
        self.assertEqual(self.page.output_status.text(), "Success")
        self.assertIn("adb.exe", self.page.output_command.text())
        self.assertEqual(self.page.output_exit.text(), "Exit code: 0")
        self.assertEqual(self.page.output_duration.text(), "Duration: 1.25 s")
        self.assertEqual(self.page.stdout_output.toPlainText(), long_stdout)
        self.assertEqual(self.page.stderr_output.toPlainText(), "diagnostic warning")

        self.page.copy_result()
        copied = QApplication.clipboard().text()
        self.assertIn("diagnostic warning", copied)
        self.assertIn("line 1499", copied)
        failed = make_result(
            ["fastboot", "getvar", "all"],
            stderr="FAILED (remote: unknown variable)",
            exit_code=1,
            status="Command failed with exit code 1",
            error_type="command_failed",
        )
        self.page._show_result(failed)
        self.assertIs(self.page.output_tabs.currentWidget(), self.page.stderr_output)
        self.assertEqual(self.page.output_exit.text(), "Exit code: 1")
        opened = []
        self.page.open_logs_requested.connect(lambda: opened.append(True))
        self.page.open_logs_button.click()
        self.assertEqual(opened, [True])
        self.page.clear_result()
        self.assertEqual(self.page.stdout_output.toPlainText(), "")
        self.assertEqual(self.page.output_status.text(), "No command has run")

    def test_root_check_result_controls_root_command_availability(self) -> None:
        self.settings.set("root_mode_enabled", True)
        root_shell = self.page.spec_by_key["root_shell"]
        self.assertFalse(self.page._availability(root_shell)[0])
        self.page._running_spec_key = "root_check"
        self.page._show_result(make_result(stdout="0\nuid=0(root) gid=0(root)"))
        self.assertEqual(self.page._root_access_state, "available")
        self.assertEqual(self.page._root_access_serial, "device-1")
        self.assertTrue(self.page._availability(root_shell)[0])

    def test_single_worker_guard_and_cancel_event(self) -> None:
        captured = []

        def fake_start(_owner, _pool, worker) -> None:
            captured.append(worker)

        def run(cancel_event: threading.Event) -> CommandResult:
            self.assertTrue(cancel_event.is_set())
            return make_result(
                stdout="partial output",
                exit_code=-9,
                status="Cancelled by user",
                error_type="cancelled",
            )

        with patch("openadb.ui.commands_page.start_worker", side_effect=fake_start):
            self.page._start_command(run, "adb shell long-command")
            self.page._start_command(run, "adb shell duplicate")
        self.assertEqual(len(captured), 1)
        self.assertTrue(self.page._command_running)
        self.assertTrue(self.page.cancel_button.isEnabled())
        self.page.cancel_running_command()
        self.assertFalse(self.page.cancel_button.isEnabled())
        captured[0].run()
        self.app.processEvents()
        self.assertFalse(self.page._command_running)
        self.assertEqual(self.page.output_status.text(), "Cancelled by user")
        self.assertEqual(self.page.stdout_output.toPlainText(), "partial output")

    def test_custom_history_tools_root_risk_and_validation(self) -> None:
        self.device_manager.active = DeviceInfo(mode="No device")
        self.page.manual.setText("adb devices -l")
        with patch.object(self.page, "_start_command") as start:
            self.page.run_manual()
        start.assert_called_once()
        self.assertEqual(self.settings.get("command_history")[0], "adb devices -l")

        self.page.manual.setText("powershell Get-Process")
        with patch.object(self.page, "_start_command") as start:
            self.page.run_manual()
        start.assert_not_called()
        self.assertIn("must begin with adb or fastboot", self.page.custom_availability.text())

        self.device_manager.active = DeviceInfo(serial="device-1", mode="ADB", state="device")
        self.page.root_shell.setChecked(True)
        self.page._root_access_state = "available"
        self.page._root_access_serial = "device-1"
        self.page.manual.setText("adb shell id")
        with (
            patch.object(self.page, "_confirm_risk", return_value=False) as confirm,
            patch.object(self.page, "_start_command") as start,
        ):
            self.page.run_manual()
        self.assertEqual(confirm.call_args.args[2].level, "Critical")
        start.assert_not_called()

    def test_all_themes_render_at_narrow_size(self) -> None:
        self.page.resize(650, 520)
        self.page.view_mode.setCurrentText("Advanced")
        for theme in ("System", "Light", "Dark"):
            apply_theme(self.app, theme)
            self.app.processEvents()
            self.assertEqual(self.page.size().width(), 650)
            self.assertEqual(self.page.size().height(), 520)
            self.assertFalse(self.page.grab().isNull())
            self.assertGreater(self.page.tree.width(), 0)
            self.assertGreater(self.page.output_panel.height(), 0)


class CancellableClientTests(unittest.TestCase):
    def test_adb_and_fastboot_use_streaming_runner_when_cancel_is_available(self) -> None:
        runner = MagicMock()
        runner.run_streaming.return_value = make_result()
        tools = SimpleNamespace(
            adb_path=Path("C:/platform-tools/adb.exe"),
            fastboot_path=Path("C:/platform-tools/fastboot.exe"),
        )
        event = threading.Event()
        adb = ADBClient(tools, runner)
        fastboot = FastbootClient(tools, runner)

        adb.run_raw(["devices", "-l"], use_serial=False, cancel_event=event)
        fastboot.run_raw(["devices"], use_serial=False, cancel_event=event)

        self.assertEqual(runner.run_streaming.call_count, 2)
        for call in runner.run_streaming.call_args_list:
            self.assertIs(call.kwargs["cancel_event"], event)
        runner.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
