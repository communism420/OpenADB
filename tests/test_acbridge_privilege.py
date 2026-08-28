from __future__ import annotations

import base64
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from openadb.core.acbridge import ACBridgeClient, ACBridgePrivilegeResult
from openadb.core.device_context import DeviceContext
from openadb.core.privilege import PrivilegeBackend, PrivilegeStatus
from openadb.models.command_result import CommandResult
from openadb.ui.main_window import MainWindow

REQUEST_ID = "ab" * 16


def command_result(
    *,
    success: bool = True,
    stdout: str = "",
    stderr: str = "",
    status: str = "",
    exit_code: int | None = None,
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
    )


def privilege_payload(
    *,
    request_id: str = REQUEST_ID,
    backend: str = "root",
    state: str = "ready",
    permission: str = "granted",
    uid: int = 0,
    message: str = "ACBridge root access is ready.",
) -> str:
    encoded_message = base64.b64encode(message.encode("utf-8")).decode("ascii")
    return "\n".join(
        (
            ACBridgeClient.PRIVILEGE_PROTOCOL_HEADER,
            f"request_id={request_id}",
            f"backend={backend}",
            f"state={state}",
            f"permission={permission}",
            f"uid={uid}",
            f"message_b64={encoded_message}",
            "",
        )
    )


def device_context(*, mode: str = "ADB") -> DeviceContext:
    profile = Path("profiles") / "device-a"
    return DeviceContext(
        serial="device-a",
        mode=mode,
        transport_id="9",
        profile_key="device-a",
        profile_kind="Phone",
        profile_path=profile,
        backups_path=profile / "backups",
        temp_path=profile / "temp",
        logs_path=profile / "logs",
        generation=7,
    )


class ACBridgePrivilegeProtocolTests(unittest.TestCase):
    def _client(self, adb=None) -> tuple[ACBridgeClient, SimpleNamespace]:
        adb = adb or SimpleNamespace(
            run_shell=MagicMock(return_value=command_result()),
            run_root_shell=MagicMock(return_value=command_result()),
        )
        client = ACBridgeClient(
            adb,
            SimpleNamespace(temp_folder=Path("temp")),
            temp_folder=Path("temp"),
        )
        client.verify_bundled_apk = MagicMock(  # type: ignore[method-assign]
            return_value=(True, "The installed helper matches the bundled APK.")
        )
        return client, adb

    def test_root_request_uses_ordinary_shell_to_start_command_activity(self) -> None:
        shell_commands: list[str] = []

        def run_shell(command: str, **_kwargs) -> CommandResult:
            shell_commands.append(command)
            if command.startswith("result1="):
                return command_result(stdout=privilege_payload())
            return command_result()

        adb = SimpleNamespace(
            run_shell=MagicMock(side_effect=run_shell),
            run_root_shell=MagicMock(return_value=command_result()),
        )
        client, _adb = self._client(adb)
        client.ensure_installed = MagicMock(return_value=(True, "trusted helper"))

        with patch(
            "openadb.core.acbridge.uuid.uuid4",
            return_value=SimpleNamespace(hex=REQUEST_ID),
        ):
            result = client.request_privilege_access("root")

        self.assertTrue(result.ready)
        self.assertEqual(result.uid, 0)
        self.assertEqual(result.request_id, REQUEST_ID)
        client.ensure_installed.assert_called_once_with(
            require_current=True,
            cancel_event=None,
        )
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)
        starts = [command for command in shell_commands if "am start " in command]
        self.assertEqual(len(starts), 1)
        self.assertIn(ACBridgeClient.PRIVILEGE_ACTIVITY, starts[0])
        self.assertIn("--es operation requestPrivilege", starts[0])
        self.assertIn("--es backend 'root'", starts[0])
        self.assertIn(f"--es request_id '{REQUEST_ID}'", starts[0])
        self.assertNotIn("su ", starts[0])
        prepare_index = next(
            index
            for index, command in enumerate(shell_commands)
            if "content delete --uri" in command
            and f"/privilege/{REQUEST_ID}" in command
        )
        start_index = shell_commands.index(starts[0])
        self.assertLess(prepare_index, start_index)
        prepare = shell_commands[prepare_index]
        public_temporary = (
            f"/sdcard/.adac/.privilege_status_{REQUEST_ID}.txt.tmp"
        )
        app_temporary = (
            "/sdcard/Android/data/io.github.communism420.openadb.acbridge/files/openadb/"
            f".privilege_status_{REQUEST_ID}.txt.tmp"
        )
        self.assertIn(public_temporary, prepare)
        self.assertIn(app_temporary, prepare)
        self.assertNotIn("mkdir -p", prepare)
        self.assertNotIn("umask 000", prepare)
        self.assertNotIn("chmod 0666", prepare)
        self.assertNotIn(": >", prepare)
        wait = next(command for command in shell_commands if command.startswith("result1="))
        self.assertIn("content read --uri", wait)
        self.assertIn(f"/privilege/{REQUEST_ID}", wait)
        self.assertIn("deadline=$(( $(date +%s) + 150 ))", wait)
        self.assertNotIn("ticks=", wait)
        adb.run_root_shell.assert_not_called()

    def test_permission_host_opens_foreground_and_dismisses_by_request_id(self) -> None:
        def host_io(command: str, **_kwargs) -> CommandResult:
            if "expected='state=ready'" in command:
                return command_result(
                    stdout=(
                        f"{ACBridgeClient.PERMISSION_HOST_PROTOCOL_HEADER}\n"
                        f"request_id={REQUEST_ID}\nstate=ready\n"
                    )
                )
            if "expected='state=closed'" in command:
                return command_result(
                    stdout=(
                        f"{ACBridgeClient.PERMISSION_HOST_PROTOCOL_HEADER}\n"
                        f"request_id={REQUEST_ID}\nstate=closed\n"
                    )
                )
            return command_result()

        adb = SimpleNamespace(
            run_shell=MagicMock(side_effect=host_io),
            run_root_shell=MagicMock(return_value=command_result()),
        )
        client, _adb = self._client(adb)
        client.ensure_installed = MagicMock(return_value=(True, "current"))

        with patch(
            "openadb.core.acbridge.uuid.uuid4",
            return_value=SimpleNamespace(hex=REQUEST_ID),
        ):
            host = client.start_permission_host("root", timeout=90)
        closed = client.dismiss_permission_host(host.request_id)

        self.assertTrue(host.started)
        self.assertTrue(closed)
        self.assertEqual(host.request_id, REQUEST_ID)
        client.ensure_installed.assert_called_once_with(
            require_current=True,
            cancel_event=None,
        )
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)
        commands = [entry.args[0] for entry in adb.run_shell.call_args_list]
        start = next(command for command in commands if "am start -W" in command)
        self.assertIn("-f 0x10000000", start)
        self.assertIn(ACBridgeClient.PERMISSION_HOST_ACTIVITY, start)
        self.assertIn("--es operation open", start)
        self.assertIn("--es backend 'root'", start)
        self.assertIn(f"--es request_id '{REQUEST_ID}'", start)
        dismiss = next(command for command in commands if "am broadcast" in command)
        self.assertIn(ACBridgeClient.PERMISSION_HOST_RECEIVER, dismiss)
        self.assertIn("--es operation dismiss", dismiss)
        self.assertTrue(any("state=ready" in command for command in commands))
        self.assertTrue(any("state=closed" in command for command in commands))

    def test_root_request_links_terminal_result_to_permission_host(self) -> None:
        client, adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "unused"))
        with (
            patch(
                "openadb.core.acbridge.uuid.uuid4",
                return_value=SimpleNamespace(hex=REQUEST_ID),
            ),
            patch.object(
                client,
                "_wait_for_privilege_result",
                return_value=command_result(stdout=privilege_payload()),
            ),
        ):
            result = client.request_privilege_access(
                "root",
                bridge_is_current=True,
                permission_host_request_id="02" * 16,
            )

        self.assertTrue(result.ready)
        client.ensure_installed.assert_not_called()
        start = next(
            entry.args[0]
            for entry in adb.run_shell.call_args_list
            if "--es operation requestPrivilege" in entry.args[0]
        )
        self.assertIn(
            "--es permission_host_request_id '" + "02" * 16 + "'",
            start,
        )

    def test_permission_host_rejects_unselected_backends_without_android_io(self) -> None:
        client, adb = self._client()

        host = client.start_permission_host("standard")

        self.assertFalse(host.started)
        self.assertEqual(host.backend, "standard")
        adb.run_shell.assert_not_called()

    def test_permission_host_rejects_an_untrusted_same_version_helper(self) -> None:
        client, adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "versionCode 31009"))
        client.verify_bundled_apk = MagicMock(  # type: ignore[method-assign]
            return_value=(
                False,
                "The installed ACBridge helper is not the exact bundled helper.",
            )
        )

        host = client.start_permission_host("root")

        self.assertFalse(host.started)
        self.assertIn("not the exact bundled", host.message)
        client.ensure_installed.assert_called_once_with(
            require_current=True,
            cancel_event=None,
        )
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)
        adb.run_shell.assert_not_called()

    def test_permission_host_dismissal_never_masks_the_permission_result(self) -> None:
        adb = SimpleNamespace(
            run_shell=MagicMock(side_effect=RuntimeError("transport lost")),
            run_root_shell=MagicMock(return_value=command_result()),
        )
        client, _adb = self._client(adb)

        result = client.dismiss_permission_host(REQUEST_ID)

        self.assertFalse(result)
        self.assertGreaterEqual(adb.run_shell.call_count, 2)

    def test_permission_host_start_exception_runs_no_throw_fallback(self) -> None:
        client, adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "current"))
        adb.run_shell.side_effect = RuntimeError("start transport lost")

        with patch(
            "openadb.core.acbridge.uuid.uuid4",
            return_value=SimpleNamespace(hex=REQUEST_ID),
        ):
            host = client.start_permission_host("shizuku")

        self.assertFalse(host.started)
        self.assertEqual(host.request_id, REQUEST_ID)
        self.assertIn("transport lost", host.message)

    def test_privilege_result_cleanup_removes_atomic_temporary_files(self) -> None:
        client, adb = self._client()
        public, app = client._privilege_result_paths(REQUEST_ID)

        client._cleanup_privilege_result(public, app)

        command = adb.run_shell.call_args.args[0]
        self.assertIn(public, command)
        self.assertIn(app, command)
        self.assertIn(
            f"/sdcard/.adac/.privilege_status_{REQUEST_ID}.txt.tmp",
            command,
        )
        self.assertIn(
            "/sdcard/Android/data/io.github.communism420.openadb.acbridge/files/openadb/"
            f".privilege_status_{REQUEST_ID}.txt.tmp",
            command,
        )
        self.assertIn("content delete --uri", command)
        self.assertIn(f"/privilege/{REQUEST_ID}", command)

    def test_preparation_failure_falls_back_to_authenticated_app_result(self) -> None:
        client, adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "trusted helper"))
        prepare_failure = command_result(
            success=False,
            stderr="Shell-owned result targets are unavailable on this OEM.",
            exit_code=13,
        )

        with (
            patch(
                "openadb.core.acbridge.uuid.uuid4",
                return_value=SimpleNamespace(hex=REQUEST_ID),
            ),
            patch.object(
                client,
                "_prepare_privilege_result_channels",
                return_value=prepare_failure,
            ),
            patch.object(
                client,
                "_wait_for_privilege_result",
                return_value=command_result(stdout=privilege_payload()),
            ),
        ):
            result = client.request_privilege_access("root")

        self.assertTrue(result.ready, result.message)
        self.assertTrue(
            any(
                "--es operation requestPrivilege" in call.args[0]
                for call in adb.run_shell.call_args_list
            )
        )

    def test_root_ready_requires_granted_uid_zero(self) -> None:
        client, _adb = self._client()
        invalid_responses = (
            privilege_payload(uid=2000),
            privilege_payload(permission="not_required"),
            privilege_payload(uid=-1),
        )

        for payload in invalid_responses:
            with self.subTest(payload=payload):
                result = client._parse_privilege_result(
                    payload,
                    expected_backend="root",
                    expected_request_id=REQUEST_ID,
                )
                self.assertEqual(result.state, "protocol_error")
                self.assertFalse(result.ready)

    def test_privilege_wait_does_not_mask_an_unreadable_result_file(self) -> None:
        client, adb = self._client()

        client._wait_for_privilege_result(
            "/sdcard/.adac/privilege_status_test.txt",
            "/sdcard/Android/data/io.github.communism420.openadb.acbridge/files/openadb/privilege_status_test.txt",
            timeout=15,
        )

        command = adb.run_shell.call_args.args[0]
        self.assertIn('cat "$result_path" && exit 0', command)
        self.assertIn("ADB shell cannot read it", command)
        self.assertNotIn('cat "$result_path" 2>/dev/null; exit 0', command)
        self.assertIn("provider=''", command)

    def test_unreadable_privilege_result_preserves_specific_shell_error(self) -> None:
        client, _adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "trusted helper"))
        unreadable = command_result(
            success=False,
            stderr="ACBridge privilege result exists but ADB shell cannot read it.",
            status="Command failed with exit code 13",
            exit_code=13,
        )

        with (
            patch("openadb.core.acbridge.uuid.uuid4", return_value=SimpleNamespace(hex=REQUEST_ID)),
            patch.object(client, "_wait_for_privilege_result", return_value=unreadable),
            patch.object(client, "_cancel_privilege_request"),
            patch.object(client, "_cleanup_privilege_result"),
        ):
            result = client.request_privilege_access("root")

        self.assertEqual(result.state, "result_unreadable")
        self.assertEqual(result.message, unreadable.stderr)

    def test_missing_privilege_result_remains_a_timeout(self) -> None:
        client, _adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "trusted helper"))
        timed_out = command_result(
            success=False,
            stderr="ACBridge privilege result was not produced before timeout.",
            status="Command failed with exit code 1",
            exit_code=1,
        )

        with (
            patch("openadb.core.acbridge.uuid.uuid4", return_value=SimpleNamespace(hex=REQUEST_ID)),
            patch.object(client, "_wait_for_privilege_result", return_value=timed_out),
            patch.object(client, "_cancel_privilege_request"),
            patch.object(client, "_cleanup_privilege_result"),
        ):
            result = client.request_privilege_access("root")

        self.assertEqual(result.state, "timed_out")
        self.assertEqual(result.message, timed_out.stderr)

    def test_denied_root_decision_is_preserved_without_becoming_ready(self) -> None:
        client, _adb = self._client()

        result = client._parse_privilege_result(
            privilege_payload(
                state="denied",
                permission="denied",
                uid=-1,
                message="The user denied ACBridge root access.",
            ),
            expected_backend="root",
            expected_request_id=REQUEST_ID,
        )

        self.assertEqual(result.state, "denied")
        self.assertEqual(result.permission, "denied")
        self.assertIsNone(result.uid)
        self.assertEqual(result.message, "The user denied ACBridge root access.")
        self.assertFalse(result.ready)

    def test_parser_rejects_mismatched_or_malformed_responses(self) -> None:
        client, _adb = self._client()
        valid = privilege_payload()
        cases = {
            "wrong header": valid.replace(
                ACBridgeClient.PRIVILEGE_PROTOCOL_HEADER,
                "OPENADB_BRIDGE_PRIVILEGE_STATUS 2",
                1,
            ),
            "wrong request": privilege_payload(request_id="cd" * 16),
            "wrong backend": privilege_payload(backend="standard"),
            "duplicate field": valid + "state=ready\n",
            "missing field": valid.replace("permission=granted\n", ""),
            "unexpected field": valid + "extra=value\n",
            "unknown state": privilege_payload(state="maybe"),
            "invalid uid": valid.replace("uid=0", "uid=not-a-number"),
            "invalid message": valid.replace(
                valid.split("message_b64=", 1)[1].splitlines()[0],
                "%%%",
            ),
        }

        for label, payload in cases.items():
            with self.subTest(label=label):
                result = client._parse_privilege_result(
                    payload,
                    expected_backend="root",
                    expected_request_id=REQUEST_ID,
                )
                self.assertEqual(result.state, "protocol_error")
                self.assertFalse(result.ready)

    def test_pre_cancelled_request_does_not_install_or_touch_android(self) -> None:
        client, adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "unused"))
        cancelled = threading.Event()
        cancelled.set()

        result = client.request_privilege_access(
            "root",
            cancel_event=cancelled,
        )

        self.assertTrue(result.cancelled)
        client.ensure_installed.assert_not_called()
        adb.run_shell.assert_not_called()
        adb.run_root_shell.assert_not_called()

    def test_cancellation_after_activity_start_cleans_request_files(self) -> None:
        cancelled = threading.Event()
        commands: list[str] = []

        def run_shell(command: str, **_kwargs) -> CommandResult:
            commands.append(command)
            if "am start " in command:
                cancelled.set()
            return command_result()

        adb = SimpleNamespace(
            run_shell=MagicMock(side_effect=run_shell),
            run_root_shell=MagicMock(return_value=command_result()),
        )
        client, _adb = self._client(adb)
        client.ensure_installed = MagicMock(return_value=(True, "trusted helper"))

        with patch(
            "openadb.core.acbridge.uuid.uuid4",
            return_value=SimpleNamespace(hex=REQUEST_ID),
        ):
            result = client.request_privilege_access(
                "root",
                cancel_event=cancelled,
            )

        self.assertTrue(result.cancelled)
        self.assertFalse(any(command.startswith("result1=") for command in commands))
        cancellation_commands = [
            command
            for command in commands
            if "--es operation cancelPrivilege" in command
        ]
        self.assertEqual(len(cancellation_commands), 1)
        self.assertIn(ACBridgeClient.PRIVILEGE_ACTIVITY, cancellation_commands[0])
        self.assertIn(REQUEST_ID, cancellation_commands[0])
        cleanup_commands = [command for command in commands if command.startswith("rm -f ")]
        self.assertEqual(len(cleanup_commands), 1)
        self.assertIn(REQUEST_ID, cleanup_commands[0])
        adb.run_root_shell.assert_not_called()

    def test_ambiguous_root_activity_start_failure_cancels_before_cleanup(self) -> None:
        commands: list[str] = []

        def run_shell(command: str, **_kwargs) -> CommandResult:
            commands.append(command)
            if "--es operation requestPrivilege" in command:
                return command_result(
                    success=False,
                    stderr="ADB transport timed out after dispatch.",
                    exit_code=1,
                )
            return command_result()

        adb = SimpleNamespace(
            run_shell=MagicMock(side_effect=run_shell),
            run_root_shell=MagicMock(return_value=command_result()),
        )
        client, _adb = self._client(adb)
        client.ensure_installed = MagicMock(return_value=(True, "trusted helper"))

        with patch(
            "openadb.core.acbridge.uuid.uuid4",
            return_value=SimpleNamespace(hex=REQUEST_ID),
        ):
            result = client.request_privilege_access("root")

        self.assertEqual(result.state, "start_failed")
        start_index = next(
            index
            for index, command in enumerate(commands)
            if "--es operation requestPrivilege" in command
        )
        cancel_index = next(
            index
            for index, command in enumerate(commands)
            if "--es operation cancelPrivilege" in command
        )
        cleanup_index = next(
            index
            for index, command in enumerate(commands)
            if command.startswith("rm -f ")
        )
        self.assertLess(start_index, cancel_index)
        self.assertLess(cancel_index, cleanup_index)
        self.assertIn(REQUEST_ID, commands[cancel_index])
        adb.run_root_shell.assert_not_called()

    def test_untrusted_or_missing_bridge_fails_before_starting_activity(self) -> None:
        client, adb = self._client()
        client.ensure_installed = MagicMock(
            return_value=(False, "The exact signed ACBridge helper is unavailable.")
        )

        result = client.request_privilege_access("root")

        self.assertEqual(result.state, "unavailable")
        self.assertIn("exact signed", result.message)
        adb.run_shell.assert_not_called()
        adb.run_root_shell.assert_not_called()

    def test_root_request_rejects_an_untrusted_same_version_helper(self) -> None:
        client, adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "versionCode 31009"))
        client.verify_bundled_apk = MagicMock(  # type: ignore[method-assign]
            return_value=(False, "Installed ACBridge bytes do not match."),
        )

        result = client.request_privilege_access("root")

        self.assertEqual(result.state, "unavailable")
        self.assertIn("do not match", result.message)
        client.ensure_installed.assert_called_once_with(
            require_current=True,
            cancel_event=None,
        )
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)
        adb.run_shell.assert_not_called()
        adb.run_root_shell.assert_not_called()

    def test_shizuku_is_not_duplicated_by_generic_bridge_protocol(self) -> None:
        client, adb = self._client()
        client.ensure_installed = MagicMock(return_value=(True, "unused"))

        result = client.request_privilege_access("shizuku")

        self.assertEqual(result.state, "unsupported")
        self.assertIn("ShizukuActivity", result.message)
        client.ensure_installed.assert_not_called()
        adb.run_shell.assert_not_called()


class MainWindowACBridgePrivilegeHandshakeTests(unittest.TestCase):
    @staticmethod
    def _status(backend: PrivilegeBackend, context: DeviceContext) -> PrivilegeStatus:
        return PrivilegeStatus(
            backend=backend,
            state="ready",
            uid=0 if backend is PrivilegeBackend.ROOT else 2000,
            level="root" if backend is PrivilegeBackend.ROOT else "shell",
            message=f"{backend.value} ready",
            device_serial=context.serial,
            device_generation=context.generation,
        )

    @staticmethod
    def _host(
        context: DeviceContext,
        shell_status: PrivilegeStatus,
        events: list[str],
    ) -> SimpleNamespace:
        manager = SimpleNamespace(
            check=MagicMock(
                side_effect=lambda *_args, **_kwargs: (
                    events.append("shell") or shell_status
                )
            ),
            validate_operation_lease=MagicMock(
                side_effect=lambda *_args, **_kwargs: events.append("lease")
            ),
        )
        return SimpleNamespace(
            privilege_manager=manager,
            device_manager=SimpleNamespace(
                is_context_current=MagicMock(return_value=True),
            ),
            adb=SimpleNamespace(for_context=MagicMock(return_value=object())),
            settings=object(),
            icon_extractor=object(),
        )

    def test_adb_root_hosts_foreground_before_shell_and_acbridge_requests(self) -> None:
        context = device_context()
        events: list[str] = []
        shell_status = self._status(PrivilegeBackend.ROOT, context)
        host = self._host(context, shell_status, events)
        bridge_result = ACBridgePrivilegeResult(
            backend="root",
            state="ready",
            permission="granted",
            uid=0,
            message="ACBridge root ready",
            request_id=REQUEST_ID,
        )
        bridge = MagicMock()
        bridge.start_permission_host.side_effect = (
            lambda *_args, **_kwargs: events.append("host-open")
            or SimpleNamespace(started=True, request_id="02" * 16, message="ready")
        )
        bridge.request_privilege_access.side_effect = (
            lambda *_args, **_kwargs: events.append("bridge") or bridge_result
        )
        bridge.dismiss_permission_host.side_effect = (
            lambda *_args, **_kwargs: events.append("host-close")
        )
        lease = object()
        cancelled = threading.Event()

        with patch(
            "openadb.ui.main_window.ACBridgeClient",
            return_value=bridge,
        ) as bridge_type:
            result = MainWindow._check_shell_and_acbridge_access(
                host,
                context,
                backend=PrivilegeBackend.ROOT,
                cancel_event=cancelled,
                privilege_lease=lease,
            )

        self.assertIs(result.shell, shell_status)
        self.assertIs(result.bridge, bridge_result)
        self.assertEqual(
            events,
            ["host-open", "shell", "lease", "bridge", "lease", "host-close"],
        )
        host.privilege_manager.check.assert_called_once_with(
            context,
            backend=PrivilegeBackend.ROOT,
            cancel_event=cancelled,
            privilege_lease=lease,
        )
        bridge_type.assert_called_once_with(
            host.adb.for_context.return_value,
            host.settings,
            host.icon_extractor,
            temp_folder=context.temp_path,
        )
        bridge.request_privilege_access.assert_called_once_with(
            "root",
            cancel_event=cancelled,
            bridge_is_current=True,
            permission_host_request_id="02" * 16,
        )
        bridge.start_permission_host.assert_called_once_with(
            "root",
            timeout=420,
            cancel_event=cancelled,
        )
        bridge.dismiss_permission_host.assert_called_once_with("02" * 16)
        self.assertEqual(
            host.privilege_manager.validate_operation_lease.call_args_list,
            [
                call(
                    lease,
                    "The selected access mode changed before ACBridge requested Root access.",
                ),
                call(
                    lease,
                    "The selected access mode changed while ACBridge requested Root access.",
                ),
            ],
        )

    def test_shizuku_uses_its_existing_acbridge_activity_without_second_request(self) -> None:
        context = device_context()
        events: list[str] = []
        shell_status = self._status(PrivilegeBackend.SHIZUKU, context)
        host = self._host(context, shell_status, events)

        with patch("openadb.ui.main_window.ACBridgeClient") as bridge_type:
            result = MainWindow._check_shell_and_acbridge_access(
                host,
                context,
                backend=PrivilegeBackend.SHIZUKU,
                cancel_event=threading.Event(),
                privilege_lease=object(),
            )

        self.assertIs(result.shell, shell_status)
        self.assertIsNone(result.bridge)
        self.assertEqual(events, ["shell"])
        bridge_type.assert_not_called()

    def test_root_permission_host_closes_when_shell_check_raises(self) -> None:
        context = device_context()
        events: list[str] = []
        shell_status = self._status(PrivilegeBackend.ROOT, context)
        host = self._host(context, shell_status, events)
        host.privilege_manager.check.side_effect = RuntimeError("shell failed")
        bridge = MagicMock()
        bridge.start_permission_host.return_value = SimpleNamespace(
            started=True,
            request_id="04" * 16,
            message="ready",
        )

        with (
            patch("openadb.ui.main_window.ACBridgeClient", return_value=bridge),
            self.assertRaisesRegex(RuntimeError, "shell failed"),
        ):
            MainWindow._check_shell_and_acbridge_access(
                host,
                context,
                backend=PrivilegeBackend.ROOT,
                cancel_event=threading.Event(),
                privilege_lease=object(),
            )

        bridge.request_privilege_access.assert_not_called()
        bridge.dismiss_permission_host.assert_called_once_with("04" * 16)
        host.privilege_manager.validate_operation_lease.assert_not_called()

    def test_failed_root_permission_host_blocks_shell_and_bridge_requests(self) -> None:
        context = device_context()
        events: list[str] = []
        shell_status = self._status(PrivilegeBackend.ROOT, context)
        host = self._host(context, shell_status, events)
        bridge = MagicMock()
        bridge.start_permission_host.return_value = SimpleNamespace(
            started=False,
            request_id="",
            message="foreground failed",
        )

        with (
            patch("openadb.ui.main_window.ACBridgeClient", return_value=bridge),
            self.assertRaisesRegex(RuntimeError, "foreground failed"),
        ):
            MainWindow._check_shell_and_acbridge_access(
                host,
                context,
                backend=PrivilegeBackend.ROOT,
                cancel_event=threading.Event(),
                privilege_lease=object(),
            )

        host.privilege_manager.check.assert_not_called()
        bridge.request_privilege_access.assert_not_called()
        bridge.dismiss_permission_host.assert_not_called()

    def test_recovery_root_does_not_try_to_launch_android_acbridge_activity(self) -> None:
        context = device_context(mode="Recovery")
        events: list[str] = []
        shell_status = self._status(PrivilegeBackend.ROOT, context)
        host = self._host(context, shell_status, events)

        with patch("openadb.ui.main_window.ACBridgeClient") as bridge_type:
            result = MainWindow._check_shell_and_acbridge_access(
                host,
                context,
                backend=PrivilegeBackend.ROOT,
                cancel_event=threading.Event(),
                privilege_lease=object(),
            )

        self.assertIs(result.shell, shell_status)
        self.assertIsNone(result.bridge)
        self.assertEqual(events, ["shell"])
        bridge_type.assert_not_called()

    def test_shell_cancellation_stops_before_acbridge_root_request(self) -> None:
        context = device_context()
        events: list[str] = []
        cancelled = threading.Event()
        shell_status = self._status(PrivilegeBackend.ROOT, context)
        host = self._host(context, shell_status, events)

        def check_then_cancel(*_args, **_kwargs) -> PrivilegeStatus:
            events.append("shell")
            cancelled.set()
            return shell_status

        host.privilege_manager.check.side_effect = check_then_cancel

        bridge = MagicMock()
        bridge.start_permission_host.return_value = SimpleNamespace(
            started=True,
            request_id="03" * 16,
            message="ready",
        )
        with patch(
            "openadb.ui.main_window.ACBridgeClient",
            return_value=bridge,
        ) as bridge_type:
            result = MainWindow._check_shell_and_acbridge_access(
                host,
                context,
                backend=PrivilegeBackend.ROOT,
                cancel_event=cancelled,
                privilege_lease=object(),
            )

        self.assertIs(result.shell, shell_status)
        self.assertIsNone(result.bridge)
        self.assertEqual(events, ["shell"])
        bridge_type.assert_called_once()
        bridge.start_permission_host.assert_called_once()
        bridge.request_privilege_access.assert_not_called()
        bridge.dismiss_permission_host.assert_called_once_with("03" * 16)

    def test_shell_and_acbridge_root_decisions_remain_separate_in_status(self) -> None:
        context = device_context()
        shell_status = self._status(PrivilegeBackend.ROOT, context)
        host = SimpleNamespace(
            _acbridge_privilege_result=ACBridgePrivilegeResult(
                backend="root",
                state="denied",
                permission="denied",
                uid=None,
                message="Root permission was denied for OpenADB Bridge.",
                request_id=REQUEST_ID,
            ),
            _acbridge_privilege_key=(
                PrivilegeBackend.ROOT,
                context.serial,
                context.generation,
            ),
        )

        combined = MainWindow._status_with_acbridge_privilege(host, shell_status)

        self.assertIsNotNone(combined)
        assert combined is not None
        self.assertTrue(combined.root)
        self.assertIn("Android shell: root ready", combined.message)
        self.assertIn("ACBridge Root: Root permission was denied", combined.message)

    def test_stale_acbridge_decision_is_not_applied_to_new_generation(self) -> None:
        old_context = device_context()
        new_context = DeviceContext(
            serial=old_context.serial,
            mode=old_context.mode,
            transport_id="10",
            profile_key=old_context.profile_key,
            profile_kind=old_context.profile_kind,
            profile_path=old_context.profile_path,
            backups_path=old_context.backups_path,
            temp_path=old_context.temp_path,
            logs_path=old_context.logs_path,
            generation=old_context.generation + 1,
        )
        new_status = self._status(PrivilegeBackend.ROOT, new_context)
        host = SimpleNamespace(
            _acbridge_privilege_result=ACBridgePrivilegeResult(
                backend="root",
                state="ready",
                permission="granted",
                uid=0,
                message="old result",
                request_id=REQUEST_ID,
            ),
            _acbridge_privilege_key=(
                PrivilegeBackend.ROOT,
                old_context.serial,
                old_context.generation,
            ),
        )

        self.assertIs(
            MainWindow._status_with_acbridge_privilege(host, new_status),
            new_status,
        )


if __name__ == "__main__":
    unittest.main()
