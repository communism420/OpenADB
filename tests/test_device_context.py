from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QThreadPool, QTimer, Slot
from PySide6.QtWidgets import QApplication

from openadb.core.adb import ADBClient
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.device_context import DeviceContext, DeviceContextUnavailable, WirelessConnectionAttempt
from openadb.core.fastboot import FastbootClient
from openadb.core.operations import OperationConflictError, OperationRegistry
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.ui.workers import Worker, start_worker


_QT_APP: QApplication | None = None


def qt_app() -> QApplication:
    global _QT_APP
    instance = QApplication.instance()
    if instance is None:
        instance = QApplication([])
    if not isinstance(instance, QApplication):
        raise RuntimeError("A QApplication instance is required for the worker lifecycle test")
    _QT_APP = instance
    return instance


def command_result(command: list[str], stdout: str = "") -> CommandResult:
    now = datetime.now()
    return CommandResult(command, 0, stdout, "", 0.0, now, now, True, "Completed")


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command, timeout=None) -> CommandResult:
        recorded = [str(part) for part in command]
        self.commands.append(recorded)
        if recorded[-1:] == ["get-state"]:
            return command_result(recorded, "device\n")
        if "getprop ro.product.model" in " ".join(recorded):
            return command_result(recorded, "Demo\nExample\n16\n36\nphone\n")
        if recorded[-2:] == ["devices", "-l"]:
            return command_result(recorded, "List of devices attached\n")
        return command_result(recorded)

    def run_streaming(self, command, timeout=None, **_kwargs) -> CommandResult:
        return self.run(command, timeout=timeout)


class ContextSettings:
    def __init__(self, root: Path) -> None:
        self.config_dir = root / "global"
        self.active_profile_serial = ""
        self.active_profile_kind = ""
        self.data = {
            "active_device_serial": "",
            "last_connected_device_serial": "",
            "backups_folder": str(self.config_dir / "backups"),
            "temp_folder": str(self.config_dir / "temp"),
            "logs_folder": str(self.config_dir / "logs"),
        }

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value, save: bool = True) -> None:
        self.data[key] = value

    def activate(self, serial: str, kind: str = "Phone") -> None:
        self.active_profile_serial = serial
        self.active_profile_kind = kind
        self.config_dir = self.config_dir.parent / kind / serial
        self.data.update(
            {
                "active_device_serial": serial,
                "backups_folder": str(self.config_dir / "backups"),
                "temp_folder": str(self.config_dir / "temp"),
                "logs_folder": str(self.config_dir / "logs"),
            }
        )


class DiscoveryADB:
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self.devices = list(devices or [])
        self.serial = ""
        self.platform_tools = SimpleNamespace(active=SimpleNamespace(has_adb=True))

    def list_devices(self) -> list[DeviceInfo]:
        return list(self.devices)

    def for_serial(self, serial: str):
        source = self

        class Bound:
            def get_device_info(self) -> DeviceInfo:
                selected = next(item for item in source.devices if item.serial == serial)
                return DeviceInfo(
                    serial=serial,
                    model=selected.model or f"Detailed {serial}",
                    mode="ADB",
                    state="device",
                    form_factor=selected.form_factor or "Phone",
                )

        return Bound()

    def set_serial(self, serial: str) -> None:
        self.serial = serial

    def reconnect_offline_device(self, _serial: str):
        return None


class DiscoveryFastboot:
    def __init__(self, devices: list[DeviceInfo] | None = None) -> None:
        self.devices = list(devices or [])
        self.serial = ""

    def list_devices(self) -> list[DeviceInfo]:
        return list(self.devices)

    def set_serial(self, serial: str) -> None:
        self.serial = serial


def adb_device(serial: str, transport_id: str = "1", model: str = "Demo") -> DeviceInfo:
    return DeviceInfo(
        serial=serial,
        model=model,
        mode="ADB",
        state="device",
        transport_id=transport_id,
        form_factor="Phone",
    )


class DeviceContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = ContextSettings(self.root)
        self.adb = DiscoveryADB([adb_device("A")])
        self.fastboot = DiscoveryFastboot()
        self.manager = DeviceManager(self.adb, self.fastboot, self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def activate_device(self, serial: str = "A", kind: str = "Phone"):
        self.settings.activate(serial, kind)
        self.manager.notify_profile_changed(serial, kind)
        return self.manager.capture_context()

    def test_context_is_frozen_and_captures_profile_paths(self) -> None:
        self.manager.refresh()
        with self.assertRaises(DeviceContextUnavailable):
            self.manager.capture_context()
        context = self.activate_device()
        self.assertEqual(context.serial, "A")
        self.assertEqual(context.profile_key, "A")
        self.assertEqual(context.profile_path, self.settings.config_dir)
        self.assertEqual(context.backups_path, self.settings.config_dir / "backups")
        with self.assertRaises(FrozenInstanceError):
            context.serial = "B"  # type: ignore[misc]

    def test_generation_is_stable_for_same_identity_refresh(self) -> None:
        self.manager.refresh()
        self.activate_device()
        generation = self.manager.current_generation
        self.adb.devices[0].model = "Updated label"
        self.manager.refresh()
        self.assertEqual(self.manager.current_generation, generation)

    def test_generation_changes_for_transport_device_disconnect_and_profile(self) -> None:
        self.manager.refresh()
        original = self.activate_device()

        self.adb.devices = [adb_device("A", transport_id="2")]
        self.manager.refresh()
        self.assertGreater(self.manager.current_generation, original.generation)
        transport_generation = self.manager.current_generation

        self.settings.data["active_device_serial"] = "B"
        self.adb.devices = [adb_device("A", "2"), adb_device("B", "3")]
        self.manager.refresh()
        self.settings.activate("B")
        self.manager.notify_profile_changed("B", "Phone")
        self.assertGreater(self.manager.current_generation, transport_generation)
        device_generation = self.manager.current_generation

        self.adb.devices = []
        self.manager.refresh()
        self.assertGreater(self.manager.current_generation, device_generation)
        disconnected_generation = self.manager.current_generation

        self.manager.invalidate_profile()
        self.assertGreater(self.manager.current_generation, disconnected_generation)

    def test_profile_path_change_invalidates_context_and_registered_operation(self) -> None:
        self.manager.refresh()
        context = self.activate_device()
        token = self.manager.operations.register("apps", device_context=context)
        self.settings.data["temp_folder"] = str(self.root / "replacement-temp")
        self.manager.notify_profile_changed("A", "Phone")
        self.assertTrue(token.cancelled)
        self.assertEqual(token.cancellation_reason, "device profile changed")
        self.assertFalse(self.manager.is_context_current(context))
        self.manager.operations.finish(token)

    def test_refresh_started_before_selection_change_cannot_overwrite_new_device(self) -> None:
        starting_generation = self.manager.current_generation
        self.manager.devices = [adb_device("B", "9")]
        self.manager.choose("B")
        stale = self.manager._commit_refresh_device(adb_device("A", "1"), starting_generation)
        self.assertEqual(stale.serial, "B")
        self.assertEqual(self.manager.active.serial, "B")

    def test_late_detail_refresh_keeps_captured_device_log_folder(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        runner = CommandRunner(self.root / "runner-initial-logs")

        class ContextLoggingADB(DiscoveryADB):
            def __init__(self) -> None:
                super().__init__([adb_device("A")])
                self.block_details = False
                self.contexts: list[DeviceContext] = []
                self.detail_result: CommandResult | None = None

            def for_context(self, context: DeviceContext):
                self.contexts.append(context)
                source = self

                class Bound:
                    def get_device_info(self) -> DeviceInfo:
                        if source.block_details:
                            entered.set()
                            if not release.wait(5):
                                raise RuntimeError("detail refresh synchronization timed out")
                            source.detail_result = runner.for_context(context).run(
                                [sys.executable, "-c", "print('device-a-detail')"]
                            )
                        return adb_device(context.serial, context.transport_id)

                return Bound()

        adb = ContextLoggingADB()
        manager = DeviceManager(adb, self.fastboot, self.settings)
        manager.refresh()
        self.settings.activate("A")
        manager.notify_profile_changed("A", "Phone")
        runner.set_logs_folder(self.settings.config_dir / "logs")
        device_a_logs = self.settings.config_dir / "logs"

        adb.block_details = True
        refresh_errors: list[BaseException] = []

        def refresh_device_a() -> None:
            try:
                manager.refresh()
            except BaseException as exc:  # pragma: no cover - asserted below
                refresh_errors.append(exc)

        refresh_thread = threading.Thread(target=refresh_device_a)
        refresh_thread.start()
        self.assertTrue(entered.wait(5))
        captured = adb.contexts[-1]
        self.assertEqual(captured.serial, "A")
        self.assertEqual(captured.logs_path, device_a_logs)

        manager.devices = [adb_device("B", "2")]
        manager.choose("B")
        self.settings.activate("B")
        manager.notify_profile_changed("B", "Phone")
        device_b_logs = self.settings.config_dir / "logs"
        runner.set_logs_folder(device_b_logs)
        release.set()
        refresh_thread.join(8)

        self.assertFalse(refresh_thread.is_alive())
        self.assertEqual(refresh_errors, [])
        self.assertEqual(manager.active.serial, "B")
        self.assertIsNotNone(adb.detail_result)
        self.assertEqual(adb.detail_result.device_serial, "A")
        self.assertEqual(Path(adb.detail_result.logs_folder), device_a_logs)
        self.assertIn("device-a-detail", (device_a_logs / "openadb.log").read_text("utf-8"))
        self.assertFalse((device_b_logs / "openadb.log").exists())

    def test_parallel_active_updates_keep_legacy_clients_aligned_with_latest_device(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingADB(DiscoveryADB):
            def set_serial(self, serial: str) -> None:
                if serial == "A":
                    entered.set()
                    self.assert_release(release)
                super().set_serial(serial)

            @staticmethod
            def assert_release(event: threading.Event) -> None:
                if not event.wait(3):
                    raise RuntimeError("test synchronization timed out")

        adb = BlockingADB([adb_device("A"), adb_device("B", "2")])
        manager = DeviceManager(adb, self.fastboot, self.settings)
        first = threading.Thread(target=manager._set_active, args=(adb_device("A"),))
        second = threading.Thread(target=manager._set_active, args=(adb_device("B", "2"),))

        first.start()
        self.assertTrue(entered.wait(3))
        second.start()
        release.set()
        first.join(3)
        second.join(3)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(manager.active.serial, "B")
        self.assertEqual(adb.serial, "B")
        self.assertEqual(self.settings.get("last_connected_device_serial"), "B")


class BoundTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = RecordingRunner()
        self.tools = SimpleNamespace(adb_path=Path("adb.exe"), fastboot_path=Path("fastboot.exe"))
        self.adb = ADBClient(self.tools, self.runner)
        self.fastboot = FastbootClient(self.tools, self.runner)

    def test_bound_adb_keeps_original_serial_and_explicit_info_query_does_not_mutate_root(self) -> None:
        self.adb.set_serial("A")
        bound = self.adb.for_serial("A")
        self.adb.set_serial("B")
        bound.run_shell("id")
        info = self.adb.get_device_info("A")
        self.assertEqual(info.serial, "A")
        self.assertEqual(self.adb.serial, "B")
        self.assertEqual(self.runner.commands[-2][1:3], ["-s", "A"])
        self.assertEqual(self.runner.commands[-1][1:3], ["-s", "A"])
        with self.assertRaises(RuntimeError):
            bound.set_serial("B")
        with self.assertRaises(RuntimeError):
            bound.get_state("B")
        with self.assertRaises(RuntimeError):
            bound.run_raw(["devices"], use_serial=False)

    def test_bound_fastboot_keeps_original_serial(self) -> None:
        self.fastboot.set_serial("A")
        bound = self.fastboot.for_serial("A")
        self.fastboot.set_serial("B")
        bound.getvar_all()
        self.assertEqual(self.runner.commands[-1][1:3], ["-s", "A"])
        with self.assertRaises(RuntimeError):
            bound.set_serial("B")
        with self.assertRaises(RuntimeError):
            bound._base(serial="B")
        with self.assertRaises(RuntimeError):
            bound.run_raw(["devices"], use_serial=False)

    def test_global_discovery_ignores_mutable_and_bound_serials(self) -> None:
        self.adb.set_serial("A")
        self.fastboot.set_serial("A")
        self.adb.list_devices()
        self.fastboot.list_devices()
        self.assertNotIn("-s", self.runner.commands[-2])
        self.assertNotIn("-s", self.runner.commands[-1])

    def test_bound_command_logging_keeps_captured_profile_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initial_logs = root / "initial-logs"
            captured_logs = root / "device-a" / "logs"
            runner = CommandRunner(initial_logs)
            context = DeviceContext(
                serial="A",
                mode="ADB",
                transport_id="1",
                profile_key="A",
                profile_kind="Phone",
                profile_path=root / "device-a",
                backups_path=root / "device-a" / "backups",
                temp_path=root / "device-a" / "temp",
                logs_path=captured_logs,
                generation=7,
            )
            bound = runner.for_context(context)
            runner.set_logs_folder(root / "device-b" / "logs")

            result = bound.run([sys.executable, "-c", "print('context-log')"])

            self.assertTrue(result.success, result.stderr)
            self.assertEqual(result.device_serial, "A")
            self.assertEqual(result.device_generation, 7)
            self.assertEqual(Path(result.logs_folder), captured_logs)
            self.assertIn("context-log", (captured_logs / "openadb.log").read_text("utf-8"))
            self.assertFalse((root / "device-b" / "logs" / "openadb.log").exists())

    def test_unwritable_captured_log_path_does_not_replace_command_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_logs = root / "device-b" / "logs"
            unavailable_logs = root / "unavailable-device-logs"
            runner = CommandRunner(root / "initial-logs")
            context = DeviceContext(
                serial="A",
                mode="ADB",
                transport_id="1",
                profile_key="A",
                profile_kind="Phone",
                profile_path=root / "device-a",
                backups_path=root / "device-a" / "backups",
                temp_path=root / "device-a" / "temp",
                logs_path=unavailable_logs,
                generation=7,
            )
            observed: list[CommandResult] = []
            runner.add_listener(observed.append)
            runner.set_logs_folder(current_logs)

            from openadb.core.path_utils import ensure_dir as real_ensure_dir

            def selective_ensure_dir(path):
                if Path(path) == unavailable_logs:
                    raise OSError("device log drive is unavailable")
                return real_ensure_dir(path)

            with patch(
                "openadb.core.command_runner.ensure_dir",
                side_effect=selective_ensure_dir,
            ):
                result = runner.for_context(context).run(
                    [sys.executable, "-c", "print('already-completed')"]
                )

            self.assertTrue(result.success, result.stderr)
            self.assertEqual(result.status, "Success")
            self.assertIn("already-completed", result.stdout)
            self.assertIn("could not be written", result.log_warning)
            self.assertEqual(Path(result.logs_folder), unavailable_logs)
            self.assertFalse((current_logs / "openadb.log").exists())
            self.assertEqual(observed, [result])

    def test_pre_cancelled_streaming_commands_never_create_processes_and_keep_context_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = DeviceContext(
                serial="A",
                mode="ADB",
                transport_id="1",
                profile_key="A",
                profile_kind="Phone",
                profile_path=root / "device-a",
                backups_path=root / "device-a" / "backups",
                temp_path=root / "device-a" / "temp",
                logs_path=root / "device-a" / "logs",
                generation=9,
            )
            runner = CommandRunner(root / "global-logs").for_context(context)
            cancelled = threading.Event()
            cancelled.set()

            with patch("openadb.core.command_runner.subprocess.Popen") as popen:
                binary_result, binary_output = runner.run_binary_output(
                    ["adb", "exec-out"],
                    cancel_event=cancelled,
                )
                results = (
                    binary_result,
                    runner.run_streaming(["adb", "reboot"], cancel_event=cancelled),
                    runner.run_with_input_stream(
                        ["adb", "push"],
                        lambda _stream: None,
                        cancel_event=cancelled,
                    ),
                    runner.run_binary_output_to_file(
                        ["adb", "exec-out"],
                        root / "must-not-exist.bin",
                        cancel_event=cancelled,
                    ),
                    runner.run_binary_output_with_writer(
                        ["adb", "exec-out"],
                        lambda _stream: None,
                        cancel_event=cancelled,
                    ),
                )

            popen.assert_not_called()
            self.assertEqual(binary_output, b"")
            self.assertFalse((root / "must-not-exist.bin").exists())
            for result in results:
                self.assertFalse(result.success)
                self.assertEqual(result.error_type, "cancelled")
                self.assertEqual(result.status, "Cancelled before execution")
                self.assertEqual(result.device_serial, "A")
                self.assertEqual(result.device_generation, 9)
                self.assertEqual(Path(result.logs_folder), context.logs_path)
            self.assertEqual(
                (context.logs_path / "openadb.log").read_text("utf-8").count("Cancelled before execution"),
                5,
            )

    def test_cancel_race_with_zero_exit_never_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = CommandRunner(root / "logs")

            class CancelRaceProcess:
                def __init__(self, cancel_event: threading.Event, *, binary: bool) -> None:
                    self.cancel_event = cancel_event
                    self.returncode = None
                    self.killed = False
                    self.stdin = io.BytesIO()
                    stream_type = io.BytesIO if binary else io.StringIO
                    self.stdout = stream_type()
                    self.stderr = stream_type()

                def poll(self):
                    self.cancel_event.set()
                    return self.returncode

                def kill(self) -> None:
                    # Model the narrow race where the process exits cleanly just
                    # as CommandRunner observes cancellation and asks it to stop.
                    self.killed = True
                    self.returncode = 0

                def wait(self, timeout=None):
                    return 0 if self.returncode is None else self.returncode

            cases = (
                (
                    "streaming",
                    False,
                    lambda event: runner.run_streaming(
                        ["adb", "shell", "true"],
                        cancel_event=event,
                    ),
                ),
                (
                    "input-stream",
                    True,
                    lambda event: runner.run_with_input_stream(
                        ["adb", "exec-in"],
                        lambda _stream: None,
                        cancel_event=event,
                    ),
                ),
                (
                    "binary-file",
                    True,
                    lambda event: runner.run_binary_output_to_file(
                        ["adb", "exec-out"],
                        root / "cancelled-output.bin",
                        cancel_event=event,
                    ),
                ),
                (
                    "binary-writer",
                    True,
                    lambda event: runner.run_binary_output_with_writer(
                        ["adb", "exec-out"],
                        lambda _stream: None,
                        cancel_event=event,
                    ),
                ),
            )

            for name, binary, invoke in cases:
                with self.subTest(api=name):
                    cancel_event = threading.Event()
                    process = CancelRaceProcess(cancel_event, binary=binary)
                    with patch(
                        "openadb.core.command_runner.subprocess.Popen",
                        return_value=process,
                    ):
                        result = invoke(cancel_event)

                    self.assertTrue(process.killed)
                    self.assertFalse(result.success)
                    self.assertEqual(result.error_type, "cancelled")
                    self.assertEqual(result.status, "Cancelled by user")

    def test_shutdown_gate_rejects_every_process_api_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = CommandRunner(root / "logs")
            destination = root / "must-not-exist.bin"
            input_calls = []
            output_calls = []
            runner.shutdown()

            with patch("openadb.core.command_runner.subprocess.Popen") as popen:
                binary_result, binary_output = runner.run_binary_output(["adb", "exec-out"])
                results = (
                    runner.run(["adb", "devices"]),
                    binary_result,
                    runner.run_streaming(["adb", "devices"]),
                    runner.run_with_input_stream(
                        ["adb", "push"],
                        lambda stream: input_calls.append(stream),
                    ),
                    runner.run_binary_output_to_file(
                        ["adb", "exec-out"],
                        destination,
                    ),
                    runner.run_binary_output_with_writer(
                        ["adb", "exec-out"],
                        lambda stream: output_calls.append(stream),
                    ),
                )

            popen.assert_not_called()
            self.assertEqual(binary_output, b"")
            self.assertEqual(input_calls, [])
            self.assertEqual(output_calls, [])
            self.assertFalse(destination.exists())
            for result in results:
                self.assertFalse(result.success)
                self.assertEqual(result.error_type, "shutdown")
                self.assertEqual(result.status, "Command runner is shutting down")
            self.assertEqual(
                (root / "logs" / "openadb.log")
                .read_text("utf-8")
                .count("Command runner is shutting down"),
                6,
            )

    def test_binary_output_cancel_after_spawn_terminates_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = CommandRunner(Path(temporary) / "logs")
            cancel_event = threading.Event()
            communicate_started = threading.Event()
            process_killed = threading.Event()
            captured = []

            class FakeProcess:
                returncode = None

                def communicate(self, timeout=None):
                    communicate_started.set()
                    if process_killed.is_set():
                        return b"", b""
                    raise subprocess.TimeoutExpired(["adb", "exec-out"], timeout)

                def kill(self) -> None:
                    self.returncode = -9
                    process_killed.set()

            with patch(
                "openadb.core.command_runner.subprocess.Popen",
                return_value=FakeProcess(),
            ) as popen:
                worker = threading.Thread(
                    target=lambda: captured.append(
                        runner.run_binary_output(
                            ["adb", "exec-out"],
                            timeout=30,
                            cancel_event=cancel_event,
                        )
                    )
                )
                worker.start()
                self.assertTrue(communicate_started.wait(3))
                cancel_event.set()
                worker.join(3)

            self.assertFalse(worker.is_alive())
            popen.assert_called_once()
            self.assertTrue(process_killed.is_set())
            self.assertEqual(len(captured), 1)
            result, output = captured[0]
            self.assertEqual(output, b"")
            self.assertFalse(result.success)
            self.assertEqual(result.error_type, "cancelled")
            self.assertEqual(result.status, "Cancelled by user")

    def test_shutdown_cannot_miss_a_process_being_created(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = CommandRunner(root / "logs")
            popen_entered = threading.Event()
            allow_popen_return = threading.Event()
            shutdown_requested = threading.Event()
            process_killed = threading.Event()
            results = []

            class FakeProcess:
                returncode = None

                def poll(self):
                    return self.returncode

                def kill(self) -> None:
                    self.returncode = -9
                    process_killed.set()

                def communicate(self, timeout=None):
                    if not process_killed.wait(3):
                        raise RuntimeError("shutdown did not terminate the registered process")
                    return "", ""

            process = FakeProcess()

            def create_process(*_args, **_kwargs):
                popen_entered.set()
                if not allow_popen_return.wait(3):
                    raise RuntimeError("test did not release process creation")
                return process

            def request_shutdown() -> None:
                shutdown_requested.set()
                runner.shutdown()

            with patch(
                "openadb.core.command_runner.subprocess.Popen",
                side_effect=create_process,
            ) as popen:
                run_thread = threading.Thread(
                    target=lambda: results.append(runner.run(["adb", "devices"])),
                )
                run_thread.start()
                self.assertTrue(popen_entered.wait(3))
                shutdown_thread = threading.Thread(target=request_shutdown)
                shutdown_thread.start()
                self.assertTrue(shutdown_requested.wait(3))
                allow_popen_return.set()
                shutdown_thread.join(3)
                run_thread.join(3)

                late_result = runner.run(["adb", "devices"])

            self.assertFalse(shutdown_thread.is_alive())
            self.assertFalse(run_thread.is_alive())
            self.assertTrue(process_killed.is_set())
            self.assertEqual(len(results), 1)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(late_result.error_type, "shutdown")


class OperationRegistryTests(unittest.TestCase):
    def test_conflicts_and_independent_operations(self) -> None:
        registry = OperationRegistry()
        first = registry.register("apps", conflict_group="package-mutation")
        with self.assertRaises(OperationConflictError):
            registry.register("backups", conflict_group="package-mutation")
        independent = registry.register("files", conflict_group="file-listing")
        self.assertEqual(registry.active_count, 2)
        self.assertTrue(registry.finish(first))
        self.assertTrue(registry.finish(independent))
        self.assertEqual(registry.active_count, 0)

    def test_operations_can_share_a_cross_page_device_resource(self) -> None:
        registry = OperationRegistry()
        transfer = registry.register(
            "file-transfer",
            conflict_group="file-manager.transfer",
            conflict_groups=("device-exclusive:serial-a",),
        )
        with self.assertRaisesRegex(OperationConflictError, "device-exclusive:serial-a"):
            registry.register(
                "dashboard-reboot",
                conflict_group="device-command",
                conflict_groups=("device-exclusive:serial-a",),
            )
        other_device = registry.register(
            "other-device-command",
            conflict_group="device-command:serial-b",
            conflict_groups=("device-exclusive:serial-b",),
        )
        self.assertTrue(registry.finish(transfer))
        self.assertTrue(registry.finish(other_device))

    def test_cancel_reasons_context_manager_and_shutdown_cleanup(self) -> None:
        registry = OperationRegistry()
        token = registry.register("apps")
        self.assertTrue(token.cancel("user cancelled"))
        self.assertFalse(token.cancel("later reason"))
        self.assertEqual(token.cancellation_reason, "user cancelled")
        registry.finish(token)

        with registry.tracked("files") as tracked:
            self.assertTrue(registry.contains(tracked))
        self.assertEqual(registry.active_count, 0)

        one = registry.register("one")
        two = registry.register("two")
        self.assertEqual(registry.shutdown(), 2)
        self.assertTrue(one.cancelled)
        self.assertTrue(two.cancelled)
        self.assertEqual(registry.active_count, 0)
        with self.assertRaises(RuntimeError):
            registry.register("late")

    def test_context_free_wireless_attempt_survives_device_generation_change(self) -> None:
        registry = OperationRegistry()
        attempt = WirelessConnectionAttempt(
            attempt_id="attempt-1",
            action="qr",
            scenario="modern",
            expected_host="demo.local",
            expected_pair_port=None,
            expected_connect_port=None,
            pairing_target="",
            connect_target="",
            expected_ready_serials=(),
            started_generation=3,
        )
        token = registry.register("wireless", device_context=None, conflict_group="wireless-connect")
        registry.cancel_stale(4)
        self.assertFalse(token.cancelled)
        self.assertTrue(attempt.expects_host("DEMO.local"))
        self.assertFalse(attempt.accepts_transport("demo.local:37123", "offline"))
        self.assertFalse(attempt.accepts_ready_serial("demo.local:37123"))
        self.assertFalse(attempt.accepts_transport("demo.local:37123", "device"))
        registry.finish(token)

    def test_delayed_older_generation_notification_keeps_newer_token_live(self) -> None:
        registry = OperationRegistry()

        def context(generation: int) -> DeviceContext:
            root = Path("profiles") / f"generation-{generation}"
            return DeviceContext(
                serial="A",
                mode="ADB",
                transport_id=str(generation),
                profile_key="A",
                profile_kind="Phone",
                profile_path=root,
                backups_path=root / "backups",
                temp_path=root / "temp",
                logs_path=root / "logs",
                generation=generation,
            )

        old_token = registry.register("old", device_context=context(0))
        current_token = registry.register("current", device_context=context(2))

        registry.cancel_stale(2, "generation 2 became active")
        self.assertTrue(old_token.cancelled)
        self.assertFalse(current_token.cancelled)

        # A thread that advanced generation 1 may notify after generation 2 is
        # already active. That delayed notification must not cancel future work.
        registry.cancel_stale(1, "delayed generation 1 notification")
        self.assertFalse(current_token.cancelled)

        registry.finish(old_token)
        registry.finish(current_token)

    def test_worker_finalizer_cleans_registry_on_success_error_cancel_and_rejected_start(self) -> None:
        class ImmediatePool:
            @staticmethod
            def start(worker: Worker) -> None:
                worker.run()

        for mode in ("success", "error", "cancel"):
            with self.subTest(mode=mode):
                registry = OperationRegistry()
                token = registry.register("worker")
                if mode == "cancel":
                    token.cancel("test cancellation")

                def operation():
                    if mode == "error":
                        raise RuntimeError("expected")
                    return mode

                owner = QObject()
                worker = Worker(operation)
                self.assertTrue(
                    start_worker(
                        owner,
                        ImmediatePool(),  # type: ignore[arg-type]
                        worker,
                        operation_registry=registry,
                        operation_token=token,
                    )
                )
                self.assertEqual(registry.active_count, 0)

        registry = OperationRegistry()
        token = registry.register("rejected")
        owner = QObject()
        owner._workers_shutting_down = True
        self.assertFalse(
            start_worker(
                owner,
                ImmediatePool(),  # type: ignore[arg-type]
                Worker(lambda: None),
                operation_registry=registry,
                operation_token=token,
            )
        )
        self.assertTrue(token.cancelled)
        self.assertEqual(registry.active_count, 0)

        class FailingPool:
            @staticmethod
            def start(_worker: Worker) -> None:
                raise RuntimeError("pool stopped")

        registry = OperationRegistry()
        token = registry.register("start-failure")
        owner = QObject()
        with self.assertRaisesRegex(RuntimeError, "pool stopped"):
            start_worker(
                owner,
                FailingPool(),  # type: ignore[arg-type]
                Worker(lambda: None),
                operation_registry=registry,
                operation_token=token,
            )
        self.assertTrue(token.cancelled)
        self.assertEqual(token.cancellation_reason, "worker could not be started")
        self.assertEqual(registry.active_count, 0)
        self.assertEqual(owner._active_workers, set())

    def test_queued_result_observes_token_before_finished_cleanup(self) -> None:
        app = qt_app()
        registry = OperationRegistry()
        token = registry.register("queued-worker")
        owner = QObject()
        pool = QThreadPool()
        pool.setMaxThreadCount(1)
        loop = QEventLoop()
        observed: list[bool] = []

        class Receiver(QObject):
            @Slot(object)
            def on_result(self, _result) -> None:
                observed.append(registry.contains(token))

            @Slot()
            def on_finished(self) -> None:
                QTimer.singleShot(0, loop.quit)

        receiver = Receiver()
        worker = Worker(lambda: "done")
        worker.signals.result.connect(receiver.on_result)
        worker.signals.finished.connect(receiver.on_finished)
        self.assertTrue(
            start_worker(
                owner,
                pool,
                worker,
                operation_registry=registry,
                operation_token=token,
            )
        )
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        pool.waitForDone(5000)
        app.processEvents()

        self.assertEqual(observed, [True])
        self.assertEqual(registry.active_count, 0)

    def test_destroyed_worker_signals_use_fallback_registry_cleanup(self) -> None:
        class DestroySignalsPool:
            @staticmethod
            def start(worker: Worker) -> None:
                del worker.signals
                worker.run()

        registry = OperationRegistry()
        token = registry.register("destroyed-signals")
        owner = QObject()
        self.assertTrue(
            start_worker(
                owner,
                DestroySignalsPool(),  # type: ignore[arg-type]
                Worker(lambda: "done"),
                operation_registry=registry,
                operation_token=token,
            )
        )
        self.assertEqual(registry.active_count, 0)
        self.assertEqual(owner._active_workers, set())


if __name__ == "__main__":
    unittest.main()
