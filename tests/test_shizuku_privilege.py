from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

from openadb.core.acbridge import ACBridgeClient
from openadb.core.adb_transfer_strategy import ADBTransferStrategy
from openadb.core.device_context import DeviceContext, StaleDeviceContext
from openadb.core.privilege import (
    PrivilegeBackend,
    PrivilegeManager,
    PrivilegeStatus,
    RootAwareADBClient,
    RootExecutionStrategy,
    ShizukuAwareADBClient,
)
from openadb.core.settings_manager import SettingsManager
from openadb.core.shizuku import (
    MAX_DESKTOP_OUTPUT_BYTES,
    ShizukuClient,
    ShizukuExecutionSession,
    ShizukuState,
)
from openadb.models.command_result import CommandResult


def command_result(
    *,
    success: bool = True,
    stdout: str = "",
    stderr: str = "",
    status: str = "",
    exit_code: int | None = None,
    error_type: str = "",
) -> CommandResult:
    now = datetime.now(timezone.utc)
    if exit_code is None and success:
        exit_code = 0
    return CommandResult(
        command=["adb", "<test>"],
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration=0.0,
        started_at=now,
        finished_at=now,
        success=success,
        status=status,
        error_type=error_type,
    )


class FakeBridge:
    def __init__(self, installed: bool = True, message: str = "ready") -> None:
        self.installed = installed
        self.message = message
        self.calls = 0
        self.verify_calls = 0
        self.permission_host_calls: list[tuple[str, dict[str, object]]] = []
        self.dismissed_permission_hosts: list[str] = []

    def ensure_installed(self, **_kwargs) -> tuple[bool, str]:
        self.calls += 1
        return self.installed, self.message

    def verify_bundled_apk(self, **_kwargs) -> tuple[bool, str]:
        self.verify_calls += 1
        return self.installed, self.message

    def start_permission_host(self, backend: str, **kwargs):
        self.permission_host_calls.append((backend, kwargs))
        return SimpleNamespace(
            backend=backend,
            request_id="01" * 16,
            started=self.installed,
            message=self.message,
        )

    def dismiss_permission_host(self, request_id: str) -> bool:
        self.dismissed_permission_hosts.append(request_id)
        return True


class RecordingADB:
    def __init__(self) -> None:
        self.serial = "device-a"
        self.device_context = SimpleNamespace(
            generation=7,
            logs_path=Path("device-a/logs"),
        )
        self.shell_commands: list[str] = []
        self.raw_commands: list[list[str]] = []
        self.request_payloads: list[bytes] = []
        self.wait_results: list[CommandResult] = []
        self.stdout_payload = b""
        self.stderr_payload = b""

    def run_shell(self, command: str, **_kwargs) -> CommandResult:
        self.shell_commands.append(command)
        if command.startswith("result="):
            if self.wait_results:
                return self.wait_results.pop(0)
            return command_result(
                success=False,
                stderr="result timeout",
                status="result timeout",
                exit_code=124,
            )
        return command_result()

    def run_raw_with_input_stream(self, command, *, input_writer, **_kwargs) -> CommandResult:
        self.raw_commands.append([str(part) for part in command])
        stream = io.BytesIO()
        input_writer(stream)
        self.request_payloads.append(stream.getvalue())
        return command_result()

    def run_raw_binary_output(self, command, **_kwargs):
        recorded = [str(part) for part in command]
        self.raw_commands.append(recorded)
        path = recorded[-1]
        payload = self.stderr_payload if path.endswith(".stderr") else self.stdout_payload
        return command_result(), payload


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


def device_context(serial: str = "device-a", generation: int = 7) -> DeviceContext:
    root = Path("profiles") / serial
    return DeviceContext(
        serial=serial,
        mode="ADB",
        transport_id="1",
        profile_key=serial,
        profile_kind="Phone",
        profile_path=root,
        backups_path=root / "backups",
        temp_path=root / "temp",
        logs_path=root / "logs",
        generation=generation,
    )


class ShizukuStateTests(unittest.TestCase):
    def test_ready_identity_distinguishes_shell_from_root(self) -> None:
        shell = ShizukuState(
            state="ready",
            installed=True,
            running=True,
            permission="granted",
            uid=2000,
            mode="shell",
        )
        root = ShizukuState(
            state="ready",
            installed=True,
            running=True,
            permission="granted",
            uid=0,
            mode="root",
        )

        self.assertTrue(shell.ready)
        self.assertTrue(shell.shell)
        self.assertFalse(shell.root)
        self.assertEqual(shell.display_name, "Shizuku shell (UID 2000)")
        self.assertTrue(root.ready)
        self.assertTrue(root.root)
        self.assertFalse(root.shell)
        self.assertEqual(root.display_name, "Shizuku root (UID 0)")

    def test_ready_requires_live_binder_permission_and_known_uid(self) -> None:
        cases = (
            ShizukuState(state="ready", running=False, permission="granted", uid=2000),
            ShizukuState(state="ready", running=True, permission="denied", uid=2000),
            ShizukuState(state="ready", running=True, permission="granted", uid=1000),
            ShizukuState(state="stopped", running=True, permission="granted", uid=2000),
        )
        for state in cases:
            with self.subTest(state=state):
                self.assertFalse(state.ready)
                self.assertFalse(state.root)
                self.assertFalse(state.shell)


class ACBridgeTrustTests(unittest.TestCase):
    def _client_for_apk(self, apk: Path, installed_bytes: bytes) -> tuple[ACBridgeClient, object]:
        adb = SimpleNamespace(
            run_shell=MagicMock(
                return_value=command_result(
                    stdout="package:/data/app/com.communism420.acbridge/base.apk\n"
                )
            ),
            run_raw=MagicMock(
                return_value=command_result(stdout=str(len(installed_bytes)))
            ),
            run_raw_binary_output=MagicMock(
                return_value=(command_result(), installed_bytes)
            ),
        )
        client = ACBridgeClient(
            adb,
            SimpleNamespace(temp_folder=apk.parent),
            temp_folder=apk.parent,
        )
        client.bundled_apk_path = MagicMock(return_value=apk)  # type: ignore[method-assign]
        return client, adb

    def test_exact_bundled_apk_is_required_for_privileged_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            payload = b"signed-test-apk"
            apk.write_bytes(payload)
            client, adb = self._client_for_apk(apk, payload)

            trusted, message = client.verify_bundled_apk()

        self.assertTrue(trusted, message)
        adb.run_raw.assert_called_once_with(
            [
                "shell",
                "stat",
                "-c",
                "%s",
                "/data/app/com.communism420.acbridge/base.apk",
            ],
            timeout=10,
            cancel_event=None,
        )
        adb.run_raw_binary_output.assert_called_once_with(
            [
                "exec-out",
                "cat",
                "/data/app/com.communism420.acbridge/base.apk",
            ],
            timeout=30,
            cancel_event=None,
        )

    def test_different_same_size_apk_is_rejected_before_shizuku_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"trusted-artifact")
            client, _adb = self._client_for_apk(apk, b"foreign-artifact")

            trusted, message = client.verify_bundled_apk()

        self.assertFalse(trusted)
        self.assertIn("not the exact helper", message)

    def test_ambiguous_installed_base_apk_is_rejected_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"trusted-artifact")
            client, adb = self._client_for_apk(apk, b"trusted-artifact")
            adb.run_shell.return_value = command_result(
                stdout=(
                    "package:/data/app/one/base.apk\n"
                    "package:/data/app/two/base.apk\n"
                )
            )

            trusted, message = client.verify_bundled_apk()

        self.assertFalse(trusted)
        self.assertIn("identify one", message)
        adb.run_raw_binary_output.assert_not_called()

    def test_unsafe_package_path_is_never_forwarded_to_remote_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"trusted-artifact")
            client, adb = self._client_for_apk(apk, b"trusted-artifact")
            adb.run_shell.return_value = command_result(
                stdout="package:/data/app/bridge;id/base.apk\n"
            )

            trusted, message = client.verify_bundled_apk()

        self.assertFalse(trusted)
        self.assertIn("unsafe", message)
        adb.run_raw.assert_not_called()
        adb.run_raw_binary_output.assert_not_called()

    def test_extra_package_split_is_rejected_even_when_base_name_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"trusted-artifact")
            client, adb = self._client_for_apk(apk, b"trusted-artifact")
            adb.run_shell.return_value = command_result(
                stdout=(
                    "package:/data/app/bridge/base.apk\n"
                    "package:/data/app/bridge/split_evil.apk\n"
                )
            )

            trusted, message = client.verify_bundled_apk()

        self.assertFalse(trusted)
        self.assertIn("monolithic", message)
        adb.run_raw.assert_not_called()
        adb.run_raw_binary_output.assert_not_called()


class ShizukuProtocolTests(unittest.TestCase):
    def make_client(self, adb: RecordingADB | None = None) -> tuple[ShizukuClient, RecordingADB]:
        adb = adb or RecordingADB()
        settings = SimpleNamespace(temp_folder=Path("temp"))
        client = ShizukuClient(adb, settings, temp_folder=Path("temp"))
        client.bridge = FakeBridge()
        return client, adb

    def test_status_parser_accepts_noise_and_preserves_equals_in_values(self) -> None:
        fields = ShizukuClient._parse_protocol(
            "ignored output\n"
            "OPENADB_SHIZUKU_STATUS 1\n"
            "state=ready\n"
            "message_b64=YWJjPT0=\n"
            "invalid key=ignored\n",
            ShizukuClient.STATUS_PREFIX,
        )

        self.assertEqual(
            fields,
            {
                "state": "ready",
                "message_b64": "YWJjPT0=",
            },
        )
        self.assertIsNone(
            ShizukuClient._parse_protocol(
                "OPENADB_SHIZUKU_STATUS 2\nstate=ready\n",
                ShizukuClient.STATUS_PREFIX,
            )
        )

    def test_protocol_responses_are_bound_to_request_id_and_expected_uid(self) -> None:
        request_id = "ab" * 16
        status = {
            "request_id": request_id,
            "state": "ready",
            "installed": "1",
            "binder": "1",
            "permission": "granted",
            "uid": "2000",
            "mode": "shell",
            "api": "13",
        }
        result = {
            "request_id": request_id,
            "state": "complete",
            "exit_code": "0",
            "uid": "2000",
            "timed_out": "0",
            "cancelled": "0",
        }

        self.assertTrue(
            ShizukuClient._valid_status_fields(status, request_id=request_id)
        )
        self.assertFalse(
            ShizukuClient._valid_status_fields(status, request_id="cd" * 16)
        )
        self.assertTrue(
            ShizukuClient._valid_result_fields(
                result,
                request_id=request_id,
                expected_uid=2000,
            )
        )
        self.assertFalse(
            ShizukuClient._valid_result_fields(
                result,
                request_id=request_id,
                expected_uid=0,
            )
        )

    def test_check_status_maps_protocol_to_ready_shell_state(self) -> None:
        client, adb = self.make_client()
        message = "Ready through official Shizuku API"
        adb.wait_results.append(
            command_result(
                stdout=(
                    "OPENADB_SHIZUKU_STATUS 1\n"
                    f"request_id={'ab' * 16}\n"
                    "state=ready\n"
                    "installed=true\n"
                    "binder=1\n"
                    "permission=granted\n"
                    "uid=2000\n"
                    "mode=root\n"
                    "api=13\n"
                    f"message_b64={base64.b64encode(message.encode()).decode()}\n"
                )
            )
        )

        with patch("openadb.core.shizuku.uuid.uuid4", return_value=SimpleNamespace(hex="ab" * 16)):
            state = client.check_status(timeout=10)

        self.assertTrue(state.ready)
        self.assertTrue(state.shell)
        self.assertEqual(state.mode, "shell")
        self.assertEqual(state.api_version, 13)
        self.assertEqual(state.message, message)
        prepare_index = next(
            index
            for index, command in enumerate(adb.shell_commands)
            if ".shizuku_status_" in command
            and "content delete --uri" in command
        )
        start_index = next(
            index
            for index, command in enumerate(adb.shell_commands)
            if command.startswith("am start")
        )
        self.assertLess(prepare_index, start_index)
        self.assertIn("rm -f", adb.shell_commands[prepare_index])
        self.assertNotIn("mkdir -p", adb.shell_commands[prepare_index])
        self.assertNotIn("umask 000", adb.shell_commands[prepare_index])
        self.assertNotIn("chmod 0666", adb.shell_commands[prepare_index])
        start = next(command for command in adb.shell_commands if command.startswith("am start"))
        self.assertIn("--es operation 'status'", start)
        self.assertIn("--es request_id '" + "ab" * 16 + "'", start)
        self.assertTrue(any(command.startswith("rm -f ") for command in adb.shell_commands))
        wait = next(command for command in adb.shell_commands if command.startswith("result="))
        self.assertIn("content read --uri", wait)
        self.assertIn("/shizuku/" + "ab" * 16, wait)

    def test_status_uses_authenticated_legacy_result_when_precreate_fails(self) -> None:
        client, adb = self.make_client()
        request_id = "af" * 16
        failed = command_result(
            success=False,
            stderr="Shell cannot pre-create the scoped-storage target.",
            status="Command failed with exit code 1",
            exit_code=1,
        )
        adb.wait_results.append(
            command_result(
                stdout=(
                    "OPENADB_SHIZUKU_STATUS 1\n"
                    f"request_id={request_id}\n"
                    "state=ready\n"
                    "installed=true\n"
                    "binder=1\n"
                    "permission=granted\n"
                    "uid=2000\n"
                    "mode=shell\n"
                    "api=13\n"
                )
            )
        )

        with (
            patch.object(client, "_prepare_status", return_value=failed),
            patch(
                "openadb.core.shizuku.uuid.uuid4",
                return_value=SimpleNamespace(hex=request_id),
            ),
        ):
            state = client.check_status(timeout=10)

        self.assertTrue(state.ready)
        self.assertTrue(
            any("--es operation 'status'" in command for command in adb.shell_commands)
        )

    def test_granted_permission_result_is_immediately_ready(self) -> None:
        client, adb = self.make_client()
        adb.wait_results.append(
            command_result(
                stdout=(
                    "OPENADB_SHIZUKU_STATUS 1\n"
                    f"request_id={'bc' * 16}\n"
                    "state=permission_granted\n"
                    "installed=true\n"
                    "binder=true\n"
                    "permission=granted\n"
                    "uid=2000\n"
                    "mode=shell\n"
                    "api=13\n"
                )
            )
        )

        with patch(
            "openadb.core.shizuku.uuid.uuid4",
            return_value=SimpleNamespace(hex="bc" * 16),
        ):
            state = client.request_permission(timeout=30)

        self.assertEqual(state.state, "ready")
        self.assertTrue(state.ready)
        self.assertTrue(state.shell)
        permission_start = next(
            command
            for command in adb.shell_commands
            if "/.ShizukuActivity" in command
            and "--es operation 'requestPermission'" in command
        )
        self.assertTrue(permission_start.startswith("am start -n"))
        self.assertNotIn("am start -W", permission_start)
        self.assertIn(
            "--es permission_host_request_id '" + "01" * 16 + "'",
            permission_start,
        )
        self.assertEqual(client.bridge.dismissed_permission_hosts, ["01" * 16])

    def test_permission_host_is_dismissed_when_shizuku_request_raises(self) -> None:
        client, _adb = self.make_client()
        client._ensure_trusted_bridge = MagicMock(return_value=(True, "current"))
        client._status_operation = MagicMock(
            side_effect=RuntimeError("request failed")
        )

        with self.assertRaisesRegex(RuntimeError, "request failed"):
            client.request_permission_then_check(
                request_timeout=60,
                check_timeout=15,
            )

        self.assertEqual(client.bridge.dismissed_permission_hosts, ["01" * 16])

    def test_verified_ready_state_creates_session_without_device_io(self) -> None:
        client, adb = self.make_client()
        state = self._ready_shell_state()

        session = client.session_from_verified_state(state, expected_uid=2000)

        self.assertTrue(session.ready)
        self.assertEqual(session.expected_uid, 2000)
        self.assertEqual(adb.shell_commands, [])
        with self.assertRaisesRegex(ValueError, "does not match"):
            client.session_from_verified_state(state, expected_uid=0)
        with self.assertRaisesRegex(ValueError, "ready"):
            client.session_from_verified_state(ShizukuState())

    def test_shizuku_wait_polling_uses_wall_clock_deadlines(self) -> None:
        client, adb = self.make_client()

        client._wait_for_file("/data/local/tmp/result", timeout=7)
        client._wait_for_execution_result(
            {"result": "/data/local/tmp/result", "status": "/data/local/tmp/status"},
            timeout=9,
        )

        wait_commands = [command for command in adb.shell_commands if command.startswith("result=")]
        self.assertEqual(len(wait_commands), 2)
        self.assertIn("deadline=$(( $(date +%s) + 7 ))", wait_commands[0])
        self.assertIn("deadline=$(( $(date +%s) + 9 ))", wait_commands[1])
        self.assertTrue(all("ticks=" not in command for command in wait_commands))
        self.assertTrue(all('sleep "$delay"' in command for command in wait_commands))
        self.assertTrue(
            all('cat "$result" && exit 0' in command for command in wait_commands)
        )
        self.assertTrue(
            all("ADB shell cannot read it" in command for command in wait_commands)
        )

    def test_permission_request_then_check_is_serialized_and_trusts_bridge_once(self) -> None:
        client, _adb = self.make_client()
        requested = ShizukuState(
            state="ready",
            installed=True,
            running=True,
            permission="granted",
            uid=2000,
            mode="shell",
            message="Permission granted.",
        )
        verified = ShizukuState(
            state="ready",
            installed=True,
            running=True,
            permission="granted",
            uid=2000,
            mode="shell",
            message="Verified.",
        )
        client._ensure_trusted_bridge = MagicMock(return_value=(True, "current"))
        client._status_operation = MagicMock(
            side_effect=(requested, verified)
        )

        state = client.request_permission_then_check(
            request_timeout=60,
            check_timeout=15,
        )

        self.assertIs(state, verified)
        client._ensure_trusted_bridge.assert_called_once_with(cancel_event=None)
        self.assertEqual(
            [call.args[0] for call in client._status_operation.call_args_list],
            ["requestPermission", "status"],
        )
        self.assertTrue(
            all(
                call.kwargs["bridge_is_trusted"]
                for call in client._status_operation.call_args_list
            )
        )
        self.assertEqual(
            client._status_operation.call_args_list[0].kwargs[
                "permission_host_request_id"
            ],
            "01" * 16,
        )
        self.assertNotIn(
            "permission_host_request_id",
            client._status_operation.call_args_list[1].kwargs,
        )
        self.assertEqual(client.bridge.permission_host_calls[0][0], "shizuku")
        self.assertEqual(client.bridge.dismissed_permission_hosts, ["01" * 16])

    def test_permission_check_waits_for_host_closed_ack_before_passive_status(self) -> None:
        client, _adb = self.make_client()
        requested = self._ready_shell_state()
        verified = replace(requested, message="Verified after host closure.")
        events: list[str] = []
        client._ensure_trusted_bridge = MagicMock(return_value=(True, "current"))

        def status_operation(operation: str, **_kwargs) -> ShizukuState:
            events.append(operation)
            return requested if operation == "requestPermission" else verified

        def dismiss_host(_request_id: str) -> bool:
            events.append("closed")
            return True

        client._status_operation = MagicMock(side_effect=status_operation)
        client.bridge.dismiss_permission_host = MagicMock(side_effect=dismiss_host)

        state = client.request_permission_then_check(
            request_timeout=60,
            check_timeout=15,
        )

        self.assertIs(state, verified)
        self.assertEqual(events, ["requestPermission", "closed", "status"])

    def test_permission_check_does_not_start_status_without_host_closed_ack(self) -> None:
        client, _adb = self.make_client()
        requested = self._ready_shell_state()
        client._ensure_trusted_bridge = MagicMock(return_value=(True, "current"))
        client._status_operation = MagicMock(return_value=requested)
        client.bridge.dismiss_permission_host = MagicMock(return_value=False)

        state = client.request_permission_then_check(
            request_timeout=60,
            check_timeout=15,
        )

        self.assertIs(state, requested)
        self.assertEqual(
            [call.args[0] for call in client._status_operation.call_args_list],
            ["requestPermission"],
        )
        self.assertEqual(client.bridge.dismiss_permission_host.call_count, 2)

    def test_cancelled_permission_result_closes_host_before_returning(self) -> None:
        client, _adb = self.make_client()
        requested = ShizukuState(
            state="cancelled",
            permission="unknown",
            message="Cancelled.",
        )
        events: list[str] = []
        client._ensure_trusted_bridge = MagicMock(return_value=(True, "current"))

        def status_operation(operation: str, **_kwargs) -> ShizukuState:
            events.append(operation)
            return requested

        def dismiss_host(_request_id: str) -> bool:
            events.append("closed")
            return True

        client._status_operation = MagicMock(side_effect=status_operation)
        client.bridge.dismiss_permission_host = MagicMock(side_effect=dismiss_host)

        state = client.request_permission_then_check(
            request_timeout=60,
            check_timeout=15,
        )

        self.assertIs(state, requested)
        self.assertEqual(events, ["requestPermission", "closed"])

    def test_permission_handshake_uses_idempotent_android_request_for_existing_grant(self) -> None:
        client, _adb = self.make_client()
        ready = self._ready_shell_state()
        client._ensure_trusted_bridge = MagicMock(return_value=(True, "current"))
        client._status_operation = MagicMock(side_effect=(ready, ready))

        state = client.request_permission_then_check(
            request_timeout=60,
            check_timeout=15,
        )

        self.assertIs(state, ready)
        self.assertEqual(
            [call.args[0] for call in client._status_operation.call_args_list],
            ["requestPermission", "status"],
        )
        self.assertEqual(client.bridge.dismissed_permission_hosts, ["01" * 16])

    def test_permission_handshake_verifies_terminal_request_states(
        self,
    ) -> None:
        for terminal_state in (
            "not_installed",
            "stopped",
            "permission_denied",
            "unsupported",
            "error",
        ):
            with self.subTest(state=terminal_state):
                client, _adb = self.make_client()
                observed = ShizukuState(
                    state=terminal_state,
                    installed=terminal_state != "not_installed",
                    running=terminal_state
                    in {"permission_denied", "unsupported", "error"},
                    permission="denied"
                    if terminal_state == "permission_denied"
                    else "unknown",
                    message=f"Observed {terminal_state}.",
                )
                client._ensure_trusted_bridge = MagicMock(
                    return_value=(True, "current")
                )
                client._status_operation = MagicMock(
                    side_effect=(observed, observed)
                )

                state = client.request_permission_then_check(
                    request_timeout=60,
                    check_timeout=15,
                )

                self.assertIs(state, observed)
                self.assertEqual(
                    [
                        call.args[0]
                        for call in client._status_operation.call_args_list
                    ],
                    ["requestPermission", "status"],
                )
                self.assertEqual(
                    client.bridge.dismissed_permission_hosts,
                    ["01" * 16],
                )

    def test_status_normalizes_denied_unsupported_and_inconsistent_ready(self) -> None:
        common = {
            "installed": "1",
            "binder": "1",
            "uid": "-1",
            "mode": "unavailable",
            "api": "13",
        }

        denied = ShizukuClient._state_from_fields(
            {
                **common,
                "state": "permission_required",
                "permission": "denied",
            }
        )
        unsupported = ShizukuClient._state_from_fields(
            {
                **common,
                "state": "permission_required",
                "permission": "unsupported",
            }
        )
        inconsistent = ShizukuClient._state_from_fields(
            {
                **common,
                "state": "ready",
                "permission": "required",
                "uid": "2000",
            }
        )

        self.assertEqual(denied.state, "permission_denied")
        self.assertEqual(denied.permission, "denied")
        self.assertIn("denied", denied.message.casefold())
        self.assertEqual(unsupported.state, "unsupported")
        self.assertEqual(unsupported.permission, "unsupported")
        self.assertEqual(inconsistent.state, "error")
        self.assertFalse(inconsistent.ready)
        self.assertIn("inconsistent", inconsistent.message.casefold())
        self.assertNotIn("is ready", inconsistent.message.casefold())
        self.assertFalse(PrivilegeStatus.from_shizuku(inconsistent).available)

    def test_check_status_rejects_invalid_protocol(self) -> None:
        client, adb = self.make_client()
        adb.wait_results.append(
            command_result(success=False, stderr="bad status", status="bad status", exit_code=1)
        )

        state = client.check_status()

        self.assertEqual(state.state, "error")
        self.assertEqual(state.message, "bad status")
        self.assertFalse(state.ready)
        self.assertTrue(
            any(
                "--es operation 'cancel'" in command
                for command in adb.shell_commands
            )
        )

    def test_unreadable_status_reports_shell_error_instead_of_generic_exit(self) -> None:
        client, adb = self.make_client()
        adb.wait_results.append(
            command_result(
                success=False,
                stderr="Shizuku result exists but ADB shell cannot read it.",
                status="Command failed with exit code 13",
                exit_code=13,
            )
        )

        state = client.check_status()

        self.assertEqual(state.state, "error")
        self.assertEqual(
            state.message,
            "Shizuku result exists but ADB shell cannot read it.",
        )

    def test_status_start_failure_sends_cancel_before_cleanup(self) -> None:
        client, _adb = self.make_client()
        operations: list[str] = []

        def start(operation: str, *_args, **_kwargs) -> CommandResult:
            operations.append(operation)
            return command_result(
                success=False,
                status="Activity start timed out",
                exit_code=1,
            )

        with (
            patch.object(client, "_start_activity", side_effect=start),
            patch(
                "openadb.core.shizuku.uuid.uuid4",
                return_value=SimpleNamespace(hex="ae" * 16),
            ),
        ):
            state = client.check_status()

        self.assertEqual(state.state, "error")
        self.assertEqual(operations, ["status", "cancel"])

    def test_unavailable_protocol_state_distinguishes_absent_from_stopped(self) -> None:
        common = {
            "state": "unavailable",
            "binder": "0",
            "permission": "unknown",
            "uid": "-1",
            "mode": "unavailable",
            "api": "-1",
        }

        absent = ShizukuClient._state_from_fields({**common, "installed": "0"})
        stopped = ShizukuClient._state_from_fields({**common, "installed": "1"})

        self.assertEqual(absent.state, "not_installed")
        self.assertIn("not installed", absent.message)
        self.assertEqual(stopped.state, "stopped")
        self.assertIn("not running", stopped.message)

    def test_open_manager_falls_back_from_shizuku_to_sui(self) -> None:
        client, adb = self.make_client()
        adb.run_shell = MagicMock(
            side_effect=(
                command_result(success=False, status="not found", exit_code=1),
                command_result(stdout="package:/data/app/rikka.sui/base.apk\n"),
                command_result(),
            )
        )

        result = client.open_manager()

        self.assertTrue(result.success)
        self.assertEqual(result.status, "Opened Sui on Android.")
        commands = [call.args[0] for call in adb.run_shell.call_args_list]
        self.assertIn("moe.shizuku.privileged.api", commands[0])
        self.assertIn("rikka.sui", commands[1])
        self.assertIn("rikka.sui", commands[2])

    def test_cancelled_permission_wait_signals_android_activity_and_cleans_status(self) -> None:
        client, adb = self.make_client()
        cancelled = threading.Event()

        def cancel_wait(*_args, **_kwargs) -> CommandResult:
            cancelled.set()
            return command_result(
                success=False,
                status="cancelled",
                exit_code=1,
            )

        with (
            patch.object(client, "_wait_for_file", side_effect=cancel_wait),
            patch(
                "openadb.core.shizuku.uuid.uuid4",
                return_value=SimpleNamespace(hex="ac" * 16),
            ),
        ):
            state = client.request_permission(cancel_event=cancelled)

        self.assertEqual(state.state, "cancelled")
        cancel_command = next(
            command
            for command in adb.shell_commands
            if "--es operation 'cancel'" in command
        )
        self.assertIn("--es request_id '" + "ac" * 16 + "'", cancel_command)
        self.assertTrue(
            any(
                command.startswith("rm -f ")
                and ".shizuku_status_" in command
                for command in adb.shell_commands
            )
        )

    def test_request_payload_is_bounded_and_round_trips_utf8_arguments(self) -> None:
        arguments = ["/system/bin/sh", "-c", "echo привет"]
        payload = ShizukuClient._request_payload(arguments).decode("ascii")
        encoded = [line.split("=", 1)[1] for line in payload.splitlines() if line.startswith("arg_b64=")]

        self.assertEqual(payload.splitlines()[0], "OPENADB_SHIZUKU_REQUEST 1")
        self.assertEqual(payload.splitlines()[1], "expected_uid=2000")
        self.assertEqual(payload.splitlines()[2], "argv_count=3")
        self.assertEqual(
            [base64.b64decode(value).decode("utf-8") for value in encoded],
            arguments,
        )
        self.assertEqual(ShizukuClient._validate_arguments(arguments), "")
        self.assertIn("too many arguments", ShizukuClient._validate_arguments(["x"] * 33))
        self.assertIn("NUL", ShizukuClient._validate_arguments(["bad\x00argument"]))
        self.assertIn("64 KiB", ShizukuClient._validate_arguments(["x" * (64 * 1024 + 1)]))
        self.assertIn(
            "encoded safety limit",
            ShizukuClient._validate_arguments(["x" * 50_000, "y" * 50_000]),
        )

    def test_execute_shell_keeps_raw_command_out_of_intent_and_result_log_identity(self) -> None:
        client, adb = self.make_client()
        secret_command = "echo private-command-marker-92fdb1"
        adb.stdout_payload = b"safe output\n"
        adb.wait_results.append(
            command_result(
                stdout=(
                    "OPENADB_SHIZUKU_RESULT 1\n"
                    f"request_id={'cd' * 16}\n"
                    "state=complete\n"
                    "exit_code=0\n"
                    "uid=2000\n"
                    "mode=shell\n"
                    "timed_out=false\n"
                    "cancelled=false\n"
                    "stdout_truncated=false\n"
                    "stderr_truncated=false\n"
                )
            )
        )

        with (
            patch.object(client, "check_status", return_value=self._ready_shell_state()),
            patch("openadb.core.shizuku.uuid.uuid4", return_value=SimpleNamespace(hex="cd" * 16)),
        ):
            result = client.execute_shell(secret_command, timeout=15, expected_uid=2000)

        self.assertTrue(result.success, result.stderr)
        self.assertEqual(result.stdout, "safe output\n")
        self.assertEqual(result.command, ["shizuku", "shell", "<protected request>"])
        self.assertNotIn(secret_command, result.command_text)
        self.assertEqual(len(adb.request_payloads), 1)
        payload = adb.request_payloads[0]
        self.assertNotIn(secret_command.encode(), payload)
        encoded_arguments = [
            line.split(b"=", 1)[1]
            for line in payload.splitlines()
            if line.startswith(b"arg_b64=")
        ]
        self.assertEqual(base64.b64decode(encoded_arguments[-1]).decode(), secret_command)
        visible_transport = "\n".join(adb.shell_commands) + repr(adb.raw_commands)
        self.assertNotIn(secret_command, visible_transport)
        start = next(command for command in adb.shell_commands if command.startswith("am start"))
        self.assertNotIn("--es command", start)
        self.assertIn("--es request_id '" + "cd" * 16 + "'", start)
        status_prepare_index = next(
            index
            for index, command in enumerate(adb.shell_commands)
            if ".shizuku_status_" + "cd" * 16 in command
            and "content delete --uri" in command
        )
        start_index = adb.shell_commands.index(start)
        self.assertLess(status_prepare_index, start_index)
        self.assertNotIn("mkdir -p", adb.shell_commands[status_prepare_index])
        self.assertNotIn("umask 000", adb.shell_commands[status_prepare_index])
        self.assertNotIn("chmod 0666", adb.shell_commands[status_prepare_index])
        self.assertTrue(
            any(
                command.startswith("rm -f ")
                and ".shizuku_status_" + "cd" * 16 in command
                and ".tmp" in command
                and "content delete --uri" in command
                and "/shizuku/" + "cd" * 16 in command
                for command in adb.shell_commands
            )
        )

    def test_execution_uses_legacy_result_when_status_channel_prepare_fails(self) -> None:
        client, adb = self.make_client()
        request_id = "cf" * 16
        failed = command_result(
            success=False,
            stderr="Scoped-storage status channel is unavailable.",
            status="Command failed with exit code 1",
            exit_code=1,
        )
        adb.stdout_payload = b"legacy-compatible output\n"
        adb.wait_results.append(
            command_result(
                stdout=(
                    "OPENADB_SHIZUKU_RESULT 1\n"
                    f"request_id={request_id}\n"
                    "state=complete\n"
                    "exit_code=0\n"
                    "uid=2000\n"
                    "mode=shell\n"
                    "timed_out=false\n"
                    "cancelled=false\n"
                    "stdout_truncated=false\n"
                    "stderr_truncated=false\n"
                )
            )
        )

        with (
            patch.object(client, "check_status", return_value=self._ready_shell_state()),
            patch.object(client, "_prepare_status", return_value=failed),
            patch(
                "openadb.core.shizuku.uuid.uuid4",
                return_value=SimpleNamespace(hex=request_id),
            ),
        ):
            result = client.execute_argv(["id"], expected_uid=2000)

        self.assertTrue(result.success, result.stderr)
        self.assertEqual(result.stdout, "legacy-compatible output\n")
        self.assertTrue(
            any("--es operation 'execute'" in command for command in adb.shell_commands)
        )

    def test_execution_maps_permission_timeout_and_cancelled_results(self) -> None:
        cases = (
            (
                "permission_required",
                "false",
                "false",
                "shizuku_permission_required",
            ),
            ("complete", "true", "false", "shizuku_timeout"),
            ("complete", "false", "true", "cancelled"),
        )
        for state, timed_out, cancelled, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                client, adb = self.make_client()
                adb.wait_results.append(
                    command_result(
                        stdout=(
                            "OPENADB_SHIZUKU_RESULT 1\n"
                            f"request_id={'de' * 16}\n"
                            f"state={state}\n"
                            "exit_code=1\n"
                            "uid=2000\n"
                            "mode=shell\n"
                            f"timed_out={timed_out}\n"
                            f"cancelled={cancelled}\n"
                        )
                    )
                )

                with (
                    patch.object(
                        client,
                        "check_status",
                        return_value=self._ready_shell_state(),
                    ),
                    patch(
                        "openadb.core.shizuku.uuid.uuid4",
                        return_value=SimpleNamespace(hex="de" * 16),
                    ),
                ):
                    result = client.execute_argv(["id"], expected_uid=2000)

                self.assertFalse(result.success)
                self.assertEqual(result.error_type, expected_error)
                self.assertNotIn("id", result.command_text)

    def test_execution_accepts_terminal_app_status_without_waiting_for_tmp_result(self) -> None:
        client, adb = self.make_client()
        message = "Grant OpenADB Bridge access in Shizuku."
        adb.wait_results.append(
            command_result(
                stdout=(
                    "OPENADB_SHIZUKU_STATUS 1\n"
                    f"request_id={'ef' * 16}\n"
                    "state=permission_required\n"
                    "installed=true\n"
                    "binder=true\n"
                    "permission=required\n"
                    "uid=-1\n"
                    "mode=unavailable\n"
                    "api=13\n"
                    f"message_b64={base64.b64encode(message.encode()).decode()}\n"
                )
            )
        )

        with (
            patch.object(client, "check_status", return_value=self._ready_shell_state()),
            patch(
                "openadb.core.shizuku.uuid.uuid4",
                return_value=SimpleNamespace(hex="ef" * 16),
            ),
        ):
            result = client.execute_shell("id", timeout=15, expected_uid=2000)

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "shizuku_permission_required")
        self.assertEqual(result.status, message)
        wait_command = next(
            command for command in adb.shell_commands if command.startswith("result=")
        )
        self.assertIn("status=", wait_command)

    def test_missing_result_signals_cancel_cleans_files_and_maps_timeout(self) -> None:
        client, adb = self.make_client()
        adb.wait_results.append(
            command_result(
                success=False,
                stderr="device did not answer",
                status="device did not answer",
                exit_code=124,
            )
        )

        with patch.object(client, "check_status", return_value=self._ready_shell_state()):
            result = client.execute_argv(["id"], timeout=1, expected_uid=2000)

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "shizuku_timeout")
        self.assertIn("device did not answer", result.status)
        self.assertTrue(any(".cancel" in command and ": >" in command for command in adb.shell_commands))
        self.assertTrue(any(command.startswith("rm -f ") for command in adb.shell_commands))

    def test_ambiguous_activity_start_failure_sends_request_scoped_cancel(self) -> None:
        client, adb = self.make_client()
        operations: list[str] = []

        def start(operation: str, *_args, **_kwargs) -> CommandResult:
            operations.append(operation)
            return command_result(
                success=False,
                status="Activity start timed out",
                exit_code=1,
            )

        with (
            patch.object(client, "check_status", return_value=self._ready_shell_state()),
            patch.object(client, "_start_activity", side_effect=start),
            patch(
                "openadb.core.shizuku.uuid.uuid4",
                return_value=SimpleNamespace(hex="ad" * 16),
            ),
        ):
            result = client.execute_argv(["id"], expected_uid=2000)

        self.assertEqual(result.error_type, "shizuku_start_failed")
        self.assertEqual(operations, ["execute", "cancel"])
        self.assertTrue(
            any(".cancel" in command and ": >" in command for command in adb.shell_commands)
        )
        self.assertTrue(any(command.startswith("rm -f ") for command in adb.shell_commands))

    def test_pre_cancelled_execution_does_not_prepare_or_start_request(self) -> None:
        client, adb = self.make_client()
        bridge = client.bridge
        cancelled = threading.Event()
        cancelled.set()

        result = client.execute_argv(["id"], cancel_event=cancelled)

        self.assertEqual(result.error_type, "cancelled")
        self.assertEqual(bridge.calls, 0)
        self.assertEqual(adb.shell_commands, [])
        self.assertEqual(adb.request_payloads, [])

    def test_output_read_honors_cancellation_and_caps_ui_text(self) -> None:
        client, adb = self.make_client()
        cancelled = threading.Event()
        cancelled.set()

        self.assertEqual(
            client._read_output("/data/local/tmp/output", cancel_event=cancelled),
            "",
        )
        self.assertEqual(adb.raw_commands, [])

        adb.stdout_payload = b"x" * (MAX_DESKTOP_OUTPUT_BYTES + 128)
        text = client._read_output("/data/local/tmp/output")

        self.assertIn("shortened to 2 MiB", text)
        self.assertLess(len(text), MAX_DESKTOP_OUTPUT_BYTES + 200)

    def test_execution_aborts_if_shizuku_uid_changed_after_confirmation(self) -> None:
        client, adb = self.make_client()
        changed = ShizukuState(
            state="ready",
            installed=True,
            running=True,
            permission="granted",
            uid=0,
            mode="root",
            message="Ready as root.",
        )

        with patch.object(client, "check_status", return_value=changed):
            result = client.execute_argv(["id"], expected_uid=2000)

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "shizuku_identity_changed")
        self.assertEqual(adb.request_payloads, [])
        self.assertFalse(any(command.startswith("am start") for command in adb.shell_commands))

    def test_prepared_session_snapshots_identity_and_skips_rechecking_trust(self) -> None:
        client, adb = self.make_client()
        status_request_id = "31" * 16
        execute_request_id = "32" * 16
        secret_command = "echo prepared-private-marker-58c2"
        adb.stdout_payload = b"prepared output\n"
        adb.wait_results.extend(
            (
                command_result(
                    stdout=self._ready_status_text(status_request_id),
                ),
                command_result(
                    stdout=self._complete_result_text(execute_request_id),
                ),
            )
        )

        with patch(
            "openadb.core.shizuku.uuid.uuid4",
            side_effect=(
                SimpleNamespace(hex=status_request_id),
                SimpleNamespace(hex=execute_request_id),
            ),
        ):
            session = client.prepare_session(expected_uid=2000)
            result = session.execute_shell(secret_command, timeout=15)

        self.assertIsInstance(session, ShizukuExecutionSession)
        self.assertTrue(session.ready)
        self.assertEqual(session.expected_uid, 2000)
        self.assertTrue(session.state.ready)
        self.assertEqual(session.state.uid, 2000)
        self.assertEqual(session.state.api_version, 13)
        self.assertTrue(result.success, result.stderr)
        self.assertEqual(result.stdout, "prepared output\n")
        self.assertEqual(client.bridge.calls, 1)
        self.assertEqual(client.bridge.verify_calls, 1)
        self.assertNotIn(secret_command, repr(session))
        self.assertNotIn(secret_command, result.command_text)
        visible_transport = "\n".join(adb.shell_commands) + repr(adb.raw_commands)
        self.assertNotIn(secret_command, visible_transport)

    def test_unprepared_execute_checks_status_and_exact_apk_only_once(self) -> None:
        client, adb = self.make_client()
        status_request_id = "33" * 16
        execute_request_id = "34" * 16
        adb.wait_results.extend(
            (
                command_result(
                    stdout=self._ready_status_text(status_request_id),
                ),
                command_result(
                    stdout=self._complete_result_text(execute_request_id),
                ),
            )
        )

        with patch(
            "openadb.core.shizuku.uuid.uuid4",
            side_effect=(
                SimpleNamespace(hex=status_request_id),
                SimpleNamespace(hex=execute_request_id),
            ),
        ):
            result = client.execute_argv(["id"], expected_uid=2000)

        self.assertTrue(result.success, result.stderr)
        self.assertEqual(client.bridge.calls, 1)
        self.assertEqual(client.bridge.verify_calls, 1)

    def test_same_device_status_operations_are_serialized(self) -> None:
        first, _first_adb = self.make_client()
        second, _second_adb = self.make_client()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        states: list[ShizukuState] = []

        def hold_first(*_args, **_kwargs) -> ShizukuState:
            first_entered.set()
            release_first.wait(2)
            return self._ready_shell_state()

        def enter_second(*_args, **_kwargs) -> ShizukuState:
            second_entered.set()
            return self._ready_shell_state()

        def run_second() -> None:
            second_started.set()
            states.append(second.check_status())

        with (
            patch.object(first, "_status_operation", side_effect=hold_first),
            patch.object(second, "_status_operation", side_effect=enter_second),
        ):
            first_thread = threading.Thread(target=first.check_status, daemon=True)
            second_thread = threading.Thread(target=run_second, daemon=True)
            try:
                first_thread.start()
                self.assertTrue(first_entered.wait(1))
                second_thread.start()
                self.assertTrue(second_started.wait(1))
                self.assertFalse(second_entered.wait(0.2))
                release_first.set()
                first_thread.join(2)
                second_thread.join(2)
            finally:
                release_first.set()
                first_thread.join(2)
                second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(states, [self._ready_shell_state()])

    def test_waiting_permission_operation_can_be_cancelled_without_starting_it(self) -> None:
        first, _first_adb = self.make_client()
        second, _second_adb = self.make_client()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        cancelled = threading.Event()
        states: list[ShizukuState] = []

        def hold_first(*_args, **_kwargs) -> ShizukuState:
            first_entered.set()
            release_first.wait(2)
            return self._ready_shell_state()

        second_operation = MagicMock(return_value=self._ready_shell_state())

        def run_second() -> None:
            second_started.set()
            states.append(second.request_permission(cancel_event=cancelled))

        with (
            patch.object(first, "_status_operation", side_effect=hold_first),
            patch.object(second, "_status_operation", second_operation),
        ):
            first_thread = threading.Thread(target=first.check_status, daemon=True)
            second_thread = threading.Thread(target=run_second, daemon=True)
            try:
                first_thread.start()
                self.assertTrue(first_entered.wait(1))
                second_thread.start()
                self.assertTrue(second_started.wait(1))
                cancelled.set()
                second_thread.join(2)
            finally:
                release_first.set()
                first_thread.join(2)
                second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].state, "cancelled")
        second_operation.assert_not_called()

    def test_waiting_execute_can_be_cancelled_before_request_creation(self) -> None:
        first, _first_adb = self.make_client()
        second, second_adb = self.make_client()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        cancelled = threading.Event()
        results: list[CommandResult] = []

        def hold_first(*_args, **_kwargs) -> ShizukuState:
            first_entered.set()
            release_first.wait(2)
            return self._ready_shell_state()

        def run_second() -> None:
            second_started.set()
            results.append(
                second.execute_argv(
                    ["id"],
                    expected_uid=2000,
                    cancel_event=cancelled,
                )
            )

        with patch.object(first, "_status_operation", side_effect=hold_first):
            first_thread = threading.Thread(target=first.check_status, daemon=True)
            second_thread = threading.Thread(target=run_second, daemon=True)
            try:
                first_thread.start()
                self.assertTrue(first_entered.wait(1))
                second_thread.start()
                self.assertTrue(second_started.wait(1))
                cancelled.set()
                second_thread.join(2)
            finally:
                release_first.set()
                first_thread.join(2)
                second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].error_type, "cancelled")
        self.assertEqual(second_adb.request_payloads, [])
        self.assertEqual(second.bridge.calls, 0)

    def test_reconnect_generation_cannot_race_the_one_shot_user_service(self) -> None:
        first, _first_adb = self.make_client()
        second_adb = RecordingADB()
        second_adb.device_context.generation = 8
        second, _second_adb = self.make_client(second_adb)
        first_entered = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()

        def hold_first(*_args, **_kwargs) -> ShizukuState:
            first_entered.set()
            release_first.wait(2)
            return self._ready_shell_state()

        def enter_second(*_args, **_kwargs) -> ShizukuState:
            second_entered.set()
            return self._ready_shell_state()

        def run_second() -> None:
            second_started.set()
            second.check_status()

        with (
            patch.object(first, "_status_operation", side_effect=hold_first),
            patch.object(second, "_status_operation", side_effect=enter_second),
        ):
            first_thread = threading.Thread(target=first.check_status, daemon=True)
            second_thread = threading.Thread(target=run_second, daemon=True)
            try:
                first_thread.start()
                self.assertTrue(first_entered.wait(1))
                second_thread.start()
                self.assertTrue(second_started.wait(1))
                self.assertFalse(second_entered.wait(0.2))
                release_first.set()
                self.assertTrue(second_entered.wait(1))
            finally:
                release_first.set()
                first_thread.join(2)
                second_thread.join(2)

        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())

    def test_prepared_session_is_rejected_after_bound_identity_changes(self) -> None:
        client, adb = self.make_client()
        with patch.object(
            client,
            "check_status",
            return_value=self._ready_shell_state(),
        ):
            session = client.prepare_session(expected_uid=2000)

        adb.device_context.generation = 8
        result = session.execute_argv(["id"])

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "shizuku_session_stale")
        self.assertEqual(adb.request_payloads, [])

    def test_device_reported_output_truncation_is_rejected_fail_closed(self) -> None:
        client, adb = self.make_client()
        request_id = "35" * 16
        adb.stdout_payload = b"partial output"
        adb.wait_results.append(
            command_result(
                stdout=self._complete_result_text(
                    request_id,
                    stdout_truncated=True,
                )
            )
        )

        with (
            patch.object(client, "check_status", return_value=self._ready_shell_state()),
            patch(
                "openadb.core.shizuku.uuid.uuid4",
                return_value=SimpleNamespace(hex=request_id),
            ),
        ):
            result = client.execute_argv(["id"], expected_uid=2000)

        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.error_type, "shizuku_output_truncated")
        self.assertIn("rejected the incomplete result", result.status)
        self.assertIn("stdout was truncated", result.stdout)

    def test_desktop_output_cap_is_rejected_fail_closed(self) -> None:
        client, adb = self.make_client()
        request_id = "36" * 16
        adb.stdout_payload = b"x" * (MAX_DESKTOP_OUTPUT_BYTES + 1)
        adb.wait_results.append(
            command_result(stdout=self._complete_result_text(request_id))
        )

        with (
            patch.object(client, "check_status", return_value=self._ready_shell_state()),
            patch(
                "openadb.core.shizuku.uuid.uuid4",
                return_value=SimpleNamespace(hex=request_id),
            ),
        ):
            result = client.execute_argv(["id"], expected_uid=2000)

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "shizuku_output_truncated")
        self.assertIn("shortened to 2 MiB", result.stdout)

    @staticmethod
    def _ready_status_text(request_id: str) -> str:
        return (
            "OPENADB_SHIZUKU_STATUS 1\n"
            f"request_id={request_id}\n"
            "state=ready\n"
            "installed=true\n"
            "binder=true\n"
            "permission=granted\n"
            "uid=2000\n"
            "mode=shell\n"
            "api=13\n"
        )

    @staticmethod
    def _complete_result_text(
        request_id: str,
        *,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
    ) -> str:
        return (
            "OPENADB_SHIZUKU_RESULT 1\n"
            f"request_id={request_id}\n"
            "state=complete\n"
            "exit_code=0\n"
            "uid=2000\n"
            "mode=shell\n"
            "timed_out=false\n"
            "cancelled=false\n"
            f"stdout_truncated={str(stdout_truncated).lower()}\n"
            f"stderr_truncated={str(stderr_truncated).lower()}\n"
        )

    @staticmethod
    def _ready_shell_state() -> ShizukuState:
        return ShizukuState(
            state="ready",
            installed=True,
            running=True,
            permission="granted",
            uid=2000,
            mode="shell",
            message="Ready as Android shell.",
        )


class FakeSettings:
    def __init__(self, backend: str = "standard") -> None:
        self.data = {"privilege_backend": backend}

    def get(self, key: str, default=None):
        return self.data.get(key, default)


class BoundPrivilegeADB:
    def __init__(self, root_available: bool = False) -> None:
        self._root_available = root_available
        self.root_checks = 0

    def root_available(self, **_kwargs) -> bool:
        self.root_checks += 1
        return self._root_available


class ContextBindingADB:
    def __init__(self, root_available: bool = False) -> None:
        self.bound = BoundPrivilegeADB(root_available)
        self.contexts: list[DeviceContext] = []

    def for_context(self, context: DeviceContext) -> BoundPrivilegeADB:
        self.contexts.append(context)
        return self.bound


class DeviceManagerShape:
    """Expose the public shape of DeviceManager without a fictitious context attribute."""

    def __init__(self, context: DeviceContext) -> None:
        self.active = SimpleNamespace(serial=context.serial)
        self.current_generation = context.generation


class PrivilegeTests(unittest.TestCase):
    def test_backend_normalization_uses_safe_standard_fallback(self) -> None:
        cases = {
            "standard": PrivilegeBackend.STANDARD,
            "adb": PrivilegeBackend.STANDARD,
            "su": PrivilegeBackend.ROOT,
            "sui": PrivilegeBackend.SHIZUKU,
            "unexpected": PrivilegeBackend.STANDARD,
            None: PrivilegeBackend.STANDARD,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertIs(PrivilegeBackend.normalize(value), expected)

    def test_check_cannot_probe_root_while_standard_is_selected(self) -> None:
        context = device_context()
        adb = ContextBindingADB(root_available=True)
        manager = PrivilegeManager(
            adb,
            FakeSettings("standard"),
            DeviceManagerShape(context),
        )

        with self.assertRaisesRegex(RuntimeError, "does not match"):
            manager.check(context, backend=PrivilegeBackend.ROOT)

        self.assertEqual(adb.contexts, [])
        self.assertEqual(adb.bound.root_checks, 0)

    def test_shizuku_entry_points_cannot_run_while_standard_is_selected(self) -> None:
        context = device_context()
        adb = ContextBindingADB()
        manager = PrivilegeManager(
            adb,
            FakeSettings("standard"),
            DeviceManagerShape(context),
        )

        with patch("openadb.core.privilege.ShizukuClient") as shizuku_client:
            with self.assertRaisesRegex(RuntimeError, "Select Shizuku"):
                manager.request_shizuku(context)
            with self.assertRaisesRegex(RuntimeError, "Select Shizuku"):
                manager.request_and_check_shizuku(context)
            with self.assertRaisesRegex(RuntimeError, "Select Shizuku"):
                manager.execute_shizuku_shell(context, "id")
            with self.assertRaisesRegex(RuntimeError, "Select Shizuku"):
                manager.open_shizuku_manager(context)

        shizuku_client.assert_not_called()
        self.assertEqual(adb.contexts, [])

    def test_request_and_check_shizuku_caches_only_verified_final_status(self) -> None:
        context = device_context()
        manager = PrivilegeManager(
            ContextBindingADB(),
            FakeSettings("shizuku"),
            DeviceManagerShape(context),
        )
        final_state = ShizukuState(
            state="ready",
            installed=True,
            running=True,
            permission="granted",
            uid=2000,
            mode="shell",
            message="Verified after permission request.",
        )
        observed: list[PrivilegeStatus | None] = []
        manager.add_status_listener(observed.append)

        with patch("openadb.core.privilege.ShizukuClient") as client_type:
            client_type.return_value.request_permission_then_check.return_value = (
                final_state
            )
            status = manager.request_and_check_shizuku(context)

        self.assertTrue(status.available)
        self.assertTrue(status.shell)
        self.assertIs(manager.cached_status(), status)
        self.assertEqual(observed, [status])
        client_type.return_value.request_permission_then_check.assert_called_once()

    def test_privilege_status_never_treats_shizuku_shell_as_root(self) -> None:
        shell = PrivilegeStatus.from_shizuku(
            ShizukuState(
                state="ready",
                installed=True,
                running=True,
                permission="granted",
                uid=2000,
                mode="shell",
                message="shell",
            )
        )
        root = PrivilegeStatus.from_shizuku(
            ShizukuState(
                state="ready",
                installed=True,
                running=True,
                permission="granted",
                uid=0,
                mode="root",
                message="root",
            )
        )

        self.assertTrue(shell.available)
        self.assertTrue(shell.shell)
        self.assertFalse(shell.root)
        self.assertEqual(shell.level, "shell")
        self.assertTrue(root.available)
        self.assertTrue(root.root)
        self.assertFalse(root.shell)
        self.assertEqual(root.level, "root")

    def test_ready_status_always_names_real_shizuku_uid_even_with_generic_android_message(self) -> None:
        shell = PrivilegeStatus.from_shizuku(
            ShizukuState(
                state="ready",
                installed=True,
                running=True,
                permission="granted",
                uid=2000,
                mode="shell",
                message="Shizuku is running and OpenADB Bridge has permission.",
            )
        )
        root = PrivilegeStatus.from_shizuku(
            ShizukuState(
                state="ready",
                installed=True,
                running=True,
                permission="granted",
                uid=0,
                mode="root",
                message="Shizuku is running and OpenADB Bridge has permission.",
            )
        )

        self.assertIn("UID 2000", shell.message)
        self.assertIn("not root", shell.message)
        self.assertIn("UID 0", root.message)

    def test_root_check_is_bound_to_captured_context_and_cached(self) -> None:
        context = device_context()
        settings = FakeSettings("root")
        adb = ContextBindingADB(root_available=True)
        manager_shape = DeviceManagerShape(context)
        manager = PrivilegeManager(adb, settings, manager_shape)

        status = manager.check(context)

        self.assertTrue(status.root)
        self.assertEqual(status.backend, PrivilegeBackend.ROOT)
        self.assertEqual(status.device_serial, context.serial)
        self.assertEqual(status.device_generation, context.generation)
        self.assertEqual(adb.contexts, [context])
        self.assertIs(manager.cached_status(), status)

    def test_status_listeners_receive_cache_changes_and_can_be_removed(self) -> None:
        context = device_context()
        manager = PrivilegeManager(
            ContextBindingADB(),
            FakeSettings("standard"),
            DeviceManagerShape(context),
        )
        observed: list[PrivilegeStatus | None] = []
        listener = observed.append
        manager.add_status_listener(listener)

        status = manager.check(context)
        manager.reset()
        manager.remove_status_listener(listener)
        manager.check(context)

        self.assertEqual(observed, [status, None])

    def test_runtime_invalidation_listeners_exclude_deliberate_resets(self) -> None:
        context = device_context()
        manager = PrivilegeManager(
            ContextBindingADB(),
            FakeSettings("standard"),
            DeviceManagerShape(context),
        )
        observed: list[str] = []

        def listener() -> None:
            observed.append("invalidated")

        manager.add_invalidation_listener(listener)
        manager.check(context)
        lease = manager.capture_operation_lease()

        self.assertTrue(manager._reset_if_lease_current(lease))
        manager.reset()
        manager.remove_invalidation_listener(listener)
        next_lease = manager.capture_operation_lease()
        self.assertTrue(manager._reset_if_lease_current(next_lease))

        self.assertEqual(observed, ["invalidated"])

    def test_stale_backend_lease_cannot_publish_status_or_notify_listeners(self) -> None:
        context = device_context()
        settings = FakeSettings("shizuku")
        manager = PrivilegeManager(
            ContextBindingADB(),
            settings,
            DeviceManagerShape(context),
        )
        observed: list[PrivilegeStatus | None] = []
        manager.add_status_listener(observed.append)
        lease = manager.capture_operation_lease()
        stale_status = PrivilegeStatus.from_shizuku(
            ShizukuState(
                state="ready",
                installed=True,
                running=True,
                permission="granted",
                uid=2000,
                mode="shell",
                message="ready",
            ),
            serial=context.serial,
            generation=context.generation,
        )

        settings.data["privilege_backend"] = "standard"
        manager.reset()
        manager._cache_if_current(stale_status, privilege_lease=lease)

        self.assertIsNone(manager.cached_status())
        self.assertEqual(observed, [])

    def test_cache_invalidates_on_generation_serial_and_backend_changes(self) -> None:
        context = device_context()
        settings = FakeSettings("standard")
        adb = ContextBindingADB()
        manager_shape = DeviceManagerShape(context)
        manager = PrivilegeManager(adb, settings, manager_shape)

        first = manager.check(context)
        self.assertIs(manager.cached_status(), first)

        manager_shape.current_generation += 1
        self.assertIsNone(manager.cached_status())

        manager_shape.current_generation = context.generation
        manager.check(context)
        manager_shape.active.serial = "device-b"
        self.assertIsNone(manager.cached_status())

        manager_shape.active.serial = context.serial
        manager.check(context)
        settings.data["privilege_backend"] = "root"
        self.assertIsNone(manager.cached_status())

    def test_root_check_cancelled_during_probe_is_not_cached(self) -> None:
        context = device_context()
        cancelled = threading.Event()

        class CancellingBoundADB:
            def root_available(self, **_kwargs) -> bool:
                cancelled.set()
                return False

        source = SimpleNamespace(for_context=lambda _context: CancellingBoundADB())
        manager = PrivilegeManager(
            source,
            FakeSettings("root"),
            DeviceManagerShape(context),
        )

        status = manager.check(context, cancel_event=cancelled)

        self.assertFalse(status.available)
        self.assertIsNone(manager.cached_status())

    def test_shizuku_checks_map_uid_and_use_bound_transport(self) -> None:
        context = device_context()
        settings = FakeSettings("shizuku")
        adb = ContextBindingADB()
        manager_shape = DeviceManagerShape(context)
        manager = PrivilegeManager(adb, settings, manager_shape)

        class FakeShizukuClient:
            states: ClassVar[list[ShizukuState]] = [
                ShizukuState(
                    state="ready",
                    installed=True,
                    running=True,
                    permission="granted",
                    uid=2000,
                    mode="shell",
                    message="shell",
                ),
                ShizukuState(
                    state="ready",
                    installed=True,
                    running=True,
                    permission="granted",
                    uid=0,
                    mode="root",
                    message="root",
                ),
            ]
            instances: ClassVar[list[object]] = []

            def __init__(self, bound_adb, used_settings, *, temp_folder) -> None:
                self.bound_adb = bound_adb
                self.settings = used_settings
                self.temp_folder = temp_folder
                self.__class__.instances.append(self)

            def check_status(self, **_kwargs) -> ShizukuState:
                return self.__class__.states.pop(0)

        with patch("openadb.core.privilege.ShizukuClient", FakeShizukuClient):
            shell = manager.check(context)
            root = manager.check(context)

        self.assertTrue(shell.shell)
        self.assertFalse(shell.root)
        self.assertTrue(root.root)
        self.assertEqual(len(FakeShizukuClient.instances), 2)
        self.assertIs(FakeShizukuClient.instances[0].bound_adb, adb.bound)
        self.assertEqual(FakeShizukuClient.instances[0].temp_folder, context.temp_path)

    def test_manager_executes_only_with_current_cached_shizuku_uid(self) -> None:
        context = device_context()
        settings = FakeSettings("shizuku")
        adb = ContextBindingADB()
        manager_shape = DeviceManagerShape(context)
        manager = PrivilegeManager(adb, settings, manager_shape)

        class FakeShizukuClient:
            execution_calls: ClassVar[list[tuple[str, dict[str, object]]]] = []

            def __init__(self, _bound_adb, _settings, *, temp_folder) -> None:
                self.temp_folder = temp_folder

            def check_status(self, **_kwargs) -> ShizukuState:
                return ShizukuState(
                    state="ready",
                    installed=True,
                    running=True,
                    permission="granted",
                    uid=2000,
                    mode="shell",
                    message="shell",
                )

            def execute_shell(self, command: str, **kwargs) -> CommandResult:
                self.__class__.execution_calls.append((command, kwargs))
                return command_result(stdout="uid=2000")

        with patch("openadb.core.privilege.ShizukuClient", FakeShizukuClient):
            status = manager.check(context)
            result = manager.execute_shizuku_shell(
                context,
                "id",
                timeout=30,
                expected_uid=status.uid,
            )

        self.assertTrue(result.success)
        self.assertEqual(len(FakeShizukuClient.execution_calls), 1)
        command, kwargs = FakeShizukuClient.execution_calls[0]
        self.assertEqual(command, "id")
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["expected_uid"], 2000)
        self.assertFalse(kwargs["cancel_event"].is_set())

    def test_manager_invalidates_ready_cache_when_live_shizuku_access_is_lost(self) -> None:
        context = device_context()
        settings = FakeSettings("shizuku")
        adb = ContextBindingADB()
        manager = PrivilegeManager(adb, settings, DeviceManagerShape(context))
        ready = PrivilegeStatus.from_shizuku(
            ShizukuState(
                state="ready",
                installed=True,
                running=True,
                permission="granted",
                uid=2000,
                mode="shell",
            ),
            serial=context.serial,
            generation=context.generation,
        )
        manager._cache_if_current(ready)

        class RevokedShizukuClient:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def execute_shell(self, *_args, **_kwargs) -> CommandResult:
                return command_result(
                    success=False,
                    status="Permission was revoked.",
                    exit_code=1,
                    error_type="shizuku_permission_required",
                )

        with patch("openadb.core.privilege.ShizukuClient", RevokedShizukuClient):
            result = manager.execute_shizuku_shell(
                context,
                "id",
                expected_uid=2000,
            )

        self.assertEqual(result.error_type, "shizuku_permission_required")
        self.assertIsNone(manager.cached_status())


class PreparedBoundRunner:
    def __init__(self) -> None:
        self.recorded: list[CommandResult] = []

    def record_result(self, result: CommandResult) -> CommandResult:
        self.recorded.append(result)
        return result


class PreparedDirectADB:
    """Small immutable-bound ADB double for operation-scoped adapter tests."""

    def __init__(
        self,
        context: DeviceContext,
        *,
        direct_uid: int | None = 2000,
        su_uid: int | None = None,
        on_direct_shell=None,
        on_root_shell=None,
    ) -> None:
        self.serial = context.serial
        self.device_context = context
        self.platform_tools = SimpleNamespace(adb_path=Path("adb"))
        self.runner = PreparedBoundRunner()
        self.raw_calls: list[tuple[list[str], dict[str, object]]] = []
        self.shell_calls: list[tuple[str, dict[str, object]]] = []
        self.root_shell_calls: list[tuple[str, dict[str, object]]] = []
        self.direct_uid = direct_uid
        self.su_uid = su_uid
        self.on_direct_shell = on_direct_shell
        self.on_root_shell = on_root_shell

    def _base(self, serial: str | None = None) -> list[str]:
        if serial not in (None, self.serial):
            raise RuntimeError("bound to another serial")
        return ["adb", "-t", self.device_context.transport_id]

    def run_raw(self, args, **kwargs) -> CommandResult:
        recorded_args = [str(value) for value in args]
        self.raw_calls.append((recorded_args, dict(kwargs)))
        return command_result(stdout="direct", status="direct")

    def run_raw_binary_output(self, *args, **kwargs):
        self.raw_calls.append((["binary", *map(str, args)], dict(kwargs)))
        return command_result(stdout="direct"), b"direct"

    def run_raw_binary_output_with_writer(self, args, **kwargs) -> CommandResult:
        return self.run_raw(args, **kwargs)

    def run_raw_streaming(self, args, **kwargs) -> CommandResult:
        return self.run_raw(args, **kwargs)

    def run_raw_with_input_stream(self, args, **kwargs) -> CommandResult:
        return self.run_raw(args, **kwargs)

    def run_raw_binary_output_to_file(self, args, **kwargs) -> CommandResult:
        return self.run_raw(args, **kwargs)

    def run_shell(self, command: str, **kwargs) -> CommandResult:
        self.shell_calls.append((str(command), dict(kwargs)))
        if callable(self.on_direct_shell):
            self.on_direct_shell()
        if str(command).strip() == "id -u":
            return command_result(
                success=self.direct_uid is not None,
                stdout="" if self.direct_uid is None else str(self.direct_uid),
                exit_code=0 if self.direct_uid is not None else 1,
                status="direct uid probe",
            )
        return command_result(stdout="direct shell", status="direct shell")

    def run_root_shell(self, command: str, **kwargs) -> CommandResult:
        self.root_shell_calls.append((str(command), dict(kwargs)))
        if callable(self.on_root_shell):
            self.on_root_shell()
        if str(command).strip() == "id -u":
            return command_result(
                success=self.su_uid is not None,
                stdout="" if self.su_uid is None else str(self.su_uid),
                exit_code=0 if self.su_uid is not None else 1,
                status="su uid probe",
            )
        return command_result(stdout="su shell", status="su shell")

    @staticmethod
    def root_shell_script(command: str) -> str:
        return f"su -c {command!r}"


class PreparedSourceADB:
    def __init__(
        self,
        *,
        direct_uid: int | None = 2000,
        su_uid: int | None = None,
        on_direct_shell=None,
        on_root_shell=None,
    ) -> None:
        self.contexts: list[DeviceContext] = []
        self.last_bound: PreparedDirectADB | None = None
        self.direct_uid = direct_uid
        self.su_uid = su_uid
        self.on_direct_shell = on_direct_shell
        self.on_root_shell = on_root_shell

    def for_context(self, context: DeviceContext) -> PreparedDirectADB:
        self.contexts.append(context)
        self.last_bound = PreparedDirectADB(
            context,
            direct_uid=self.direct_uid,
            su_uid=self.su_uid,
            on_direct_shell=self.on_direct_shell,
            on_root_shell=self.on_root_shell,
        )
        return self.last_bound


class PreparedSession:
    def __init__(
        self,
        state: ShizukuState,
        responses: list[CommandResult] | None = None,
    ) -> None:
        self.state = state
        self.expected_uid = state.uid if state.ready else None
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def ready(self) -> bool:
        return self.state.ready and self.expected_uid in {0, 2000}

    def execute_shell(self, command: str, **kwargs) -> CommandResult:
        self.calls.append((command, dict(kwargs)))
        if self.responses:
            return self.responses.pop(0)
        return command_result(stdout="session", status="Shizuku command completed")


class PreparedShizukuClientDouble:
    session: ClassVar[PreparedSession | None] = None
    instances: ClassVar[list[PreparedShizukuClientDouble]] = []
    on_prepare: ClassVar[object | None] = None

    def __init__(self, direct_adb, settings, *, temp_folder) -> None:
        self.direct_adb = direct_adb
        self.settings = settings
        self.temp_folder = temp_folder
        self.prepare_calls: list[dict[str, object]] = []
        self.__class__.instances.append(self)

    def prepare_session(self, **kwargs) -> PreparedSession:
        self.prepare_calls.append(dict(kwargs))
        callback = self.__class__.on_prepare
        if callable(callback):
            callback()
        if self.__class__.session is None:
            raise RuntimeError("test session was not configured")
        return self.__class__.session


class PreparedPrivilegeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        PreparedShizukuClientDouble.instances = []
        PreparedShizukuClientDouble.session = None
        PreparedShizukuClientDouble.on_prepare = None

    @staticmethod
    def _ready_state(uid: int = 2000) -> ShizukuState:
        return ShizukuState(
            state="ready",
            installed=True,
            running=True,
            permission="granted",
            uid=uid,
            mode="root" if uid == 0 else "shell",
            message=f"Ready as UID {uid}.",
        )

    @staticmethod
    def _manager(
        backend: str = "shizuku",
        context: DeviceContext | None = None,
        *,
        source: PreparedSourceADB | None = None,
    ) -> tuple[PrivilegeManager, PreparedSourceADB, DeviceManagerShape, DeviceContext]:
        context = context or device_context()
        source = source or PreparedSourceADB()
        device_manager = DeviceManagerShape(context)
        manager = PrivilegeManager(source, FakeSettings(backend), device_manager)
        return manager, source, device_manager, context

    def _prepare(
        self,
        *,
        uid: int = 2000,
        responses: list[CommandResult] | None = None,
    ) -> tuple[
        PrivilegeManager,
        PreparedSourceADB,
        PreparedSession,
        ShizukuAwareADBClient,
    ]:
        manager, source, _device_manager, context = self._manager()
        session = PreparedSession(self._ready_state(uid), responses)
        PreparedShizukuClientDouble.session = session
        with patch("openadb.core.privilege.ShizukuClient", PreparedShizukuClientDouble):
            prepared = manager.prepare_adb(context)
        self.assertIsInstance(prepared, ShizukuAwareADBClient)
        return manager, source, session, prepared

    def test_standard_returns_ordinary_immutable_bound_adb_with_backend_metadata(self) -> None:
        manager, source, _device_manager, context = self._manager("standard")

        prepared = manager.prepare_adb(context)

        self.assertIs(prepared, source.last_bound)
        self.assertIs(type(prepared), PreparedDirectADB)
        self.assertEqual(prepared.device_context, context)
        self.assertIs(
            prepared.requested_privilege_backend,
            PrivilegeBackend.STANDARD,
        )
        self.assertIs(
            prepared.effective_privilege_backend,
            PrivilegeBackend.STANDARD,
        )
        self.assertEqual(prepared.privilege_fallback_message, "")
        self.assertEqual(PreparedShizukuClientDouble.instances, [])

    def test_standard_blocks_shell_when_direct_adbd_is_already_root(self) -> None:
        source = PreparedSourceADB(direct_uid=0)
        manager, source, _device_manager, context = self._manager(
            "standard",
            source=source,
        )

        with self.assertRaisesRegex(RuntimeError, "already running as UID 0"):
            manager.prepare_adb(context)

        self.assertEqual([call[0] for call in source.last_bound.shell_calls], ["id -u"])
        self.assertEqual(source.last_bound.root_shell_calls, [])
        status = manager.cached_status()
        self.assertIsNotNone(status)
        self.assertEqual(status.state, "blocked")
        self.assertFalse(status.available)

    def test_standard_blocks_unexpected_non_shell_uid(self) -> None:
        source = PreparedSourceADB(direct_uid=1000)
        manager, source, _device_manager, context = self._manager(
            "standard",
            source=source,
        )

        with self.assertRaisesRegex(RuntimeError, "unexpected UID 1000"):
            manager.prepare_adb(context)

        self.assertEqual([call[0] for call in source.last_bound.shell_calls], ["id -u"])
        self.assertEqual(source.last_bound.root_shell_calls, [])
        status = manager.cached_status()
        self.assertIsNotNone(status)
        self.assertEqual(status.state, "blocked")
        self.assertFalse(status.available)

    def test_captured_backend_lease_rejects_change_before_prepare(self) -> None:
        context = device_context()
        settings = FakeSettings("root")
        source = PreparedSourceADB(direct_uid=0)
        manager = PrivilegeManager(source, settings, DeviceManagerShape(context))
        lease = manager.capture_operation_lease()

        settings.data["privilege_backend"] = "standard"
        manager.reset()

        with self.assertRaisesRegex(RuntimeError, "access mode changed"):
            manager.prepare_adb(context, privilege_lease=lease)
        self.assertIsNone(source.last_bound)

    def test_direct_root_adbd_is_prepared_without_su(self) -> None:
        source = PreparedSourceADB(direct_uid=0, su_uid=None)
        manager, source, _device_manager, context = self._manager(
            "root",
            source=source,
        )

        prepared = manager.prepare_adb(context)

        self.assertIsInstance(prepared, RootAwareADBClient)
        self.assertIs(prepared.direct_adb, source.last_bound)
        self.assertIs(prepared.root_strategy, RootExecutionStrategy.DIRECT)
        self.assertIs(prepared.requested_privilege_backend, PrivilegeBackend.ROOT)
        self.assertIs(prepared.effective_privilege_backend, PrivilegeBackend.ROOT)
        self.assertEqual(prepared.verified_uid, 0)
        self.assertEqual([call[0] for call in source.last_bound.shell_calls], ["id -u"])
        self.assertEqual(source.last_bound.root_shell_calls, [])
        self.assertTrue(manager.cached_status().root)

        source.last_bound.shell_calls.clear()
        prepared.run_shell("pm list packages")
        prepared.run_root_shell("id")

        self.assertEqual(
            [call[0] for call in source.last_bound.shell_calls],
            ["pm list packages", "id"],
        )
        self.assertEqual(source.last_bound.root_shell_calls, [])
        self.assertEqual(prepared.root_shell_script("cat /protected"), "cat /protected")

    def test_su_root_backend_routes_all_shell_helpers_without_double_wrapping(self) -> None:
        source = PreparedSourceADB(direct_uid=2000, su_uid=0)
        manager, source, _device_manager, context = self._manager(
            "root",
            source=source,
        )

        prepared = manager.prepare_adb(context)

        self.assertIsInstance(prepared, RootAwareADBClient)
        self.assertIs(prepared.root_strategy, RootExecutionStrategy.SU)
        self.assertEqual([call[0] for call in source.last_bound.shell_calls], ["id -u"])
        self.assertEqual([call[0] for call in source.last_bound.root_shell_calls], ["id -u"])

        source.last_bound.shell_calls.clear()
        source.last_bound.root_shell_calls.clear()
        prepared.run_shell("pm list packages")
        prepared.run_root_shell("id")
        prepared.enable_package("com.example.app")
        prepared.stat("/data/protected", use_root=True)

        commands = [call[0] for call in source.last_bound.root_shell_calls]
        self.assertEqual(commands[:2], ["pm list packages", "id"])
        self.assertIn("pm enable 'com.example.app'", commands[2])
        self.assertIn("stat '/data/protected'", commands[3])
        self.assertTrue(all("su -c" not in command for command in commands))
        embedded = prepared.root_shell_script("cat /protected")
        self.assertEqual(embedded.count("su -c"), 1)

        prepared.pull_tar_streaming(
            "/data/protected",
            "tar",
            lambda _stream: None,
            use_root=True,
        )
        tar_command = source.last_bound.raw_calls[-1][0][3]
        self.assertEqual(tar_command.count("su -c"), 1)

    def test_root_facade_keeps_raw_push_pull_install_and_reboot_on_direct_adb(self) -> None:
        source = PreparedSourceADB(direct_uid=2000, su_uid=0)
        manager, source, _device_manager, context = self._manager(
            "root",
            source=source,
        )
        prepared = manager.prepare_adb(context)
        source.last_bound.raw_calls.clear()
        source.last_bound.shell_calls.clear()
        source.last_bound.root_shell_calls.clear()

        prepared.push(Path("app.apk"), "/sdcard/app.apk")
        prepared.pull("/sdcard/app.apk", Path("app.apk"))
        prepared.install_apk(Path("app.apk"))
        prepared.reboot("recovery")
        prepared.run_raw(["get-state"])

        self.assertEqual(source.last_bound.shell_calls, [])
        self.assertEqual(source.last_bound.root_shell_calls, [])
        self.assertEqual(
            [call[0][0] for call in source.last_bound.raw_calls],
            ["push", "pull", "install", "reboot", "get-state"],
        )

    def test_unavailable_root_has_explicit_best_effort_standard_fallback(self) -> None:
        source = PreparedSourceADB(direct_uid=2000, su_uid=None)
        manager, source, _device_manager, context = self._manager(
            "root",
            source=source,
        )

        prepared = manager.prepare_adb(context)

        self.assertIs(prepared, source.last_bound)
        self.assertIs(type(prepared), PreparedDirectADB)
        self.assertIs(prepared.requested_privilege_backend, PrivilegeBackend.ROOT)
        self.assertIs(
            prepared.effective_privilege_backend,
            PrivilegeBackend.STANDARD,
        )
        self.assertIn("Root access is unavailable", prepared.privilege_fallback_message)
        self.assertEqual([call[0] for call in prepared.shell_calls], ["id -u"])
        self.assertEqual([call[0] for call in prepared.root_shell_calls], ["id -u"])
        status = manager.cached_status()
        self.assertIsNotNone(status)
        self.assertIs(status.backend, PrivilegeBackend.ROOT)
        self.assertFalse(status.available)

    def test_root_preparation_honors_cancellation_between_direct_and_su_probes(self) -> None:
        cancelled = threading.Event()
        source = PreparedSourceADB(
            direct_uid=2000,
            su_uid=0,
            on_direct_shell=cancelled.set,
        )
        manager, source, _device_manager, context = self._manager(
            "root",
            source=source,
        )

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            manager.prepare_adb(context, cancel_event=cancelled)

        self.assertEqual([call[0] for call in source.last_bound.shell_calls], ["id -u"])
        self.assertEqual(source.last_bound.root_shell_calls, [])
        self.assertIsNone(manager.cached_status())

    def test_root_preparation_rejects_device_or_backend_change_during_probe(self) -> None:
        context = device_context()
        device_manager = DeviceManagerShape(context)
        source = PreparedSourceADB(
            direct_uid=0,
            on_direct_shell=lambda: setattr(
                device_manager,
                "current_generation",
                context.generation + 1,
            ),
        )
        manager = PrivilegeManager(source, FakeSettings("root"), device_manager)

        with self.assertRaises(StaleDeviceContext):
            manager.prepare_adb(context)
        self.assertIsNone(manager.cached_status())

        settings = FakeSettings("root")
        source = PreparedSourceADB(
            direct_uid=0,
            on_direct_shell=lambda: settings.data.__setitem__(
                "privilege_backend",
                "standard",
            ),
        )
        manager = PrivilegeManager(source, settings, DeviceManagerShape(context))
        with self.assertRaisesRegex(RuntimeError, "backend changed"):
            manager.prepare_adb(context)
        self.assertIsNone(manager.cached_status())

    def test_prepared_root_facade_cannot_be_retargeted_and_checks_context(self) -> None:
        source = PreparedSourceADB(direct_uid=0)
        manager, _source, device_manager, context = self._manager(
            "root",
            source=source,
        )
        prepared = manager.prepare_adb(context)

        self.assertIs(prepared.for_context(context), prepared)
        self.assertIs(prepared.for_serial(context.serial), prepared)
        with self.assertRaisesRegex(RuntimeError, "cannot be rebound"):
            prepared.for_serial("another-device")

        device_manager.current_generation += 1
        with self.assertRaises(StaleDeviceContext):
            prepared.run_shell("id")
        with self.assertRaises(StaleDeviceContext):
            prepared.root_available()

    def test_backend_reset_invalidates_prepared_root_shell_and_raw_calls(self) -> None:
        context = device_context()
        settings = FakeSettings("root")
        source = PreparedSourceADB(direct_uid=0)
        manager = PrivilegeManager(source, settings, DeviceManagerShape(context))
        prepared = manager.prepare_adb(context)

        def switch_backend_during_shell() -> None:
            settings.data["privilege_backend"] = "standard"
            manager.reset()

        source.last_bound.on_direct_shell = switch_backend_during_shell
        with self.assertRaisesRegex(RuntimeError, "access mode changed"):
            prepared.run_shell("pm list packages")

        settings.data["privilege_backend"] = "root"
        prepared = manager.prepare_adb(context)
        direct_run_raw = source.last_bound.run_raw

        def switch_backend_during_raw(*args, **kwargs):
            settings.data["privilege_backend"] = "standard"
            manager.reset()
            return direct_run_raw(*args, **kwargs)

        source.last_bound.run_raw = switch_backend_during_raw
        with self.assertRaisesRegex(RuntimeError, "access mode changed"):
            prepared.run_raw(["exec-out", "id"])

    def test_backend_reset_cancels_prepared_root_tar_output_stream(self) -> None:
        context = device_context()
        settings = FakeSettings("root")
        source = PreparedSourceADB(direct_uid=0)
        manager = PrivilegeManager(source, settings, DeviceManagerShape(context))
        prepared = manager.prepare_adb(context)

        direct_stream = source.last_bound.run_raw_binary_output_with_writer

        def switch_backend_during_stream(*args, **kwargs):
            settings.data["privilege_backend"] = "standard"
            manager.reset()
            result = direct_stream(*args, **kwargs)
            self.assertTrue(kwargs["cancel_event"].is_set())
            return result

        source.last_bound.run_raw_binary_output_with_writer = switch_backend_during_stream
        with self.assertRaisesRegex(RuntimeError, "access mode changed"):
            prepared.pull_tar_streaming(
                "/data/protected",
                "tar",
                lambda _stream: None,
                use_root=True,
            )

        recorded_args, recorded_kwargs = source.last_bound.raw_calls[-1]
        self.assertEqual(recorded_args[:3], ["exec-out", "sh", "-c"])
        self.assertIn("/data/protected", recorded_args[3])
        self.assertNotIn("su -c", recorded_args[3])
        self.assertTrue(recorded_kwargs["cancel_event"].is_set())

    def test_backend_reset_invalidates_prepared_shizuku_session(self) -> None:
        context = device_context()
        settings = FakeSettings("shizuku")
        source = PreparedSourceADB()
        manager = PrivilegeManager(source, settings, DeviceManagerShape(context))
        session = PreparedSession(self._ready_state())
        PreparedShizukuClientDouble.session = session
        with patch("openadb.core.privilege.ShizukuClient", PreparedShizukuClientDouble):
            prepared = manager.prepare_adb(context)

        def switch_backend_during_shell(_command: str, **_kwargs) -> CommandResult:
            settings.data["privilege_backend"] = "standard"
            manager.reset()
            return command_result(stdout="stale Shizuku result")

        session.execute_shell = switch_backend_during_shell
        with self.assertRaisesRegex(RuntimeError, "access mode changed"):
            prepared.run_shell("id")

    def test_stale_root_facade_cannot_cancel_new_shizuku_generation(self) -> None:
        context = device_context()
        settings = FakeSettings("root")
        source = PreparedSourceADB(direct_uid=0)
        manager = PrivilegeManager(source, settings, DeviceManagerShape(context))
        stale_root = manager.prepare_adb(context)

        settings.data["privilege_backend"] = "shizuku"
        manager.reset()
        session = PreparedSession(self._ready_state())
        PreparedShizukuClientDouble.session = session
        with patch("openadb.core.privilege.ShizukuClient", PreparedShizukuClientDouble):
            current_shizuku = manager.prepare_adb(context)

        with self.assertRaisesRegex(RuntimeError, "access mode changed"):
            stale_root.run_shell("id")

        result = current_shizuku.run_shell("pm list packages")
        self.assertTrue(result.success)
        self.assertFalse(current_shizuku._backend_cancel_event.is_set())
        self.assertEqual([call[0] for call in session.calls], ["pm list packages"])

    def test_stale_shizuku_error_cannot_cancel_new_root_generation(self) -> None:
        context = device_context()
        settings = FakeSettings("shizuku")
        source = PreparedSourceADB(direct_uid=2000, su_uid=0)
        manager = PrivilegeManager(source, settings, DeviceManagerShape(context))
        session = PreparedSession(self._ready_state())
        PreparedShizukuClientDouble.session = session
        with patch("openadb.core.privilege.ShizukuClient", PreparedShizukuClientDouble):
            stale_shizuku = manager.prepare_adb(context)

        settings.data["privilege_backend"] = "root"
        manager.reset()
        current_root = manager.prepare_adb(context)
        root_status = manager.cached_status()

        stale_shizuku._prepare_logical_result(
            command_result(
                success=False,
                status="permission revoked",
                error_type="shizuku_permission_required",
                exit_code=1,
            )
        )

        self.assertFalse(current_root._backend_cancel_event.is_set())
        self.assertIs(manager.cached_status(), root_status)
        self.assertTrue(current_root.run_shell("id").success)

    def test_non_adb_mode_does_not_silently_fallback_to_standard(self) -> None:
        context = replace(device_context(), mode="Recovery")
        manager, source, _device_manager, _context = self._manager(context=context)

        with patch("openadb.core.privilege.ShizukuClient", PreparedShizukuClientDouble):
            with self.assertRaisesRegex(
                RuntimeError,
                "Select Standard ADB or Root explicitly",
            ):
                manager.prepare_adb(context)

        self.assertIsNotNone(source.last_bound)
        self.assertEqual(PreparedShizukuClientDouble.instances, [])

    def test_prepared_shell_uses_one_session_and_original_direct_adb(self) -> None:
        manager, source, session, prepared = self._prepare()

        result = prepared.enable_package("com.example.app")

        self.assertTrue(result.success)
        self.assertIs(prepared.direct_adb, source.last_bound)
        self.assertIs(PreparedShizukuClientDouble.instances[0].direct_adb, source.last_bound)
        prepare_calls = PreparedShizukuClientDouble.instances[0].prepare_calls
        self.assertEqual(len(prepare_calls), 1)
        preparation_cancel = prepare_calls[0]["cancel_event"]
        self.assertTrue(callable(getattr(preparation_cancel, "is_set", None)))
        self.assertFalse(preparation_cancel.is_set())
        self.assertEqual(len(session.calls), 1)
        self.assertIn("pm enable 'com.example.app'", session.calls[0][0])
        self.assertEqual(source.last_bound.raw_calls, [])
        self.assertEqual(source.last_bound.runner.recorded, [result])
        self.assertEqual(manager.cached_status().uid, 2000)

    def test_prepare_reuses_current_cached_shizuku_state_without_new_probe(self) -> None:
        class CachedStateClientDouble(PreparedShizukuClientDouble):
            instances: ClassVar[list[CachedStateClientDouble]] = []
            session: ClassVar[PreparedSession | None] = None

            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.verified_state_calls: list[tuple[ShizukuState, int | None]] = []

            def session_from_verified_state(
                self,
                state: ShizukuState,
                *,
                expected_uid: int | None = None,
            ) -> PreparedSession:
                self.verified_state_calls.append((state, expected_uid))
                return PreparedSession(state)

        manager, _source, _device_manager, context = self._manager()
        CachedStateClientDouble.session = PreparedSession(self._ready_state())
        with patch("openadb.core.privilege.ShizukuClient", CachedStateClientDouble):
            first = manager.prepare_adb(context)
            second = manager.prepare_adb(context)

        self.assertIsInstance(first, ShizukuAwareADBClient)
        self.assertIsInstance(second, ShizukuAwareADBClient)
        self.assertEqual(len(CachedStateClientDouble.instances), 2)
        first_client, second_client = CachedStateClientDouble.instances
        self.assertEqual(len(first_client.prepare_calls), 1)
        self.assertEqual(second_client.prepare_calls, [])
        self.assertEqual(len(second_client.verified_state_calls), 1)
        reused_state, reused_uid = second_client.verified_state_calls[0]
        self.assertTrue(reused_state.ready)
        self.assertEqual(reused_uid, 2000)
        self.assertEqual(second.run_shell("id").stdout, "session")

    def test_cached_shizuku_state_is_not_reused_after_device_generation_changes(self) -> None:
        class GenerationClientDouble(PreparedShizukuClientDouble):
            instances: ClassVar[list[GenerationClientDouble]] = []
            session: ClassVar[PreparedSession | None] = None

            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.verified_state_calls = 0

            def session_from_verified_state(self, state, **_kwargs):
                self.verified_state_calls += 1
                return PreparedSession(state)

        manager, _source, device_manager, context = self._manager()
        GenerationClientDouble.session = PreparedSession(self._ready_state())
        with patch("openadb.core.privilege.ShizukuClient", GenerationClientDouble):
            manager.prepare_adb(context)
            device_manager.current_generation += 1
            next_context = replace(
                context,
                generation=device_manager.current_generation,
                transport_id="next-transport",
            )
            manager.prepare_adb(next_context)

        self.assertEqual(len(GenerationClientDouble.instances), 2)
        self.assertEqual(len(GenerationClientDouble.instances[0].prepare_calls), 1)
        self.assertEqual(len(GenerationClientDouble.instances[1].prepare_calls), 1)
        self.assertEqual(GenerationClientDouble.instances[1].verified_state_calls, 0)

    def test_cached_shizuku_trust_expires_and_forces_a_fresh_probe(self) -> None:
        class ExpiringClientDouble(PreparedShizukuClientDouble):
            instances: ClassVar[list[ExpiringClientDouble]] = []
            session: ClassVar[PreparedSession | None] = None

            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.verified_state_calls = 0

            def session_from_verified_state(self, state, **_kwargs):
                self.verified_state_calls += 1
                return PreparedSession(state)

        manager, _source, _device_manager, context = self._manager()
        ExpiringClientDouble.session = PreparedSession(self._ready_state())
        clock = [100.0]
        with (
            patch("openadb.core.privilege.ShizukuClient", ExpiringClientDouble),
            patch(
                "openadb.core.privilege.time.monotonic",
                side_effect=lambda: clock[0],
            ),
        ):
            manager.prepare_adb(context)
            clock[0] = 161.0
            manager.prepare_adb(context)

        self.assertEqual(len(ExpiringClientDouble.instances), 2)
        self.assertEqual(len(ExpiringClientDouble.instances[1].prepare_calls), 1)
        self.assertEqual(ExpiringClientDouble.instances[1].verified_state_calls, 0)

    def test_raw_push_pull_and_install_stay_on_direct_adb(self) -> None:
        _manager, source, session, prepared = self._prepare()

        prepared.push(Path("app.apk"), "/sdcard/app.apk")
        prepared.pull("/sdcard/app.apk", Path("app.apk"))
        prepared.install_apk(Path("app.apk"))
        prepared.run_raw(["get-state"])

        self.assertEqual(session.calls, [])
        self.assertEqual(
            [call[0][0] for call in source.last_bound.raw_calls],
            ["push", "pull", "install", "get-state"],
        )

    def test_public_storage_listing_uses_equivalent_direct_shell_for_uid_2000(self) -> None:
        _manager, source, session, prepared = self._prepare(uid=2000)
        expected = (["direct-item"], {"free_bytes": 123})
        source.last_bound.list_files_with_storage = MagicMock(return_value=expected)

        actual = prepared.list_files_with_storage("/sdcard/Download")

        self.assertEqual(actual, expected)
        source.last_bound.list_files_with_storage.assert_called_once()
        forwarded_call = source.last_bound.list_files_with_storage.call_args
        self.assertEqual(forwarded_call.args, ("/sdcard/Download",))
        self.assertFalse(forwarded_call.kwargs["use_root"])
        forwarded_cancel = forwarded_call.kwargs["cancel_event"]
        self.assertFalse(forwarded_cancel.is_set())
        self.assertEqual(session.calls, [])

    def test_uid0_ordinary_internal_listing_uses_equivalent_direct_shell(self) -> None:
        _manager, source, session, prepared = self._prepare(uid=0)
        expected = (["direct-item"], {"free_bytes": 123})
        source.last_bound.list_files_with_storage = MagicMock(return_value=expected)

        first = prepared.list_files_with_storage("/sdcard/Download")
        second = prepared.list_files_with_storage("/storage/emulated/0/Documents")

        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(source.last_bound.list_files_with_storage.call_count, 2)
        for forwarded_call in source.last_bound.list_files_with_storage.call_args_list:
            self.assertFalse(forwarded_call.kwargs["use_root"])
            self.assertFalse(forwarded_call.kwargs["cancel_event"].is_set())
        self.assertEqual(session.calls, [])

    def test_public_storage_fast_path_fails_closed_on_backend_reset(self) -> None:
        for uid in (0, 2000):
            with self.subTest(uid=uid):
                manager, source, _session, prepared = self._prepare(uid=uid)
                observed_cancel = []

                def switch_backend(_path, **kwargs):
                    observed_cancel.append(kwargs["cancel_event"])
                    manager.settings.data["privilege_backend"] = "standard"
                    manager.reset()
                    return [], {}

                source.last_bound.list_files_with_storage = MagicMock(
                    side_effect=switch_backend
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "access mode changed|cancelled",
                ):
                    prepared.list_files_with_storage("/sdcard/Download")

                self.assertEqual(len(observed_cancel), 1)
                self.assertTrue(observed_cancel[0].is_set())

    def test_public_storage_direct_shell_is_context_and_backend_guarded(self) -> None:
        manager, source, _session, prepared = self._prepare(uid=2000)
        source.last_bound.run_shell = MagicMock(return_value=command_result())

        result = prepared.run_public_storage_shell(
            "/sdcard/Download/file.bin",
            "mv temporary final",
        )

        self.assertTrue(result.success)
        source.last_bound.run_shell.assert_called_once()
        forwarded = source.last_bound.run_shell.call_args.kwargs["cancel_event"]
        self.assertFalse(forwarded.is_set())
        with self.assertRaisesRegex(RuntimeError, "limited"):
            prepared.run_public_storage_shell("/data/local/tmp/file.bin", "mv a b")

        manager.settings.data["privilege_backend"] = "standard"
        manager.reset()
        with self.assertRaisesRegex(RuntimeError, "access mode changed|cancelled"):
            prepared.run_public_storage_shell(
                "/sdcard/Download/file.bin",
                "mv temporary final",
            )

    def test_uid0_shizuku_public_transfer_finalizes_on_direct_adb(self) -> None:
        _manager, source, root_session, prepared = self._prepare(uid=0)
        source.last_bound.run_shell = MagicMock(return_value=command_result())

        result = prepared.run_public_storage_shell(
            "/sdcard/Download/file.bin",
            "mv temporary final",
        )

        self.assertTrue(result.success)
        source.last_bound.run_shell.assert_called_once()
        forwarded = source.last_bound.run_shell.call_args.kwargs["cancel_event"]
        self.assertFalse(forwarded.is_set())
        self.assertEqual(root_session.calls, [])
        with self.assertRaisesRegex(RuntimeError, "limited"):
            prepared.run_public_storage_shell(
                "/data/local/tmp/file.bin",
                "mv temporary final",
            )

    def test_uid0_protected_and_removable_listings_keep_shizuku_semantics(self) -> None:
        _manager, source, root_session, prepared = self._prepare(uid=0)
        source.last_bound.list_files_with_storage = MagicMock()

        protected_paths = (
            "/sdcard/Android/data",
            "/storage/emulated/0/Android/obb",
            "/sdcard/Download/../Android/data",
            "/storage/ABCD-1234/Movies",
        )
        for path in protected_paths:
            prepared.list_files_with_storage(path)

        source.last_bound.list_files_with_storage.assert_not_called()
        self.assertEqual(len(root_session.calls), len(protected_paths))
        for path, call in zip(protected_paths, root_session.calls, strict=True):
            self.assertIn(path, call[0])

    def test_uid0_shizuku_single_file_push_streams_and_finalizes_directly(self) -> None:
        _manager, source, root_session, prepared = self._prepare(uid=0)
        streamed = bytearray()

        def accept_stream(_args, **kwargs):
            sink = io.BytesIO()
            kwargs["input_writer"](sink)
            streamed.extend(sink.getvalue())
            return command_result()

        source.last_bound.run_raw_with_input_stream = MagicMock(
            side_effect=accept_stream
        )
        source.last_bound.run_shell = MagicMock(return_value=command_result())
        with tempfile.TemporaryDirectory() as temporary:
            local_file = Path(temporary) / "pixel-transfer.bin"
            local_file.write_bytes(b"pixel shizuku transfer regression")
            result, sent = ADBTransferStrategy()._stream_push_file_to_android_target(
                adb=prepared,
                source=local_file,
                target="/sdcard/Download/pixel-transfer.bin",
                cancel_event=threading.Event(),
                output_callback=None,
                item_callback=None,
                base_done_bytes=0,
                base_done_files=0,
                total_bytes=local_file.stat().st_size,
                total_files=1,
                started=0.0,
                expected_size=local_file.stat().st_size,
            )

        self.assertTrue(result.success)
        self.assertEqual(sent, len(streamed))
        self.assertEqual(bytes(streamed), b"pixel shizuku transfer regression")
        source.last_bound.run_raw_with_input_stream.assert_called_once()
        source.last_bound.run_shell.assert_called_once()
        self.assertIn("mv -f", source.last_bound.run_shell.call_args.args[0])
        self.assertEqual(root_session.calls, [])

    def test_root_semantics_follow_the_verified_shizuku_uid(self) -> None:
        _manager, _source, shell_session, shell = self._prepare(uid=2000)
        self.assertFalse(shell.root_available())
        with self.assertRaisesRegex(RuntimeError, "root UID 0"):
            shell.run_root_shell("id -u")
        self.assertEqual(shell_session.calls, [])

        _manager, _source, root_session, root = self._prepare(uid=0)
        self.assertTrue(root.root_available())
        result = root.run_root_shell("id -u")
        self.assertTrue(result.success)
        self.assertEqual(root_session.calls[0][0], "id -u")

    def test_package_details_are_serialized_in_small_batches(self) -> None:
        packages = [f"com.example.app{index}" for index in range(10)]

        def output_for(values: list[str]) -> str:
            return "\n".join(
                line
                for index, package in enumerate(values)
                for line in (
                    f"OPENADB_PACKAGE:{package}",
                    f"OPENADB_VERSION_NAME:1.{index}",
                    f"OPENADB_VERSION_CODE:{100 + index}",
                    f"OPENADB_LABEL:Example {index}",
                    "OPENADB_END",
                )
            )

        responses = [
            command_result(stdout=output_for(packages[:8])),
            command_result(stdout=output_for(packages[8:])),
        ]
        _manager, _source, session, prepared = self._prepare(responses=responses)
        progress: list[tuple[int, int, str, dict[str, str]]] = []

        details = prepared.get_package_details_many(
            packages,
            max_workers=8,
            progress_callback=lambda *values: progress.append(values),
        )

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(progress), 10)
        self.assertEqual(details[packages[0]]["versionName"], "1.0")
        self.assertEqual(details[packages[8]]["versionName"], "1.0")
        self.assertTrue(all("dumpsys package" in call[0] for call in session.calls))

    def test_truncated_output_is_rejected_and_recorded_without_partial_stdout(self) -> None:
        truncated = command_result(
            stdout="partial\n[OpenADB: Shizuku stdout was truncated at the safety limit.]",
        )
        _manager, source, _session, prepared = self._prepare(responses=[truncated])

        with self.assertRaisesRegex(RuntimeError, "truncated"):
            prepared.run_shell("dumpsys package com.example")

        self.assertFalse(truncated.success)
        self.assertEqual(truncated.stdout, "")
        self.assertEqual(truncated.error_type, "shizuku_output_truncated")
        self.assertEqual(source.last_bound.runner.recorded, [truncated])

    def test_fatal_session_error_invalidates_cached_identity_and_fails_closed(self) -> None:
        revoked = command_result(
            success=False,
            stdout="partial privileged data",
            stderr="permission revoked",
            status="permission revoked",
            exit_code=1,
            error_type="shizuku_permission_required",
        )
        manager, _source, _session, prepared = self._prepare(responses=[revoked])
        self.assertIsNotNone(manager.cached_status())

        with self.assertRaisesRegex(RuntimeError, "permission revoked"):
            prepared.run_shell("id")

        self.assertFalse(revoked.success)
        self.assertEqual(revoked.stdout, "")
        self.assertIsNone(manager.cached_status())

    def test_storage_like_helpers_surface_infrastructure_failure(self) -> None:
        unavailable = command_result(
            success=False,
            stdout="Filesystem      1K-blocks Used Available Use% Mounted on\npartial",
            stderr="Shizuku binder stopped.",
            status="Shizuku binder stopped.",
            exit_code=1,
            error_type="shizuku_unavailable",
        )
        _manager, source, _session, prepared = self._prepare(responses=[unavailable])

        with self.assertRaisesRegex(RuntimeError, "binder stopped"):
            prepared.storage_info("/data/local/tmp")

        self.assertEqual(unavailable.stdout, "")
        self.assertEqual(source.last_bound.runner.recorded, [unavailable])

    def test_normal_command_failure_and_cancellation_remain_command_results(self) -> None:
        normal_failure = command_result(
            success=False,
            stdout="partial",
            stderr="operation not permitted",
            status="Command failed",
            exit_code=1,
            error_type="shizuku_command_failed",
        )
        cancelled = command_result(
            success=False,
            stdout="partial",
            status="Cancelled",
            exit_code=None,
            error_type="cancelled",
        )
        _manager, _source, _session, prepared = self._prepare(
            responses=[normal_failure, cancelled]
        )

        self.assertIs(prepared.run_shell("false"), normal_failure)
        self.assertIs(prepared.run_shell("sleep 1"), cancelled)
        self.assertEqual(normal_failure.stdout, "")
        self.assertEqual(cancelled.stdout, "")

    def test_adb_mode_never_silently_falls_back_when_shizuku_is_unavailable(self) -> None:
        manager, source, _device_manager, context = self._manager()
        unavailable = ShizukuState(
            state="permission_denied",
            installed=True,
            running=True,
            permission="denied",
            uid=None,
            message="Grant Shizuku permission.",
        )
        PreparedShizukuClientDouble.session = PreparedSession(unavailable)

        with (
            patch("openadb.core.privilege.ShizukuClient", PreparedShizukuClientDouble),
            self.assertRaisesRegex(RuntimeError, "Grant Shizuku permission"),
        ):
            manager.prepare_adb(context)

        self.assertFalse(hasattr(source.last_bound, "effective_privilege_backend"))
        self.assertIsNone(manager.cached_status())

    def test_context_is_checked_before_and_after_session_preparation(self) -> None:
        manager, source, device_manager, context = self._manager()
        device_manager.current_generation += 1
        with self.assertRaises(StaleDeviceContext):
            manager.prepare_adb(context)
        self.assertEqual(source.contexts, [])

        manager, source, device_manager, context = self._manager()
        PreparedShizukuClientDouble.session = PreparedSession(self._ready_state())
        PreparedShizukuClientDouble.on_prepare = lambda: setattr(
            device_manager,
            "current_generation",
            context.generation + 1,
        )
        with (
            patch("openadb.core.privilege.ShizukuClient", PreparedShizukuClientDouble),
            self.assertRaises(StaleDeviceContext),
        ):
            manager.prepare_adb(context)
        self.assertEqual(source.contexts, [context])
        self.assertIsNone(manager.cached_status())

    def test_cancelled_checks_requests_and_preparation_are_not_cached(self) -> None:
        manager, source, _device_manager, context = self._manager()
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            manager.prepare_adb(context, cancel_event=cancelled)
        self.assertEqual(source.contexts, [])
        self.assertIsNone(manager.cached_status())

        cancelled_state = ShizukuState(
            state="cancelled",
            message="Shizuku check was cancelled.",
        )

        class CancelledClient:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def check_status(self, **_kwargs) -> ShizukuState:
                return cancelled_state

            def request_permission(self, **_kwargs) -> ShizukuState:
                return cancelled_state

        with patch("openadb.core.privilege.ShizukuClient", CancelledClient):
            manager.check(context)
            self.assertIsNone(manager.cached_status())
            manager.request_shizuku(context)
            self.assertIsNone(manager.cached_status())


class SettingsPrivilegeTests(unittest.TestCase):
    def test_legacy_root_mode_migrates_to_privilege_backend(self) -> None:
        for legacy_enabled, expected in ((False, "standard"), (True, "root")):
            with self.subTest(legacy_enabled=legacy_enabled), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                root.mkdir(parents=True, exist_ok=True)
                (root / "settings.json").write_text(
                    json.dumps({"root_mode_enabled": legacy_enabled}),
                    encoding="utf-8",
                )

                settings = IsolatedSettings(root)

                self.assertEqual(settings.get("privilege_backend"), expected)
                self.assertEqual(settings.get("root_mode_enabled"), legacy_enabled)

    def test_explicit_shizuku_backend_does_not_inherit_legacy_root_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "privilege_backend": "shizuku",
                        "root_mode_enabled": True,
                    }
                ),
                encoding="utf-8",
            )

            settings = IsolatedSettings(root)

            self.assertEqual(settings.get("privilege_backend"), "shizuku")
            self.assertFalse(settings.get("root_mode_enabled"))

    def test_invalid_backend_falls_back_to_standard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "settings.json").write_text(
                json.dumps(
                    {
                        "privilege_backend": "unknown-elevation-provider",
                        "root_mode_enabled": True,
                    }
                ),
                encoding="utf-8",
            )

            settings = IsolatedSettings(root)

            self.assertEqual(settings.get("privilege_backend"), "standard")
            self.assertFalse(settings.get("root_mode_enabled"))

    def test_privilege_backend_is_isolated_per_profile_with_safe_new_profile_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = IsolatedSettings(Path(temporary))

            settings.activate_device_profile("device-a", "Phone A", "Phone")
            self.assertEqual(settings.get("privilege_backend"), "standard")
            self.assertFalse(settings.get("root_mode_enabled"))
            settings.data["privilege_backend"] = "shizuku"
            settings.data["root_mode_enabled"] = False
            settings.save()

            settings.activate_device_profile("device-b", "Phone B", "Phone")
            self.assertEqual(settings.get("privilege_backend"), "standard")
            self.assertFalse(settings.get("root_mode_enabled"))
            settings.data["privilege_backend"] = "root"
            settings.data["root_mode_enabled"] = True
            settings.save()

            settings.activate_device_profile("device-a", "Phone A", "Phone")
            self.assertEqual(settings.get("privilege_backend"), "shizuku")
            self.assertFalse(settings.get("root_mode_enabled"))

            settings.activate_device_profile("device-b", "Phone B", "Phone")
            self.assertEqual(settings.get("privilege_backend"), "root")
            self.assertTrue(settings.get("root_mode_enabled"))

            settings.activate_device_profile("device-c", "Phone C", "Phone")
            self.assertEqual(settings.get("privilege_backend"), "standard")
            self.assertFalse(settings.get("root_mode_enabled"))


if __name__ == "__main__":
    unittest.main()
