from __future__ import annotations

# ruff: noqa: E402 -- direct execution adds the repository root before imports.

import argparse
import getpass
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openadb.core.file_manager_errors import redact_sensitive_text
from openadb.core.quiet_output import quiet_third_party_output
from openadb.core.safety import is_dangerous_package
from openadb.version import VERSION


REPORT_SCHEMA = "openadb.device-lab.v1"
NOT_RUN_HARDWARE = "Not run — hardware unavailable"
CHANGE_OPERATIONS = ("install-disposable-apk", "uninstall-disposable-package")
_PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_DISPOSABLE_SEGMENTS = {"demo", "devicelab", "lab", "sample", "test", "tests"}
_SYSTEM_PACKAGE_PREFIXES = (
    "android.",
    "com.android.",
    "com.amazon.",
    "com.google.android.",
    "com.huawei.",
    "com.lge.",
    "com.miui.",
    "com.motorola.",
    "com.nvidia.",
    "com.oneplus.",
    "com.oppo.",
    "com.samsung.",
    "com.sec.",
    "com.sony.",
    "com.vivo.",
    "com.xiaomi.",
    "org.lineageos.",
)
_ALLOWED_PROPERTIES = {
    "ro.build.version.sdk",
    "ro.product.cpu.abi",
}
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\d.])")
_BRACKETED_IPV6_RE = re.compile(r"\[[0-9a-fA-F:%.]+\](?::\d{1,5})?")
_UNBRACKETED_IPV6_RE = re.compile(
    r"(?<![\w:])(?=[0-9a-fA-F:]*:[0-9a-fA-F:]*:)[0-9a-fA-F:]{3,}"
    r"(?:%[\w.-]+)?(?::\d{1,5})?(?![\w:])"
)
_HOME_PATH_RE = re.compile(
    r"(?i)(?:[a-z]:[\\/]users[\\/][^\\/\s]+(?:[\\/][^\r\n]*)?"
    r"|/(?:home|users)/[^/\s]+(?:/[^\r\n]*)?)"
)
_FILENAME_RE = re.compile(
    r"(?i)(?<![\w.-])[^\\/\s<>:\"|?*]+\."
    r"(?:apk|exe|msi|zip|7z|rar|tar|gz|img|bin|iso|json|xml|txt|log|png|jpe?g|pdf)\b"
)
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_GENERIC_ENV_RE = re.compile(r"^[A-Za-z0-9_.+() -]{1,80}$")


class SafetyError(ValueError):
    """A fail-closed device-lab validation error."""


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    unavailable: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.unavailable


@dataclass(frozen=True, slots=True)
class LabCheck:
    check_id: str
    status: str
    message: str
    duration_ms: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.check_id,
            "status": self.status,
            "message": self.message,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class LabReport:
    mode: str
    checks: list[LabCheck] = field(default_factory=list)
    adb_transport_count: int = 0
    fastboot_transport_count: int = 0

    @property
    def hardware_available(self) -> bool:
        return self.adb_transport_count + self.fastboot_transport_count > 0

    @property
    def summary(self) -> dict[str, int]:
        return {
            status: sum(check.status == status for check in self.checks)
            for status in ("passed", "failed", "not_run")
        }

    @property
    def status(self) -> str:
        if any(check.status == "failed" for check in self.checks):
            return "failed"
        if not self.hardware_available:
            return "not_run"
        if any(check.status == "not_run" for check in self.checks):
            return "partial"
        return "passed"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": REPORT_SCHEMA,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "product": {
                "name": "OpenADB",
                "version": VERSION,
                "source_commit": source_commit(),
            },
            "environment": generic_environment(),
            "mode": self.mode,
            "status": self.status,
            "hardware": {
                "available": self.hardware_available,
                "adb_transport_count": self.adb_transport_count,
                "fastboot_transport_count": self.fastboot_transport_count,
            },
            "summary": self.summary,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class LabConfig:
    timeout: float = 20.0
    serial: str = ""
    allow_device_changes: bool = False
    change_operation: str = ""
    package: str = ""
    apk_path: Path | None = None
    disposable_target: bool = False
    confirmation: str = ""


@dataclass(frozen=True, slots=True)
class ValidatedChange:
    operation: str
    package: str
    apk_path: Path | None = None
    apk_sha256: str = ""


CommandExecutor = Callable[[Sequence[str], float], CommandOutcome]


class SubprocessExecutor:
    """Run a pre-authorized command without a shell or persistent raw logs."""

    def __call__(self, command: Sequence[str], timeout: float) -> CommandOutcome:
        try:
            completed = subprocess.run(
                [str(part) for part in command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return CommandOutcome(None, timed_out=True)
        except (FileNotFoundError, OSError):
            return CommandOutcome(None, unavailable=True)
        return CommandOutcome(
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
        )


class DeviceLab:
    """Narrow device-lab runner whose default command set is read-only."""

    def __init__(
        self,
        config: LabConfig,
        *,
        adb_path: str | Path | None,
        fastboot_path: str | Path | None,
        executor: CommandExecutor | None = None,
    ) -> None:
        self.config = config
        self.adb_path = str(adb_path) if adb_path else ""
        self.fastboot_path = str(fastboot_path) if fastboot_path else ""
        self.executor = executor or SubprocessExecutor()
        self._known_identifiers: list[str] = [config.serial] if config.serial else []

    def run(self) -> LabReport:
        mode = "device_change" if self.config.change_operation else "read_only"
        report = LabReport(mode=mode)
        try:
            change = validate_change_request(self.config)
        except SafetyError as exc:
            report.checks.append(self._check("safety.change_request", "failed", str(exc)))
            return report

        adb_devices = self._probe_adb(report)
        self._probe_fastboot(report)
        if change is None:
            report.checks.append(
                self._check("device_change", "not_run", "Not run — read-only mode")
            )
        else:
            self._perform_change(report, change, adb_devices)
        return report

    def _probe_adb(self, report: LabReport) -> list[tuple[str, str]]:
        if not self.adb_path:
            report.checks.extend(
                [
                    self._check("tool.adb", "not_run", "ADB executable is unavailable."),
                    self._check("adb.transports", "not_run", NOT_RUN_HARDWARE),
                ]
            )
            return []

        version, duration = self._execute([self.adb_path, "version"])
        report.checks.append(
            self._check(
                "tool.adb",
                "passed" if version.success else "failed",
                "ADB executable responded." if version.success else _outcome_failure("ADB version probe", version),
                duration,
            )
        )
        devices_result, duration = self._execute([self.adb_path, "devices", "-l"])
        if not devices_result.success:
            report.checks.append(
                self._check("adb.transports", "failed", _outcome_failure("ADB transport probe", devices_result), duration)
            )
            return []

        devices = parse_adb_devices(devices_result.stdout)
        self._known_identifiers.extend(serial for serial, _state in devices)
        report.adb_transport_count = len(devices)
        if not devices:
            report.checks.append(self._check("adb.transports", "not_run", NOT_RUN_HARDWARE, duration))
            return []

        ready = sum(state == "device" for _serial, state in devices)
        report.checks.append(
            self._check(
                "adb.transports",
                "passed",
                f"Detected {len(devices)} ADB transport(s); {ready} ready.",
                duration,
            )
        )
        for index, (serial, state) in enumerate(devices, start=1):
            report.checks.append(
                self._check(
                    f"adb.transport.{index}.state",
                    "passed",
                    f"Transport {index} reported {safe_transport_state(state)}.",
                )
            )
            if self.config.serial and serial != self.config.serial:
                report.checks.append(
                    self._check(
                        f"adb.transport.{index}.properties",
                        "not_run",
                        "Not run — transport was not explicitly selected.",
                    )
                )
                continue
            if state != "device":
                report.checks.append(
                    self._check(
                        f"adb.transport.{index}.properties",
                        "not_run",
                        f"Not run — transport state is {safe_transport_state(state)}.",
                    )
                )
                continue
            self._probe_ready_adb_transport(report, index, serial)
        if self.config.serial and all(serial != self.config.serial for serial, _state in devices):
            report.checks.append(
                self._check(
                    "adb.selected_transport",
                    "failed",
                    "The explicit read-only target was not found.",
                )
            )
        return devices

    def _probe_ready_adb_transport(self, report: LabReport, index: int, serial: str) -> None:
        probes = (
            ("get_state", ["get-state"]),
            ("sdk", ["shell", "getprop", "ro.build.version.sdk"]),
            ("abi", ["shell", "getprop", "ro.product.cpu.abi"]),
        )
        for suffix, args in probes:
            outcome, duration = self._execute([self.adb_path, "-s", serial, *args])
            report.checks.append(
                self._check(
                    f"adb.transport.{index}.{suffix}",
                    "passed" if outcome.success else "failed",
                    "Read-only query completed." if outcome.success else _outcome_failure("Read-only ADB query", outcome),
                    duration,
                )
            )

    def _probe_fastboot(self, report: LabReport) -> None:
        if not self.fastboot_path:
            report.checks.extend(
                [
                    self._check("tool.fastboot", "not_run", "fastboot executable is unavailable."),
                    self._check("fastboot.transports", "not_run", NOT_RUN_HARDWARE),
                ]
            )
            return

        version, duration = self._execute([self.fastboot_path, "--version"])
        report.checks.append(
            self._check(
                "tool.fastboot",
                "passed" if version.success else "failed",
                "fastboot executable responded."
                if version.success
                else _outcome_failure("fastboot version probe", version),
                duration,
            )
        )
        devices_result, duration = self._execute([self.fastboot_path, "devices"])
        if not devices_result.success:
            report.checks.append(
                self._check(
                    "fastboot.transports",
                    "failed",
                    _outcome_failure("fastboot transport probe", devices_result),
                    duration,
                )
            )
            return
        serials = parse_fastboot_devices(devices_result.stdout)
        self._known_identifiers.extend(serials)
        report.fastboot_transport_count = len(serials)
        if not serials:
            report.checks.append(self._check("fastboot.transports", "not_run", NOT_RUN_HARDWARE, duration))
            return
        report.checks.append(
            self._check(
                "fastboot.transports",
                "passed",
                f"Detected {len(serials)} fastboot transport(s); detection only.",
                duration,
            )
        )

    def _perform_change(
        self,
        report: LabReport,
        change: ValidatedChange,
        adb_devices: list[tuple[str, str]],
    ) -> None:
        if not self.adb_path:
            report.checks.append(self._check("device_change", "not_run", NOT_RUN_HARDWARE))
            return
        selected_state = next(
            (state for serial, state in adb_devices if serial == self.config.serial),
            "",
        )
        if selected_state != "device":
            message = NOT_RUN_HARDWARE if not adb_devices else "Not run — the explicit target is not a ready ADB device."
            report.checks.append(self._check("device_change", "not_run", message))
            return

        package = change.package
        system_result, _duration = self._execute(
            [self.adb_path, "-s", self.config.serial, "shell", "pm", "list", "packages", "-s", package]
        )
        if not system_result.success:
            report.checks.append(
                self._check("device_change.preflight", "failed", _outcome_failure("System-package preflight", system_result))
            )
            return
        if package_in_pm_output(system_result.stdout, package):
            report.checks.append(
                self._check("device_change.preflight", "failed", "Refused: the disposable target is a system package.")
            )
            return

        user_result, _duration = self._execute(
            [self.adb_path, "-s", self.config.serial, "shell", "pm", "list", "packages", "-3", package]
        )
        path_result, _duration = self._execute(
            [self.adb_path, "-s", self.config.serial, "shell", "pm", "path", package]
        )
        if not user_result.success or not path_result.success:
            report.checks.append(
                self._check("device_change.preflight", "failed", "Package safety preflight did not complete; no change was made.")
            )
            return

        installed_user_app = package_in_pm_output(user_result.stdout, package)
        installed_anywhere = package_path_present(path_result.stdout)
        verified_apk: Path | None = None
        if change.operation == "install-disposable-apk":
            if installed_anywhere:
                report.checks.append(
                    self._check("device_change.preflight", "failed", "Refused: installation would replace an existing package.")
                )
                return
            try:
                apk_context = verified_apk_copy(change)
                verified_apk = apk_context.__enter__()
            except SafetyError:
                report.checks.append(
                    self._check("device_change.preflight", "failed", "Refused: the APK changed after validation.")
                )
                return
            command = [self.adb_path, "-s", self.config.serial, "install", str(verified_apk)]
        else:
            if not installed_user_app or not installed_anywhere:
                report.checks.append(
                    self._check("device_change.preflight", "failed", "Refused: target is not an installed third-party package.")
                )
                return
            command = [self.adb_path, "-s", self.config.serial, "uninstall", package]

        report.checks.append(self._check("device_change.preflight", "passed", "Disposable-target safety checks passed."))
        try:
            outcome, duration = self._execute(command, allow_change=True)
        finally:
            if verified_apk is not None:
                apk_context.__exit__(None, None, None)
        report.checks.append(
            self._check(
                "device_change",
                "passed" if outcome.success else "failed",
                "Explicit disposable-target operation completed."
                if outcome.success
                else _outcome_failure("Disposable-target operation", outcome),
                duration,
            )
        )

    def _execute(self, command: Sequence[str], *, allow_change: bool = False) -> tuple[CommandOutcome, int]:
        if not command_is_authorized(command, allow_change=allow_change):
            raise SafetyError("Command policy refused a non-approved device-lab operation.")
        started = time.perf_counter()
        outcome = self.executor(command, 300.0 if allow_change else self.config.timeout)
        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        return outcome, duration_ms

    def _check(
        self,
        check_id: str,
        status: str,
        message: str,
        duration_ms: int = 0,
    ) -> LabCheck:
        return LabCheck(
            check_id=check_id,
            status=status,
            message=anonymize_text(message, self._known_identifiers),
            duration_ms=duration_ms,
        )


def validate_change_request(config: LabConfig) -> ValidatedChange | None:
    has_change_argument = any(
        (
            config.allow_device_changes,
            config.change_operation,
            config.package,
            config.apk_path is not None,
            config.disposable_target,
            config.confirmation,
        )
    )
    if not has_change_argument:
        return None
    if not config.change_operation:
        raise SafetyError("Device-change arguments require an explicit change operation.")
    if config.change_operation not in CHANGE_OPERATIONS:
        raise SafetyError("The requested device-change operation is not supported.")
    if not config.allow_device_changes:
        raise SafetyError("Device-changing mode requires --allow-device-changes.")
    if not config.serial.strip():
        raise SafetyError("Device-changing mode requires an explicit --serial target.")
    if not config.disposable_target:
        raise SafetyError("Device-changing mode requires --disposable-target.")

    package = config.package.strip()
    if not _PACKAGE_RE.fullmatch(package):
        raise SafetyError("A valid lowercase Android package name is required.")
    if is_dangerous_package(package) or package == "android" or package.startswith(_SYSTEM_PACKAGE_PREFIXES):
        raise SafetyError("System and protected packages are prohibited.")
    if not (_DISPOSABLE_SEGMENTS & set(package.split("."))):
        raise SafetyError("The package name must explicitly identify a disposable test, lab, demo, or sample target.")

    expected_confirmation = confirmation_phrase(package)
    if config.confirmation != expected_confirmation:
        raise SafetyError("Typed confirmation did not exactly match the disposable target.")

    if config.change_operation == "uninstall-disposable-package":
        if config.apk_path is not None:
            raise SafetyError("Uninstall does not accept an APK path.")
        return ValidatedChange(config.change_operation, package)

    if config.apk_path is None:
        raise SafetyError("APK installation requires an explicit --apk-path.")
    apk_path = config.apk_path.expanduser()
    try:
        if apk_path.is_symlink():
            raise SafetyError("Symbolic-link APK targets are refused.")
        apk_path = apk_path.resolve(strict=True)
    except OSError as exc:
        raise SafetyError("The explicit APK path is unavailable.") from exc
    if not apk_path.is_file() or apk_path.suffix.casefold() != ".apk":
        raise SafetyError("The explicit path must be an existing APK file.")
    manifest_package = read_apk_package(apk_path)
    if manifest_package != package:
        raise SafetyError("The APK manifest package does not match the explicit disposable package.")
    return ValidatedChange(
        config.change_operation,
        package,
        apk_path,
        _sha256_file(apk_path),
    )


def confirmation_phrase(package: str) -> str:
    return f"CHANGE DISPOSABLE {package}"


def read_apk_package(path: Path) -> str:
    try:
        from apkutils2 import APK
    except ImportError as exc:  # pragma: no cover - a pinned runtime dependency
        raise SafetyError("APK metadata support is unavailable; installation was refused.") from exc
    try:
        with quiet_third_party_output():
            manifest = APK(str(path)).get_manifest() or {}
    except Exception as exc:
        raise SafetyError("APK metadata could not be verified; installation was refused.") from exc
    return str(manifest.get("@package", "")).strip()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def verified_apk_copy(change: ValidatedChange):
    """Copy and revalidate an APK immediately before adb opens the stable copy."""

    if change.apk_path is None or not change.apk_sha256:
        raise SafetyError("APK validation evidence is incomplete.")
    with tempfile.TemporaryDirectory(prefix="openadb-device-lab-") as temp:
        stable_copy = Path(temp) / "disposable-target.apk"
        try:
            shutil.copyfile(change.apk_path, stable_copy)
            valid = (
                _sha256_file(stable_copy) == change.apk_sha256
                and read_apk_package(stable_copy) == change.package
            )
        except (OSError, SafetyError) as exc:
            raise SafetyError("APK changed or could not be revalidated.") from exc
        if not valid:
            raise SafetyError("APK changed or could not be revalidated.")
        yield stable_copy


def command_is_authorized(command: Sequence[str], *, allow_change: bool = False) -> bool:
    """Allow only enumerated read-only probes or two gated disposable-app actions."""

    if not command:
        return False
    tool = str(command[0]).replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if tool.endswith(".exe"):
        tool = tool[:-4]
    args = [str(part) for part in command[1:]]
    if tool == "fastboot":
        return args in (["--version"], ["devices"])
    if tool != "adb":
        return False
    if args in (["version"], ["devices", "-l"]):
        return True
    if len(args) < 3 or args[0] != "-s" or not args[1]:
        return False
    operation = args[2:]
    if operation == ["get-state"]:
        return True
    if len(operation) == 3 and operation[:2] == ["shell", "getprop"]:
        return operation[2] in _ALLOWED_PROPERTIES
    if (
        len(operation) == 6
        and operation[:4] == ["shell", "pm", "list", "packages"]
        and operation[4] in {"-s", "-3"}
    ):
        return _PACKAGE_RE.fullmatch(operation[5]) is not None
    if len(operation) == 4 and operation[:3] == ["shell", "pm", "path"]:
        return _PACKAGE_RE.fullmatch(operation[3]) is not None
    if not allow_change:
        return False
    if len(operation) == 2 and operation[0] == "uninstall":
        return _PACKAGE_RE.fullmatch(operation[1]) is not None
    if len(operation) == 2 and operation[0] == "install":
        raw_path = operation[1]
        path = Path(raw_path)
        absolute = path.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", raw_path) is not None
        return absolute and path.suffix.casefold() == ".apk"
    return False


def parse_adb_devices(output: str) -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0].strip(), parts[1].strip().casefold()
        if serial and state:
            devices.append((serial, safe_transport_state(state)))
    return devices


def parse_fastboot_devices(output: str) -> list[str]:
    devices: list[str] = []
    for raw_line in str(output or "").splitlines():
        parts = raw_line.strip().split()
        if len(parts) == 1 or (len(parts) >= 2 and parts[1].casefold() == "fastboot"):
            devices.append(parts[0])
    return devices


def safe_transport_state(state: str) -> str:
    normalized = str(state or "").strip().casefold()
    return normalized if normalized in {"device", "offline", "unauthorized", "recovery", "sideload"} else "unknown"


def package_in_pm_output(output: str, package: str) -> bool:
    for line in str(output or "").splitlines():
        value = line.strip()
        if value.startswith("package:"):
            value = value.removeprefix("package:").split("=", 1)[-1]
            if value == package or value.endswith("/" + package):
                return True
    return False


def package_path_present(output: str) -> bool:
    """Return whether `pm path <package>` produced at least one APK path."""

    return any(
        line.strip().startswith("package:") and bool(line.strip().removeprefix("package:").strip())
        for line in str(output or "").splitlines()
    )


def anonymize_text(value: object, identifiers: Iterable[str] = ()) -> str:
    """Remove identifiers and personal path/file data from report text."""

    text = redact_sensitive_text(value)
    candidates = {str(item).strip() for item in identifiers if str(item).strip()}
    home = str(Path.home())
    if home:
        candidates.add(home)
    try:
        username = getpass.getuser().strip()
    except (ImportError, OSError):  # pragma: no cover - platform fallback
        username = ""
    for candidate in sorted(candidates, key=len, reverse=True):
        text = re.sub(re.escape(candidate), "[REDACTED]", text, flags=re.IGNORECASE)
    text = _HOME_PATH_RE.sub("[REDACTED PATH]", text)
    text = _IPV4_RE.sub("[REDACTED IP]", text)
    text = _BRACKETED_IPV6_RE.sub("[REDACTED IP]", text)
    text = _UNBRACKETED_IPV6_RE.sub("[REDACTED IP]", text)
    if username and len(username) >= 3:
        text = re.sub(rf"(?i)(?<![\w]){re.escape(username)}(?![\w])", "[REDACTED ACCOUNT]", text)
    return _FILENAME_RE.sub("[REDACTED FILE]", text)


def source_commit() -> str:
    """Return only a full GitHub Actions commit SHA, never arbitrary env text."""

    candidate = str(os.environ.get("GITHUB_SHA", "")).strip()
    return candidate.casefold() if _COMMIT_RE.fullmatch(candidate) else "unavailable"


def generic_environment() -> dict[str, str]:
    """Return generic host traits without hostname, username, or filesystem data."""

    return {
        "os": _generic_environment_value(platform.system()),
        "release": _generic_environment_value(platform.release()),
        "architecture": _generic_environment_value(platform.machine()),
    }


def _generic_environment_value(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _GENERIC_ENV_RE.fullmatch(candidate) else "unavailable"


def _outcome_failure(label: str, outcome: CommandOutcome) -> str:
    if outcome.timed_out:
        return f"{label} timed out."
    if outcome.unavailable:
        return f"{label} could not start."
    return f"{label} failed with exit code {outcome.returncode}."


def resolve_platform_tool(platform_tools: Path | None, name: str) -> Path | None:
    executable_names = (f"{name}.exe", name)
    if platform_tools is not None:
        candidate = platform_tools.expanduser()
        if candidate.is_file() and candidate.name.casefold() in {item.casefold() for item in executable_names}:
            return candidate.resolve()
        for executable_name in executable_names:
            executable = candidate / executable_name
            if executable.is_file():
                return executable.resolve()
        return None
    for directory in (ROOT / "platform-tools", ROOT):
        for executable_name in executable_names:
            candidate = directory / executable_name
            if candidate.is_file():
                return candidate.resolve()
    found = shutil.which(f"{name}.exe") or shutil.which(name)
    return Path(found).resolve() if found else None


def write_json_report(path: Path, report: LabReport) -> None:
    payload = json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
    if str(path) == "-":
        sys.stdout.write(payload)
        return
    _atomic_write(path, payload.encode("utf-8"))


def write_junit_report(path: Path, report: LabReport) -> None:
    summary = report.summary
    suite = ET.Element(
        "testsuite",
        {
            "name": "OpenADB device lab",
            "tests": str(len(report.checks)),
            "failures": str(summary["failed"]),
            "skipped": str(summary["not_run"]),
        },
    )
    for check in report.checks:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": "device_lab_smoke",
                "name": check.check_id,
                "time": f"{check.duration_ms / 1000:.3f}",
            },
        )
        if check.status == "failed":
            ET.SubElement(case, "failure", {"message": check.message})
        elif check.status == "not_run":
            ET.SubElement(case, "skipped", {"message": check.message})
    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    payload = ET.tostring(suite, encoding="utf-8", xml_declaration=True)
    _atomic_write(path, payload)


def _atomic_write(path: Path, payload: bytes) -> None:
    """Replace a report only after its complete sanitized payload is durable."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a privacy-safe OpenADB device-lab smoke check. Default operations are read-only.",
    )
    parser.add_argument("--platform-tools", type=Path, help="Explicit platform-tools directory.")
    parser.add_argument("--serial", default="", help="Explicit target serial; never written to reports.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Read-only command timeout (1-120 seconds).")
    parser.add_argument("--json-report", type=Path, default=Path("device-lab-report.json"))
    parser.add_argument("--junit-report", type=Path)
    parser.add_argument("--allow-device-changes", action="store_true")
    parser.add_argument("--change-operation", choices=CHANGE_OPERATIONS, default="")
    parser.add_argument("--package", default="", help="Explicit disposable package name.")
    parser.add_argument("--apk-path", type=Path, help="Explicit disposable APK path for install.")
    parser.add_argument("--disposable-target", action="store_true")
    parser.add_argument(
        "--confirmation",
        default="",
        help="Exact typed phrase: CHANGE DISPOSABLE <package>.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    executor: CommandExecutor | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.timeout <= 120:
        raise SystemExit("--timeout must be between 1 and 120 seconds")
    config = LabConfig(
        timeout=args.timeout,
        serial=args.serial,
        allow_device_changes=args.allow_device_changes,
        change_operation=args.change_operation,
        package=args.package,
        apk_path=args.apk_path,
        disposable_target=args.disposable_target,
        confirmation=args.confirmation,
    )
    lab = DeviceLab(
        config,
        adb_path=resolve_platform_tool(args.platform_tools, "adb"),
        fastboot_path=resolve_platform_tool(args.platform_tools, "fastboot"),
        executor=executor,
    )
    report = lab.run()
    write_json_report(args.json_report, report)
    if args.junit_report is not None:
        write_junit_report(args.junit_report, report)
    if str(args.json_report) != "-":
        print(f"Device-lab report status: {report.status}")
    return 1 if report.status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
