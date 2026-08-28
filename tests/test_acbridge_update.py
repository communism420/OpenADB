from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from openadb.core.acbridge import ACBridgeClient, ACBridgeUpdateResult
from openadb.core.device_context import DeviceContext
from openadb.core.operations import OperationConflictError, OperationRegistry
from openadb.core.privilege import PrivilegeBackend
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo
from openadb.ui.main_window import MainWindow
from openadb.version import ACBRIDGE_VERSION_CODE


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


def device_context(
    serial: str = "device-a",
    generation: int = 7,
    transport_id: str = "11",
) -> DeviceContext:
    root = Path("profiles") / serial
    return DeviceContext(
        serial=serial,
        mode="ADB",
        transport_id=transport_id,
        profile_key=serial,
        profile_kind="Phone",
        profile_path=root,
        backups_path=root / "backups",
        temp_path=root / "temp",
        logs_path=root / "logs",
        generation=generation,
    )


class ACBridgeUpdateDecisionTests(unittest.TestCase):
    def _client(
        self,
        apk: Path,
        versions: list[str | CommandResult],
        *,
        installed: bool | list[bool] = True,
        path_result: CommandResult | list[CommandResult] | None = None,
        package_list_result: CommandResult | None = None,
        install_result: CommandResult | None = None,
    ) -> tuple[ACBridgeClient, SimpleNamespace]:
        version_outputs = list(versions)
        installed_states = list(installed) if isinstance(installed, list) else None
        last_installed_state = bool(installed_states[-1]) if installed_states else bool(installed)
        path_results = list(path_result) if isinstance(path_result, list) else None

        def run_shell(command: str, **_kwargs) -> CommandResult:
            if command.startswith("pm path "):
                if path_results:
                    return path_results.pop(0)
                if isinstance(path_result, CommandResult):
                    return path_result
                current_installed = (
                    installed_states.pop(0)
                    if installed_states
                    else last_installed_state
                )
                return command_result(
                    stdout=(
                        "package:/data/app/com.communism420.acbridge/base.apk\n"
                        if current_installed
                        else ""
                    )
                )
            if command.startswith("pm list packages "):
                if package_list_result is not None:
                    return package_list_result
                return command_result(
                    success=False,
                    status="Unexpected secondary package query in test fixture",
                    exit_code=1,
                )
            if command.startswith("dumpsys package "):
                output = version_outputs.pop(0) if version_outputs else ""
                return output if isinstance(output, CommandResult) else command_result(stdout=output)
            raise AssertionError(f"Unexpected shell command: {command}")

        adb = SimpleNamespace(
            run_shell=MagicMock(side_effect=run_shell),
            run_raw=MagicMock(return_value=install_result or command_result()),
            install_apk_with_permissions=MagicMock(
                return_value=install_result or command_result()
            ),
        )
        client = ACBridgeClient(
            adb,
            SimpleNamespace(temp_folder=apk.parent),
            temp_folder=apk.parent,
        )
        client.bundled_apk_path = MagicMock(return_value=apk)  # type: ignore[method-assign]
        client.verify_bundled_apk = MagicMock(  # type: ignore[method-assign]
            return_value=(True, "exact helper")
        )
        return client, adb

    def test_older_helper_is_replaced_and_exactly_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge-3.1.0.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [
                    "  versionCode=31003 minSdk=23\n",
                    f"versionCode={ACBRIDGE_VERSION_CODE}\n",
                ],
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "updated")
        self.assertEqual(result.previous_version_code, 31003)
        self.assertEqual(result.installed_version_code, ACBRIDGE_VERSION_CODE)
        adb.run_raw.assert_called_once_with(
            ["install", "-r", str(apk)],
            timeout=300,
            cancel_event=None,
        )
        self.assertNotIn("-g", adb.run_raw.call_args.args[0])
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)

    def test_equal_helper_is_not_reinstalled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(
                Path(temporary) / "ACBridge.apk",
                [f"versionCode={ACBRIDGE_VERSION_CODE}\n"],
            )
            result = client.update_if_outdated()

        self.assertEqual(result.state, "current")
        adb.run_raw.assert_not_called()
        client.verify_bundled_apk.assert_not_called()
        client.bundled_apk_path.assert_not_called()

    def test_newer_helper_is_never_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(
                Path(temporary) / "ACBridge.apk",
                ["versionCode=41000\n"],
            )
            result = client.update_if_outdated()

        self.assertEqual(result.state, "newer")
        adb.run_raw.assert_not_called()
        client.verify_bundled_apk.assert_not_called()
        client.bundled_apk_path.assert_not_called()

    def test_missing_helper_is_installed_and_exactly_verified_on_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [f"versionCode={ACBRIDGE_VERSION_CODE}\n"],
                installed=[False, True],
            )
            result = client.update_if_outdated()

        self.assertEqual(result.state, "installed")
        self.assertTrue(result.installed)
        self.assertTrue(result.changed)
        self.assertFalse(result.updated)
        self.assertIsNone(result.previous_version_code)
        self.assertEqual(result.installed_version_code, ACBRIDGE_VERSION_CODE)
        adb.install_apk_with_permissions.assert_called_once_with(
            apk,
            cancel_event=None,
        )
        adb.run_raw.assert_not_called()
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)

    def test_failed_empty_pm_path_installs_after_exact_empty_package_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [f"versionCode={ACBRIDGE_VERSION_CODE}\n"],
                path_result=[
                    command_result(success=False, exit_code=1),
                    command_result(
                        stdout="package:/data/app/com.communism420.acbridge/base.apk\n"
                    ),
                ],
                package_list_result=command_result(stdout=""),
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "installed")
        adb.install_apk_with_permissions.assert_called_once_with(
            apk,
            cancel_event=None,
        )
        package_list_calls = [
            call
            for call in adb.run_shell.call_args_list
            if call.args[0].startswith("pm list packages ")
        ]
        self.assertEqual(len(package_list_calls), 1)
        self.assertIn(ACBridgeClient.PACKAGE, package_list_calls[0].args[0])
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)

    def test_failed_empty_pm_path_with_uncertain_secondary_query_never_installs(self) -> None:
        secondary_results = (
            command_result(success=False, exit_code=1),
            command_result(
                success=False,
                status="device offline",
                exit_code=1,
                error_type="device_offline",
            ),
            command_result(stdout="unexpected package-manager output\n"),
            command_result(stderr="unexpected package-manager warning\n"),
        )
        for secondary_result in secondary_results:
            with self.subTest(secondary_result=secondary_result), tempfile.TemporaryDirectory() as temporary:
                client, adb = self._client(
                    Path(temporary) / "ACBridge.apk",
                    [],
                    path_result=command_result(success=False, exit_code=1),
                    package_list_result=secondary_result,
                )

                result = client.update_if_outdated()

            self.assertEqual(result.state, "query_failed")
            adb.install_apk_with_permissions.assert_not_called()
            adb.run_raw.assert_not_called()
            package_list_calls = [
                call
                for call in adb.run_shell.call_args_list
                if call.args[0].startswith("pm list packages ")
            ]
            self.assertEqual(len(package_list_calls), 1)

    def test_failed_empty_pm_path_with_exact_package_present_never_installs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(
                Path(temporary) / "ACBridge.apk",
                [],
                path_result=command_result(success=False, exit_code=1),
                package_list_result=command_result(
                    stdout="package:com.communism420.acbridge\n"
                ),
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "query_failed")
        adb.install_apk_with_permissions.assert_not_called()
        adb.run_raw.assert_not_called()
        package_list_calls = [
            call
            for call in adb.run_shell.call_args_list
            if call.args[0].startswith("pm list packages ")
        ]
        self.assertEqual(len(package_list_calls), 1)

    def test_unknown_package_primary_requires_consistent_secondary_absence(self) -> None:
        contradictory_results = (
            command_result(
                success=False,
                status="secondary query failed",
                exit_code=1,
            ),
            command_result(stdout="package:com.communism420.acbridge\n"),
        )
        for secondary_result in contradictory_results:
            with (
                self.subTest(secondary_result=secondary_result),
                tempfile.TemporaryDirectory() as temporary,
            ):
                client, adb = self._client(
                    Path(temporary) / "ACBridge.apk",
                    [],
                    path_result=command_result(
                        success=False,
                        stderr="Error: Unknown package: com.communism420.acbridge",
                        exit_code=1,
                    ),
                    package_list_result=secondary_result,
                )

                result = client.update_if_outdated()

            self.assertEqual(result.state, "query_failed")
            adb.install_apk_with_permissions.assert_not_called()
            adb.run_raw.assert_not_called()
            package_list_calls = [
                call
                for call in adb.run_shell.call_args_list
                if call.args[0].startswith("pm list packages ")
            ]
            self.assertEqual(len(package_list_calls), 1)

    def test_unknown_package_primary_installs_after_exact_empty_secondary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [f"versionCode={ACBRIDGE_VERSION_CODE}\n"],
                path_result=[
                    command_result(
                        success=False,
                        stderr="Error: Unknown package: com.communism420.acbridge",
                        exit_code=1,
                    ),
                    command_result(
                        stdout="package:/data/app/com.communism420.acbridge/base.apk\n"
                    ),
                ],
                package_list_result=command_result(stdout=""),
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "installed")
        adb.install_apk_with_permissions.assert_called_once_with(
            apk,
            cancel_event=None,
        )
        package_list_calls = [
            call
            for call in adb.run_shell.call_args_list
            if call.args[0].startswith("pm list packages ")
        ]
        self.assertEqual(len(package_list_calls), 1)
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)

    def test_malformed_secondary_package_names_never_trigger_install(self) -> None:
        malformed_outputs = (
            "package:\n",
            "package:!!!\n",
            "package:/data/app/com.communism420.acbridge/base.apk\n",
            "package:com.communism420..acbridge\n",
            "package:com.communism420.acbridge extra\n",
            "package: com.communism420.acbridge.beta\n",
            " package:com.communism420.acbridge.beta\n",
        )
        for output in malformed_outputs:
            with (
                self.subTest(output=output),
                tempfile.TemporaryDirectory() as temporary,
            ):
                client, adb = self._client(
                    Path(temporary) / "ACBridge.apk",
                    [],
                    path_result=command_result(success=False, exit_code=1),
                    package_list_result=command_result(stdout=output),
                )

                result = client.update_if_outdated()

            self.assertEqual(result.state, "query_failed")
            adb.install_apk_with_permissions.assert_not_called()
            adb.run_raw.assert_not_called()
            package_list_calls = [
                call
                for call in adb.run_shell.call_args_list
                if call.args[0].startswith("pm list packages ")
            ]
            self.assertEqual(len(package_list_calls), 1)

    def test_valid_sibling_package_still_confirms_exact_package_absence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [f"versionCode={ACBRIDGE_VERSION_CODE}\n"],
                path_result=[
                    command_result(success=False, exit_code=1),
                    command_result(
                        stdout="package:/data/app/com.communism420.acbridge/base.apk\n"
                    ),
                ],
                package_list_result=command_result(
                    stdout="package:com.communism420.acbridge.beta\n"
                ),
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "installed")
        adb.install_apk_with_permissions.assert_called_once_with(
            apk,
            cancel_event=None,
        )

    def test_successful_pm_path_with_garbage_or_stderr_is_fail_closed(self) -> None:
        malformed_results = (
            command_result(stdout="unexpected package-manager output\n"),
            command_result(stderr="unexpected package-manager warning\n"),
            command_result(
                stdout="package:/data/app/com.communism420.acbridge/base.apk\n",
                stderr="unexpected package-manager warning\n",
            ),
            command_result(
                stdout=(
                    "package:/data/app/com.communism420.acbridge/base.apk\n"
                    "unexpected trailing output\n"
                ),
            ),
        )
        for path_result in malformed_results:
            with (
                self.subTest(path_result=path_result),
                tempfile.TemporaryDirectory() as temporary,
            ):
                client, adb = self._client(
                    Path(temporary) / "ACBridge.apk",
                    [f"versionCode={ACBRIDGE_VERSION_CODE}\n"],
                    path_result=path_result,
                    package_list_result=command_result(stdout=""),
                )

                result = client.update_if_outdated()

            self.assertEqual(result.state, "query_failed")
            adb.install_apk_with_permissions.assert_not_called()
            adb.run_raw.assert_not_called()
            package_list_calls = [
                call
                for call in adb.run_shell.call_args_list
                if call.args[0].startswith("pm list packages ")
            ]
            self.assertEqual(package_list_calls, [])

    def test_first_post_install_absence_is_retried_until_package_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [f"versionCode={ACBRIDGE_VERSION_CODE}\n"],
                installed=[False, False, True],
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "installed")
        self.assertEqual(result.installed_version_code, ACBRIDGE_VERSION_CODE)
        adb.install_apk_with_permissions.assert_called_once_with(
            apk,
            cancel_event=None,
        )
        package_path_calls = [
            call
            for call in adb.run_shell.call_args_list
            if call.args[0].startswith("pm path ")
        ]
        self.assertEqual(len(package_path_calls), 3)
        client.verify_bundled_apk.assert_called_once_with(cancel_event=None)

    def test_missing_helper_install_failure_never_attempts_destructive_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [],
                installed=False,
                install_result=command_result(
                    success=False,
                    stderr="Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]",
                    exit_code=1,
                ),
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "install_failed")
        self.assertIn("INSTALL_FAILED_INSUFFICIENT_STORAGE", result.message)
        self.assertFalse(result.transient)
        self.assertFalse(result.should_retry)
        adb.install_apk_with_permissions.assert_called_once_with(
            apk,
            cancel_event=None,
        )
        adb.run_raw.assert_not_called()
        client.verify_bundled_apk.assert_not_called()

    def test_missing_helper_cancellation_after_probe_prevents_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            cancelled = threading.Event()
            client, adb = self._client(apk, [], installed=False)
            original = adb.run_shell.side_effect

            def cancel_after_missing_probe(command: str, **kwargs) -> CommandResult:
                result = original(command, **kwargs)
                if command.startswith("pm path "):
                    cancelled.set()
                return result

            adb.run_shell.side_effect = cancel_after_missing_probe
            result = client.update_if_outdated(cancel_event=cancelled)

        self.assertEqual(result.state, "cancelled")
        adb.run_raw.assert_not_called()
        adb.install_apk_with_permissions.assert_not_called()
        client.verify_bundled_apk.assert_not_called()

    def test_missing_helper_without_bundled_apk_fails_without_device_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(
                Path(temporary) / "missing.apk",
                [],
                installed=False,
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "install_failed")
        self.assertIn("not found", result.message)
        adb.run_raw.assert_not_called()
        adb.install_apk_with_permissions.assert_not_called()
        client.verify_bundled_apk.assert_not_called()

    def test_missing_helper_signature_mismatch_is_safe_and_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [],
                installed=False,
                install_result=command_result(
                    success=False,
                    status="INSTALL_FAILED_UPDATE_INCOMPATIBLE",
                    exit_code=1,
                ),
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "install_failed")
        self.assertIn("different signature", result.message)
        self.assertFalse(result.transient)
        self.assertFalse(result.should_retry)
        adb.install_apk_with_permissions.assert_called_once_with(
            apk,
            cancel_event=None,
        )
        adb.run_raw.assert_not_called()
        client.verify_bundled_apk.assert_not_called()

    def test_missing_helper_transient_install_failures_request_bounded_retry(self) -> None:
        transient_failures = (
            command_result(
                success=False,
                status="device offline",
                exit_code=1,
                error_type="device_offline",
            ),
            command_result(
                success=False,
                stderr="transport closed",
                exit_code=1,
                error_type="transport_error",
            ),
            command_result(
                success=False,
                status="Command timed out",
                exit_code=1,
                error_type="timeout",
            ),
        )
        for install_result in transient_failures:
            with self.subTest(install_result=install_result), tempfile.TemporaryDirectory() as temporary:
                apk = Path(temporary) / "ACBridge.apk"
                apk.write_bytes(b"apk")
                client, adb = self._client(
                    apk,
                    [],
                    installed=False,
                    install_result=install_result,
                )

                result = client.update_if_outdated()

            self.assertEqual(result.state, "install_failed")
            self.assertTrue(result.transient)
            self.assertTrue(result.should_retry)
            adb.install_apk_with_permissions.assert_called_once()
            adb.run_raw.assert_not_called()

    def test_transport_error_is_not_misread_as_missing_or_old(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(
                Path(temporary) / "ACBridge.apk",
                [],
                path_result=command_result(
                    success=False,
                    status="device offline",
                    exit_code=1,
                    error_type="device_offline",
                ),
            )
            result = client.update_if_outdated()

        self.assertEqual(result.state, "query_failed")
        adb.run_raw.assert_not_called()

    def test_malformed_or_ambiguous_version_never_triggers_install(self) -> None:
        cases = (
            "versionName=3.0.3\n",
            "versionCode=30003\n  versionCode=31002\n",
            "versionCode=0\n",
        )
        for output in cases:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as temporary:
                client, adb = self._client(
                    Path(temporary) / "ACBridge.apk",
                    [output],
                )
                result = client.update_if_outdated()
            self.assertEqual(result.state, "query_failed")
            adb.run_raw.assert_not_called()

    def test_pre_cancelled_check_performs_no_adb_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(Path(temporary) / "ACBridge.apk", [])
            cancelled = threading.Event()
            cancelled.set()
            result = client.update_if_outdated(cancel_event=cancelled)

        self.assertEqual(result.state, "cancelled")
        adb.run_shell.assert_not_called()
        adb.run_raw.assert_not_called()

    def test_cancellation_after_version_query_prevents_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            cancelled = threading.Event()
            client, adb = self._client(apk, ["versionCode=30003\n"])
            original = adb.run_shell.side_effect

            def cancel_after_query(command: str, **kwargs) -> CommandResult:
                result = original(command, **kwargs)
                if command.startswith("dumpsys package "):
                    cancelled.set()
                return result

            adb.run_shell.side_effect = cancel_after_query
            result = client.update_if_outdated(cancel_event=cancelled)

        self.assertEqual(result.state, "cancelled")
        adb.run_raw.assert_not_called()

    def test_signature_mismatch_never_uninstalls_existing_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                ["versionCode=30003\n"],
                install_result=command_result(
                    success=False,
                    status="INSTALL_FAILED_UPDATE_INCOMPATIBLE",
                    exit_code=1,
                ),
            )
            result = client.update_if_outdated()

        self.assertEqual(result.state, "update_failed")
        self.assertIn("different signature", result.message)
        self.assertEqual(adb.run_raw.call_count, 1)
        self.assertEqual(adb.run_raw.call_args.args[0][0:2], ["install", "-r"])

    def test_old_post_install_version_is_not_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                ["versionCode=30003\n", "versionCode=30003\n"],
            )
            result = client.update_if_outdated()

        self.assertEqual(result.state, "update_failed")
        self.assertEqual(result.installed_version_code, 30003)
        adb.run_raw.assert_called_once()
        client.verify_bundled_apk.assert_not_called()

    def test_exact_apk_verification_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, _adb = self._client(
                apk,
                [
                    "versionCode=30003\n",
                    f"versionCode={ACBRIDGE_VERSION_CODE}\n",
                ],
            )
            client.verify_bundled_apk.return_value = (False, "APK bytes differ")
            result = client.update_if_outdated()

        self.assertEqual(result.state, "verification_failed")
        self.assertIn("bytes differ", result.message)

    def test_post_install_package_query_is_retried_before_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                [
                    "versionCode=30003\n",
                    command_result(
                        success=False,
                        stderr="PackageManager is still publishing the update",
                        exit_code=1,
                    ),
                    f"versionCode={ACBRIDGE_VERSION_CODE}\n",
                ],
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "updated")
        dumpsys_calls = [
            call
            for call in adb.run_shell.call_args_list
            if call.args[0].startswith("dumpsys package ")
        ]
        self.assertEqual(len(dumpsys_calls), 3)
        client.verify_bundled_apk.assert_called_once()

    def test_install_failure_prefers_package_manager_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, _adb = self._client(
                apk,
                ["versionCode=30003\n"],
                install_result=command_result(
                    success=False,
                    stderr="Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]",
                    status="Command failed with exit code 1",
                    exit_code=1,
                ),
            )

            result = client.update_if_outdated()

        self.assertEqual(result.state, "update_failed")
        self.assertIn("INSTALL_FAILED_INSUFFICIENT_STORAGE", result.message)

    def test_cancellation_while_waiting_for_install_lock_runs_no_adb_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(Path(temporary) / "ACBridge.apk", [])
            cancelled = threading.Event()
            results: list[ACBridgeUpdateResult] = []
            lock = ACBridgeClient._INSTALL_LOCK
            lock.acquire()
            try:
                worker = threading.Thread(
                    target=lambda: results.append(
                        client.update_if_outdated(cancel_event=cancelled)
                    )
                )
                worker.start()
                time.sleep(0.05)
                cancelled.set()
                worker.join(timeout=2)
            finally:
                lock.release()

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0].state, "cancelled")
        adb.run_shell.assert_not_called()
        adb.run_raw.assert_not_called()

    def test_missing_bundled_apk_blocks_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(
                Path(temporary) / "missing.apk",
                ["versionCode=30003\n"],
            )
            result = client.update_if_outdated()

        self.assertEqual(result.state, "update_failed")
        adb.run_raw.assert_not_called()

    def test_require_current_does_not_accept_old_installed_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            client, adb = self._client(
                apk,
                ["versionCode=30003\n", "versionCode=30003\n"],
            )

            ready, message = client.ensure_installed(require_current=True)

        self.assertFalse(ready)
        self.assertIn(f"required versionCode {ACBRIDGE_VERSION_CODE}", message)
        adb.install_apk_with_permissions.assert_called_once()

    def test_explicit_setup_does_not_install_when_package_query_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, adb = self._client(
                Path(temporary) / "ACBridge.apk",
                [],
                path_result=command_result(
                    success=False,
                    stderr="Security exception while querying package manager",
                    status="Command failed with exit code 1",
                    exit_code=1,
                    error_type="permission_denied",
                ),
            )

            ready, message = client.ensure_installed(require_current=True)

        self.assertFalse(ready)
        self.assertIn("Security exception", message)
        adb.install_apk_with_permissions.assert_not_called()

    def test_concurrent_checks_share_one_install_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            apk = Path(temporary) / "ACBridge.apk"
            apk.write_bytes(b"apk")
            state_lock = threading.Lock()
            state = {"version": 30003, "installs": 0}

            def run_shell(command: str, **_kwargs) -> CommandResult:
                if command.startswith("pm path "):
                    return command_result(
                        stdout="package:/data/app/com.communism420.acbridge/base.apk\n"
                    )
                with state_lock:
                    version = state["version"]
                return command_result(stdout=f"versionCode={version}\n")

            def run_raw(_command, **_kwargs) -> CommandResult:
                with state_lock:
                    state["installs"] += 1
                time.sleep(0.05)
                with state_lock:
                    state["version"] = ACBRIDGE_VERSION_CODE
                return command_result()

            adb = SimpleNamespace(
                run_shell=MagicMock(side_effect=run_shell),
                run_raw=MagicMock(side_effect=run_raw),
            )
            clients = [
                ACBridgeClient(
                    adb,
                    SimpleNamespace(temp_folder=apk.parent),
                    temp_folder=apk.parent,
                )
                for _index in range(2)
            ]
            for client in clients:
                client.bundled_apk_path = MagicMock(return_value=apk)  # type: ignore[method-assign]
                client.verify_bundled_apk = MagicMock(  # type: ignore[method-assign]
                    return_value=(True, "exact")
                )
            barrier = threading.Barrier(3)
            results: list[ACBridgeUpdateResult] = []

            def run(client: ACBridgeClient) -> None:
                barrier.wait()
                results.append(client.update_if_outdated())

            threads = [threading.Thread(target=run, args=(client,)) for client in clients]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=5)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(state["installs"], 1)
        self.assertCountEqual([result.state for result in results], ["updated", "current"])


class ACBridgeConnectionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _scheduler_host(context: DeviceContext):
        operations = OperationRegistry()
        manager = SimpleNamespace(
            operations=operations,
            active=DeviceInfo(
                serial=context.serial,
                mode="ADB",
                state="device",
                transport_id=context.transport_id,
            ),
            is_context_current=MagicMock(return_value=True),
        )
        host = SimpleNamespace(
            _closing=False,
            _acbridge_update_token=None,
            _last_acbridge_update_key=None,
            _pending_acbridge_update_context=None,
            _acbridge_update_retry_key=None,
            _acbridge_update_attempts={},
            _acbridge_maintenance_ui_busy=False,
            _automatic_shizuku_ui_busy=False,
            _pending_privilege_recheck=False,
            device_manager=manager,
            device_bar=SimpleNamespace(pool=MagicMock()),
            statusBar=MagicMock(return_value=MagicMock()),
            _run_acbridge_update=MagicMock(),
            _acbridge_update_result=MagicMock(),
            _acbridge_update_error=MagicMock(),
            _acbridge_update_finished=MagicMock(),
            _acbridge_update_callback_is_current=MagicMock(return_value=True),
            _queue_acbridge_update_retry=MagicMock(),
            apps_page=MagicMock(),
            backups_page=MagicMock(),
            file_manager_page=MagicMock(),
        )
        return host, operations

    def test_scheduler_runs_once_per_generation_and_rechecks_reconnect(self) -> None:
        first = device_context(generation=7)
        host, operations = self._scheduler_host(first)
        with patch("openadb.ui.main_window.start_worker", return_value=True) as start:
            self.assertTrue(MainWindow._schedule_acbridge_update(host, first))
            first_token = host._acbridge_update_token
            self.assertTrue(MainWindow._schedule_acbridge_update(host, first))
            self.assertEqual(start.call_count, 1)

            host._acbridge_update_token = None
            operations.finish(first_token)
            self.assertFalse(MainWindow._schedule_acbridge_update(host, first))

            second = device_context(generation=8, transport_id="12")
            self.assertTrue(MainWindow._schedule_acbridge_update(host, second))

        self.assertEqual(start.call_count, 2)
        self.assertIs(host._acbridge_update_token.device_context, second)

    def test_auto_update_owns_shared_acbridge_maintenance_barrier(self) -> None:
        context = device_context()
        host, operations = self._scheduler_host(context)

        with patch("openadb.ui.main_window.start_worker", return_value=True):
            self.assertTrue(MainWindow._schedule_acbridge_update(host, context))

        barrier = f"acbridge-maintenance:{context.serial}"
        self.assertIn(barrier, host._acbridge_update_token.conflict_groups)
        host.apps_page.setEnabled.assert_called_with(False)
        host.backups_page.setEnabled.assert_called_with(False)
        host.file_manager_page.setEnabled.assert_called_with(False)
        with self.assertRaises(OperationConflictError):
            operations.register(
                "apps.assets",
                device_context=context,
                conflict_group=f"apps-assets:{context.serial}",
                conflict_groups=(barrier,),
            )

    def test_transient_query_failure_is_retried_but_not_applied(self) -> None:
        context = device_context()
        host, _operations = self._scheduler_host(context)
        key = (context.serial, context.generation)
        token = SimpleNamespace(device_context=context)
        host._acbridge_update_attempts = {key: 1}
        host._last_acbridge_update_key = key
        result = ACBridgeUpdateResult(
            state="query_failed",
            bundled_version_code=31002,
            transient=True,
            message="transport warming up",
        )

        MainWindow._acbridge_update_result(host, token, result)

        self.assertIsNone(host._last_acbridge_update_key)
        host._queue_acbridge_update_retry.assert_called_once_with(context)
        host.statusBar.return_value.showMessage.assert_not_called()

    def test_third_query_failure_stops_retry_and_resumes_deferred_privilege(self) -> None:
        context = device_context()
        host, operations = self._scheduler_host(context)
        key = (context.serial, context.generation)
        token = operations.register("test", device_context=context)
        host._acbridge_update_token = token
        host._last_acbridge_update_key = key
        host._acbridge_update_attempts = {key: 3}
        host._pending_privilege_recheck = True
        host._resume_privilege_recheck_after_acbridge = MagicMock()
        result = ACBridgeUpdateResult(
            state="query_failed",
            bundled_version_code=31002,
            transient=True,
            message="transport remained unavailable",
        )

        MainWindow._acbridge_update_result(host, token, result)
        MainWindow._acbridge_update_finished(host, token)

        host._queue_acbridge_update_retry.assert_not_called()
        host.statusBar.return_value.showMessage.assert_called_once()
        host._resume_privilege_recheck_after_acbridge.assert_called_once()

    def test_transient_install_failure_uses_the_same_bounded_retry(self) -> None:
        context = device_context()
        host, _operations = self._scheduler_host(context)
        key = (context.serial, context.generation)
        token = SimpleNamespace(device_context=context)
        host._acbridge_update_attempts = {key: 1}
        host._last_acbridge_update_key = key
        result = ACBridgeUpdateResult(
            state="install_failed",
            bundled_version_code=31002,
            transient=True,
            message="device offline",
        )

        MainWindow._acbridge_update_result(host, token, result)

        self.assertIsNone(host._last_acbridge_update_key)
        host._queue_acbridge_update_retry.assert_called_once_with(context)
        host.statusBar.return_value.showMessage.assert_not_called()

    def test_permanent_install_failure_is_reported_without_retry(self) -> None:
        context = device_context()
        host, _operations = self._scheduler_host(context)
        key = (context.serial, context.generation)
        token = SimpleNamespace(device_context=context)
        host._acbridge_update_attempts = {key: 1}
        host._last_acbridge_update_key = key
        result = ACBridgeUpdateResult(
            state="install_failed",
            bundled_version_code=31002,
            transient=False,
            message="INSTALL_FAILED_INSUFFICIENT_STORAGE",
        )

        MainWindow._acbridge_update_result(host, token, result)

        self.assertEqual(host._last_acbridge_update_key, key)
        host._queue_acbridge_update_retry.assert_not_called()
        host.statusBar.return_value.showMessage.assert_called_once()

    def test_worker_exception_uses_the_same_bounded_retry(self) -> None:
        context = device_context()
        host, operations = self._scheduler_host(context)
        key = (context.serial, context.generation)
        token = operations.register("test", device_context=context)
        host._acbridge_update_token = token
        host._last_acbridge_update_key = key
        host._acbridge_update_attempts = {key: 1}

        MainWindow._acbridge_update_error(host, token, "temporary worker failure")

        self.assertIsNone(host._last_acbridge_update_key)
        host._queue_acbridge_update_retry.assert_called_once_with(context)
        host.statusBar.return_value.showMessage.assert_not_called()

    def test_stale_result_and_error_callbacks_have_no_side_effects(self) -> None:
        context = device_context()
        host, _operations = self._scheduler_host(context)
        token = SimpleNamespace(device_context=context)
        host._acbridge_update_callback_is_current.return_value = False
        host._invalidate_privilege_status = MagicMock()
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.SHIZUKU,
        )
        result = ACBridgeUpdateResult(
            state="updated",
            bundled_version_code=31002,
            installed_version_code=31002,
            previous_version_code=30003,
            message="updated",
        )

        MainWindow._acbridge_update_result(host, token, result)
        MainWindow._acbridge_update_error(host, token, "late error")

        host._invalidate_privilege_status.assert_not_called()
        host._queue_acbridge_update_retry.assert_not_called()
        host.statusBar.return_value.showMessage.assert_not_called()

    def test_first_install_is_applied_as_a_helper_change(self) -> None:
        context = device_context()
        host, _operations = self._scheduler_host(context)
        token = SimpleNamespace(device_context=context)
        host._invalidate_privilege_status = MagicMock()
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.SHIZUKU,
        )
        result = ACBridgeUpdateResult(
            state="installed",
            bundled_version_code=31002,
            installed_version_code=31002,
            message="ACBridge was installed automatically.",
        )

        MainWindow._acbridge_update_result(host, token, result)

        host._invalidate_privilege_status.assert_called_once_with()
        self.assertTrue(host._pending_privilege_recheck)
        host.statusBar.return_value.showMessage.assert_called_once_with(
            result.message,
            10000,
        )

    def test_current_helper_result_is_a_silent_no_op(self) -> None:
        context = device_context()
        host, _operations = self._scheduler_host(context)
        token = SimpleNamespace(device_context=context)
        host._invalidate_privilege_status = MagicMock()
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.SHIZUKU,
        )
        result = ACBridgeUpdateResult(
            state="current",
            bundled_version_code=31002,
            installed_version_code=31002,
            previous_version_code=31002,
            message="ACBridge versionCode 31002 is current.",
        )

        MainWindow._acbridge_update_result(host, token, result)

        host._invalidate_privilege_status.assert_not_called()
        self.assertFalse(host._pending_privilege_recheck)
        host.statusBar.return_value.showMessage.assert_not_called()

    def test_reconnect_queues_latest_context_and_ignores_old_result(self) -> None:
        first = device_context(generation=7, transport_id="11")
        second = device_context(generation=8, transport_id="12")
        host, operations = self._scheduler_host(first)
        current = [first]
        host.device_manager.is_context_current.side_effect = (
            lambda candidate: candidate == current[0]
        )
        host._schedule_acbridge_update = (
            lambda context: MainWindow._schedule_acbridge_update(host, context)
        )
        host._resume_acbridge_update = (
            lambda context: MainWindow._resume_acbridge_update(host, context)
        )
        host._resume_privilege_recheck_after_acbridge = MagicMock()
        host._invalidate_privilege_status = MagicMock()
        host._acbridge_update_callback_is_current = (
            lambda token: MainWindow._acbridge_update_callback_is_current(host, token)
        )
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.SHIZUKU,
        )
        callbacks = []

        with (
            patch("openadb.ui.main_window.start_worker", return_value=True) as start,
            patch(
                "openadb.ui.main_window.QTimer.singleShot",
                side_effect=lambda _delay, callback: callbacks.append(callback),
            ),
        ):
            self.assertTrue(host._schedule_acbridge_update(first))
            first_token = host._acbridge_update_token
            current[0] = second
            first_token.cancel("transport changed")
            self.assertTrue(host._schedule_acbridge_update(second))
            self.assertIs(host._pending_acbridge_update_context, second)

            MainWindow._acbridge_update_result(
                host,
                first_token,
                ACBridgeUpdateResult(
                    state="updated",
                    bundled_version_code=31002,
                    installed_version_code=31002,
                    previous_version_code=30003,
                ),
            )
            MainWindow._acbridge_update_finished(host, first_token)
            operations.finish(first_token)
            self.assertEqual(len(callbacks), 1)
            callbacks.pop()()

        self.assertEqual(start.call_count, 2)
        self.assertIs(host._acbridge_update_token.device_context, second)
        host._invalidate_privilege_status.assert_not_called()

    def test_device_exclusive_conflict_retries_once_after_release(self) -> None:
        context = device_context()
        host, operations = self._scheduler_host(context)
        host._schedule_acbridge_update = (
            lambda candidate: MainWindow._schedule_acbridge_update(host, candidate)
        )
        host._queue_acbridge_update_retry = (
            lambda candidate: MainWindow._queue_acbridge_update_retry(host, candidate)
        )
        host._retry_acbridge_update = (
            lambda candidate, key: MainWindow._retry_acbridge_update(host, candidate, key)
        )
        host._resume_privilege_recheck_after_acbridge = MagicMock()
        blocker = operations.register(
            "file-transfer",
            device_context=context,
            conflict_group=f"device-exclusive:{context.serial}",
        )
        callbacks = []

        with (
            patch("openadb.ui.main_window.start_worker", return_value=True) as start,
            patch(
                "openadb.ui.main_window.QTimer.singleShot",
                side_effect=lambda _delay, callback: callbacks.append(callback),
            ),
        ):
            self.assertTrue(host._schedule_acbridge_update(context))
            self.assertEqual(len(callbacks), 1)
            start.assert_not_called()
            operations.finish(blocker)
            callbacks.pop()()

        start.assert_called_once()
        self.assertIs(host._acbridge_update_token.device_context, context)

    def test_worker_binds_every_command_to_captured_transport(self) -> None:
        context = device_context(transport_id="42")
        update_result = ACBridgeUpdateResult(
            state="current",
            bundled_version_code=31002,
            installed_version_code=31002,
        )
        bridge = MagicMock()
        bridge.update_if_outdated.return_value = update_result
        bound_adb = object()
        host = SimpleNamespace(
            adb=SimpleNamespace(for_context=MagicMock(return_value=bound_adb)),
            settings=object(),
            icon_extractor=object(),
            device_manager=SimpleNamespace(is_context_current=MagicMock(return_value=True)),
        )
        token = OperationRegistry().register(
            "test",
            device_context=context,
        )

        with patch("openadb.ui.main_window.ACBridgeClient", return_value=bridge) as client:
            result = MainWindow._run_acbridge_update(host, token, context)

        self.assertIs(result, update_result)
        host.adb.for_context.assert_called_once_with(context)
        client.assert_called_once_with(
            bound_adb,
            host.settings,
            host.icon_extractor,
            temp_folder=context.temp_path,
        )
        bridge.update_if_outdated.assert_called_once_with(
            cancel_event=token.cancel_event
        )

    def test_all_connection_methods_use_the_central_install_or_update_hook(self) -> None:
        connections = (
            ("USB", "USB-SERIAL"),
            ("Wireless debugging QR", "adb-qr._adb-tls-connect._tcp"),
            ("Wireless debugging pairing code", "adb-code._adb-tls-connect._tcp"),
            ("Wireless debugging host and port", "192.0.2.5:37001"),
            ("Legacy TCP/IP", "192.0.2.5:5555"),
            ("Android TV", "android-tv:5555"),
        )
        for connection_method, serial in connections:
            with self.subTest(connection_method=connection_method, serial=serial):
                context = device_context(serial=serial)
                host = self._refresh_host(context)
                device = DeviceInfo(serial=serial, mode="ADB", state="device")

                MainWindow._on_device_refreshed(host, device)

                host._schedule_acbridge_update.assert_called_once_with(context)

    def test_unready_modes_do_not_query_or_install_acbridge(self) -> None:
        cases = (
            DeviceInfo(mode="No device", state="none"),
            DeviceInfo(serial="a", mode="Unauthorized", state="unauthorized"),
            DeviceInfo(serial="a", mode="Offline", state="offline"),
            DeviceInfo(serial="a", mode="Recovery", state="recovery"),
            DeviceInfo(serial="a", mode="Fastboot", state="fastboot"),
        )
        for device in cases:
            with self.subTest(mode=device.mode):
                host = self._refresh_host(device_context())
                MainWindow._on_device_refreshed(host, device)
                host._schedule_acbridge_update.assert_not_called()
                host.device_manager.require_context.assert_not_called()

    def test_shizuku_recheck_is_deferred_until_bridge_worker_finishes(self) -> None:
        context = device_context()
        host = self._refresh_host(context)
        host._activate_device_profile.return_value = True
        host._schedule_acbridge_update.return_value = True
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.SHIZUKU,
            reset=MagicMock(),
        )
        host._apply_privilege_status = MagicMock()
        host.check_privilege_access = MagicMock()
        device = DeviceInfo(serial=context.serial, mode="ADB", state="device")

        with patch("openadb.ui.main_window.QTimer.singleShot") as single_shot:
            MainWindow._on_device_refreshed(host, device)

        self.assertTrue(host._pending_privilege_recheck)
        single_shot.assert_not_called()
        host.check_privilege_access.assert_not_called()

    def test_root_recheck_keeps_pages_gated_across_acbridge_handoff(self) -> None:
        context = device_context()
        host, _operations = self._scheduler_host(context)
        host._pending_privilege_recheck = True
        host._privilege_barrier_waits_for_recheck = False
        host._acbridge_maintenance_ui_busy = True
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.ROOT,
        )
        host._schedule_privilege_recheck = MagicMock()

        MainWindow._resume_privilege_recheck_after_acbridge(host)

        self.assertTrue(host._privilege_barrier_waits_for_recheck)
        host._schedule_privilege_recheck.assert_called_once_with()
        host.apps_page.setEnabled.assert_called_with(False)
        host.backups_page.setEnabled.assert_called_with(False)
        host.file_manager_page.setEnabled.assert_called_with(False)

        MainWindow._set_acbridge_maintenance_ui_busy(host, False)

        host.apps_page.setEnabled.assert_called_with(False)
        host.backups_page.setEnabled.assert_called_with(False)
        host.file_manager_page.setEnabled.assert_called_with(False)

        MainWindow._set_privilege_feature_barrier_busy(host, False)

        host.apps_page.setEnabled.assert_called_with(True)
        host.backups_page.setEnabled.assert_called_with(True)
        host.file_manager_page.setEnabled.assert_called_with(True)

    def test_metadata_only_refresh_does_not_reset_same_connection_privileges(self) -> None:
        context = device_context()
        host = self._refresh_host(context)
        host._activate_device_profile.return_value = False
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.SHIZUKU,
            reset=MagicMock(),
        )
        host._apply_privilege_status = MagicMock()
        host.check_privilege_access = MagicMock()
        first = DeviceInfo(
            serial=context.serial,
            mode="ADB",
            state="device",
            model="Initial name",
        )
        enriched = DeviceInfo(
            serial=context.serial,
            mode="ADB",
            state="device",
            model="Enriched device name",
            android_version="15",
        )

        with patch("openadb.ui.main_window.QTimer.singleShot") as single_shot:
            MainWindow._on_device_refreshed(host, first)
            MainWindow._on_device_refreshed(host, enriched)

        host.privilege_manager.reset.assert_called_once_with()
        host._apply_privilege_status.assert_called_once_with(None)
        single_shot.assert_called_once()

    def test_bridge_current_still_defers_active_page_for_shizuku_handshake(self) -> None:
        context = device_context()
        host = self._refresh_host(context)
        host._activate_device_profile.return_value = False
        host._schedule_acbridge_update.return_value = False
        host._schedule_privilege_recheck = MagicMock()
        host._automatic_shizuku_workflow_pending = MagicMock(return_value=True)
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.SHIZUKU,
            reset=MagicMock(),
        )
        host._apply_privilege_status = MagicMock()
        host.stack.currentWidget.return_value = host.file_manager_page
        device = DeviceInfo(serial=context.serial, mode="ADB", state="device")

        MainWindow._on_device_refreshed(host, device)

        host.file_manager_page.refresh_all.assert_not_called()
        self.assertEqual(
            host._pending_acbridge_feature_refresh,
            {"file-manager"},
        )

    def test_disconnect_keeps_follow_up_privilege_barrier_until_worker_finishes(
        self,
    ) -> None:
        context = device_context()
        host = self._refresh_host(context)
        operations = OperationRegistry()
        token = operations.register(
            "privilege-access",
            device_context=context,
            conflict_groups=(f"device-exclusive:{context.serial}",),
        )
        host.device_manager.operations = operations
        host.device_manager.active = DeviceInfo(mode="No device", state="missing")
        host._last_privilege_connection_key = (context.serial, context.generation)
        host._privilege_token = token
        host._privilege_operation_kind = "check"
        host._privilege_barrier_waits_for_recheck = False
        host._automatic_shizuku_ui_busy = False
        host._acbridge_maintenance_ui_busy = False
        host._set_automatic_shizuku_ui_busy = MagicMock()
        host.privilege_manager = SimpleNamespace(
            selected_backend=PrivilegeBackend.ROOT,
            reset=MagicMock(),
        )
        host._apply_privilege_status = MagicMock()

        MainWindow._on_device_refreshed(
            host,
            DeviceInfo(mode="No device", state="missing"),
        )

        self.assertTrue(operations.contains(token))
        self.assertTrue(host._privilege_barrier_waits_for_recheck)
        host.apps_page.setEnabled.assert_called_with(False)
        host.file_manager_page.setEnabled.assert_called_with(False)
        host._set_automatic_shizuku_ui_busy.assert_not_called()

    @staticmethod
    def _refresh_host(context: DeviceContext):
        current_page = object()
        manager = SimpleNamespace(
            current_generation=context.generation,
            require_context=MagicMock(return_value=context),
        )
        host = SimpleNamespace(
            _activate_device_profile=MagicMock(return_value=False),
            _schedule_acbridge_update=MagicMock(return_value=False),
            _last_device_refresh_signature=None,
            _last_privilege_connection_key=None,
            _last_automatic_shizuku_key=None,
            _automatic_shizuku_inflight_key=None,
            _automatic_shizuku_attempts={},
            _automatic_shizuku_failure_status=None,
            _privilege_barrier_waits_for_recheck=False,
            _privilege_token=None,
            _privilege_operation_kind="",
            _pending_privilege_recheck=False,
            _pending_acbridge_feature_refresh=set(),
            device_manager=manager,
            dashboard=MagicMock(),
            apps_page=MagicMock(apps=[]),
            file_manager_page=MagicMock(),
            commands_page=MagicMock(),
            stack=MagicMock(),
            _closing=False,
            _acbridge_update_token=None,
            _acbridge_update_retry_key=None,
            _pending_acbridge_update_context=None,
        )
        manager.active = DeviceInfo(
            serial=context.serial,
            mode="ADB",
            state="device",
            transport_id=context.transport_id,
        )
        host._schedule_privilege_recheck = (
            lambda **kwargs: MainWindow._schedule_privilege_recheck(host, **kwargs)
        )
        host.stack.currentWidget.return_value = current_page
        return host


if __name__ == "__main__":
    unittest.main()
