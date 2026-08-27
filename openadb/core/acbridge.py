from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openadb.core.adb import ADBClient
from openadb.models.command_result import CommandResult
from openadb.version import (
    ACBRIDGE_APK_FILENAME,
    ACBRIDGE_PACKAGE,
    ACBRIDGE_VERSION_CODE,
    VERSION,
)

from .icon_extractor import IconExtractor
from .path_utils import ensure_dir, package_root, safe_filename, shell_quote
from .settings_manager import SettingsManager

LOGGER = logging.getLogger(__name__)


@dataclass
class ACBridgeResult:
    available: bool
    labels: dict[str, str]
    metadata: dict[str, dict[str, str]]
    icons: dict[str, Path]
    message: str


@dataclass(slots=True)
class ACBridgeExportState:
    data_ready: bool
    icons_ready: bool
    error_ready: bool
    elapsed: float
    data_path: str = ""
    icons_path: str = ""
    error_path: str = ""
    raw_output: str = ""


@dataclass(frozen=True, slots=True)
class ACBridgeUpdateResult:
    """Outcome of connection-time ACBridge provisioning and maintenance."""

    state: str
    bundled_version_code: int
    installed_version_code: int | None = None
    previous_version_code: int | None = None
    message: str = ""
    transient: bool = False

    @property
    def updated(self) -> bool:
        return self.state == "updated"

    @property
    def installed(self) -> bool:
        return self.state == "installed"

    @property
    def changed(self) -> bool:
        return self.state in {"installed", "updated"}

    @property
    def should_retry(self) -> bool:
        return self.state == "query_failed" or self.transient

    @property
    def failed(self) -> bool:
        return self.state in {
            "install_failed",
            "query_failed",
            "update_failed",
            "verification_failed",
        }


@dataclass(frozen=True, slots=True)
class ACBridgePrivilegeResult:
    """Fixed-purpose privilege response produced by the trusted bridge APK."""

    backend: str
    state: str
    permission: str
    uid: int | None
    message: str
    request_id: str = ""

    @property
    def ready(self) -> bool:
        if self.backend == "root":
            return (
                self.state == "ready"
                and self.permission == "granted"
                and self.uid == 0
            )
        return self.state == "ready" and self.permission == "not_required"

    @property
    def cancelled(self) -> bool:
        return self.state == "cancelled"


@dataclass(frozen=True, slots=True)
class ACBridgePermissionHostResult:
    """Result of creating ACBridge's temporary foreground permission task."""

    backend: str
    request_id: str
    started: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class _ACBridgeVersionProbe:
    state: str
    version_code: int | None = None
    message: str = ""


class ACBridgeClient:
    """Read app labels and rendered icons produced by a device-side bridge app.

    OpenADB bundles its own independent helper APK. The helper uses Android's
    PackageManager on the device, so labels and rendered icons can be exported
    without pulling every APK to the PC.
    """

    PACKAGE = ACBRIDGE_PACKAGE
    ACTIVITY = f"{PACKAGE}/.CommandActivity"
    PRIVILEGE_ACTIVITY = f"{PACKAGE}/.PrivilegeActivity"
    PERMISSION_HOST_ACTIVITY = f"{PACKAGE}/.PermissionHostActivity"
    PERMISSION_HOST_RECEIVER = f"{PACKAGE}/.PermissionHostReceiver"
    HOST_STATUS_AUTHORITY = "com.communism420.acbridge.openadb.status"
    VERSION_NAME = VERSION
    VERSION_CODE = ACBRIDGE_VERSION_CODE
    APK_FILENAME = ACBRIDGE_APK_FILENAME
    REMOTE_DIR = "/sdcard/.adac"
    REMOTE_APP_DIR = f"/sdcard/Android/data/{PACKAGE}/files/openadb"
    REMOTE_SETTINGS = "/sdcard/.adac/settings"
    REMOTE_APP_SETTINGS = f"{REMOTE_APP_DIR}/settings"
    REMOTE_DATA = "/sdcard/.adac/.acbridge"
    REMOTE_METADATA = "/sdcard/.adac/metadata.tsv"
    REMOTE_APP_DATA = f"{REMOTE_APP_DIR}/.acbridge"
    REMOTE_APP_METADATA = f"{REMOTE_APP_DIR}/metadata.tsv"
    REMOTE_ICONS_ZIP = "/sdcard/.adac/icons.zip"
    REMOTE_APP_ICONS_ZIP = f"{REMOTE_APP_DIR}/icons.zip"
    REMOTE_ERROR = "/sdcard/.adac/error.txt"
    REMOTE_PROGRESS = "/sdcard/.adac/progress.txt"
    REMOTE_REQUEST = "/sdcard/.adac/packages.txt"
    REMOTE_APP_ERROR = f"{REMOTE_APP_DIR}/error.txt"
    REMOTE_APP_PROGRESS = f"{REMOTE_APP_DIR}/progress.txt"
    REMOTE_APP_REQUEST = f"{REMOTE_APP_DIR}/packages.txt"
    REMOTE_DELETE_RESULT = "/sdcard/.adac/delete_result.txt"
    REMOTE_APP_DELETE_RESULT = f"{REMOTE_APP_DIR}/delete_result.txt"
    REMOTE_PRIVILEGE_PREFIX = "/sdcard/.adac/privilege_status_"
    REMOTE_APP_PRIVILEGE_PREFIX = f"{REMOTE_APP_DIR}/privilege_status_"
    PRIVILEGE_PROTOCOL_HEADER = "OPENADB_BRIDGE_PRIVILEGE_STATUS 1"
    PERMISSION_HOST_PROTOCOL_HEADER = "OPENADB_PERMISSION_HOST_STATUS 1"
    PRIVILEGE_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
    LABEL_SEPARATOR = "\\+\\"
    STORAGE_TREE_ACTION = "android.intent.action.OPEN_DOCUMENT_TREE"
    DOCUMENTS_UI_PACKAGE = "com.android.documentsui"
    DOCUMENTS_UI_PACKAGES = (
        DOCUMENTS_UI_PACKAGE,
        "com.google.android.documentsui",
    )
    STORAGE_TREE_COMPONENT_RE = re.compile(
        r"(?m)^[A-Za-z0-9._]+/[A-Za-z0-9._$]+$"
    )
    PACKAGE_NAME_RE = re.compile(
        r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z"
    )
    PACKAGE_PATH_RE = re.compile(r"package:(/[^\x00-\x1f]+)\Z")
    MAX_TRUSTED_APK_BYTES = 64 * 1024 * 1024
    _INSTALL_LOCK = threading.RLock()

    def __init__(
        self,
        adb: ADBClient,
        settings: SettingsManager,
        icon_extractor: IconExtractor | None = None,
        *,
        temp_folder: str | Path | None = None,
    ) -> None:
        self.adb = adb
        self.settings = settings
        self.icon_extractor = icon_extractor
        self._temp_folder = Path(temp_folder).expanduser() if temp_folder is not None else None

    def _local_temp_dir(self) -> Path:
        base = self._temp_folder
        if base is None:
            base = Path(self.settings.temp_folder).expanduser()
        return ensure_dir(base / "acbridge")

    def start_permission_host(
        self,
        backend: str,
        *,
        timeout: int = 360,
        cancel_event=None,
        bridge_is_current: bool = False,
    ) -> ACBridgePermissionHostResult:
        """Keep a foreground ACBridge task alive during an external grant UI."""

        selected = str(backend or "").strip().casefold()
        if selected not in {"root", "shizuku"}:
            return ACBridgePermissionHostResult(
                backend=selected or "unknown",
                request_id="",
                started=False,
                message="ACBridge permission hosting supports Root and Shizuku only.",
            )
        if cancel_event is not None and cancel_event.is_set():
            return ACBridgePermissionHostResult(
                backend=selected,
                request_id="",
                started=False,
                message="ACBridge permission hosting was cancelled.",
            )
        if not bridge_is_current:
            installed, message = self.ensure_trusted(
                cancel_event=cancel_event,
            )
            if not installed:
                return ACBridgePermissionHostResult(
                    backend=selected,
                    request_id="",
                    started=False,
                    message=message or "The trusted ACBridge helper is unavailable.",
                )

        request_id = uuid.uuid4().hex
        timeout = max(30, min(900, int(timeout)))
        provider_uri = self._host_status_uri("permission_host", request_id)
        try:
            self.adb.run_shell(
                f"content delete --uri {shell_quote(provider_uri)} >/dev/null 2>&1 || true",
                timeout=10,
                cancel_event=cancel_event,
            )
            start = self.adb.run_shell(
                (
                    f"am start -W -f 0x10000000 "
                    f"-n {shell_quote(self.PERMISSION_HOST_ACTIVITY)} "
                    "--es operation open "
                    f"--es backend {shell_quote(selected)} "
                    f"--es request_id {shell_quote(request_id)} "
                    f"--ei timeout_seconds {timeout}"
                ),
                timeout=20,
                cancel_event=cancel_event,
            )
        except (OSError, RuntimeError) as exc:
            self.dismiss_permission_host(request_id)
            return ACBridgePermissionHostResult(
                backend=selected,
                request_id=request_id,
                started=False,
                message=f"Android could not open the ACBridge permission host: {exc}",
            )
        if cancel_event is not None and cancel_event.is_set():
            self.dismiss_permission_host(request_id)
            return ACBridgePermissionHostResult(
                backend=selected,
                request_id=request_id,
                started=False,
                message="ACBridge permission hosting was cancelled.",
            )
        if not start.success:
            self.dismiss_permission_host(request_id)
            return ACBridgePermissionHostResult(
                backend=selected,
                request_id=request_id,
                started=False,
                message=(
                    start.status
                    or start.stderr
                    or "Android could not keep ACBridge in the foreground for the permission request."
                ),
            )
        ready = self._wait_for_permission_host_state(
            request_id,
            expected_state="ready",
            timeout=12,
        )
        if not ready:
            self.dismiss_permission_host(request_id)
            return ACBridgePermissionHostResult(
                backend=selected,
                request_id=request_id,
                started=False,
                message=(
                    "ACBridge opened, but Android did not confirm that its "
                    "permission host had foreground focus. Unlock the device and try again."
                ),
            )
        return ACBridgePermissionHostResult(
            backend=selected,
            request_id=request_id,
            started=True,
            message="Android confirmed that the ACBridge permission host is in the foreground.",
        )

    def dismiss_permission_host(self, request_id: str) -> bool:
        """Close one host token without masking the permission result.

        The Android receiver ignores stale request ids and publishes a closed
        acknowledgement.  This method intentionally never forwards the caller's
        cancellation event and never raises from a surrounding ``finally`` block.
        ``True`` means Android acknowledged that the exact task token is closed.
        """

        normalized = str(request_id or "").strip().casefold()
        if not self.PRIVILEGE_REQUEST_ID_RE.fullmatch(normalized):
            return False
        provider_uri = self._host_status_uri("permission_host", normalized)
        closed = False
        for _attempt in range(2):
            try:
                self.adb.run_shell(
                    (
                        "am broadcast --receiver-foreground "
                        f"-n {shell_quote(self.PERMISSION_HOST_RECEIVER)} "
                        "--es operation dismiss "
                        f"--es request_id {shell_quote(normalized)}"
                    ),
                    timeout=15,
                )
            except (OSError, RuntimeError) as exc:
                LOGGER.debug(
                    "ACBridge permission-host dismissal could not be delivered: %s",
                    exc,
                )
                continue
            if self._wait_for_permission_host_state(
                normalized,
                expected_state="closed",
                timeout=6,
            ):
                closed = True
                break
        try:
            self.adb.run_shell(
                f"content delete --uri {shell_quote(provider_uri)} >/dev/null 2>&1 || true",
                timeout=10,
            )
        except (OSError, RuntimeError) as exc:
            LOGGER.debug("ACBridge permission-host status cleanup was skipped: %s", exc)
        return closed

    def _wait_for_permission_host_state(
        self,
        request_id: str,
        *,
        expected_state: str,
        timeout: int,
    ) -> bool:
        normalized = str(request_id or "").strip().casefold()
        state = str(expected_state or "").strip().casefold()
        if (
            not self.PRIVILEGE_REQUEST_ID_RE.fullmatch(normalized)
            or state not in {"ready", "closed"}
        ):
            return False
        provider_uri = self._host_status_uri("permission_host", normalized)
        bounded_timeout = max(1, min(20, int(timeout)))
        script = (
            f"provider={shell_quote(provider_uri)}; "
            f"deadline=$(( $(date +%s) + {bounded_timeout} )); "
            f"expected={shell_quote(f'state={state}')}; "
            "while [ \"$(date +%s)\" -lt \"$deadline\" ]; do "
            "payload=\"$(content read --uri \"$provider\" 2>/dev/null)\"; "
            f"case \"$payload\" in \"{self.PERMISSION_HOST_PROTOCOL_HEADER}\"*) "
            "case \"$payload\" in *\"$expected\"*) printf '%s\\n' \"$payload\"; exit 0;; esac;; "
            "esac; sleep 0.1; done; exit 124"
        )
        try:
            result = self.adb.run_shell(script, timeout=bounded_timeout + 5)
        except (OSError, RuntimeError) as exc:
            LOGGER.debug("ACBridge permission-host acknowledgement failed: %s", exc)
            return False
        if not result.success:
            return False
        lines = [line.rstrip("\r") for line in str(result.stdout or "").splitlines()]
        if not lines or lines[0] != self.PERMISSION_HOST_PROTOCOL_HEADER:
            return False
        fields: dict[str, str] = {}
        for line in lines[1:]:
            key, separator, value = line.partition("=")
            if not separator or key in fields:
                return False
            fields[key] = value
        return fields == {
            "request_id": normalized,
            "state": state,
        }

    def request_privilege_access(
        self,
        backend: str,
        *,
        timeout: int = 150,
        cancel_event=None,
        bridge_is_current: bool = False,
        permission_host_request_id: str = "",
    ) -> ACBridgePrivilegeResult:
        """Ask ACBridge itself for the selected fixed-purpose access grant.

        Root managers authorize Android application UIDs independently from the
        ADB shell UID.  Consequently a successful ``adb shell su`` check cannot
        prove that ACBridge has root.  This protocol starts the DUMP-protected
        command activity through ordinary ADB and lets the trusted app execute
        only the literal ``su -c id -u`` probe implemented in the APK.

        Shizuku is intentionally excluded: its permission is already requested
        by ACBridge's ShizukuActivity and verified through its UserService, so a
        second request here would duplicate the same package-scoped permission.
        """

        selected = str(backend or "").strip().casefold()
        if selected not in {"standard", "root"}:
            return ACBridgePrivilegeResult(
                backend=selected or "unknown",
                state="unsupported",
                permission="unknown",
                uid=None,
                message=(
                    "ACBridge privilege requests support Standard and Root only. "
                    "Shizuku access is requested through ACBridge ShizukuActivity."
                ),
            )
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_privilege_result(selected)

        normalized_host_request_id = str(
            permission_host_request_id or ""
        ).strip().casefold()
        if normalized_host_request_id and not self.PRIVILEGE_REQUEST_ID_RE.fullmatch(
            normalized_host_request_id
        ):
            return ACBridgePrivilegeResult(
                backend=selected,
                state="protocol_error",
                permission="unknown",
                uid=None,
                message="OpenADB supplied an invalid permission-host request identifier.",
            )

        installed, install_message = (
            (True, "The trusted ACBridge helper was already verified for this host.")
            if bridge_is_current
            else self.ensure_trusted(
                cancel_event=cancel_event,
            )
        )
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_privilege_result(selected)
        if not installed:
            return ACBridgePrivilegeResult(
                backend=selected,
                state="unavailable",
                permission="unavailable",
                uid=None,
                message=install_message or "The trusted ACBridge helper is unavailable.",
            )

        request_id = uuid.uuid4().hex
        if not self.PRIVILEGE_REQUEST_ID_RE.fullmatch(request_id):
            return ACBridgePrivilegeResult(
                backend=selected,
                state="protocol_error",
                permission="unknown",
                uid=None,
                message="ACBridge generated an invalid privilege request identifier.",
            )
        timeout = max(5, min(300, int(timeout)))
        remote_result, remote_app_result = self._privilege_result_paths(request_id)
        prepare = self._prepare_privilege_result_channels(
            remote_result,
            remote_app_result,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_privilege_result(selected, request_id=request_id)
        prepare_warning = ""
        if not prepare.success:
            # The app-private provider is authoritative.  Legacy cleanup is
            # best effort so older helpers can still publish a readable file.
            prepare_warning = (
                prepare.stderr
                or prepare.status
                or "ACBridge could not prepare its privilege result channel."
            )

        host_argument = (
            " --es permission_host_request_id "
            f"{shell_quote(normalized_host_request_id)}"
            if normalized_host_request_id
            else ""
        )
        start = self.adb.run_shell(
            (
                f"am start -n {shell_quote(self.PRIVILEGE_ACTIVITY)} "
                "--es operation requestPrivilege "
                f"--es backend {shell_quote(selected)} "
                f"--es request_id {shell_quote(request_id)} "
                f"--ei timeout_seconds {timeout}"
                f"{host_argument} "
                "--ez endexit true"
            ),
            timeout=20,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            self._cancel_privilege_request(request_id)
            self._cleanup_privilege_result(remote_result, remote_app_result)
            return self._cancelled_privilege_result(selected, request_id=request_id)
        if not start.success:
            # ``am start`` can report a transport timeout after Android has
            # already created the Activity.  Cancel the request explicitly so
            # its Root worker cannot outlive this failed host attempt and race
            # the next backend switch.
            self._cancel_privilege_request(request_id)
            self._cleanup_privilege_result(remote_result, remote_app_result)
            message = (
                start.status
                or start.stderr
                or "ACBridge privilege request could not be started."
            )
            return ACBridgePrivilegeResult(
                backend=selected,
                state="start_failed",
                permission="unknown",
                uid=None,
                message=message,
                request_id=request_id,
            )

        try:
            raw = self._wait_for_privilege_result(
                remote_result,
                remote_app_result,
                timeout=timeout,
                cancel_event=cancel_event,
                request_id=request_id,
            )
            if cancel_event is not None and cancel_event.is_set():
                self._cancel_privilege_request(request_id)
                return self._cancelled_privilege_result(
                    selected,
                    request_id=request_id,
                )
            if not raw.success:
                self._cancel_privilege_request(request_id)
                message = (
                    raw.stderr
                    or raw.status
                    or "ACBridge did not return a privilege decision before timeout."
                )
                if prepare_warning:
                    message = (
                        f"{message} Shell-owned result preparation also failed: "
                        f"{prepare_warning}"
                    )
                unreadable = bool(
                    raw.exit_code == 13
                    or "cannot read" in str(raw.stderr or "").casefold()
                )
                return ACBridgePrivilegeResult(
                    backend=selected,
                    state="result_unreadable" if unreadable else "timed_out",
                    permission="unknown",
                    uid=None,
                    message=message,
                    request_id=request_id,
                )
            return self._parse_privilege_result(
                raw.stdout,
                expected_backend=selected,
                expected_request_id=request_id,
            )
        finally:
            self._cleanup_privilege_result(remote_result, remote_app_result)

    @staticmethod
    def _cancelled_privilege_result(
        backend: str,
        *,
        request_id: str = "",
    ) -> ACBridgePrivilegeResult:
        return ACBridgePrivilegeResult(
            backend=backend,
            state="cancelled",
            permission="unknown",
            uid=None,
            message="ACBridge privilege request was cancelled.",
            request_id=request_id,
        )

    def _privilege_result_paths(self, request_id: str) -> tuple[str, str]:
        if not self.PRIVILEGE_REQUEST_ID_RE.fullmatch(request_id):
            raise ValueError("Invalid ACBridge privilege request identifier")
        filename = f"{request_id}.txt"
        return (
            self.REMOTE_PRIVILEGE_PREFIX + filename,
            self.REMOTE_APP_PRIVILEGE_PREFIX + filename,
        )

    @classmethod
    def _host_status_uri(cls, kind: str, request_id: str) -> str:
        normalized_kind = str(kind or "").strip().casefold()
        if normalized_kind not in {"privilege", "shizuku", "permission_host"}:
            raise ValueError("Invalid ACBridge host-status kind")
        if not cls.PRIVILEGE_REQUEST_ID_RE.fullmatch(str(request_id or "")):
            raise ValueError("Invalid ACBridge host-status request identifier")
        return (
            f"content://{cls.HOST_STATUS_AUTHORITY}/"
            f"{normalized_kind}/{request_id}"
        )

    @classmethod
    def _privilege_request_id_from_path(cls, result_path: str) -> str:
        match = re.search(
            r"/privilege_status_([0-9a-f]{32})\.txt\Z",
            str(result_path or ""),
        )
        if match is None:
            raise ValueError("Invalid ACBridge privilege result path")
        return match.group(1)

    @staticmethod
    def _privilege_temporary_path(result_path: str) -> str:
        directory, separator, filename = str(result_path or "").rpartition("/")
        if not separator or not directory or not filename:
            raise ValueError("Invalid ACBridge privilege result path")
        return f"{directory}/.{filename}.tmp"

    def _prepare_privilege_result_channels(
        self,
        remote_result: str,
        remote_app_result: str,
        *,
        cancel_event=None,
    ) -> CommandResult:
        """Clear request-scoped legacy files without changing their ownership.

        The authoritative result now lives in ACBridge app-private storage and
        is read through a DUMP-protected provider.  In particular, do not create
        ``Android/data`` directories or temporary files as ADB shell here:
        Android 16 can prevent a newly installed ACBridge UID from replacing
        those cross-UID objects even when their Unix mode appears writable.
        """

        remote_temporary = self._privilege_temporary_path(remote_result)
        remote_app_temporary = self._privilege_temporary_path(remote_app_result)
        request_id = self._privilege_request_id_from_path(remote_result)
        provider_uri = self._host_status_uri("privilege", request_id)
        command = (
            f"content delete --uri {shell_quote(provider_uri)} >/dev/null 2>&1 || true; "
            "rm -f "
            f"{shell_quote(remote_result)} {shell_quote(remote_temporary)} "
            f"{shell_quote(remote_app_result)} {shell_quote(remote_app_temporary)} "
            ">/dev/null 2>&1 || true"
        )
        return self.adb.run_shell(
            command,
            timeout=20,
            cancel_event=cancel_event,
        )

    def _wait_for_privilege_result(
        self,
        remote_result: str,
        remote_app_result: str,
        *,
        timeout: int,
        cancel_event=None,
        request_id: str = "",
    ) -> CommandResult:
        normalized_request_id = str(request_id or "")
        if not self.PRIVILEGE_REQUEST_ID_RE.fullmatch(normalized_request_id):
            try:
                normalized_request_id = self._privilege_request_id_from_path(remote_result)
            except ValueError:
                normalized_request_id = ""
        provider_uri = (
            self._host_status_uri("privilege", normalized_request_id)
            if normalized_request_id
            else ""
        )
        bounded_timeout = max(5, min(300, int(timeout)))
        script = (
            f"result1={shell_quote(remote_result)}; result2={shell_quote(remote_app_result)}; "
            f"provider={shell_quote(provider_uri)}; "
            f"deadline=$(( $(date +%s) + {bounded_timeout} )); delay=0.2; "
            "while [ \"$(date +%s)\" -lt \"$deadline\" ]; do "
            "if [ -n \"$provider\" ]; then "
            "provider_payload=\"$(content read --uri \"$provider\" 2>/dev/null)\"; "
            "case \"$provider_payload\" in "
            "\"OPENADB_BRIDGE_PRIVILEGE_STATUS 1\"*) "
            "printf '%s\\n' \"$provider_payload\"; exit 0;; esac; fi; "
            "result_path=''; "
            "[ -s \"$result1\" ] && result_path=\"$result1\"; "
            "[ -z \"$result_path\" ] && [ -s \"$result2\" ] && result_path=\"$result2\"; "
            "if [ -n \"$result_path\" ]; then "
            "cat \"$result_path\" && exit 0; "
            "echo 'ACBridge privilege result exists but ADB shell cannot read it.' >&2; exit 13; "
            "fi; "
            "sleep \"$delay\"; "
            "done; echo 'ACBridge privilege result was not produced before timeout.' >&2; exit 1"
        )
        return self.adb.run_shell(
            script,
            timeout=max(5, min(300, int(timeout))) + 8,
            cancel_event=cancel_event,
        )

    def _cleanup_privilege_result(
        self,
        remote_result: str,
        remote_app_result: str,
    ) -> None:
        remote_temporary = self._privilege_temporary_path(remote_result)
        remote_app_temporary = self._privilege_temporary_path(remote_app_result)
        request_id = self._privilege_request_id_from_path(remote_result)
        provider_uri = self._host_status_uri("privilege", request_id)
        try:
            self.adb.run_shell(
                "rm -f "
                f"{shell_quote(remote_result)} {shell_quote(remote_temporary)} "
                f"{shell_quote(remote_app_result)} {shell_quote(remote_app_temporary)} "
                ">/dev/null 2>&1 || true; "
                f"content delete --uri {shell_quote(provider_uri)} >/dev/null 2>&1 || true",
                timeout=10,
            )
        except (OSError, RuntimeError) as exc:
            LOGGER.debug("ACBridge privilege-result cleanup was skipped: %s", exc)

    def _cancel_privilege_request(self, request_id: str) -> None:
        if not self.PRIVILEGE_REQUEST_ID_RE.fullmatch(request_id):
            return
        remote_result, remote_app_result = self._privilege_result_paths(request_id)
        remote_temporary = self._privilege_temporary_path(remote_result)
        remote_app_temporary = self._privilege_temporary_path(remote_app_result)
        provider_uri = self._host_status_uri("privilege", request_id)
        try:
            self.adb.run_shell(
                (
                    f"am start -n {shell_quote(self.PRIVILEGE_ACTIVITY)} "
                    "--es operation cancelPrivilege "
                    f"--es request_id {shell_quote(request_id)}; "
                    # The active Activity writes its terminal cancellation result
                    # asynchronously on the Android main thread.  Remove that
                    # request-scoped file after the callback has settled so a
                    # cancelled mode switch cannot accumulate stale artifacts.
                    "sleep 1; "
                    f"content delete --uri {shell_quote(provider_uri)} >/dev/null 2>&1 || true; "
                    "rm -f "
                    f"{shell_quote(remote_result)} {shell_quote(remote_temporary)} "
                    f"{shell_quote(remote_app_result)} {shell_quote(remote_app_temporary)}"
                ),
                timeout=12,
            )
        except (OSError, RuntimeError) as exc:
            LOGGER.debug("ACBridge privilege cancellation could not be delivered: %s", exc)

    def _parse_privilege_result(
        self,
        payload: str,
        *,
        expected_backend: str,
        expected_request_id: str,
    ) -> ACBridgePrivilegeResult:
        lines = [line.rstrip("\r") for line in str(payload or "").splitlines()]
        if not lines or lines[0] != self.PRIVILEGE_PROTOCOL_HEADER:
            return self._privilege_protocol_error(
                expected_backend,
                expected_request_id,
                "ACBridge returned an unrecognized privilege protocol header.",
            )
        fields: dict[str, str] = {}
        for line in lines[1:]:
            key, separator, value = line.partition("=")
            if not separator or key in fields:
                return self._privilege_protocol_error(
                    expected_backend,
                    expected_request_id,
                    "ACBridge returned malformed or duplicate privilege fields.",
                )
            fields[key] = value
        required = {"request_id", "backend", "state", "permission", "uid", "message_b64"}
        if set(fields) != required:
            return self._privilege_protocol_error(
                expected_backend,
                expected_request_id,
                "ACBridge privilege response fields were incomplete or unexpected.",
            )
        if (
            fields["request_id"] != expected_request_id
            or fields["backend"] != expected_backend
        ):
            return self._privilege_protocol_error(
                expected_backend,
                expected_request_id,
                "ACBridge privilege response did not match the current request.",
            )
        allowed_states = {
            "ready",
            "denied",
            "unavailable",
            "timed_out",
            "error",
            "activity_destroyed",
            "cancelled",
            "invalid_request",
        }
        allowed_permissions = {
            "granted",
            "not_required",
            "denied",
            "unavailable",
            "unknown",
        }
        state = fields["state"]
        permission = fields["permission"]
        if state not in allowed_states or permission not in allowed_permissions:
            return self._privilege_protocol_error(
                expected_backend,
                expected_request_id,
                "ACBridge returned an invalid privilege state.",
            )
        try:
            raw_uid = int(fields["uid"])
        except ValueError:
            return self._privilege_protocol_error(
                expected_backend,
                expected_request_id,
                "ACBridge returned an invalid Android UID.",
            )
        uid = raw_uid if raw_uid >= 0 else None
        try:
            message = base64.b64decode(
                fields["message_b64"],
                validate=True,
            ).decode("utf-8", errors="strict").strip()
        except (binascii.Error, ValueError, UnicodeError):
            return self._privilege_protocol_error(
                expected_backend,
                expected_request_id,
                "ACBridge returned an invalid privilege message.",
            )
        result = ACBridgePrivilegeResult(
            backend=expected_backend,
            state=state,
            permission=permission,
            uid=uid,
            message=message or "ACBridge privilege request finished.",
            request_id=expected_request_id,
        )
        valid_ready = (
            result.ready
            and (
                (expected_backend == "root" and uid == 0)
                or (expected_backend == "standard" and uid is not None)
            )
        )
        if state == "ready" and not valid_ready:
            return self._privilege_protocol_error(
                expected_backend,
                expected_request_id,
                "ACBridge claimed access without the required verified identity.",
            )
        return result

    @staticmethod
    def _privilege_protocol_error(
        backend: str,
        request_id: str,
        message: str,
    ) -> ACBridgePrivilegeResult:
        return ACBridgePrivilegeResult(
            backend=backend,
            state="protocol_error",
            permission="unknown",
            uid=None,
            message=message,
            request_id=request_id,
        )

    def delete_path(
        self,
        android_path: str,
        recursive: bool = True,
        use_root: bool = False,
        timeout: int = 90,
        cancel_event=None,
    ) -> CommandResult:
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_bridge_result("delete-path", self.adb)
        installed, install_message = self.ensure_installed(
            require_current=True,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_bridge_result("delete-path", self.adb)
        if not installed:
            result = self.adb.run_shell(
                "true",
                timeout=5,
                cancel_event=cancel_event,
            )
            result.success = False
            result.exit_code = 1
            result.status = install_message
            result.stderr = install_message
            return result

        root_available = bool(
            use_root and self.adb.root_available(cancel_event=cancel_event)
        )
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_bridge_result("delete-path", self.adb)
        self._prepare_delete(
            use_root=root_available,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_bridge_result("delete-path", self.adb)
        start_result = self._start_delete(
            android_path,
            recursive=recursive,
            use_root=root_available,
            cancel_event=cancel_event,
        )
        if not start_result.success:
            start_result.status = start_result.status or start_result.stderr or "ACBridge delete operation could not be started."
            return start_result

        wait_result = self._wait_for_delete(timeout=timeout, cancel_event=cancel_event)
        output = (wait_result.stdout or wait_result.stderr or "").strip()
        if output.startswith("OPENADB_DELETE_RESULT "):
            output = output.split(" ", 1)[1].strip()
        if output.startswith("OK\t"):
            wait_result.success = True
            wait_result.exit_code = 0
            wait_result.status = output[3:].strip() or f"Deleted through ACBridge: {android_path}"
        elif output.startswith("ERROR\t"):
            wait_result.success = False
            wait_result.exit_code = wait_result.exit_code if wait_result.exit_code not in (None, 0) else 1
            wait_result.status = output[6:].strip() or f"ACBridge could not delete: {android_path}"
            wait_result.stderr = wait_result.status
        elif wait_result.success:
            wait_result.status = output or f"ACBridge delete finished: {android_path}"
        else:
            wait_result.status = output or wait_result.status or f"ACBridge delete timed out for: {android_path}"
        if wait_result.success:
            verify = self.adb.run_shell(
                f"if [ -e {shell_quote(android_path)} ]; then echo exists; exit 1; fi",
                timeout=12,
                cancel_event=cancel_event,
            )
            if not verify.success:
                wait_result.success = False
                wait_result.exit_code = 1
                wait_result.status = (
                    f"{wait_result.status} Android still reports this path after ACBridge delete attempt: {android_path}"
                )
                wait_result.stderr = wait_result.status
        if root_available and wait_result.status:
            wait_result.status += " Root mode: active."
        return wait_result

    def grant_storage_access(
        self,
        android_path: str = "",
        timeout: int = 600,
        cancel_event=None,
    ) -> CommandResult:
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_bridge_result("grant-storage-access", self.adb)
        installed, install_message = self.ensure_installed(
            require_current=True,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_bridge_result("grant-storage-access", self.adb)
        if not installed:
            result = self.adb.run_shell(
                "true",
                timeout=5,
                cancel_event=cancel_event,
            )
            result.success = False
            result.exit_code = 1
            result.status = install_message
            result.stderr = install_message
            return result
        if self._uses_removable_storage_tree(android_path):
            self._restore_storage_tree_picker_if_needed(cancel_event=cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                return _cancelled_bridge_result("grant-storage-access", self.adb)
        prepare_result = self._prepare_storage_grant(cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_bridge_result("grant-storage-access", self.adb)
        if not prepare_result.success:
            prepare_result.status = (
                prepare_result.status
                or prepare_result.stderr
                or "ACBridge could not clear the previous storage-permission result."
            )
            return prepare_result
        start_result = self._start_storage_grant(
            android_path,
            cancel_event=cancel_event,
        )
        if not start_result.success:
            start_result.status = start_result.status or start_result.stderr or "ACBridge storage permission request could not be started."
            return start_result
        wait_result = self._wait_for_delete(timeout=timeout, cancel_event=cancel_event)
        output = (wait_result.stdout or wait_result.stderr or "").strip()
        if output.startswith("OPENADB_DELETE_RESULT "):
            output = output.split(" ", 1)[1].strip()
        if output.startswith("OK\t"):
            wait_result.success = True
            wait_result.exit_code = 0
            wait_result.status = output[3:].strip() or "Android TV storage access was granted."
        elif output.startswith("ERROR\t"):
            wait_result.success = False
            wait_result.exit_code = wait_result.exit_code if wait_result.exit_code not in (None, 0) else 1
            wait_result.status = output[6:].strip() or "Android TV storage access was not granted."
            wait_result.stderr = wait_result.status
        elif wait_result.success:
            wait_result.status = output or "Android TV storage access request finished."
        else:
            wait_result.status = output or wait_result.status or "Android TV storage access request timed out."
        return wait_result

    def load_app_data(
        self,
        apps_by_package: dict[str, tuple[str, str]],
        device_serial: str = "",
        icon_size: int = 96,
        timeout: int = 90,
        need_labels: bool = True,
        need_icons: bool = True,
        need_metadata: bool = True,
        use_root: bool = False,
        progress_callback=None,
        cancel_event=None,
    ) -> ACBridgeResult:
        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        def cancelled_result() -> ACBridgeResult:
            return ACBridgeResult(False, {}, {}, {}, "ACBridge app data loading was cancelled.")

        if cancelled():
            return cancelled_result()
        if not apps_by_package:
            return ACBridgeResult(False, {}, {}, {}, "ACBridge skipped: no packages.")
        if not need_labels and not need_icons and not need_metadata:
            return ACBridgeResult(False, {}, {}, {}, "ACBridge skipped: local cache is complete.")
        self._emit(progress_callback, "Checking ACBridge helper...")
        installed, install_message = self.ensure_installed(cancel_event=cancel_event)
        if cancelled():
            return cancelled_result()
        if not installed:
            return ACBridgeResult(False, {}, {}, {}, install_message)
        self._emit(progress_callback, install_message)
        root_available = bool(
            use_root and self.adb.root_available(cancel_event=cancel_event)
        )
        if cancelled():
            return cancelled_result()
        if use_root and root_available:
            self._emit(progress_callback, "ACBridge root mode is available. Preparing bridge files through su/root.")
        elif use_root:
            self._emit(progress_callback, "ACBridge root mode was requested, but su/root was not granted.")

        started_at = time.monotonic()
        self._emit(progress_callback, "Preparing ACBridge export files on Android...")
        self._prepare_run(
            icon_size,
            need_icons=need_icons,
            package_names=apps_by_package.keys(),
            use_root=root_available,
            cancel_event=cancel_event,
        )
        if cancelled():
            return cancelled_result()
        self._emit(progress_callback, "Starting ACBridge on the phone...")
        start_result = self._start_bridge(
            icon_size,
            need_icons=need_icons,
            use_root=root_available,
            cancel_event=cancel_event,
        )
        if cancelled():
            return cancelled_result()
        if not start_result.success:
            return ACBridgeResult(
                True,
                {},
                {},
                {},
                start_result.status or start_result.stderr or "ACBridge could not be started.",
            )

        self._emit(progress_callback, "Waiting for ACBridge to export app labels and icons...")
        export_state = self._wait_for_export(
            timeout,
            need_icons=need_icons,
            package_count=len(apps_by_package),
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
        if cancelled():
            return cancelled_result()
        if not export_state.data_ready:
            error_text = ""
            if export_state.error_ready and export_state.error_path:
                error_text = self._download_remote_text(
                    export_state.error_path,
                    timeout=10,
                    cancel_event=cancel_event,
                )
            if cancelled():
                return cancelled_result()
            diagnostic = self._acbridge_diagnostic(cancel_event=cancel_event)
            if cancelled():
                return cancelled_result()
            message = "ACBridge did not export app data before timeout."
            if error_text:
                message += f" Device error: {error_text}"
            if diagnostic:
                message += f" Diagnostic: {diagnostic}"
            return ACBridgeResult(True, {}, {}, {}, message)

        local_dir = self._local_temp_dir()
        serial_key = safe_filename(device_serial or self.adb.serial or "device")
        local_data = local_dir / f"{serial_key}_acbridge.txt"
        local_icons_zip = local_dir / f"{serial_key}_icons.zip"

        data_path = export_state.data_path or self.REMOTE_APP_DATA
        icons_path = export_state.icons_path or self.REMOTE_APP_ICONS_ZIP
        data_ok, data_message = self._download_text_file_fast(
            data_path,
            local_data,
            timeout=45,
            use_root=root_available,
            cancel_event=cancel_event,
        )
        if cancelled():
            return cancelled_result()
        if not data_ok:
            return ACBridgeResult(True, {}, {}, {}, data_message or "Unable to pull ACBridge app data.")

        labels = self._parse_labels(local_data, set(apps_by_package)) if need_labels else {}
        metadata = (
            self._download_and_parse_metadata(
                export_state,
                local_dir,
                serial_key,
                set(apps_by_package),
                use_root=root_available,
                cancel_event=cancel_event,
            )
            if need_metadata
            else {}
        )
        if cancelled():
            return cancelled_result()
        icon_versions = dict(apps_by_package)
        for package_name, details in metadata.items():
            current_version_name, current_version_code = icon_versions.get(package_name, ("", ""))
            icon_versions[package_name] = (
                details.get("versionName", "") or current_version_name,
                details.get("versionCode", "") or current_version_code,
            )
        icons: dict[str, Path] = {}
        if need_icons and export_state.icons_ready:
            zip_timeout = max(180, min(420, 60 + len(apps_by_package)))
            zip_ok, _zip_message = self._download_binary_file_fast(
                icons_path,
                local_icons_zip,
                timeout=zip_timeout,
                use_root=root_available,
                cancel_event=cancel_event,
            )
            if cancelled():
                return cancelled_result()
            if zip_ok and local_icons_zip.exists():
                icons = self._import_icons(local_icons_zip, icon_versions, source_key=f"acbridge_{serial_key}")

        if cancelled():
            return cancelled_result()

        duration = time.monotonic() - started_at
        icon_note = "" if icons or not need_icons else " Icon archive was not exported; fallback loader will continue."

        return ACBridgeResult(
            True,
            labels,
            metadata,
            icons,
            (
                f"{install_message} ACBridge fast path loaded {len(labels)} labels, {len(metadata)} metadata rows, "
                f"and {len(icons)} rendered icons "
                f"in {duration:.1f}s. Root mode: {'active' if root_available else 'not used'}.{icon_note}"
            ),
        )

    def _start_bridge(
        self,
        icon_size: int,
        need_icons: bool,
        use_root: bool,
        cancel_event=None,
    ) -> object:
        command = (
            f"am start -n {shell_quote(self.ACTIVITY)} "
            f"--ez showicons {'true' if need_icons else 'false'} "
            "--ez endexit true "
            f"--ei iconsize {max(48, min(192, int(icon_size)))} "
            "--ez appsizes true "
            "--ez legacy false "
            f"--ez rootmode {'true' if use_root else 'false'}"
        )
        if use_root:
            return self.adb.run_root_shell(
                command,
                timeout=20,
                cancel_event=cancel_event,
            )
        return self.adb.run_shell(command, timeout=20, cancel_event=cancel_event)

    def _start_delete(
        self,
        android_path: str,
        recursive: bool,
        use_root: bool,
        cancel_event=None,
    ) -> CommandResult:
        command = (
            f"am start -n {shell_quote(self.ACTIVITY)} "
            "--es operation delete "
            f"--es path {shell_quote(android_path)} "
            f"--ez recursive {'true' if recursive else 'false'} "
            f"--ez rootmode {'true' if use_root else 'false'} "
            "--ez endexit true"
        )
        if use_root:
            return self.adb.run_root_shell(
                command,
                timeout=20,
                cancel_event=cancel_event,
            )
        return self.adb.run_shell(command, timeout=20, cancel_event=cancel_event)

    def _start_storage_grant(self, android_path: str, cancel_event=None) -> CommandResult:
        command = (
            f"am start -n {shell_quote(self.ACTIVITY)} "
            "--es operation grantStorage "
            f"--es path {shell_quote(android_path)} "
            "--ez endexit true"
        )
        return self.adb.run_shell(command, timeout=20, cancel_event=cancel_event)

    @staticmethod
    def _uses_removable_storage_tree(android_path: str) -> bool:
        clean = str(android_path or "").replace("\\", "/").rstrip("/")
        if not clean.startswith("/storage/"):
            return False
        return not (
            clean == "/storage/emulated"
            or clean.startswith("/storage/emulated/")
            or clean == "/storage/self/primary"
            or clean.startswith("/storage/self/primary/")
        )

    def _storage_tree_picker_available(
        self,
        *,
        user_id: str = "current",
        cancel_event=None,
    ) -> bool:
        action = shell_quote(self.STORAGE_TREE_ACTION)
        safe_user = user_id if str(user_id).isdigit() else "current"
        result = self.adb.run_shell(
            f"cmd package resolve-activity --brief --user {safe_user} "
            f"-a {action} 2>/dev/null || true",
            timeout=10,
            cancel_event=cancel_event,
        )
        output = str(result.stdout or "").strip()
        return bool(self.STORAGE_TREE_COMPONENT_RE.search(output))

    def _current_android_user(self, cancel_event=None) -> str:
        result = self.adb.run_shell(
            "am get-current-user",
            timeout=10,
            cancel_event=cancel_event,
        )
        user_id = str(result.stdout or "").strip()
        return user_id if user_id.isdigit() else ""

    def _restore_storage_tree_picker_if_needed(self, cancel_event=None) -> bool:
        """Restore the stock SAF picker when Android TV hid it for this user.

        Some TV images ship DocumentsUI in the system partition but leave it
        uninstalled for the active user.  In that state ACTION_OPEN_DOCUMENT_TREE
        has no handler, so ACBridge can never present Android's real folder grant.
        Platform Tools may safely install that existing system package; this does
        not sideload an APK or grant ACBridge access by itself.
        """

        if self._storage_tree_picker_available(cancel_event=cancel_event):
            return True
        if cancel_event is not None and cancel_event.is_set():
            return False
        user_id = self._current_android_user(cancel_event=cancel_event)
        if not user_id:
            return False
        safe_user = user_id
        packages = self.adb.run_shell(
            f"pm list packages -s -u --user {safe_user}",
            timeout=30,
            cancel_event=cancel_event,
        )
        available_packages = {
            line.split(":", 1)[1].strip()
            for line in str(packages.stdout or "").splitlines()
            if line.startswith("package:") and ":" in line
        }
        for package_name in self.DOCUMENTS_UI_PACKAGES:
            if package_name not in available_packages:
                continue
            if cancel_event is not None and cancel_event.is_set():
                return False
            package = shell_quote(package_name)
            state = self.adb.run_shell(
                f"dumpsys package {package} 2>/dev/null",
                timeout=20,
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                return False
            user_state = re.search(
                rf"(?m)^\s*User\s+{re.escape(safe_user)}:\s*([^\r\n]*)$",
                str(state.stdout or ""),
            )
            if user_state is None or "installed=false" not in user_state.group(1):
                # Do not override an explicitly disabled installed package. Only
                # restore a system DocumentsUI that this user does not have.
                continue
            self.adb.run_shell(
                "cmd package install-existing "
                f"--user {safe_user} --wait {package} >/dev/null 2>&1 || "
                f"pm install-existing --user {safe_user} {package} >/dev/null 2>&1 || true",
                timeout=30,
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                return False
            if self._storage_tree_picker_available(
                user_id=safe_user,
                cancel_event=cancel_event,
            ):
                return True
        return False

    def is_installed(self, cancel_event=None) -> bool:
        result = self.adb.run_shell(
            f"pm path {shell_quote(self.PACKAGE)}",
            timeout=10,
            cancel_event=cancel_event,
        )
        return bool(result.stdout and "package:" in result.stdout)

    def ensure_installed(
        self,
        require_current: bool = False,
        cancel_event=None,
    ) -> tuple[bool, str]:
        with self._installation_guard(cancel_event) as acquired:
            if not acquired:
                return False, "ACBridge setup was cancelled while waiting for another helper update."
            return self._ensure_installed_unlocked(
                require_current=require_current,
                cancel_event=cancel_event,
            )

    def ensure_trusted(self, cancel_event=None) -> tuple[bool, str]:
        """Require the exact bundled helper before a privileged app flow.

        ``versionCode`` is update metadata, not an authenticity boundary.  In
        particular, ACBridge uses an intentionally public development signing
        identity, and Android may already contain a same/newer package that
        OpenADB did not install.  Privileged Root/Shizuku entry points must pin
        the installed ``base.apk`` bytes before they launch that package.
        """

        installed, message = self.ensure_installed(
            require_current=True,
            cancel_event=cancel_event,
        )
        if not installed or (cancel_event is not None and cancel_event.is_set()):
            return installed, message
        trusted, trust_message = self.verify_bundled_apk(
            cancel_event=cancel_event,
        )
        if not trusted:
            return False, trust_message
        return True, message

    def _ensure_installed_unlocked(
        self,
        require_current: bool = False,
        cancel_event=None,
    ) -> tuple[bool, str]:
        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge setup was cancelled before it started."
        probe = self._probe_installed_version(cancel_event=cancel_event)
        if probe.state == "cancelled":
            return False, probe.message or "ACBridge setup was cancelled."
        if probe.state == "query_failed":
            return False, probe.message or "Android could not report the installed ACBridge version."
        installed_version = probe.version_code or 0
        if probe.state == "installed" and installed_version >= self.VERSION_CODE:
            return True, f"ACBridge is already installed (versionCode {installed_version})."

        apk = self.bundled_apk_path()
        if not apk.exists():
            return (
                False,
                f"ACBridge APK was not found at {apk}. Build it with tools/build_acbridge.py or place ACBridge.apk there.",
            )

        result = self.adb.install_apk_with_permissions(
            apk,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge setup was cancelled."
        if not result.success and self._looks_like_signature_mismatch(result.stdout + "\n" + result.stderr + "\n" + result.status):
            if installed_version > 0:
                if require_current:
                    return (
                        False,
                        (
                            f"This ACBridge operation requires bundled versionCode {self.VERSION_CODE}, but Android reports "
                            f"an installed helper with versionCode {installed_version} and a different signature. "
                            "Uninstall com.communism420.acbridge manually, then try again."
                        ),
                    )
                return (
                    True,
                    (
                        f"Using existing ACBridge versionCode {installed_version}. "
                        "Bundled ACBridge could not update it because Android reports a signature mismatch; "
                        "OpenADB did not delete the existing helper."
                    ),
                )
            return (
                False,
                (
                    "ACBridge is installed with a different signature. OpenADB will not delete it automatically; "
                    "uninstall com.communism420.acbridge manually if you want OpenADB to install the bundled helper."
                ),
            )
        if not result.success:
            return False, result.status or result.stderr or "Unable to install ACBridge helper APK."

        installed_probe = self._probe_installed_version(cancel_event=cancel_event)
        if installed_probe.state == "cancelled":
            return False, installed_probe.message or "ACBridge setup was cancelled."
        if installed_probe.state == "query_failed":
            return False, installed_probe.message or "Android could not confirm the installed ACBridge version."
        installed_version = installed_probe.version_code or 0
        if installed_probe.state == "installed" and installed_version >= self.VERSION_CODE:
            return True, f"ACBridge installed from bundled APK (versionCode {installed_version or self.VERSION_CODE})."
        if installed_probe.state == "installed":
            return False, (
                "ACBridge install command finished, but Android reported versionCode "
                f"{installed_version}; required versionCode {self.VERSION_CODE}."
            )
        if require_current:
            return False, (
                "ACBridge install command finished, but Android did not report the required "
                f"versionCode {self.VERSION_CODE}."
            )
        return False, "ACBridge install command finished, but Android does not report the helper package as installed."

    def update_if_outdated(self, cancel_event=None) -> ACBridgeUpdateResult:
        """Install a missing helper or update an older one without downgrading.

        An unreadable package/version state is never treated as absence, so a
        transport failure cannot trigger a blind install. A successfully
        installed or replaced helper must report the exact bundled versionCode
        and match the bundled APK byte-for-byte.
        """

        with self._installation_guard(cancel_event) as acquired:
            if not acquired:
                return self._update_result(
                    "cancelled",
                    message="ACBridge setup was cancelled while another helper operation was running.",
                )
            probe = self._probe_installed_version(cancel_event=cancel_event)
            if probe.state == "cancelled":
                return self._update_result("cancelled", message=probe.message)
            if probe.state == "not_installed":
                previous_version = None
                success_state = "installed"
                failure_state = "install_failed"
            else:
                if probe.state != "installed" or probe.version_code is None:
                    return self._update_result("query_failed", message=probe.message)

                previous_version = probe.version_code
                if previous_version == self.VERSION_CODE:
                    return self._update_result(
                        "current",
                        installed_version_code=previous_version,
                        previous_version_code=previous_version,
                        message=f"ACBridge versionCode {previous_version} is current.",
                    )
                if previous_version > self.VERSION_CODE:
                    return self._update_result(
                        "newer",
                        installed_version_code=previous_version,
                        previous_version_code=previous_version,
                        message=(
                            f"ACBridge versionCode {previous_version} is newer than the bundled "
                            f"versionCode {self.VERSION_CODE}; OpenADB did not downgrade it."
                        ),
                    )
                success_state = "updated"
                failure_state = "update_failed"
            if cancel_event is not None and cancel_event.is_set():
                return self._update_result(
                    "cancelled",
                    previous_version_code=previous_version,
                    message="ACBridge setup was cancelled before installation.",
                )

            apk = self.bundled_apk_path()
            if not apk.is_file():
                return self._update_result(
                    failure_state,
                    installed_version_code=previous_version,
                    previous_version_code=previous_version,
                    message=f"The bundled ACBridge APK was not found at {apk}.",
                )
            if previous_version is None:
                install_result = self.adb.install_apk_with_permissions(
                    apk,
                    cancel_event=cancel_event,
                )
            else:
                install_result = self.adb.run_raw(
                    ["install", "-r", str(apk)],
                    timeout=300,
                    cancel_event=cancel_event,
                )
            if cancel_event is not None and cancel_event.is_set():
                return self._update_result(
                    "cancelled",
                    previous_version_code=previous_version,
                    message="ACBridge setup was cancelled during installation.",
                )
            if not install_result.success:
                detail = self._command_failure_detail(
                    install_result,
                    (
                        "Android rejected the ACBridge installation."
                        if previous_version is None
                        else "Android rejected the ACBridge update."
                    ),
                )
                if self._looks_like_signature_mismatch(
                    "\n".join(
                        (
                            install_result.stdout or "",
                            install_result.stderr or "",
                            install_result.status or "",
                        )
                    )
                ):
                    if previous_version is None:
                        detail = (
                            "Android reports an ACBridge package with a different signature. "
                            "OpenADB did not uninstall it or delete its data; remove it manually "
                            "before installing the bundled helper."
                        )
                    else:
                        detail = (
                            "The installed ACBridge has a different signature. OpenADB did not "
                            "uninstall it or delete its data; remove it manually before installing "
                            "the bundled helper."
                        )
                return self._update_result(
                    failure_state,
                    installed_version_code=previous_version,
                    previous_version_code=previous_version,
                    message=detail,
                    transient=self._looks_like_transient_transport_failure(
                        install_result
                    ),
                )

            installed_probe = self._probe_after_update(cancel_event=cancel_event)
            if installed_probe.state == "cancelled":
                return self._update_result(
                    "cancelled",
                    previous_version_code=previous_version,
                    message=installed_probe.message,
                )
            if (
                installed_probe.state != "installed"
                or installed_probe.version_code != self.VERSION_CODE
            ):
                reported = installed_probe.version_code
                detail = installed_probe.message or (
                    "Android did not report a readable ACBridge version after installation."
                )
                if reported is not None:
                    detail = (
                        f"Android still reports ACBridge versionCode {reported}; expected "
                        f"{self.VERSION_CODE}."
                    )
                return self._update_result(
                    "query_failed" if installed_probe.state == "query_failed" else failure_state,
                    installed_version_code=reported,
                    previous_version_code=previous_version,
                    message=detail,
                )

            verified, verification_message = self.verify_bundled_apk(
                cancel_event=cancel_event
            )
            if cancel_event is not None and cancel_event.is_set():
                return self._update_result(
                    "cancelled",
                    installed_version_code=self.VERSION_CODE,
                    previous_version_code=previous_version,
                    message="ACBridge verification was cancelled.",
                )
            if not verified:
                return self._update_result(
                    "verification_failed",
                    installed_version_code=self.VERSION_CODE,
                    previous_version_code=previous_version,
                    message=verification_message,
                )
            return self._update_result(
                success_state,
                installed_version_code=self.VERSION_CODE,
                previous_version_code=previous_version,
                message=(
                    f"ACBridge was installed automatically (versionCode {self.VERSION_CODE})."
                    if previous_version is None
                    else (
                        f"ACBridge was updated automatically from versionCode {previous_version} "
                        f"to {self.VERSION_CODE}."
                    )
                ),
            )

    def _probe_installed_version(self, cancel_event=None) -> _ACBridgeVersionProbe:
        if cancel_event is not None and cancel_event.is_set():
            return _ACBridgeVersionProbe(
                "cancelled",
                message="ACBridge version check was cancelled.",
            )
        path_result = self.adb.run_shell(
            f"pm path {shell_quote(self.PACKAGE)}",
            timeout=10,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return _ACBridgeVersionProbe(
                "cancelled",
                message="ACBridge version check was cancelled.",
            )
        output_lines = (path_result.stdout or "").splitlines()
        path_matches = [self.PACKAGE_PATH_RE.fullmatch(line) for line in output_lines]
        package_paths = [match.group(1) for match in path_matches if match is not None]
        detail = self._command_failure_detail(
            path_result,
            "Android did not return the ACBridge package path.",
        )
        if not path_result.success:
            # A failed ``pm path`` is ambiguous on several OEM builds, even
            # when it claims that the package is missing. Never install from
            # that claim alone. A path in failed output is contradictory and
            # must also leave the installed package untouched.
            if package_paths:
                return _ACBridgeVersionProbe(
                    "query_failed",
                    message=(
                        "Android returned an ACBridge package path from a failed package "
                        "query; OpenADB left the package untouched."
                    ),
                )
            confirmation = self._confirm_package_absence(
                cancel_event=cancel_event,
            )
            if confirmation.state in {"cancelled", "not_installed"}:
                return confirmation
            return _ACBridgeVersionProbe(
                "query_failed",
                message=(
                    f"Unable to check the installed ACBridge package: {detail} "
                    f"Secondary package check: {confirmation.message}"
                ).strip(),
            )
        if (path_result.stderr or "").strip() or any(
            match is None for match in path_matches
        ):
            return _ACBridgeVersionProbe(
                "query_failed",
                message=(
                    "Android returned an unexpected response to the ACBridge package-path "
                    "query; OpenADB left the package untouched."
                ),
            )
        if not package_paths:
            return _ACBridgeVersionProbe("not_installed")

        version_result = self.adb.run_shell(
            f"dumpsys package {shell_quote(self.PACKAGE)}",
            timeout=15,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return _ACBridgeVersionProbe(
                "cancelled",
                message="ACBridge version check was cancelled.",
            )
        if not version_result.success:
            detail = (
                version_result.status
                or version_result.stderr
                or version_result.stdout
                or "Android rejected the package version query."
            )
            return _ACBridgeVersionProbe(
                "query_failed",
                message=f"Unable to read the installed ACBridge version: {detail}",
            )
        version_codes = {
            int(value)
            for value in re.findall(
                r"(?m)^\s*versionCode=(\d+)\b",
                version_result.stdout or "",
            )
        }
        if len(version_codes) != 1 or next(iter(version_codes), 0) <= 0:
            return _ACBridgeVersionProbe(
                "query_failed",
                message=(
                    "Android reported an absent, invalid, or ambiguous ACBridge versionCode; "
                    "OpenADB did not modify the package."
                ),
            )
        return _ACBridgeVersionProbe("installed", version_code=version_codes.pop())

    def _confirm_package_absence(self, cancel_event=None) -> _ACBridgeVersionProbe:
        """Resolve OEM ``pm path`` exit-1 ambiguity without installing blindly."""

        if cancel_event is not None and cancel_event.is_set():
            return _ACBridgeVersionProbe(
                "cancelled",
                message="ACBridge package check was cancelled.",
            )
        result = self.adb.run_shell(
            f"pm list packages {shell_quote(self.PACKAGE)}",
            timeout=10,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return _ACBridgeVersionProbe(
                "cancelled",
                message="ACBridge package check was cancelled.",
            )
        if not result.success:
            detail = self._command_failure_detail(
                result,
                "Android rejected the secondary package query.",
            )
            return _ACBridgeVersionProbe(
                "query_failed",
                message=detail,
            )
        output_lines = (result.stdout or "").splitlines()
        package_names: set[str] = set()
        malformed_output = False
        for line in output_lines:
            if not line.startswith("package:"):
                malformed_output = True
                break
            package_name = line.removeprefix("package:")
            if self.PACKAGE_NAME_RE.fullmatch(package_name) is None:
                malformed_output = True
                break
            package_names.add(package_name)
        if (result.stderr or "").strip() or malformed_output:
            return _ACBridgeVersionProbe(
                "query_failed",
                message=(
                    "Android returned an unexpected response to the secondary package query; "
                    "OpenADB left the package untouched."
                ),
            )
        if self.PACKAGE in package_names:
            return _ACBridgeVersionProbe(
                "query_failed",
                message=(
                    "Android lists ACBridge as installed but did not expose one readable "
                    "package path; OpenADB left the package untouched."
                ),
            )
        return _ACBridgeVersionProbe("not_installed")

    def _probe_after_update(self, cancel_event=None) -> _ACBridgeVersionProbe:
        """Allow PackageManager a short bounded window to publish an installation."""

        probe = _ACBridgeVersionProbe("query_failed")
        for attempt in range(3):
            probe = self._probe_installed_version(cancel_event=cancel_event)
            if probe.state not in {"not_installed", "query_failed"} or attempt == 2:
                return probe
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    return _ACBridgeVersionProbe(
                        "cancelled",
                        message="ACBridge verification was cancelled.",
                    )
                time.sleep(0.02)
        return probe

    @staticmethod
    def _command_failure_detail(result: CommandResult, fallback: str) -> str:
        values = (
            str(result.stderr or "").strip(),
            str(result.stdout or "").strip(),
            str(result.status or "").strip(),
        )
        return next((value for value in values if value), fallback)

    @staticmethod
    def _looks_like_transient_transport_failure(result: CommandResult) -> bool:
        error_type = str(result.error_type or "").casefold()
        text = "\n".join(
            (
                str(result.status or ""),
                str(result.stderr or ""),
                str(result.stdout or ""),
            )
        ).casefold()
        markers = (
            "closed",
            "connection reset",
            "device offline",
            "device unauthorized",
            "more than one device",
            "no devices/emulators",
            "protocol fault",
            "timeout",
            "timed out",
            "transport",
        )
        return any(marker in error_type or marker in text for marker in markers)

    @contextmanager
    def _installation_guard(self, cancel_event=None):
        acquired = False
        try:
            while not acquired:
                if cancel_event is not None and cancel_event.is_set():
                    yield False
                    return
                acquired = self._INSTALL_LOCK.acquire(timeout=0.1)
            yield True
        finally:
            if acquired:
                self._INSTALL_LOCK.release()

    def _update_result(
        self,
        state: str,
        *,
        installed_version_code: int | None = None,
        previous_version_code: int | None = None,
        message: str = "",
        transient: bool = False,
    ) -> ACBridgeUpdateResult:
        return ACBridgeUpdateResult(
            state=state,
            bundled_version_code=self.VERSION_CODE,
            installed_version_code=installed_version_code,
            previous_version_code=previous_version_code,
            message=message,
            transient=transient,
        )

    def bundled_apk_path(self) -> Path:
        return package_root() / "resources" / "acbridge" / self.APK_FILENAME

    def verify_bundled_apk(self, cancel_event=None) -> tuple[bool, str]:
        """Fail closed unless Android is running this exact bundled helper APK.

        ACBridge development builds use a publicly known debug key, so a
        certificate or versionCode check alone is not a sufficient trust
        boundary for privileged Shizuku execution. Android retains the signed
        base APK byte-for-byte and stores optimized code separately, which lets
        OpenADB pin the exact bundled artifact without depending on apksigner.
        """

        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge verification was cancelled."
        apk = self.bundled_apk_path()
        if not apk.is_file():
            return False, f"The bundled ACBridge APK was not found at {apk}."
        expected_size = apk.stat().st_size
        if expected_size <= 0 or expected_size > self.MAX_TRUSTED_APK_BYTES:
            return False, "The bundled ACBridge APK has an invalid size."

        path_result = self.adb.run_shell(
            f"pm path {shell_quote(self.PACKAGE)}",
            timeout=10,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge verification was cancelled."
        if not path_result.success:
            detail = path_result.status or path_result.stderr or "package path query failed"
            return False, f"Unable to verify the installed ACBridge package: {detail}"
        package_paths = [
            line.removeprefix("package:").strip()
            for line in (path_result.stdout or "").splitlines()
            if line.strip().startswith("package:")
        ]
        base_paths = [path for path in package_paths if path.rsplit("/", 1)[-1] == "base.apk"]
        if len(package_paths) != 1 or len(base_paths) != 1:
            return False, (
                "Unable to identify one monolithic installed ACBridge base.apk. "
                "Uninstall com.communism420.acbridge manually, then let OpenADB install its bundled helper."
            )
        base_path = base_paths[0]
        if (
            not re.fullmatch(r"/[A-Za-z0-9._~+/=-]+", base_path)
            or ".." in base_path.split("/")
        ):
            return False, "Android returned an unsafe ACBridge package path; privileged access was blocked."

        size_result = self.adb.run_raw(
            ["shell", "stat", "-c", "%s", base_path],
            timeout=10,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge verification was cancelled."
        remote_size_text = (size_result.stdout or "").strip()
        if not size_result.success or not remote_size_text.isdigit():
            return False, "Android could not report the installed ACBridge APK size; privileged access was blocked."
        if int(remote_size_text) != expected_size:
            return False, (
                "The installed ACBridge helper does not match this OpenADB build. "
                "Uninstall com.communism420.acbridge manually, then try again."
            )

        read_result, installed_bytes = self.adb.run_raw_binary_output(
            ["exec-out", "cat", base_path],
            timeout=30,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge verification was cancelled."
        if not read_result.success or len(installed_bytes) != expected_size:
            detail = read_result.status or read_result.stderr or "installed APK read failed"
            return False, f"Unable to verify the installed ACBridge APK: {detail}"
        expected_digest = hashlib.sha256(apk.read_bytes()).digest()
        installed_digest = hashlib.sha256(installed_bytes).digest()
        if not hmac.compare_digest(installed_digest, expected_digest):
            return False, (
                "The installed ACBridge helper is not the exact helper bundled with this OpenADB build. "
                "Privileged access was blocked; uninstall com.communism420.acbridge manually, then try again."
            )
        return True, "The installed ACBridge helper matches the bundled APK."

    def installed_version_code(self, cancel_event=None) -> int:
        result = self.adb.run_shell(
            f"dumpsys package {shell_quote(self.PACKAGE)}",
            timeout=15,
            cancel_event=cancel_event,
        )
        output = result.stdout or ""
        match = re.search(r"versionCode=(\d+)", output)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    def _prepare_run(
        self,
        icon_size: int,
        need_icons: bool,
        package_names,
        use_root: bool = False,
        cancel_event=None,
    ) -> None:
        settings_text = "\n".join(
            [
                f"showicons={'true' if need_icons else 'false'}",
                "endexit=true",
                "iconscache=true",
                "backup=false",
                "appsizes=true",
                f"rootmode={'true' if use_root else 'false'}",
                f"iconsize={max(48, min(192, int(icon_size)))}",
            ]
        ) + "\n"
        commands = [
            f"mkdir -p {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)}",
            (
                f"printf %s {shell_quote(settings_text)} > {shell_quote(self.REMOTE_SETTINGS)}; "
                f"printf %s {shell_quote(settings_text)} > {shell_quote(self.REMOTE_APP_SETTINGS)}"
            ),
            (
                f"rm -f {shell_quote(self.REMOTE_DATA)} {shell_quote(self.REMOTE_METADATA)} "
                f"{shell_quote(self.REMOTE_ICONS_ZIP)} "
                f"{shell_quote(self.REMOTE_ERROR)} {shell_quote(self.REMOTE_PROGRESS)} "
                f"{shell_quote(self.REMOTE_APP_DATA)} {shell_quote(self.REMOTE_APP_METADATA)} "
                f"{shell_quote(self.REMOTE_APP_ICONS_ZIP)} "
                f"{shell_quote(self.REMOTE_APP_ERROR)} {shell_quote(self.REMOTE_APP_PROGRESS)}"
            ),
            f"pm grant {shell_quote(self.PACKAGE)} android.permission.WRITE_EXTERNAL_STORAGE >/dev/null 2>&1 || true",
            f"pm grant {shell_quote(self.PACKAGE)} android.permission.READ_EXTERNAL_STORAGE >/dev/null 2>&1 || true",
            f"appops set {shell_quote(self.PACKAGE)} android:legacy_storage allow >/dev/null 2>&1 || true",
            f"appops set {shell_quote(self.PACKAGE)} MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true",
            f"appops set --uid {shell_quote(self.PACKAGE)} MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true",
            f"chmod -R 0777 {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)} >/dev/null 2>&1 || true",
        ]
        command = "; ".join(commands)
        if use_root:
            self.adb.run_root_shell(
                command,
                timeout=30,
                cancel_event=cancel_event,
            )
        else:
            self.adb.run_shell(command, timeout=30, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return
        self._write_package_request(
            package_names,
            use_root=use_root,
            cancel_event=cancel_event,
        )

    def _write_package_request(
        self,
        package_names,
        use_root: bool = False,
        cancel_event=None,
    ) -> None:
        local_dir = self._local_temp_dir()
        request_path = local_dir / "packages.txt"
        packages = sorted(str(package) for package in package_names if package)
        try:
            request_path.write_text("\n".join(packages) + "\n", encoding="utf-8")
        except OSError:
            return
        if cancel_event is not None and cancel_event.is_set():
            return
        self.adb.push(
            request_path,
            self.REMOTE_REQUEST,
            timeout=30,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return
        self.adb.push(
            request_path,
            self.REMOTE_APP_REQUEST,
            timeout=30,
            cancel_event=cancel_event,
        )
        if use_root:
            self.adb.run_root_shell(
                (
                    f"chmod 0666 {shell_quote(self.REMOTE_REQUEST)} {shell_quote(self.REMOTE_APP_REQUEST)} "
                    ">/dev/null 2>&1 || true"
                ),
                timeout=10,
                cancel_event=cancel_event,
            )

    def _prepare_delete(self, use_root: bool = False, cancel_event=None) -> None:
        commands = [
            f"mkdir -p {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)}",
            f"rm -f {shell_quote(self.REMOTE_DELETE_RESULT)} {shell_quote(self.REMOTE_APP_DELETE_RESULT)}",
            f"pm grant {shell_quote(self.PACKAGE)} android.permission.WRITE_EXTERNAL_STORAGE >/dev/null 2>&1 || true",
            f"pm grant {shell_quote(self.PACKAGE)} android.permission.READ_EXTERNAL_STORAGE >/dev/null 2>&1 || true",
            f"appops set {shell_quote(self.PACKAGE)} android:legacy_storage allow >/dev/null 2>&1 || true",
            f"appops set {shell_quote(self.PACKAGE)} MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true",
            f"appops set --uid {shell_quote(self.PACKAGE)} MANAGE_EXTERNAL_STORAGE allow >/dev/null 2>&1 || true",
            f"chmod -R 0777 {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)} >/dev/null 2>&1 || true",
        ]
        command = "; ".join(commands)
        if use_root:
            self.adb.run_root_shell(
                command,
                timeout=30,
                cancel_event=cancel_event,
            )
        else:
            self.adb.run_shell(command, timeout=30, cancel_event=cancel_event)

    def _prepare_storage_grant(self, cancel_event=None) -> CommandResult:
        """Clear grant results without silently approving broad storage access.

        The interactive storage flow must decide which Android storage backend is
        usable before P2P publishes READY.  In particular, forcing
        MANAGE_EXTERNAL_STORAGE here makes Environment.isExternalStorageManager()
        true even on TV firmware where raw writes to removable storage still fail,
        which used to bypass the only user-visible permission step.
        """
        commands = [
            f"mkdir -p {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)}",
            f"rm -f {shell_quote(self.REMOTE_DELETE_RESULT)} {shell_quote(self.REMOTE_APP_DELETE_RESULT)}",
            f"chmod -R 0777 {shell_quote(self.REMOTE_DIR)} {shell_quote(self.REMOTE_APP_DIR)} >/dev/null 2>&1 || true",
            (
                f"if [ -e {shell_quote(self.REMOTE_DELETE_RESULT)} ] || "
                f"[ -e {shell_quote(self.REMOTE_APP_DELETE_RESULT)} ]; then "
                "echo 'ACBridge could not clear a stale storage grant result' >&2; exit 1; fi"
            ),
        ]
        return self.adb.run_shell(
            "; ".join(commands),
            timeout=30,
            cancel_event=cancel_event,
        )

    def _wait_for_export(
        self,
        timeout: int,
        need_icons: bool,
        package_count: int,
        progress_callback=None,
        cancel_event=None,
    ) -> ACBridgeExportState:
        started = time.monotonic()
        timeout = max(5, int(timeout))
        script = (
            f"data1={shell_quote(self.REMOTE_DATA)}; data2={shell_quote(self.REMOTE_APP_DATA)}; "
            f"icons1={shell_quote(self.REMOTE_ICONS_ZIP)}; icons2={shell_quote(self.REMOTE_APP_ICONS_ZIP)}; "
            f"error1={shell_quote(self.REMOTE_ERROR)}; error2={shell_quote(self.REMOTE_APP_ERROR)}; "
            f"progress1={shell_quote(self.REMOTE_PROGRESS)}; progress2={shell_quote(self.REMOTE_APP_PROGRESS)}; "
            f"need_icons={'1' if need_icons else '0'}; "
            f"timeout={timeout}; i=0; data_seen=0; icons_seen=0; error_seen=0; "
            "while [ \"$i\" -lt \"$timeout\" ]; do "
            "data_path=''; icons_path=''; error_path=''; "
            "[ -s \"$data1\" ] && data_path=\"$data1\"; "
            "[ -z \"$data_path\" ] && [ -s \"$data2\" ] && data_path=\"$data2\"; "
            "[ -s \"$icons1\" ] && icons_path=\"$icons1\"; "
            "[ -z \"$icons_path\" ] && [ -s \"$icons2\" ] && icons_path=\"$icons2\"; "
            "[ -s \"$error1\" ] && error_path=\"$error1\"; "
            "[ -z \"$error_path\" ] && [ -s \"$error2\" ] && error_path=\"$error2\"; "
            "[ -n \"$data_path\" ] && data_seen=1 || data_seen=0; "
            "[ -n \"$icons_path\" ] && icons_seen=1 || icons_seen=0; "
            "[ -n \"$error_path\" ] && error_seen=1 || error_seen=0; "
            "progress=''; [ -s \"$progress1\" ] && progress=$(cat \"$progress1\" 2>/dev/null | tr '\\n' ' '); "
            "[ -z \"$progress\" ] && [ -s \"$progress2\" ] && progress=$(cat \"$progress2\" 2>/dev/null | tr '\\n' ' '); "
            "echo OPENADB_PROGRESS data=$data_seen icons_ready=$icons_seen error=$error_seen $progress; "
            "if [ \"$error_seen\" = 1 ]; then "
            "echo OPENADB_EXPORT data=$data_seen icons=$icons_seen error=$error_seen data_path=$data_path icons_path=$icons_path error_path=$error_path; exit 1; "
            "fi; "
            "if [ \"$data_seen\" = 1 ]; then "
            "if [ \"$need_icons\" != 1 ] || [ \"$icons_seen\" = 1 ]; then "
            "echo OPENADB_EXPORT data=$data_seen icons=$icons_seen error=$error_seen data_path=$data_path icons_path=$icons_path error_path=$error_path; exit 0; "
            "fi; "
            "fi; "
            "i=$((i + 1)); sleep 1; "
            "done; "
            "echo OPENADB_EXPORT data=$data_seen icons=$icons_seen error=$error_seen data_path=$data_path icons_path=$icons_path error_path=$error_path; exit 1"
        )
        last_progress = ""

        def on_output(channel: str, text: str) -> None:
            nonlocal last_progress
            for line in (text or "").splitlines():
                if not line.startswith("OPENADB_PROGRESS"):
                    continue
                fields = self._key_value_fields(line)
                if not {"labels", "icons", "total", "stage"}.issubset(fields):
                    continue
                labels = fields.get("labels", "0")
                icons = fields.get("icons", "0")
                total = fields.get("total", str(package_count))
                stage = fields.get("stage", "waiting")
                message = f"ACBRIDGE_PROGRESS labels={labels} icons={icons} total={total} stage={stage}"
                if message != last_progress:
                    last_progress = message
                    self._emit(progress_callback, message)

        result = self.adb.run_raw_streaming(
            ["shell", script],
            timeout=timeout + 8,
            output_callback=on_output,
            cancel_event=cancel_event,
        )
        output = result.stdout or result.stderr or ""
        export_line = self._last_prefixed_line(output, "OPENADB_EXPORT")
        export_fields = self._key_value_fields(export_line)
        data_ready = export_fields.get("data") == "1"
        icons_ready = export_fields.get("icons") == "1"
        error_ready = export_fields.get("error") == "1"
        data_path = export_fields.get("data_path", "")
        icons_path = export_fields.get("icons_path", "")
        error_path = export_fields.get("error_path", "")
        return ACBridgeExportState(data_ready, icons_ready, error_ready, time.monotonic() - started, data_path, icons_path, error_path, output)

    def _wait_for_delete(self, timeout: int, cancel_event=None) -> CommandResult:
        timeout = max(10, int(timeout))
        script = (
            f"result1={shell_quote(self.REMOTE_DELETE_RESULT)}; result2={shell_quote(self.REMOTE_APP_DELETE_RESULT)}; "
            f"timeout={timeout}; i=0; "
            "while [ \"$i\" -lt \"$timeout\" ]; do "
            "result_path=''; "
            "[ -s \"$result1\" ] && result_path=\"$result1\"; "
            "[ -z \"$result_path\" ] && [ -s \"$result2\" ] && result_path=\"$result2\"; "
            "if [ -n \"$result_path\" ]; then "
            "printf 'OPENADB_DELETE_RESULT '; cat \"$result_path\" 2>/dev/null; exit 0; "
            "fi; "
            "i=$((i + 1)); sleep 1; "
            "done; "
            "echo 'ERROR\tACBridge delete result was not produced before timeout.'; exit 1"
        )
        return self.adb.run_shell(
            script,
            timeout=timeout + 8,
            cancel_event=cancel_event,
        )

    def _last_prefixed_line(self, output: str, prefix: str) -> str:
        for line in reversed((output or "").splitlines()):
            if line.startswith(prefix):
                return line
        return ""

    def _key_value_fields(self, output: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", output or ""):
            fields[match.group(1)] = match.group(2)
        return fields

    def _download_and_parse_metadata(
        self,
        export_state: ACBridgeExportState,
        local_dir: Path,
        serial_key: str,
        wanted: set[str],
        use_root: bool = False,
        cancel_event=None,
    ) -> dict[str, dict[str, str]]:
        local_metadata = local_dir / f"{serial_key}_metadata.tsv"
        candidates = (
            [self.REMOTE_METADATA, self.REMOTE_APP_METADATA]
            if export_state.data_path == self.REMOTE_DATA
            else [self.REMOTE_APP_METADATA, self.REMOTE_METADATA]
        )
        for remote_path in candidates:
            if cancel_event is not None and cancel_event.is_set():
                return {}
            ok, _message = self._download_text_file_fast(
                remote_path,
                local_metadata,
                timeout=30,
                use_root=use_root,
                cancel_event=cancel_event,
            )
            if ok and local_metadata.exists():
                parsed = self._parse_metadata(local_metadata, wanted)
                if parsed:
                    return parsed
        return {}

    def _download_text_file_fast(
        self,
        remote_path: str,
        local_path: Path,
        timeout: int,
        use_root: bool = False,
        cancel_event=None,
    ) -> tuple[bool, str]:
        result, data = self.adb.read_remote_file(
            remote_path,
            timeout=timeout,
            use_root=use_root,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge export download was cancelled."
        if result.success and data:
            try:
                local_path.write_bytes(data)
                return True, ""
            except OSError as exc:
                return False, str(exc)

        fallback = self.adb.pull(
            remote_path,
            local_path,
            timeout=timeout,
            cancel_event=cancel_event,
        )
        if fallback.success and local_path.exists():
            return True, ""
        return False, fallback.status or fallback.stderr or result.status or "Unable to download ACBridge text export."

    def _download_binary_file_fast(
        self,
        remote_path: str,
        local_path: Path,
        timeout: int,
        use_root: bool = False,
        cancel_event=None,
    ) -> tuple[bool, str]:
        if use_root:
            result = self.adb.pull_file_streaming_to_file(
                remote_path,
                local_path,
                timeout=timeout,
                use_root=True,
                cancel_event=cancel_event,
            )
        else:
            result = self.adb.pull(
                remote_path,
                local_path,
                timeout=timeout,
                cancel_event=cancel_event,
            )
        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge export download was cancelled."
        if result.success and local_path.exists():
            return True, ""
        fallback, data = self.adb.read_remote_file(
            remote_path,
            timeout=timeout,
            use_root=use_root,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            return False, "ACBridge export download was cancelled."
        if fallback.success and data:
            try:
                local_path.write_bytes(data)
                return True, ""
            except OSError as exc:
                return False, str(exc)
        return False, result.status or result.stderr or fallback.status or "Unable to download ACBridge binary export."

    def _download_remote_text(self, remote_path: str, timeout: int, cancel_event=None) -> str:
        result, data = self.adb.read_remote_file(
            remote_path,
            timeout=timeout,
            cancel_event=cancel_event,
        )
        if result.success and data:
            return data.decode("utf-8", "replace").strip()
        return ""

    def _acbridge_diagnostic(self, cancel_event=None) -> str:
        result = self.adb.run_shell(
            (
                f"echo package:; dumpsys package {shell_quote(self.PACKAGE)} | grep -m 1 versionCode; "
                f"echo files:; ls -l {shell_quote(self.REMOTE_APP_DIR)} {shell_quote(self.REMOTE_DIR)} 2>/dev/null; "
                f"echo progress:; cat {shell_quote(self.REMOTE_PROGRESS)} {shell_quote(self.REMOTE_APP_PROGRESS)} 2>/dev/null; "
                f"echo crashes:; logcat -d -t 80 2>/dev/null | grep -i {shell_quote(self.PACKAGE)} | tail -n 20"
            ),
            timeout=20,
            cancel_event=cancel_event,
        )
        return " ".join((result.stdout or result.stderr or "").split())[:700]

    def _emit(self, progress_callback, message: str) -> None:
        if progress_callback:
            progress_callback.emit(message)

    def _parse_labels(self, path: Path, wanted: set[str]) -> dict[str, str]:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            return {}
        labels: dict[str, str] = {}
        for chunk in text.split("|"):
            if self.LABEL_SEPARATOR not in chunk:
                continue
            package_name, label = chunk.split(self.LABEL_SEPARATOR, 1)
            package_name = package_name.strip()
            label = " ".join(label.strip().split())
            if package_name in wanted and label and label != package_name:
                labels[package_name] = label
        return labels

    def _parse_metadata(self, path: Path, wanted: set[str]) -> dict[str, dict[str, str]]:
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError:
            return {}
        metadata: dict[str, dict[str, str]] = {}
        for raw_line in text.splitlines():
            parts = raw_line.split("\t")
            if len(parts) < 3:
                continue
            package_name = parts[0].strip()
            if package_name not in wanted:
                continue
            metadata[package_name] = {
                "versionName": parts[1].strip(),
                "versionCode": parts[2].strip(),
            }
            if len(parts) >= 4:
                metadata[package_name]["sizeBytes"] = parts[3].strip()
        return metadata
    def _import_icons(
        self,
        icons_zip: Path,
        apps_by_package: dict[str, tuple[str, str]],
        source_key: str = "",
    ) -> dict[str, Path]:
        icons: dict[str, Path] = {}
        if self.icon_extractor is None:
            return icons
        try:
            with zipfile.ZipFile(icons_zip) as archive:
                tasks: list[tuple[str, str, str, bytes]] = []
                for info in archive.infolist():
                    base_name = Path(info.filename).name
                    if not base_name.lower().endswith(".png"):
                        continue
                    package_name = base_name[:-4]
                    if package_name not in apps_by_package or not self._looks_like_package(package_name):
                        continue
                    version_name, version_code = apps_by_package[package_name]
                    tasks.append((package_name, version_name, version_code, archive.read(info)))
        except (OSError, zipfile.BadZipFile):
            return icons

        return self.icon_extractor.import_pre_rendered_icon_batch(
            [(package_name, data, version_name, version_code, source_key) for package_name, version_name, version_code, data in tasks]
        )

    def _looks_like_package(self, value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", value))

    def _looks_like_signature_mismatch(self, text: str) -> bool:
        lowered = (text or "").lower()
        return "update_incompatible" in lowered or "signatures do not match" in lowered or "inconsistent certificates" in lowered


def _cancelled_bridge_result(operation: str, adb: ADBClient) -> CommandResult:
    now = datetime.now()
    context = getattr(adb, "device_context", None)
    return CommandResult(
        command=["acbridge", operation],
        exit_code=None,
        stdout="",
        stderr="",
        duration=0.0,
        started_at=now,
        finished_at=now,
        success=False,
        status="Cancelled by user",
        error_type="cancelled",
        device_serial=context.serial if context is not None else "",
        device_generation=context.generation if context is not None else None,
        logs_folder=str(context.logs_path) if context is not None else "",
    )
