from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tools.device_lab_smoke import (
    NOT_RUN_HARDWARE,
    REPORT_SCHEMA,
    CommandOutcome,
    DeviceLab,
    LabConfig,
    SafetyError,
    ValidatedChange,
    anonymize_text,
    command_is_authorized,
    confirmation_phrase,
    main,
    package_path_present,
    source_commit,
    validate_change_request,
    verified_apk_copy,
    write_json_report,
    write_junit_report,
)


class FakeExecutor:
    def __init__(self, handler):
        self.handler = handler
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command, _timeout: float) -> CommandOutcome:
        normalized = tuple(str(part) for part in command)
        self.commands.append(normalized)
        return self.handler(normalized)


def no_hardware_handler(command: tuple[str, ...]) -> CommandOutcome:
    operation = command[1:]
    if operation in (("version",), ("--version",)):
        return CommandOutcome(0, "version output containing no identifiers")
    if operation in (("devices", "-l"), ("devices",)):
        return CommandOutcome(0, "List of devices attached\n" if operation[-1] == "-l" else "")
    raise AssertionError(f"Unexpected command: {command!r}")


class DeviceLabCommandPolicyTests(unittest.TestCase):
    def test_only_enumerated_read_only_commands_are_authorized_by_default(self) -> None:
        allowed = (
            ["adb.exe", "version"],
            ["adb.exe", "devices", "-l"],
            ["adb.exe", "-s", "SERIAL", "get-state"],
            ["adb.exe", "-s", "SERIAL", "shell", "getprop", "ro.build.version.sdk"],
            ["fastboot.exe", "--version"],
            ["fastboot.exe", "devices"],
        )
        for command in allowed:
            with self.subTest(command=command):
                self.assertTrue(command_is_authorized(command))

        prohibited = (
            ["fastboot.exe", "flash", "boot", "boot.img"],
            ["fastboot.exe", "erase", "userdata"],
            ["fastboot.exe", "format", "userdata"],
            ["fastboot.exe", "flashing", "unlock"],
            ["fastboot.exe", "flashing", "lock"],
            ["adb.exe", "reboot", "bootloader"],
            ["adb.exe", "-s", "SERIAL", "shell", "rm", "-rf", "/data"],
            ["adb.exe", "-s", "SERIAL", "install", "C:/test.apk"],
            ["adb.exe", "-s", "SERIAL", "uninstall", "com.example.test"],
        )
        for command in prohibited:
            with self.subTest(command=command):
                self.assertFalse(command_is_authorized(command))

    def test_change_policy_has_no_arbitrary_fastboot_escape_hatch(self) -> None:
        self.assertTrue(
            command_is_authorized(
                ["adb.exe", "-s", "SERIAL", "uninstall", "com.example.test"],
                allow_change=True,
            )
        )
        self.assertTrue(
            command_is_authorized(
                ["adb.exe", "-s", "SERIAL", "install", "C:/lab/test.apk"],
                allow_change=True,
            )
        )
        for operation in ("flash", "erase", "format", "unlock", "lock", "boot"):
            self.assertFalse(
                command_is_authorized(["fastboot.exe", operation, "anything"], allow_change=True)
            )
        for command in (
            ["adb.exe", "reboot", "bootloader"],
            ["adb.exe", "-s", "SERIAL", "reboot"],
            ["adb.exe", "-s", "SERIAL", "shell", "sh", "-c", "anything"],
        ):
            self.assertFalse(command_is_authorized(command, allow_change=True))


class DeviceLabPrivacyAndReportTests(unittest.TestCase):
    def test_default_probe_is_read_only_and_identifiers_never_enter_report(self) -> None:
        serial = "192.168.1.42:5555"

        def handler(command: tuple[str, ...]) -> CommandOutcome:
            operation = command[1:]
            if operation == ("version",) or operation == ("--version",):
                return CommandOutcome(0, "Android tool version")
            if operation == ("devices", "-l"):
                return CommandOutcome(
                    0,
                    "List of devices attached\n"
                    f"{serial} device product:private model:Private_Phone transport_id:8\n"
                    "DEMO-SERIAL unauthorized product:private\n",
                )
            if operation == ("devices",):
                return CommandOutcome(0, "")
            if operation[:2] == ("-s", serial):
                return CommandOutcome(0, "private raw property value")
            raise AssertionError(f"Unexpected command: {command!r}")

        executor = FakeExecutor(handler)
        report = DeviceLab(
            LabConfig(),
            adb_path="adb.exe",
            fastboot_path="fastboot.exe",
            executor=executor,
        ).run()
        serialized = json.dumps(report.to_dict(), ensure_ascii=False)

        self.assertTrue(report.hardware_available)
        self.assertNotIn(serial, serialized)
        self.assertNotIn("192.168.1.42", serialized)
        self.assertNotIn("DEMO-SERIAL", serialized)
        self.assertNotIn("Private_Phone", serialized)
        self.assertNotIn("private raw property value", serialized)
        self.assertTrue(all(command_is_authorized(command) for command in executor.commands))
        self.assertFalse(any("install" in command or "uninstall" in command for command in executor.commands))

    def test_no_hardware_is_truthfully_not_run_in_json_and_junit(self) -> None:
        report = DeviceLab(
            LabConfig(),
            adb_path="adb.exe",
            fastboot_path="fastboot.exe",
            executor=FakeExecutor(no_hardware_handler),
        ).run()

        self.assertEqual(report.status, "not_run")
        self.assertFalse(report.hardware_available)
        self.assertGreaterEqual(sum(check.message == NOT_RUN_HARDWARE for check in report.checks), 2)
        with tempfile.TemporaryDirectory() as temp:
            json_path = Path(temp) / "report.json"
            junit_path = Path(temp) / "report.xml"
            write_json_report(json_path, report)
            write_junit_report(junit_path, report)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            suite = ET.parse(junit_path).getroot()
            temporary_reports = tuple(Path(temp).glob(".*.tmp"))

        self.assertEqual(payload["schema"], REPORT_SCHEMA)
        self.assertEqual(payload["product"]["version"], "3.0.2")
        self.assertRegex(payload["product"]["source_commit"], r"^(?:unavailable|[0-9a-f]{40})$")
        self.assertEqual(set(payload["environment"]), {"os", "release", "architecture"})
        self.assertEqual(payload["status"], "not_run")
        self.assertFalse(payload["hardware"]["available"])
        self.assertEqual(suite.tag, "testsuite")
        self.assertGreater(int(suite.attrib["skipped"]), 0)
        self.assertEqual(temporary_reports, ())

    def test_redactor_removes_serial_ip_username_home_path_and_filename(self) -> None:
        serial = "DEMO-SERIAL"
        private_ipv6 = "fe80::" + "42"
        home = str(Path.home())
        username = Path.home().name
        raw = (
            f"serial={serial}; endpoint=192.168.1.42:5555; ipv6={private_ipv6}; user={username}; "
            f"home={home}; file=family-photo.png; token=one-shot-secret"
        )
        sanitized = anonymize_text(raw, [serial])
        for private in (
            serial,
            "192.168.1.42",
            private_ipv6,
            username,
            home,
            "family-photo.png",
            "one-shot-secret",
        ):
            self.assertNotIn(private.casefold(), sanitized.casefold())

    def test_source_commit_accepts_only_a_full_hex_github_sha(self) -> None:
        with patch.dict("os.environ", {"GITHUB_SHA": "A" * 40}, clear=False):
            self.assertEqual(source_commit(), "a" * 40)
        with patch.dict("os.environ", {"GITHUB_SHA": "refs/heads/main; private"}, clear=False):
            self.assertEqual(source_commit(), "unavailable")

    def test_realistic_pm_path_is_detected_without_exposing_the_path(self) -> None:
        self.assertTrue(
            package_path_present(
                "package:/data/app/~~random/org.example.test-random/base.apk\n"
            )
        )
        self.assertFalse(package_path_present(""))

    def test_explicit_read_only_serial_never_queries_another_transport(self) -> None:
        def handler(command: tuple[str, ...]) -> CommandOutcome:
            operation = command[1:]
            if operation in (("version",), ("--version",)):
                return CommandOutcome(0, "version")
            if operation == ("devices", "-l"):
                return CommandOutcome(
                    0,
                    "List of devices attached\nDEMO-SERIAL-A device\nDEMO-SERIAL-B device\n",
                )
            if operation == ("devices",):
                return CommandOutcome(0, "")
            if operation[:2] == ("-s", "DEMO-SERIAL-B"):
                return CommandOutcome(0, "ok")
            raise AssertionError(f"Unexpected query target: {command!r}")

        executor = FakeExecutor(handler)
        report = DeviceLab(
            LabConfig(serial="DEMO-SERIAL-B"),
            adb_path="adb.exe",
            fastboot_path="fastboot.exe",
            executor=executor,
        ).run()
        serialized = json.dumps(report.to_dict())

        self.assertFalse(any("DEMO-SERIAL-A" in command[1:] for command in executor.commands))
        self.assertTrue(any("DEMO-SERIAL-B" in command[1:] for command in executor.commands))
        self.assertNotIn("DEMO-SERIAL-A", serialized)
        self.assertNotIn("DEMO-SERIAL-B", serialized)


class DeviceLabChangeSafetyTests(unittest.TestCase):
    def test_every_change_gate_is_required_before_any_command(self) -> None:
        base = dict(
            serial="SERIAL",
            allow_device_changes=True,
            change_operation="uninstall-disposable-package",
            package="com.example.test",
            disposable_target=True,
            confirmation=confirmation_phrase("com.example.test"),
        )
        invalid_configs = (
            LabConfig(**{**base, "allow_device_changes": False}),
            LabConfig(**{**base, "serial": ""}),
            LabConfig(**{**base, "package": ""}),
            LabConfig(**{**base, "disposable_target": False}),
            LabConfig(**{**base, "confirmation": "CONFIRM"}),
        )
        for config in invalid_configs:
            executor = FakeExecutor(lambda command: CommandOutcome(0, ""))
            report = DeviceLab(
                config,
                adb_path="adb.exe",
                fastboot_path="fastboot.exe",
                executor=executor,
            ).run()
            with self.subTest(config=config):
                self.assertEqual(report.status, "failed")
                self.assertEqual(executor.commands, [])

    def test_system_and_non_disposable_packages_are_rejected_statically(self) -> None:
        for package in (
            "com.android.systemui",
            "com.android.test.helper",
            "com.google.android.test.helper",
            "com.example.production",
        ):
            config = LabConfig(
                serial="SERIAL",
                allow_device_changes=True,
                change_operation="uninstall-disposable-package",
                package=package,
                disposable_target=True,
                confirmation=confirmation_phrase(package),
            )
            with self.subTest(package=package), self.assertRaisesRegex(ValueError, "prohibited|disposable"):
                validate_change_request(config)

    def test_dynamic_system_package_check_blocks_uninstall(self) -> None:
        package = "org.example.lab.helper"

        def handler(command: tuple[str, ...]) -> CommandOutcome:
            operation = command[1:]
            if operation in (("version",), ("--version",)):
                return CommandOutcome(0, "version")
            if operation == ("devices", "-l"):
                return CommandOutcome(0, "List of devices attached\nSERIAL device\n")
            if operation == ("devices",):
                return CommandOutcome(0, "")
            if operation[:2] == ("-s", "SERIAL") and operation[2:] in (
                ("get-state",),
                ("shell", "getprop", "ro.build.version.sdk"),
                ("shell", "getprop", "ro.product.cpu.abi"),
            ):
                return CommandOutcome(0, "ok")
            if operation[-6:] == ("shell", "pm", "list", "packages", "-s", package):
                return CommandOutcome(0, f"package:{package}\n")
            raise AssertionError(f"Unexpected command: {command!r}")

        executor = FakeExecutor(handler)
        report = DeviceLab(
            LabConfig(
                serial="SERIAL",
                allow_device_changes=True,
                change_operation="uninstall-disposable-package",
                package=package,
                disposable_target=True,
                confirmation=confirmation_phrase(package),
            ),
            adb_path="adb.exe",
            fastboot_path="fastboot.exe",
            executor=executor,
        ).run()
        self.assertEqual(report.status, "failed")
        self.assertFalse(any("uninstall" in command for command in executor.commands))

    def test_verified_new_disposable_apk_can_be_installed_after_all_gates(self) -> None:
        package = "org.example.devicelab.helper"
        with tempfile.TemporaryDirectory() as temp:
            apk_path = Path(temp) / "private-build.apk"
            apk_path.write_bytes(b"verified disposable apk")

            def handler(command: tuple[str, ...]) -> CommandOutcome:
                operation = command[1:]
                if operation in (("version",), ("--version",)):
                    return CommandOutcome(0, "version")
                if operation == ("devices", "-l"):
                    return CommandOutcome(0, "List of devices attached\nSERIAL device\n")
                if operation == ("devices",):
                    return CommandOutcome(0, "")
                if operation[:2] == ("-s", "SERIAL") and operation[2:] in (
                    ("get-state",),
                    ("shell", "getprop", "ro.build.version.sdk"),
                    ("shell", "getprop", "ro.product.cpu.abi"),
                ):
                    return CommandOutcome(0, "ok")
                if operation[-6:-1] == ("shell", "pm", "list", "packages", "-s"):
                    return CommandOutcome(0, "")
                if operation[-6:-1] == ("shell", "pm", "list", "packages", "-3"):
                    return CommandOutcome(0, "")
                if operation[-4:-1] == ("shell", "pm", "path"):
                    return CommandOutcome(0, "")
                if len(operation) == 4 and operation[2] == "install":
                    return CommandOutcome(0, "Success")
                raise AssertionError(f"Unexpected command: {command!r}")

            executor = FakeExecutor(handler)
            config = LabConfig(
                serial="SERIAL",
                allow_device_changes=True,
                change_operation="install-disposable-apk",
                package=package,
                apk_path=apk_path,
                disposable_target=True,
                confirmation=confirmation_phrase(package),
            )
            with patch("tools.device_lab_smoke.read_apk_package", return_value=package):
                report = DeviceLab(
                    config,
                    adb_path="adb.exe",
                    fastboot_path="fastboot.exe",
                    executor=executor,
                ).run()

            serialized = json.dumps(report.to_dict())
            change_commands = [command for command in executor.commands if "install" in command]
            self.assertEqual(report.status, "partial")
            self.assertEqual(len(change_commands), 1)
            self.assertNotEqual(change_commands[0][-1], str(apk_path))
            self.assertFalse(Path(change_commands[0][-1]).exists())
            self.assertNotIn(str(apk_path), serialized)
            self.assertNotIn(apk_path.name, serialized)
            self.assertNotIn("SERIAL", serialized)

    def test_existing_package_path_refuses_install_without_replacement(self) -> None:
        package = "org.example.lab.existing"
        with tempfile.TemporaryDirectory() as temp:
            apk_path = Path(temp) / "candidate.apk"
            apk_path.write_bytes(b"verified disposable apk")

            def handler(command: tuple[str, ...]) -> CommandOutcome:
                operation = command[1:]
                if operation in (("version",), ("--version",)):
                    return CommandOutcome(0, "version")
                if operation == ("devices", "-l"):
                    return CommandOutcome(0, "List of devices attached\nSERIAL device\n")
                if operation == ("devices",):
                    return CommandOutcome(0, "")
                if operation[:2] == ("-s", "SERIAL") and operation[2:] in (
                    ("get-state",),
                    ("shell", "getprop", "ro.build.version.sdk"),
                    ("shell", "getprop", "ro.product.cpu.abi"),
                ):
                    return CommandOutcome(0, "ok")
                if operation[-6:-1] in (
                    ("shell", "pm", "list", "packages", "-s"),
                    ("shell", "pm", "list", "packages", "-3"),
                ):
                    return CommandOutcome(0, "")
                if operation[-4:-1] == ("shell", "pm", "path"):
                    return CommandOutcome(
                        0,
                        "package:/data/app/~~demo/org.example.lab.existing-demo/base.apk\n",
                    )
                raise AssertionError(f"Unexpected command: {command!r}")

            executor = FakeExecutor(handler)
            with patch("tools.device_lab_smoke.read_apk_package", return_value=package):
                report = DeviceLab(
                    LabConfig(
                        serial="SERIAL",
                        allow_device_changes=True,
                        change_operation="install-disposable-apk",
                        package=package,
                        apk_path=apk_path,
                        disposable_target=True,
                        confirmation=confirmation_phrase(package),
                    ),
                    adb_path="adb.exe",
                    fastboot_path="fastboot.exe",
                    executor=executor,
                ).run()

        self.assertEqual(report.status, "failed")
        self.assertFalse(any("install" in command for command in executor.commands))

    def test_apk_changed_after_initial_validation_is_refused(self) -> None:
        package = "org.example.test.changed"
        with tempfile.TemporaryDirectory() as temp:
            apk_path = Path(temp) / "candidate.apk"
            initial = b"initial APK bytes"
            apk_path.write_bytes(initial)
            change = ValidatedChange(
                operation="install-disposable-apk",
                package=package,
                apk_path=apk_path,
                apk_sha256=hashlib.sha256(initial).hexdigest(),
            )
            apk_path.write_bytes(b"changed after validation")
            with (
                patch("tools.device_lab_smoke.read_apk_package", return_value=package),
                self.assertRaises(SafetyError),
                verified_apk_copy(change),
            ):
                self.fail("A changed APK must never reach the install command")

    def test_verified_disposable_user_package_can_be_uninstalled(self) -> None:
        package = "org.example.test.helper"

        def handler(command: tuple[str, ...]) -> CommandOutcome:
            operation = command[1:]
            if operation in (("version",), ("--version",)):
                return CommandOutcome(0, "version")
            if operation == ("devices", "-l"):
                return CommandOutcome(0, "List of devices attached\nSERIAL device\n")
            if operation == ("devices",):
                return CommandOutcome(0, "")
            if operation[:2] == ("-s", "SERIAL") and operation[2:] in (
                ("get-state",),
                ("shell", "getprop", "ro.build.version.sdk"),
                ("shell", "getprop", "ro.product.cpu.abi"),
            ):
                return CommandOutcome(0, "ok")
            if operation[-6:-1] == ("shell", "pm", "list", "packages", "-s"):
                return CommandOutcome(0, "")
            if operation[-6:-1] == ("shell", "pm", "list", "packages", "-3"):
                return CommandOutcome(0, f"package:{package}\n")
            if operation[-4:-1] == ("shell", "pm", "path"):
                return CommandOutcome(
                    0,
                    "package:/data/app/~~random/org.example.test.helper-random/base.apk\n",
                )
            if operation[-2:] == ("uninstall", package):
                return CommandOutcome(0, "Success")
            raise AssertionError(f"Unexpected command: {command!r}")

        executor = FakeExecutor(handler)
        report = DeviceLab(
            LabConfig(
                serial="SERIAL",
                allow_device_changes=True,
                change_operation="uninstall-disposable-package",
                package=package,
                disposable_target=True,
                confirmation=confirmation_phrase(package),
            ),
            adb_path="adb.exe",
            fastboot_path="fastboot.exe",
            executor=executor,
        ).run()
        serialized = json.dumps(report.to_dict())

        self.assertEqual(sum("uninstall" in command for command in executor.commands), 1)
        self.assertTrue(any(check.check_id == "device_change" and check.status == "passed" for check in report.checks))
        self.assertNotIn(package, serialized)
        self.assertNotIn("SERIAL", serialized)


class DeviceLabCliTests(unittest.TestCase):
    def test_cli_writes_sanitized_json_and_optional_junit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            tools = temp_path / "platform-tools"
            tools.mkdir()
            (tools / "adb.exe").write_bytes(b"")
            (tools / "fastboot.exe").write_bytes(b"")
            json_path = temp_path / "out" / "report.json"
            junit_path = temp_path / "out" / "report.xml"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "--platform-tools",
                        str(tools),
                        "--json-report",
                        str(json_path),
                        "--junit-report",
                        str(junit_path),
                    ],
                    executor=FakeExecutor(no_hardware_handler),
                )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            suite = ET.parse(junit_path).getroot()

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["schema"], REPORT_SCHEMA)
        self.assertEqual(payload["mode"], "read_only")
        self.assertEqual(payload["status"], "not_run")
        self.assertEqual(suite.attrib["failures"], "0")


if __name__ == "__main__":
    unittest.main()
