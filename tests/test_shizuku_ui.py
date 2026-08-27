from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.operations import OperationConflictError, OperationRegistry
from openadb.core.privilege import PrivilegeBackend, PrivilegeStatus
from openadb.core.settings_manager import SettingsManager
from openadb.models.command_result import CommandResult, format_command
from openadb.models.device_info import DeviceInfo
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.commands_page import CommandsPage
from openadb.ui.dashboard_page import DashboardPage
from openadb.ui.main_window import MainWindow
from openadb.ui.settings_page import SettingsPage


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


def result(command: list[str] | None = None, *, stdout: str = "") -> CommandResult:
    now = datetime.now(UTC)
    return CommandResult(
        command=command or ["shizuku", "shell", "<protected request>"],
        exit_code=0,
        stdout=stdout,
        stderr="",
        duration=0.01,
        started_at=now,
        finished_at=now,
        success=True,
        status="Success",
    )


def shizuku_status(
    *,
    state: str = "ready",
    uid: int | None = 2000,
    message: str | None = None,
    serial: str = "device-1",
    generation: int = 7,
) -> PrivilegeStatus:
    ready = state == "ready"
    level = "root" if ready and uid == 0 else ("shell" if ready and uid == 2000 else "unavailable")
    return PrivilegeStatus(
        backend=PrivilegeBackend.SHIZUKU,
        state=state,
        uid=uid,
        level=level,
        message=message or {
            "ready": f"Ready as UID {uid}.",
            "permission_required": "Permission is required.",
            "permission_denied": "Permission was denied.",
            "stopped": "Shizuku is stopped.",
        }.get(state, "Shizuku is unavailable."),
        device_serial=serial,
        device_generation=generation,
    )


class SettingsShizukuUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = IsolatedSettings(Path(self.temporary.name))
        self.page = SettingsPage(self.settings)

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_three_way_privilege_selector_persists_backend_and_legacy_mirror(self) -> None:
        self.page.privilege_mode.setCurrentIndex(
            self.page.privilege_mode.findData("root")
        )

        self.assertEqual(self.page.privilege_mode.backend(), PrivilegeBackend.ROOT)
        self.assertEqual(self.settings.get("privilege_backend"), "root")
        self.assertTrue(self.settings.get("root_mode_enabled"))

        self.page.privilege_mode.setCurrentIndex(
            self.page.privilege_mode.findData("shizuku")
        )

        self.assertEqual(
            self.page.privilege_mode.backend(),
            PrivilegeBackend.SHIZUKU,
        )
        self.assertEqual(self.settings.get("privilege_backend"), "shizuku")
        self.assertFalse(self.settings.get("root_mode_enabled"))
        persisted = json.loads(self.settings.path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["privilege_backend"], "shizuku")
        self.assertFalse(persisted["root_mode_enabled"])

        self.page.privilege_mode.setCurrentIndex(
            self.page.privilege_mode.findData("standard")
        )

        self.assertEqual(self.settings.get("privilege_backend"), "standard")
        self.assertFalse(self.settings.get("root_mode_enabled"))

    def test_shizuku_status_buttons_emit_distinct_signals_and_follow_busy_state(self) -> None:
        emitted: list[str] = []
        self.page.check_privilege_requested.connect(lambda: emitted.append("check"))
        self.page.request_shizuku_permission_requested.connect(lambda: emitted.append("request"))
        self.page.open_shizuku_requested.connect(lambda: emitted.append("open"))

        self.assertFalse(self.page.request_shizuku_button.isEnabled())
        self.assertFalse(self.page.open_shizuku_button.isEnabled())
        self.page.privilege_mode.setCurrentIndex(
            self.page.privilege_mode.findData("shizuku")
        )
        self.page.check_privilege_button.click()
        self.page.request_shizuku_button.click()
        self.page.open_shizuku_button.click()

        self.assertEqual(emitted, ["check", "request", "open"])
        status = shizuku_status(message="Official Shizuku UserService is ready (UID 2000).")
        self.page.set_privilege_status(status)
        self.assertEqual(
            self.page.privilege_status.text(),
            "Shizuku: Official Shizuku UserService is ready (UID 2000).",
        )
        self.assertEqual(self.page.privilege_status.toolTip(), self.page.privilege_status.text())

        self.page.set_privilege_busy(True, "Waiting for Android permission…")
        self.assertFalse(self.page.check_privilege_button.isEnabled())
        self.assertFalse(self.page.request_shizuku_button.isEnabled())
        self.assertFalse(self.page.open_shizuku_button.isEnabled())
        self.assertEqual(self.page.privilege_status.text(), "Waiting for Android permission…")

        self.page.set_privilege_busy(False)
        self.assertTrue(self.page.check_privilege_button.isEnabled())
        self.assertTrue(self.page.request_shizuku_button.isEnabled())
        self.assertTrue(self.page.open_shizuku_button.isEnabled())


class DashboardShizukuUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = IsolatedSettings(Path(self.temporary.name))
        self.page = DashboardPage(self.settings)

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_dashboard_always_exposes_textual_shizuku_status_and_reset_state(self) -> None:
        status = shizuku_status(message="Ready as Android shell (UID 2000), not root.")

        self.page.update_privilege_status(status)

        label = self.page.detail_labels["Privileged access"]
        self.assertEqual(label.full_text(), "Shizuku: Ready as Android shell (UID 2000), not root.")
        self.assertEqual(label.toolTip(), label.full_text())

        self.page.update_privilege_status(None)

        expected = "Standard ADB selected; no Root or Shizuku is requested"
        self.assertEqual(label.full_text(), expected)
        self.assertEqual(label.toolTip(), expected)


class FakeCommandRunner:
    def __init__(self) -> None:
        self.run_streaming = MagicMock(return_value=result(["adb", "devices"]))
        self.record_result = MagicMock(side_effect=lambda value, **_kwargs: value)
        self.contexts: list[DeviceContext] = []

    def for_context(self, context: DeviceContext):
        self.contexts.append(context)
        return self

    @staticmethod
    def command_text(command) -> str:
        return format_command(list(command))


class FakeCommandAdb:
    def __init__(self, platform_tools) -> None:
        self.platform_tools = platform_tools
        self.serial = "device-1"
        self.run_raw = MagicMock(return_value=result())
        self.run_shell = MagicMock(return_value=result(["adb", "shell"]))
        self.run_root_shell = MagicMock(return_value=result(["adb", "shell", "su", "-c"]))
        self.pair_wireless_target = MagicMock(return_value=result())
        self.bound_contexts: list[DeviceContext] = []

    def for_context(self, context: DeviceContext):
        self.bound_contexts.append(context)
        return self

    @staticmethod
    def root_shell_script(command: str) -> str:
        return f"su -c '{command}'"


class FakeCommandFastboot:
    def __init__(self, platform_tools) -> None:
        self.platform_tools = platform_tools
        self.serial = ""
        self.run_raw = MagicMock(return_value=result(["fastboot", "devices"]))


class FakeCommandDeviceManager:
    def __init__(self, context: DeviceContext) -> None:
        self.context = context
        self.active = DeviceInfo(
            serial=context.serial,
            model="Test phone",
            mode=context.mode,
            state="device",
        )
        self.current_generation = context.generation
        self.operations = OperationRegistry()

    def require_context(self, _required_modes=None) -> DeviceContext:
        return self.context

    def is_context_current(self, candidate: DeviceContext) -> bool:
        return bool(
            candidate.serial == self.active.serial
            and candidate.generation == self.current_generation
        )


class FakePrivilegeManager:
    def __init__(self, adb, settings) -> None:
        self.adb = adb
        self.settings = settings
        self.status: PrivilegeStatus | None = None
        self.reset_calls = 0
        self.prepared_clients: list[SimpleNamespace] = []
        self.leases: list[SimpleNamespace] = []
        self.prepare_adb = MagicMock(side_effect=self._prepare_adb)
        # Compatibility assertion: Commands must no longer bypass the common
        # operation-scoped facade through this legacy one-off entry point.
        self.execute_shizuku_shell = MagicMock(return_value=result(stdout="shizuku output"))

    def cached_status(self) -> PrivilegeStatus | None:
        return self.status

    def reset(self) -> None:
        self.reset_calls += 1
        self.status = None

    def capture_operation_lease(self):
        lease = SimpleNamespace(
            backend=PrivilegeBackend.normalize(
                self.settings.get("privilege_backend", "standard")
            )
        )
        self.leases.append(lease)
        return lease

    def _prepare_adb(
        self,
        context: DeviceContext,
        *,
        cancel_event=None,
        privilege_lease=None,
    ):
        del cancel_event
        backend = PrivilegeBackend.normalize(
            self.settings.get("privilege_backend", "standard")
        )
        if privilege_lease is not None and privilege_lease.backend is not backend:
            raise RuntimeError("The selected access mode changed before execution.")
        if backend is PrivilegeBackend.STANDARD:
            self.adb.effective_privilege_backend = PrivilegeBackend.STANDARD
            return self.adb
        uid = 0 if backend is PrivilegeBackend.ROOT else getattr(self.status, "uid", 2000)
        prepared = SimpleNamespace(
            device_context=context,
            effective_privilege_backend=backend,
            verified_uid=uid,
            run_shell=MagicMock(
                return_value=result([backend.value, "shell", "<protected request>"])
            ),
            run_root_shell=MagicMock(
                return_value=result([backend.value, "root-shell", "<protected request>"])
            ),
        )
        self.prepared_clients.append(prepared)
        return prepared


class CommandsShizukuUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = IsolatedSettings(self.root)
        self.settings.set("privilege_backend", "shizuku", save=False)
        self.settings.set("root_mode_enabled", False)
        tools_dir = self.root / "platform-tools"
        tools_dir.mkdir()
        adb_path = tools_dir / "adb.exe"
        fastboot_path = tools_dir / "fastboot.exe"
        adb_path.touch()
        fastboot_path.touch()
        tools = SimpleNamespace(
            active=PlatformToolsInfo(
                folder=tools_dir,
                adb_path=adb_path,
                fastboot_path=fastboot_path,
                source="Test",
            ),
            adb_path=adb_path,
            fastboot_path=fastboot_path,
        )
        self.context = DeviceContext(
            serial="device-1",
            mode="ADB",
            transport_id="7",
            profile_key="device-1",
            profile_kind="Phone",
            profile_path=self.root,
            backups_path=self.root / "backups",
            temp_path=self.root / "temp",
            logs_path=self.root / "logs",
            generation=7,
        )
        self.device_manager = FakeCommandDeviceManager(self.context)
        self.adb = FakeCommandAdb(tools)
        self.fastboot = FakeCommandFastboot(tools)
        self.runner = FakeCommandRunner()
        self.privileges = FakePrivilegeManager(self.adb, self.settings)
        self.page = CommandsPage(
            self.adb,
            self.fastboot,
            self.runner,
            self.settings,
            self.device_manager,
            MagicMock(),
            privilege_manager=self.privileges,
        )

    def tearDown(self) -> None:
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_root_and_shizuku_shell_selection_is_mutually_exclusive_and_persisted(self) -> None:
        self.assertTrue(self.page.shizuku_shell.isChecked())
        self.assertFalse(self.page.root_shell.isChecked())

        self.page.root_shell.setChecked(True)

        self.assertTrue(self.page.root_shell.isChecked())
        self.assertFalse(self.page.shizuku_shell.isChecked())
        self.assertEqual(self.settings.get("privilege_backend"), "root")
        self.assertTrue(self.settings.get("root_mode_enabled"))

        self.page.shizuku_shell.setChecked(True)

        self.assertFalse(self.page.root_shell.isChecked())
        self.assertTrue(self.page.shizuku_shell.isChecked())
        self.assertEqual(self.settings.get("privilege_backend"), "shizuku")
        self.assertFalse(self.settings.get("root_mode_enabled"))

    def test_shizuku_availability_distinguishes_unknown_permission_stopped_shell_and_root(self) -> None:
        battery = self.page.spec_by_key["battery"]
        root_shell = self.page.spec_by_key["root_shell"]

        available, reason = self.page._availability(battery)
        self.assertFalse(available)
        self.assertIn("Check Shizuku", reason)

        cases = (
            (
                shizuku_status(state="permission_required", uid=None),
                "Grant OpenADB Bridge permission",
            ),
            (shizuku_status(state="stopped", uid=None), "Start Shizuku"),
        )
        for status, expected in cases:
            with self.subTest(state=status.state):
                self.page.set_privilege_status(status)
                available, reason = self.page._availability(battery)
                self.assertFalse(available)
                self.assertIn(expected, reason)

        shell = shizuku_status(uid=2000, message="Ready as Android shell (UID 2000).")
        self.page.set_privilege_status(shell)
        self.assertTrue(self.page._availability(battery)[0])
        available, reason = self.page._availability(root_shell)
        self.assertFalse(available)
        self.assertIn("UID 0", reason)
        self.assertIn("UID 2000", reason)

        root = shizuku_status(uid=0, message="Ready as root (UID 0).")
        self.page.set_privilege_status(root)
        self.assertTrue(self.page._availability(battery)[0])
        self.assertTrue(self.page._availability(root_shell)[0])

    def test_builtin_adb_shell_routes_through_selected_prepared_facade(self) -> None:
        self.page.set_privilege_status(shizuku_status(uid=2000))

        with patch.object(self.page, "_start_command") as start_command:
            self.page.run_spec(self.page.spec_by_key["battery"])

        start_command.assert_called_once()
        command_fn = start_command.call_args.args[0]
        cancel = threading.Event()
        command_fn(cancel_event=cancel)

        self.privileges.prepare_adb.assert_called_once()
        prepare_call = self.privileges.prepare_adb.call_args
        self.assertEqual(prepare_call.args, (self.context,))
        self.assertIs(prepare_call.kwargs["cancel_event"], cancel)
        self.assertIs(prepare_call.kwargs["privilege_lease"], self.privileges.leases[-1])
        prepared = self.privileges.prepared_clients[-1]
        prepared.run_shell.assert_called_once_with(
            "dumpsys battery",
            timeout=self.page.spec_by_key["battery"].timeout,
            cancel_event=cancel,
        )
        self.privileges.execute_shizuku_shell.assert_not_called()
        self.runner.run_streaming.assert_not_called()
        self.adb.run_shell.assert_not_called()
        self.adb.run_root_shell.assert_not_called()

    def test_manual_adb_shell_routes_through_prepared_facade_not_windows_runner(self) -> None:
        self.page.set_privilege_status(shizuku_status(uid=2000))
        self.page.manual.setText("adb shell dumpsys package com.example")

        with patch.object(self.page, "_start_command") as start_command:
            self.page.run_manual()

        start_command.assert_called_once()
        command_fn = start_command.call_args.args[0]
        cancel = threading.Event()
        command_fn(cancel_event=cancel)

        self.privileges.prepare_adb.assert_called_once()
        prepare_call = self.privileges.prepare_adb.call_args
        self.assertEqual(prepare_call.args, (self.context,))
        self.assertIs(prepare_call.kwargs["cancel_event"], cancel)
        self.assertIs(prepare_call.kwargs["privilege_lease"], self.privileges.leases[-1])
        prepared = self.privileges.prepared_clients[-1]
        prepared.run_shell.assert_called_once_with(
            "dumpsys package com.example",
            timeout=300,
            cancel_event=cancel,
        )
        self.privileges.execute_shizuku_shell.assert_not_called()
        self.runner.run_streaming.assert_not_called()
        self.assertEqual(
            self.settings.get("command_history")[0],
            "adb shell dumpsys package com.example",
        )

    def test_registered_prepared_result_is_not_logged_twice_by_page(self) -> None:
        token = self.device_manager.operations.register(
            "commands-page",
            device_context=self.context,
        )
        shizuku_result = result(stdout="safe output")

        returned = self.page._run_registered_command(
            token,
            self.context,
            MagicMock(return_value=shizuku_result),
            "adb shell id",
        )

        self.assertIs(returned, shizuku_result)
        self.runner.record_result.assert_not_called()
        self.device_manager.operations.finish(token)

    def test_shell_worker_rejects_backend_switch_after_command_was_planned(self) -> None:
        self.page.set_privilege_status(shizuku_status(uid=2000))
        with patch.object(self.page, "_start_command") as start_command:
            self.page.run_spec(self.page.spec_by_key["battery"])
        command_fn = start_command.call_args.args[0]

        self.settings.set("privilege_backend", "root", save=False)
        with self.assertRaisesRegex(RuntimeError, "access mode changed"):
            command_fn(cancel_event=threading.Event())

        self.assertEqual(self.privileges.prepared_clients, [])

    def test_access_failure_clears_visible_shizuku_status(self) -> None:
        self.page.set_privilege_status(shizuku_status(uid=2000))
        invalidated: list[bool] = []
        self.page.privilege_status_invalidated.connect(
            lambda: invalidated.append(True)
        )
        failed = result()
        failed.success = False
        failed.exit_code = 1
        failed.status = "Shizuku permission was revoked."
        failed.error_type = "shizuku_permission_required"

        self.page._show_result(failed)

        self.assertEqual(invalidated, [True])
        self.assertIsNone(self.page._privilege_status)
        self.assertIn("not been checked", self.page.privilege_status.text())

    def test_shell_named_argument_cannot_be_reinterpreted_as_shizuku_operation(self) -> None:
        self.page.set_privilege_status(shizuku_status(uid=2000))
        parts = ["adb", "push", "shell", "rm -rf /sdcard"]

        self.assertFalse(self.page._manual_uses_shizuku(parts))
        self.assertEqual(self.page._manual_shell_command(parts), "")
        self.assertEqual(self.page._rootify_adb_shell_parts(parts, force=True), parts)

        self.page.manual.setText('adb push shell "rm -rf /sdcard"')
        with (
            patch.object(self.page, "_confirm_risk", return_value=True),
            patch.object(self.page, "_start_command") as start_command,
        ):
            self.page.run_manual()

        self.assertTrue(start_command.called)
        self.privileges.execute_shizuku_shell.assert_not_called()

    def test_standard_and_root_manual_paths_do_not_regress_into_shizuku(self) -> None:
        self.page.shizuku_shell.setChecked(False)
        with patch.object(self.page, "_start_command") as start_command:
            self.page.run_spec(self.page.spec_by_key["battery"])
        standard_fn = start_command.call_args.args[0]
        cancel = threading.Event()
        standard_fn(cancel_event=cancel)
        self.adb.run_shell.assert_called_once_with(
            "dumpsys battery",
            timeout=self.page.spec_by_key["battery"].timeout,
            cancel_event=cancel,
        )
        self.privileges.execute_shizuku_shell.assert_not_called()

        self.page.root_shell.setChecked(True)
        self.page._root_access_state = "available"
        self.page._root_access_serial = self.context.serial
        self.page.manual.setText("adb shell id")
        with (
            patch.object(self.page, "_confirm_risk", return_value=True),
            patch.object(self.page, "_start_command") as start_command,
        ):
            self.page.run_manual()
        root_fn = start_command.call_args.args[0]
        root_fn(cancel_event=cancel)

        prepared = self.privileges.prepared_clients[-1]
        self.assertEqual(
            prepared.effective_privilege_backend,
            PrivilegeBackend.ROOT,
        )
        prepared.run_shell.assert_called_once_with(
            "id",
            timeout=300,
            cancel_event=cancel,
        )
        self.runner.run_streaming.assert_not_called()
        self.privileges.execute_shizuku_shell.assert_not_called()

    def test_status_from_previous_generation_or_device_is_cleared_before_use(self) -> None:
        self.page.set_privilege_status(shizuku_status(uid=2000))
        self.assertTrue(self.page._availability(self.page.spec_by_key["battery"])[0])

        self.device_manager.current_generation = 8
        self.page.update_device_state(self.device_manager.active)

        self.assertIsNone(self.page._privilege_status)
        self.assertEqual(
            self.privileges.reset_calls,
            0,
            "Commands must leave shared privilege lifecycle resets to MainWindow",
        )
        self.assertIn("not been checked for this device", self.page.privilege_status.text())
        available, reason = self.page._availability(self.page.spec_by_key["battery"])
        self.assertFalse(available)
        self.assertIn("Check Shizuku", reason)

        new_status = shizuku_status(uid=2000, serial="device-2", generation=8)
        self.page.set_privilege_status(new_status)
        self.assertFalse(self.page._availability(self.page.spec_by_key["battery"])[0])

    def test_root_status_does_not_survive_same_serial_generation_change(self) -> None:
        self.page.root_shell.setChecked(True)
        status = PrivilegeStatus(
            backend=PrivilegeBackend.ROOT,
            state="ready",
            uid=0,
            level="root",
            message="Root ready.",
            device_serial="device-1",
            device_generation=7,
        )
        self.page.set_privilege_status(status)
        root_shell = self.page.spec_by_key["root_shell"]
        self.assertTrue(self.page._availability(root_shell)[0])

        self.device_manager.current_generation = 8
        self.page.update_device_state(self.device_manager.active)

        self.assertFalse(self.page._availability(root_shell)[0])


class MainWindowPrivilegeOrchestrationTests(unittest.TestCase):
    def test_privilege_status_is_fanned_out_to_all_ui_surfaces(self) -> None:
        host = SimpleNamespace(
            settings_page=SimpleNamespace(set_privilege_status=MagicMock()),
            commands_page=SimpleNamespace(set_privilege_status=MagicMock()),
            apps_page=SimpleNamespace(update_privilege_status=MagicMock()),
            backups_page=SimpleNamespace(update_privilege_status=MagicMock()),
            dashboard=SimpleNamespace(update_privilege_status=MagicMock()),
            file_manager_page=SimpleNamespace(set_privilege_status=MagicMock()),
        )
        status = shizuku_status()

        MainWindow._apply_privilege_status(host, status)

        host.settings_page.set_privilege_status.assert_called_once_with(status)
        host.commands_page.set_privilege_status.assert_called_once_with(status)
        host.apps_page.update_privilege_status.assert_called_once_with(status)
        host.backups_page.update_privilege_status.assert_called_once_with(status)
        host.dashboard.update_privilege_status.assert_called_once_with(status)
        host.file_manager_page.set_privilege_status.assert_called_once_with(status)

    def test_queued_status_from_previous_backend_is_discarded_everywhere(self) -> None:
        host = SimpleNamespace(
            privilege_manager=SimpleNamespace(
                selected_backend=PrivilegeBackend.ROOT,
                status_is_current=MagicMock(return_value=False),
            ),
            settings_page=SimpleNamespace(set_privilege_status=MagicMock()),
            commands_page=SimpleNamespace(set_privilege_status=MagicMock()),
            apps_page=SimpleNamespace(update_privilege_status=MagicMock()),
            backups_page=SimpleNamespace(update_privilege_status=MagicMock()),
            dashboard=SimpleNamespace(update_privilege_status=MagicMock()),
            file_manager_page=SimpleNamespace(set_privilege_status=MagicMock()),
        )

        MainWindow._apply_privilege_status(host, shizuku_status())

        host.settings_page.set_privilege_status.assert_called_once_with(None)
        host.commands_page.set_privilege_status.assert_called_once_with(None)
        host.apps_page.update_privilege_status.assert_called_once_with(None)
        host.backups_page.update_privilege_status.assert_called_once_with(None)
        host.dashboard.update_privilege_status.assert_called_once_with(None)
        host.file_manager_page.set_privilege_status.assert_called_once_with(None)

    def test_status_update_cannot_replace_active_transition_overlay(self) -> None:
        status = shizuku_status(state="ready", uid=0)
        host = SimpleNamespace(
            _privilege_token=None,
            _privilege_feature_barrier_busy=True,
            _automatic_shizuku_ui_busy=False,
            privilege_manager=SimpleNamespace(
                selected_backend=PrivilegeBackend.SHIZUKU,
                status_is_current=MagicMock(return_value=True),
            ),
            settings_page=SimpleNamespace(
                set_privilege_status=MagicMock(),
                set_privilege_busy=MagicMock(),
            ),
            commands_page=SimpleNamespace(
                set_privilege_status=MagicMock(),
                set_privilege_busy=MagicMock(),
            ),
            apps_page=SimpleNamespace(update_privilege_status=MagicMock()),
            backups_page=SimpleNamespace(update_privilege_status=MagicMock()),
            dashboard=SimpleNamespace(update_privilege_status=MagicMock()),
            file_manager_page=SimpleNamespace(set_privilege_status=MagicMock()),
            _set_global_privilege_status_text=MagicMock(),
        )

        MainWindow._apply_privilege_status(host, status)

        message = (
            "Applying Shizuku access after active device operations finish…"
        )
        host._set_global_privilege_status_text.assert_called_with(message)
        host.settings_page.set_privilege_busy.assert_called_with(True, message)
        host.commands_page.set_privilege_busy.assert_called_with(True, message)

    def test_stale_privilege_worker_result_is_not_applied(self) -> None:
        host = SimpleNamespace(
            _privilege_callback_is_current=MagicMock(return_value=False),
            _apply_privilege_status=MagicMock(),
        )
        token = object()

        MainWindow._privilege_operation_result(host, token, shizuku_status())

        host._privilege_callback_is_current.assert_called_once_with(token)
        host._apply_privilege_status.assert_not_called()

    def test_privilege_callback_accepts_its_registered_current_token(self) -> None:
        registry = OperationRegistry()
        context = DeviceContext(
            serial="device-1",
            mode="ADB",
            transport_id="7",
            profile_key="device-1",
            profile_kind="Phone",
            profile_path=Path("profile"),
            backups_path=Path("backups"),
            temp_path=Path("temp"),
            logs_path=Path("logs"),
            generation=3,
        )
        token = registry.register("privilege-access", device_context=context)
        device_manager = SimpleNamespace(
            operations=registry,
            is_context_current=MagicMock(return_value=True),
        )
        host = SimpleNamespace(
            _closing=False,
            _privilege_token=token,
            device_manager=device_manager,
        )

        self.assertTrue(MainWindow._privilege_callback_is_current(host, token))
        device_manager.is_context_current.assert_called_once_with(context)

        token.cancel("device changed")
        self.assertFalse(MainWindow._privilege_callback_is_current(host, token))

    def test_noninteractive_recheck_is_queued_while_old_worker_finishes(self) -> None:
        host = SimpleNamespace(
            _privilege_token=object(),
            _pending_privilege_recheck=False,
            statusBar=MagicMock(),
        )

        MainWindow._start_privilege_operation(
            host,
            "check",
            self._context(),
            MagicMock(),
            "Checking…",
            interactive=False,
        )

        self.assertTrue(host._pending_privilege_recheck)
        host.statusBar.assert_not_called()

    def test_noninteractive_recheck_is_queued_after_operation_conflict(self) -> None:
        operations = MagicMock()
        operations.register.side_effect = OperationConflictError("device busy")
        host = SimpleNamespace(
            _privilege_token=None,
            _pending_privilege_recheck=False,
            device_manager=SimpleNamespace(operations=operations),
            statusBar=MagicMock(),
        )

        MainWindow._start_privilege_operation(
            host,
            "check",
            self._context(),
            MagicMock(),
            "Checking…",
            interactive=False,
        )

        self.assertTrue(host._pending_privilege_recheck)
        host.statusBar.assert_not_called()

    def test_finished_worker_schedules_queued_recheck_after_releasing_token(self) -> None:
        token = object()
        host = SimpleNamespace(
            _privilege_token=token,
            _privilege_operation_kind="check",
            _privilege_operation_interactive=False,
            _pending_privilege_recheck=True,
            _privilege_recheck_callback_scheduled=False,
            _closing=False,
            settings_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            commands_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            privilege_manager=SimpleNamespace(
                cached_status=MagicMock(return_value=None),
                selected_backend=PrivilegeBackend.ROOT,
            ),
            _apply_privilege_status=MagicMock(),
            check_privilege_access=MagicMock(return_value=True),
            _acbridge_update_token=None,
            _acbridge_update_retry_key=None,
            _pending_acbridge_update_context=None,
            device_manager=SimpleNamespace(
                active=DeviceInfo(
                    serial="device-1",
                    mode="ADB",
                    state="device",
                )
            ),
        )
        host._schedule_privilege_recheck = (
            lambda **kwargs: MainWindow._schedule_privilege_recheck(host, **kwargs)
        )
        callbacks = []

        with patch(
            "openadb.ui.main_window.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            MainWindow._privilege_operation_finished(host, token)

        self.assertIsNone(host._privilege_token)
        self.assertTrue(host._pending_privilege_recheck)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertFalse(host._pending_privilege_recheck)
        host.check_privilege_access.assert_called_once_with(interactive=False)

    def test_old_privilege_worker_cannot_consume_bridge_deferred_recheck(self) -> None:
        token = object()
        host = SimpleNamespace(
            _privilege_token=token,
            _privilege_operation_kind="check",
            _privilege_operation_interactive=False,
            _pending_privilege_recheck=True,
            _closing=False,
            settings_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            commands_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            privilege_manager=SimpleNamespace(
                cached_status=MagicMock(return_value=None),
                selected_backend=PrivilegeBackend.SHIZUKU,
            ),
            _apply_privilege_status=MagicMock(),
            check_privilege_access=MagicMock(),
            _acbridge_update_token=object(),
            _acbridge_update_retry_key=None,
            _pending_acbridge_update_context=None,
            device_manager=SimpleNamespace(
                active=DeviceInfo(
                    serial="device-2",
                    mode="ADB",
                    state="device",
                )
            ),
        )
        host._schedule_privilege_recheck = (
            lambda **kwargs: MainWindow._schedule_privilege_recheck(host, **kwargs)
        )

        with patch("openadb.ui.main_window.QTimer.singleShot") as single_shot:
            MainWindow._privilege_operation_finished(host, token)

        self.assertTrue(host._pending_privilege_recheck)
        single_shot.assert_not_called()
        host.check_privilege_access.assert_not_called()

    def test_automatic_shizuku_handshake_requests_then_checks_once_per_generation(
        self,
    ) -> None:
        host, context = self._automatic_host()
        callbacks: list[tuple[int, object]] = []

        with patch(
            "openadb.ui.main_window.QTimer.singleShot",
            side_effect=lambda delay, callback: callbacks.append((delay, callback)),
        ):
            self.assertTrue(
                MainWindow._schedule_automatic_shizuku_handshake(host)
            )
            self.assertTrue(
                MainWindow._schedule_automatic_shizuku_handshake(host)
            )
            self.assertEqual(len(callbacks), 1)
            callbacks.pop(0)[1]()

            self.assertIsNone(host._last_automatic_shizuku_key)
            token = SimpleNamespace(device_context=context)
            host._privilege_operation_kind = "automatic-shizuku"
            MainWindow._privilege_operation_result(
                host,
                token,
                shizuku_status(generation=context.generation),
            )

            self.assertTrue(
                MainWindow._schedule_automatic_shizuku_handshake(host)
            )
            self.assertEqual(callbacks, [])

        host._start_privilege_operation.assert_called_once()
        operation, started_context, operation_fn, message = (
            host._start_privilege_operation.call_args.args
        )
        self.assertEqual(operation, "automatic-shizuku")
        self.assertIs(started_context, context)
        self.assertIn("Requesting and verifying", message)
        self.assertFalse(
            host._start_privilege_operation.call_args.kwargs["interactive"]
        )

        operation_fn(threading.Event())
        host.privilege_manager.request_and_check_shizuku.assert_called_once_with(
            context,
            cancel_event=unittest.mock.ANY,
            privilege_lease=host.privilege_manager.capture_operation_lease.return_value,
        )

    def test_completed_automatic_handshake_does_not_fall_through_to_second_check(
        self,
    ) -> None:
        host, context = self._automatic_host()
        host._last_automatic_shizuku_key = (
            context.serial,
            context.generation,
        )

        with patch("openadb.ui.main_window.QTimer.singleShot") as single_shot:
            MainWindow._schedule_privilege_recheck(host)

        single_shot.assert_not_called()
        host.check_privilege_access.assert_not_called()
        self.assertFalse(host._pending_privilege_recheck)

    def test_missing_context_does_not_queue_zero_delay_recheck_loop(self) -> None:
        host, _context = self._automatic_host()
        host.device_manager.require_context.side_effect = DeviceContextUnavailable(
            "The active device profile is not ready."
        )

        with patch("openadb.ui.main_window.QTimer.singleShot") as single_shot:
            MainWindow._schedule_privilege_recheck(host)

        single_shot.assert_not_called()
        host.check_privilege_access.assert_not_called()
        self.assertFalse(host._pending_privilege_recheck)

    def test_automatic_shizuku_handshake_runs_again_after_reconnect(self) -> None:
        host, first = self._automatic_host()
        callbacks = []

        with patch(
            "openadb.ui.main_window.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            MainWindow._schedule_automatic_shizuku_handshake(host)
            callbacks.pop(0)()
            host._privilege_operation_kind = "automatic-shizuku"
            MainWindow._privilege_operation_result(
                host,
                SimpleNamespace(device_context=first),
                shizuku_status(generation=first.generation),
            )

            second = DeviceContext(
                serial=first.serial,
                mode=first.mode,
                transport_id="9",
                profile_key=first.profile_key,
                profile_kind=first.profile_kind,
                profile_path=first.profile_path,
                backups_path=first.backups_path,
                temp_path=first.temp_path,
                logs_path=first.logs_path,
                generation=first.generation + 1,
            )
            host.device_manager.current_context = second
            host.device_manager.require_context.return_value = second
            host.device_manager.active.transport_id = second.transport_id

            MainWindow._schedule_automatic_shizuku_handshake(host)
            callbacks.pop(0)()

        self.assertEqual(host._start_privilege_operation.call_count, 2)
        self.assertEqual(
            host._automatic_shizuku_inflight_key,
            (second.serial, second.generation),
        )

    def test_cancelled_same_generation_handshake_can_be_restarted(self) -> None:
        host, context = self._automatic_host()
        host._automatic_shizuku_inflight_key = (
            context.serial,
            context.generation,
        )
        host._privilege_token = SimpleNamespace(cancelled=True)
        host._privilege_operation_kind = "automatic-shizuku"
        callbacks = []

        with patch(
            "openadb.ui.main_window.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            self.assertTrue(
                MainWindow._schedule_automatic_shizuku_handshake(host)
            )

        self.assertEqual(len(callbacks), 1)
        self.assertIs(host._pending_automatic_shizuku_context, context)

    def test_automatic_worker_without_result_retries_then_shows_failure(self) -> None:
        host, context = self._automatic_host()
        token = SimpleNamespace(device_context=context)
        key = (context.serial, context.generation)
        host._privilege_token = token
        host._privilege_operation_kind = "automatic-shizuku"
        host._automatic_shizuku_inflight_key = key
        host._automatic_shizuku_attempts[key] = 1
        callbacks = []

        with patch(
            "openadb.ui.main_window.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            MainWindow._privilege_operation_finished(host, token)

        self.assertIsNone(host._last_automatic_shizuku_key)
        self.assertIsNone(host._automatic_shizuku_inflight_key)
        self.assertIsNone(host._privilege_token)
        self.assertIs(host._pending_automatic_shizuku_context, context)
        self.assertTrue(host._pending_privilege_recheck)
        self.assertEqual(len(callbacks), 1)
        host._resume_feature_refresh_after_acbridge.assert_not_called()

        second_token = SimpleNamespace(device_context=context)
        host._privilege_token = second_token
        host._privilege_operation_kind = "automatic-shizuku"
        host._automatic_shizuku_inflight_key = key
        host._pending_automatic_shizuku_context = None
        host._automatic_shizuku_scheduled_key = None
        host._pending_privilege_recheck = False
        host._automatic_shizuku_attempts[key] = 2

        MainWindow._privilege_operation_finished(host, second_token)

        self.assertEqual(host._last_automatic_shizuku_key, key)
        self.assertEqual(host._automatic_shizuku_failure_status.state, "error")
        host._apply_privilege_status.assert_called_with(
            host._automatic_shizuku_failure_status
        )
        host._resume_feature_refresh_after_acbridge.assert_called_once_with()

    def test_cancelled_automatic_result_is_retried_not_completed(self) -> None:
        host, context = self._automatic_host()
        token = SimpleNamespace(device_context=context)
        key = (context.serial, context.generation)
        host._privilege_token = token
        host._privilege_operation_kind = "automatic-shizuku"
        host._automatic_shizuku_inflight_key = key
        host._automatic_shizuku_attempts[key] = 1

        MainWindow._privilege_operation_result(
            host,
            token,
            shizuku_status(
                state="cancelled",
                uid=None,
                serial=context.serial,
                generation=context.generation,
            ),
        )

        self.assertIsNone(host._last_automatic_shizuku_key)
        self.assertEqual(host._automatic_shizuku_inflight_key, key)
        host._apply_privilege_status.assert_not_called()

    def test_error_automatic_result_is_retried_not_completed(self) -> None:
        host, context = self._automatic_host()
        token = SimpleNamespace(device_context=context)
        key = (context.serial, context.generation)
        host._privilege_token = token
        host._privilege_operation_kind = "automatic-shizuku"
        host._automatic_shizuku_inflight_key = key
        host._automatic_shizuku_attempts[key] = 1

        MainWindow._privilege_operation_result(
            host,
            token,
            shizuku_status(
                state="error",
                uid=None,
                serial=context.serial,
                generation=context.generation,
            ),
        )

        self.assertIsNone(host._last_automatic_shizuku_key)
        self.assertEqual(host._automatic_shizuku_inflight_key, key)
        host._apply_privilege_status.assert_not_called()

    def test_worker_start_failures_consume_bounded_automatic_attempts(self) -> None:
        host, context = self._automatic_host()
        operations = OperationRegistry()
        host.device_manager.operations = operations
        host.device_bar = SimpleNamespace(pool=object())
        host._start_privilege_operation = (
            lambda *args, **kwargs: MainWindow._start_privilege_operation(
                host,
                *args,
                **kwargs,
            )
        )
        host._privilege_operation_finished = (
            lambda token, **kwargs: MainWindow._privilege_operation_finished(
                host,
                token,
                **kwargs,
            )
        )
        callbacks = []

        def reject_worker(*_args, **kwargs) -> bool:
            operations.finish(kwargs["operation_token"])
            return False

        with (
            patch(
                "openadb.ui.main_window.QTimer.singleShot",
                side_effect=lambda _delay, callback: callbacks.append(callback),
            ),
            patch(
                "openadb.ui.main_window.start_worker",
                side_effect=reject_worker,
            ) as start,
        ):
            MainWindow._schedule_automatic_shizuku_handshake(host)
            callbacks.pop(0)()
            self.assertEqual(host._automatic_shizuku_attempts[(context.serial, context.generation)], 1)
            callbacks.pop(0)()

        self.assertEqual(start.call_count, 2)
        self.assertEqual(
            host._last_automatic_shizuku_key,
            (context.serial, context.generation),
        )
        self.assertEqual(host._automatic_shizuku_failure_status.state, "error")
        self.assertEqual(callbacks, [])
        host._resume_feature_refresh_after_acbridge.assert_called_once_with()

    def test_stale_automatic_worker_cannot_complete_new_generation(self) -> None:
        host, context = self._automatic_host()
        token = SimpleNamespace(device_context=context)
        key = (context.serial, context.generation)
        host._privilege_token = token
        host._privilege_operation_kind = "automatic-shizuku"
        host._automatic_shizuku_inflight_key = key
        host.device_manager.is_context_current.side_effect = None
        host.device_manager.is_context_current.return_value = False

        MainWindow._privilege_operation_finished(host, token)

        self.assertIsNone(host._last_automatic_shizuku_key)
        self.assertIsNone(host._automatic_shizuku_inflight_key)

    def test_automatic_shizuku_handshake_retries_a_busy_operation_group(self) -> None:
        host, context = self._automatic_host()
        host._start_privilege_operation.side_effect = (False, True)
        callbacks: list[tuple[int, object]] = []

        with patch(
            "openadb.ui.main_window.QTimer.singleShot",
            side_effect=lambda delay, callback: callbacks.append((delay, callback)),
        ):
            MainWindow._schedule_automatic_shizuku_handshake(host)
            first_delay, first_callback = callbacks.pop(0)
            first_callback()
            retry_delay, retry_callback = callbacks.pop(0)
            retry_callback()

        self.assertEqual(first_delay, 0)
        self.assertEqual(retry_delay, 750)
        self.assertEqual(host._start_privilege_operation.call_count, 2)
        self.assertEqual(
            host._automatic_shizuku_inflight_key,
            (context.serial, context.generation),
        )
        self.assertFalse(host._pending_privilege_recheck)

    def test_automatic_shizuku_waits_for_acbridge_and_holds_feature_refreshes(
        self,
    ) -> None:
        host, _context = self._automatic_host()
        host._acbridge_update_token = object()
        host._pending_acbridge_feature_refresh = {"apps"}
        host.stack = SimpleNamespace(currentWidget=MagicMock(return_value=host.apps_page))
        host.apps_page.refresh_apps = MagicMock()
        callbacks = []

        with patch(
            "openadb.ui.main_window.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            self.assertTrue(
                MainWindow._schedule_automatic_shizuku_handshake(
                    host,
                    force_defer=True,
                )
            )
            MainWindow._resume_feature_refresh_after_acbridge(host)
            self.assertEqual(callbacks, [])
            self.assertEqual(host._pending_acbridge_feature_refresh, {"apps"})

            host._acbridge_update_token = None
            MainWindow._schedule_automatic_shizuku_handshake(host)
            callbacks.pop(0)()
            host._privilege_token = object()
            host._privilege_operation_kind = "automatic-shizuku"
            MainWindow._resume_feature_refresh_after_acbridge(host)
            self.assertEqual(callbacks, [])
            host._privilege_token = None
            host._privilege_operation_kind = ""
            host._automatic_shizuku_inflight_key = None
            host.privilege_manager.cached_status.return_value = shizuku_status()
            MainWindow._resume_feature_refresh_after_acbridge(host)
            callbacks.pop(0)()

        host.apps_page.refresh_apps.assert_called_once_with()
        self.assertEqual(host._pending_acbridge_feature_refresh, set())

    def test_page_navigation_defers_device_refresh_during_automatic_handshake(
        self,
    ) -> None:
        apps = MagicMock(apps=[])
        backups = MagicMock()
        file_manager = MagicMock()
        pages = {
            "Dashboard": object(),
            "Apps": apps,
            "Backups": backups,
            "File Manager": file_manager,
        }
        host = SimpleNamespace(
            pages=pages,
            apps_page=apps,
            backups_page=backups,
            file_manager_page=file_manager,
            device_manager=SimpleNamespace(
                active=DeviceInfo(serial="device-1", mode="ADB", state="device")
            ),
            _pending_acbridge_feature_refresh=set(),
            _automatic_shizuku_workflow_pending=MagicMock(return_value=True),
        )

        MainWindow._on_page_changed(host, 1)
        MainWindow._on_page_changed(host, 2)
        MainWindow._on_page_changed(host, 3)

        self.assertEqual(
            host._pending_acbridge_feature_refresh,
            {"apps", "backups", "file-manager"},
        )
        apps.refresh_apps.assert_not_called()
        backups.refresh.assert_not_called()
        file_manager.refresh_all.assert_not_called()

    def test_page_navigation_defers_device_refresh_during_acbridge_update(
        self,
    ) -> None:
        apps = MagicMock(apps=[])
        backups = MagicMock()
        file_manager = MagicMock()
        host = SimpleNamespace(
            pages={
                "Dashboard": object(),
                "Apps": apps,
                "Backups": backups,
                "File Manager": file_manager,
            },
            apps_page=apps,
            backups_page=backups,
            file_manager_page=file_manager,
            device_manager=SimpleNamespace(
                active=DeviceInfo(serial="device-1", mode="ADB", state="device")
            ),
            _pending_acbridge_feature_refresh=set(),
            _automatic_shizuku_workflow_pending=MagicMock(return_value=False),
            _acbridge_update_token=object(),
            _acbridge_update_retry_key=None,
            _pending_acbridge_update_context=None,
            _privilege_barrier_waits_for_recheck=False,
        )

        MainWindow._on_page_changed(host, 1)
        MainWindow._on_page_changed(host, 2)
        MainWindow._on_page_changed(host, 3)

        self.assertEqual(
            host._pending_acbridge_feature_refresh,
            {"apps", "backups", "file-manager"},
        )
        apps.refresh_apps.assert_not_called()
        backups.refresh.assert_not_called()
        file_manager.refresh_all.assert_not_called()

    def test_acbridge_and_shizuku_busy_states_do_not_reenable_each_other(self) -> None:
        host = SimpleNamespace(
            _automatic_shizuku_ui_busy=False,
            _privilege_feature_barrier_busy=False,
            _acbridge_maintenance_ui_busy=False,
            apps_page=MagicMock(),
            backups_page=MagicMock(),
            file_manager_page=MagicMock(),
            settings_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            commands_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            _set_global_privilege_status_text=MagicMock(),
        )

        MainWindow._set_acbridge_maintenance_ui_busy(host, True)
        MainWindow._set_automatic_shizuku_ui_busy(host, True)
        MainWindow._set_acbridge_maintenance_ui_busy(host, False)

        host.apps_page.setEnabled.assert_called_with(False)
        host.backups_page.setEnabled.assert_called_with(False)
        host.file_manager_page.setEnabled.assert_called_with(False)

        MainWindow._set_automatic_shizuku_ui_busy(host, False)

        host.apps_page.setEnabled.assert_called_with(True)
        host.backups_page.setEnabled.assert_called_with(True)
        host.file_manager_page.setEnabled.assert_called_with(True)

    def test_transition_and_automatic_busy_sources_do_not_reenable_each_other(self) -> None:
        ready_status = shizuku_status(state="ready", uid=2000)
        host = SimpleNamespace(
            _automatic_shizuku_ui_busy=False,
            _privilege_feature_barrier_busy=False,
            _privilege_token=None,
            _privilege_operation_busy_message="",
            _acbridge_maintenance_ui_busy=False,
            _last_privilege_display_status=ready_status,
            privilege_manager=SimpleNamespace(
                selected_backend=PrivilegeBackend.SHIZUKU,
            ),
            apps_page=MagicMock(),
            backups_page=MagicMock(),
            file_manager_page=MagicMock(),
            settings_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            commands_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            _set_global_privilege_status_text=MagicMock(),
            _apply_privilege_status=MagicMock(),
        )

        MainWindow._set_privilege_feature_barrier_busy(host, True)
        MainWindow._set_automatic_shizuku_ui_busy(host, True)
        MainWindow._set_privilege_feature_barrier_busy(host, False)

        host.apps_page.setEnabled.assert_called_with(False)
        host.backups_page.setEnabled.assert_called_with(False)
        host.file_manager_page.setEnabled.assert_called_with(False)
        host.settings_page.set_privilege_busy.assert_called_with(
            True,
            "Preparing automatic Shizuku permission and access check…",
        )
        host.commands_page.set_privilege_busy.assert_called_with(
            True,
            "Preparing automatic Shizuku permission and access check…",
        )

        MainWindow._set_automatic_shizuku_ui_busy(host, False)

        host.apps_page.setEnabled.assert_called_with(True)
        host.backups_page.setEnabled.assert_called_with(True)
        host.file_manager_page.setEnabled.assert_called_with(True)
        host.settings_page.set_privilege_busy.assert_called_with(False)
        host.commands_page.set_privilege_busy.assert_called_with(False)
        host._apply_privilege_status.assert_called_with(ready_status)

    def test_busy_status_is_not_restored_until_every_source_is_clear(self) -> None:
        ready_status = shizuku_status(state="ready", uid=2000)
        host = SimpleNamespace(
            _automatic_shizuku_ui_busy=True,
            _privilege_feature_barrier_busy=True,
            _privilege_token=None,
            _privilege_operation_busy_message="",
            _last_privilege_display_status=ready_status,
            privilege_manager=SimpleNamespace(
                selected_backend=PrivilegeBackend.SHIZUKU,
            ),
            settings_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            commands_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            _set_global_privilege_status_text=MagicMock(),
            _apply_privilege_status=MagicMock(),
        )

        MainWindow._set_privilege_feature_barrier_busy(host, False)

        host._apply_privilege_status.assert_not_called()
        host._set_global_privilege_status_text.assert_called_with(
            "Preparing automatic Shizuku permission and access check…"
        )

        MainWindow._set_automatic_shizuku_ui_busy(host, False)

        host._apply_privilege_status.assert_called_once_with(ready_status)

    def test_invalidated_shizuku_status_queues_new_same_connection_handshake(
        self,
    ) -> None:
        key = ("device-1", 3)
        host = SimpleNamespace(
            _closing=False,
            _last_automatic_shizuku_key=key,
            _automatic_shizuku_attempts={key: 2},
            _automatic_shizuku_failure_status=shizuku_status(
                state="error",
                uid=None,
                generation=3,
            ),
            _pending_privilege_recheck=False,
            privilege_manager=SimpleNamespace(
                selected_backend=PrivilegeBackend.SHIZUKU,
                reset=MagicMock(),
            ),
            _apply_privilege_status=MagicMock(),
            _schedule_privilege_recheck=MagicMock(),
        )

        MainWindow._invalidate_privilege_status(host)

        host.privilege_manager.reset.assert_called_once_with()
        host._apply_privilege_status.assert_called_once_with(None)
        self.assertIsNone(host._last_automatic_shizuku_key)
        self.assertEqual(host._automatic_shizuku_attempts, {})
        self.assertIsNone(host._automatic_shizuku_failure_status)
        self.assertTrue(host._pending_privilege_recheck)
        host._schedule_privilege_recheck.assert_called_once_with()

    def test_runtime_invalidation_recovers_without_resetting_manager_again(
        self,
    ) -> None:
        key = ("device-1", 3)
        host = SimpleNamespace(
            _closing=False,
            _last_automatic_shizuku_key=key,
            _automatic_shizuku_attempts={key: 1},
            _automatic_shizuku_failure_status=shizuku_status(
                state="error",
                uid=None,
                generation=3,
            ),
            _pending_privilege_recheck=False,
            privilege_manager=SimpleNamespace(
                selected_backend=PrivilegeBackend.SHIZUKU,
                reset=MagicMock(),
            ),
            _apply_privilege_status=MagicMock(),
            _schedule_privilege_recheck=MagicMock(),
        )

        MainWindow._recover_privilege_status_after_runtime_invalidation(host)

        host.privilege_manager.reset.assert_not_called()
        host._apply_privilege_status.assert_called_once_with(None)
        self.assertIsNone(host._last_automatic_shizuku_key)
        self.assertEqual(host._automatic_shizuku_attempts, {})
        self.assertIsNone(host._automatic_shizuku_failure_status)
        self.assertTrue(host._pending_privilege_recheck)
        host._schedule_privilege_recheck.assert_called_once_with()

    def test_backend_switch_keeps_feature_barrier_until_auto_worker_finishes(
        self,
    ) -> None:
        context = self._context()
        operations = OperationRegistry()
        token = operations.register(
            "privilege-access",
            device_context=context,
            conflict_groups=(
                f"device-exclusive:{context.serial}",
                f"acbridge-maintenance:{context.serial}",
            ),
        )
        backup_manager = MagicMock()
        backup_manager.root = Path("backups")
        device_manager = SimpleNamespace(
            active=DeviceInfo(serial=context.serial, mode="ADB", state="device"),
            operations=operations,
            notify_profile_changed=MagicMock(),
        )
        host = SimpleNamespace(
            _privilege_profile_available=True,
            _last_privilege_backend=PrivilegeBackend.SHIZUKU,
            _privilege_token=token,
            _privilege_operation_kind="automatic-shizuku",
            _automatic_shizuku_inflight_key=(context.serial, context.generation),
            _pending_automatic_shizuku_context=None,
            _automatic_shizuku_scheduled_key=None,
            _automatic_shizuku_attempts={(context.serial, context.generation): 1},
            _automatic_shizuku_failure_status=None,
            _pending_privilege_recheck=False,
            _privilege_recheck_callback_scheduled=False,
            _privilege_barrier_waits_for_recheck=False,
            _closing=False,
            settings=SimpleNamespace(
                active_profile_serial=context.serial,
                active_profile_kind="Phone",
                logs_folder=Path("logs"),
            ),
            device_manager=device_manager,
            device_bar=SimpleNamespace(configure_timer=MagicMock()),
            backup_manager=backup_manager,
            runner=SimpleNamespace(set_logs_folder=MagicMock()),
            logs_page=SimpleNamespace(set_logs_folder=MagicMock()),
            icon_extractor=SimpleNamespace(refresh_root=MagicMock()),
            apps_page=SimpleNamespace(refresh_storage_roots=MagicMock()),
            settings_page=SimpleNamespace(
                reload_from_settings=MagicMock(),
                set_privilege_busy=MagicMock(),
            ),
            dashboard=SimpleNamespace(reload_from_settings=MagicMock()),
            commands_page=SimpleNamespace(
                reload_from_settings=MagicMock(),
                set_privilege_busy=MagicMock(),
            ),
            file_manager_page=SimpleNamespace(reload_from_settings=MagicMock()),
            privilege_mode_selector=SimpleNamespace(set_backend=MagicMock()),
            privilege_manager=SimpleNamespace(
                selected_backend=PrivilegeBackend.ROOT,
                reset=MagicMock(),
                cached_status=MagicMock(return_value=None),
            ),
            _configured_privilege_value=MagicMock(
                return_value=PrivilegeBackend.ROOT
            ),
            _apply_privilege_status=MagicMock(),
            _clear_pending_automatic_shizuku=MagicMock(),
            _set_automatic_shizuku_ui_busy=MagicMock(),
            _resume_feature_refresh_after_acbridge=MagicMock(),
        )

        MainWindow._settings_changed(host)

        self.assertTrue(token.cancelled)
        self.assertTrue(operations.contains(token))
        self.assertEqual(
            host._automatic_shizuku_inflight_key,
            (context.serial, context.generation),
        )
        self.assertTrue(host._pending_privilege_recheck)
        self.assertTrue(host._privilege_barrier_waits_for_recheck)
        host._set_automatic_shizuku_ui_busy.assert_not_called()
        host._resume_feature_refresh_after_acbridge.assert_not_called()

        device_manager.is_context_current = MagicMock(return_value=True)
        host._automatic_shizuku_workflow_pending = (
            lambda: MainWindow._automatic_shizuku_workflow_pending(host)
        )
        callbacks = []
        with patch(
            "openadb.ui.main_window.QTimer.singleShot",
            side_effect=lambda _delay, callback: callbacks.append(callback),
        ):
            MainWindow._privilege_operation_finished(host, token)

        self.assertTrue(operations.contains(token))
        self.assertEqual(len(callbacks), 1)
        host._resume_feature_refresh_after_acbridge.assert_not_called()

        operations.finish(token)
        root_tokens = []

        def start_root_check(*, interactive: bool) -> bool:
            self.assertFalse(interactive)
            root_token = operations.register(
                "privilege-access",
                device_context=context,
                conflict_groups=(f"device-exclusive:{context.serial}",),
            )
            root_tokens.append(root_token)
            host._privilege_token = root_token
            host._privilege_operation_kind = "check"
            host._privilege_operation_interactive = False
            return True

        host.check_privilege_access = start_root_check
        callbacks.pop(0)()
        self.assertEqual(len(root_tokens), 1)
        host._resume_feature_refresh_after_acbridge.assert_not_called()

        host._configured_privilege_value.return_value = PrivilegeBackend.STANDARD
        host.privilege_manager.selected_backend = PrivilegeBackend.STANDARD
        MainWindow._settings_changed(host)

        self.assertTrue(root_tokens[0].cancelled)
        self.assertTrue(operations.contains(root_tokens[0]))
        self.assertTrue(host._privilege_barrier_waits_for_recheck)
        host._resume_feature_refresh_after_acbridge.assert_not_called()

        MainWindow._privilege_operation_finished(host, root_tokens[0])

        self.assertFalse(host._privilege_barrier_waits_for_recheck)
        host._set_automatic_shizuku_ui_busy.assert_called_with(False)
        host._resume_feature_refresh_after_acbridge.assert_called_once_with()

    def test_clean_root_to_standard_switch_gates_pages_until_worker_finishes(
        self,
    ) -> None:
        context = self._context()
        operations = OperationRegistry()
        token = operations.register(
            "privilege-access",
            device_context=context,
            conflict_groups=(f"device-exclusive:{context.serial}",),
        )
        backup_manager = MagicMock()
        backup_manager.root = Path("backups")
        device_manager = SimpleNamespace(
            active=DeviceInfo(serial=context.serial, mode="ADB", state="device"),
            operations=operations,
            notify_profile_changed=MagicMock(),
            is_context_current=MagicMock(return_value=True),
        )
        host = SimpleNamespace(
            _privilege_profile_available=True,
            _last_privilege_backend=PrivilegeBackend.ROOT,
            _privilege_token=token,
            _privilege_operation_kind="check",
            _privilege_operation_interactive=False,
            _last_automatic_shizuku_key=None,
            _automatic_shizuku_inflight_key=None,
            _pending_automatic_shizuku_context=None,
            _automatic_shizuku_scheduled_key=None,
            _automatic_shizuku_attempts={},
            _automatic_shizuku_failure_status=None,
            _automatic_shizuku_ui_busy=False,
            _acbridge_maintenance_ui_busy=False,
            _pending_privilege_recheck=False,
            _privilege_recheck_callback_scheduled=False,
            _privilege_barrier_waits_for_recheck=False,
            _closing=False,
            settings=SimpleNamespace(
                active_profile_serial=context.serial,
                active_profile_kind="Phone",
                logs_folder=Path("logs"),
            ),
            device_manager=device_manager,
            device_bar=SimpleNamespace(configure_timer=MagicMock()),
            backup_manager=backup_manager,
            runner=SimpleNamespace(set_logs_folder=MagicMock()),
            logs_page=SimpleNamespace(set_logs_folder=MagicMock()),
            icon_extractor=SimpleNamespace(refresh_root=MagicMock()),
            apps_page=MagicMock(),
            backups_page=MagicMock(),
            file_manager_page=MagicMock(),
            settings_page=MagicMock(),
            dashboard=MagicMock(),
            commands_page=MagicMock(),
            privilege_mode_selector=SimpleNamespace(set_backend=MagicMock()),
            privilege_manager=SimpleNamespace(
                selected_backend=PrivilegeBackend.STANDARD,
                reset=MagicMock(),
                cached_status=MagicMock(return_value=None),
            ),
            _configured_privilege_value=MagicMock(
                return_value=PrivilegeBackend.STANDARD
            ),
            _apply_privilege_status=MagicMock(),
            _clear_pending_automatic_shizuku=MagicMock(),
            _set_global_privilege_status_text=MagicMock(),
            _resume_feature_refresh_after_acbridge=MagicMock(),
        )
        host._automatic_shizuku_workflow_pending = (
            lambda: MainWindow._automatic_shizuku_workflow_pending(host)
        )
        host._set_automatic_shizuku_ui_busy = (
            lambda busy: MainWindow._set_automatic_shizuku_ui_busy(host, busy)
        )

        MainWindow._settings_changed(host)

        self.assertTrue(token.cancelled)
        self.assertTrue(host._privilege_barrier_waits_for_recheck)
        host.apps_page.setEnabled.assert_called_with(False)
        host.backups_page.setEnabled.assert_called_with(False)
        host.file_manager_page.setEnabled.assert_called_with(False)
        host._resume_feature_refresh_after_acbridge.assert_not_called()

        MainWindow._privilege_operation_finished(host, token)

        self.assertFalse(host._privilege_barrier_waits_for_recheck)
        host.apps_page.setEnabled.assert_called_with(True)
        host.backups_page.setEnabled.assert_called_with(True)
        host.file_manager_page.setEnabled.assert_called_with(True)
        host._resume_feature_refresh_after_acbridge.assert_called_once_with()

    def _automatic_host(self):
        context = self._context()
        device_manager = SimpleNamespace(
            active=DeviceInfo(
                serial=context.serial,
                mode="ADB",
                state="device",
                transport_id=context.transport_id,
            ),
            current_context=context,
            require_context=MagicMock(return_value=context),
        )
        device_manager.is_context_current = MagicMock(
            side_effect=lambda candidate: candidate == device_manager.current_context
        )
        privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.SHIZUKU,
            capture_operation_lease=MagicMock(return_value=object()),
            request_and_check_shizuku=MagicMock(return_value=shizuku_status()),
            cached_status=MagicMock(return_value=None),
        )
        host = SimpleNamespace(
            _closing=False,
            _privilege_token=None,
            _privilege_operation_kind="",
            _pending_privilege_recheck=False,
            _last_automatic_shizuku_key=None,
            _automatic_shizuku_inflight_key=None,
            _pending_automatic_shizuku_context=None,
            _automatic_shizuku_scheduled_key=None,
            _automatic_shizuku_attempts={},
            _automatic_shizuku_failure_status=None,
            _privilege_recheck_callback_scheduled=False,
            _privilege_barrier_waits_for_recheck=False,
            _acbridge_update_token=None,
            _acbridge_update_retry_key=None,
            _pending_acbridge_update_context=None,
            _pending_acbridge_feature_refresh=set(),
            privilege_manager=privilege_manager,
            device_manager=device_manager,
            _start_privilege_operation=MagicMock(return_value=True),
            check_privilege_access=MagicMock(),
            _privilege_callback_is_current=MagicMock(return_value=True),
            _apply_privilege_status=MagicMock(),
            _set_global_privilege_status_text=MagicMock(),
            statusBar=MagicMock(return_value=MagicMock()),
            settings_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            commands_page=SimpleNamespace(set_privilege_busy=MagicMock()),
            apps_page=MagicMock(),
            backups_page=MagicMock(),
            file_manager_page=MagicMock(),
            _resume_feature_refresh_after_acbridge=MagicMock(),
        )
        host._clear_pending_automatic_shizuku = (
            lambda **kwargs: MainWindow._clear_pending_automatic_shizuku(
                host,
                **kwargs,
            )
        )
        host._queue_automatic_shizuku_start = (
            lambda context, **kwargs: MainWindow._queue_automatic_shizuku_start(
                host,
                context,
                **kwargs,
            )
        )
        host._start_automatic_shizuku_handshake = (
            lambda context, key: MainWindow._start_automatic_shizuku_handshake(
                host,
                context,
                key,
            )
        )
        host._automatic_shizuku_workflow_pending = (
            lambda: MainWindow._automatic_shizuku_workflow_pending(host)
        )
        host._set_automatic_shizuku_ui_busy = (
            lambda busy: MainWindow._set_automatic_shizuku_ui_busy(host, busy)
        )
        host._schedule_automatic_shizuku_handshake = (
            lambda **kwargs: MainWindow._schedule_automatic_shizuku_handshake(
                host,
                **kwargs,
            )
        )
        return host, context

    @staticmethod
    def _context() -> DeviceContext:
        return DeviceContext(
            serial="device-1",
            mode="ADB",
            transport_id="7",
            profile_key="device-1",
            profile_kind="Phone",
            profile_path=Path("profile"),
            backups_path=Path("backups"),
            temp_path=Path("temp"),
            logs_path=Path("logs"),
            generation=3,
        )


if __name__ == "__main__":
    unittest.main()
