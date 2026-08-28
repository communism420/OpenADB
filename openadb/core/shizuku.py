from __future__ import annotations

import base64
import binascii
import re
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, ClassVar

from openadb.core.acbridge import ACBridgeClient
from openadb.core.path_utils import shell_quote
from openadb.models.command_result import CommandResult

SHIZUKU_MANAGER_PACKAGE = "moe.shizuku.privileged.api"
SUI_MANAGER_PACKAGE = "rikka.sui"
SHIZUKU_MANAGER_PACKAGES = (SHIZUKU_MANAGER_PACKAGE, SUI_MANAGER_PACKAGE)
SHIZUKU_ACTIVITY = (
    f"{ACBridgeClient.PACKAGE}/{ACBridgeClient.COMPONENT_PACKAGE}.ShizukuActivity"
)
SHIZUKU_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 128 * 1024
MAX_ARGUMENTS = 32
MAX_DESKTOP_OUTPUT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ShizukuState:
    """Current device-side Shizuku state for one captured ADB transport."""

    state: str = "unknown"
    installed: bool = False
    running: bool = False
    permission: str = "unknown"
    uid: int | None = None
    mode: str = "unavailable"
    api_version: int | None = None
    message: str = "Shizuku status has not been checked."

    @property
    def ready(self) -> bool:
        return (
            self.state == "ready"
            and self.running
            and self.permission == "granted"
            and self.uid in {0, 2000}
        )

    @property
    def root(self) -> bool:
        return self.ready and self.uid == 0

    @property
    def shell(self) -> bool:
        return self.ready and self.uid == 2000

    @property
    def display_name(self) -> str:
        if self.root:
            return "Shizuku root (UID 0)"
        if self.shell:
            return "Shizuku shell (UID 2000)"
        return "Shizuku unavailable"


ShizukuDeviceIdentity = tuple[str, str, int | None, int]


@dataclass(frozen=True, slots=True)
class ShizukuExecutionSession:
    """One prepared Shizuku identity snapshot for a bounded operation.

    A session never stores command text.  It only proves that ACBridge was
    trusted and Shizuku reported the expected UID when the operation was
    prepared.  The Android UserService still verifies ``expected_uid`` before
    starting every command and the desktop validates it again in the result.
    """

    state: ShizukuState
    expected_uid: int | None
    device_identity: ShizukuDeviceIdentity
    _client: ShizukuClient = field(repr=False, compare=False)

    @property
    def ready(self) -> bool:
        return bool(
            self.state.ready
            and self.expected_uid in {0, 2000}
            and self.state.uid == self.expected_uid
        )

    def execute_shell(
        self,
        command: str,
        *,
        timeout: float | None = 120,
        cancel_event=None,
    ) -> CommandResult:
        return self._client._execute_prepared_shell(
            self,
            command,
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def execute_argv(
        self,
        argv: Iterable[str],
        *,
        timeout: float | None = 120,
        cancel_event=None,
    ) -> CommandResult:
        return self._client._execute_prepared_argv(
            self,
            argv,
            timeout=timeout,
            cancel_event=cancel_event,
        )


class ShizukuClient:
    """Desktop control plane for ACBridge's official Shizuku UserService.

    ADB remains the authenticated transport between the PC and Android. Raw
    command text is streamed into a request-scoped file and is never placed in
    Activity arguments or the normal command log. ACBridge can then execute the
    request through a Shizuku UserService after the user grants permission.
    """

    STATUS_PREFIX = "OPENADB_SHIZUKU_STATUS"
    RESULT_PREFIX = "OPENADB_SHIZUKU_RESULT"
    REQUEST_PREFIX = "OPENADB_SHIZUKU_REQUEST"
    REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
    # ACBridge exposes one request-scoped Shizuku UserService.  Transport IDs
    # and device generations change on reconnect, and USB/Wi-Fi aliases cannot
    # always be proven to represent different Android installations.  A single
    # process gate therefore prevents an old, cancelling request from racing a
    # new request after reconnect.  OpenADB has one active-device UI, so this
    # deliberately conservative serialization does not reduce useful parallelism.
    _USER_SERVICE_GATE: ClassVar[threading.RLock] = threading.RLock()

    def __init__(
        self,
        adb,
        settings,
        *,
        temp_folder: str | Path | None = None,
    ) -> None:
        self.adb = adb
        self.settings = settings
        self.bridge = ACBridgeClient(
            adb,
            settings,
            temp_folder=temp_folder,
        )

    def check_status(
        self,
        *,
        timeout: int = 25,
        cancel_event=None,
    ) -> ShizukuState:
        with self._device_operation_guard(cancel_event) as acquired:
            if not acquired:
                return ShizukuState(
                    state="cancelled",
                    message="Shizuku check was cancelled while waiting for another operation.",
                )
            return self._status_operation(
                "status",
                timeout=max(10, min(int(timeout), 90)),
                cancel_event=cancel_event,
            )

    def request_permission(
        self,
        *,
        timeout: int = 180,
        cancel_event=None,
    ) -> ShizukuState:
        """Request Shizuku access through its real on-device permission UI."""

        with self._device_operation_guard(cancel_event) as acquired:
            if not acquired:
                return ShizukuState(
                    state="cancelled",
                    message=(
                        "Shizuku permission request was cancelled while waiting "
                        "for another operation."
                    ),
                )
            installed, message = self._ensure_trusted_bridge(
                cancel_event=cancel_event,
            )
            if not installed:
                return ShizukuState(
                    state=(
                        "cancelled"
                        if cancel_event is not None and cancel_event.is_set()
                        else "bridge_unavailable"
                    ),
                    message=message,
                )
            bounded_timeout = max(30, min(int(timeout), 600))
            host = self.bridge.start_permission_host(
                "shizuku",
                # ShizukuActivity owns the decision timeout and closes this
                # request token from its terminal callback. This longer value
                # is only an orphan guard if desktop cleanup is unavailable.
                timeout=min(900, bounded_timeout + 90),
                cancel_event=cancel_event,
                bridge_is_current=True,
            )
            if not host.started:
                return ShizukuState(
                    state=(
                        "cancelled"
                        if cancel_event is not None and cancel_event.is_set()
                        else "permission_failed"
                    ),
                    message=host.message,
                )
            try:
                return self._status_operation(
                    "requestPermission",
                    timeout=bounded_timeout,
                    cancel_event=cancel_event,
                    bridge_is_trusted=True,
                    permission_host_request_id=host.request_id,
                )
            finally:
                self.bridge.dismiss_permission_host(host.request_id)

    def request_permission_then_check(
        self,
        *,
        request_timeout: int = 180,
        check_timeout: int = 25,
        cancel_event=None,
    ) -> ShizukuState:
        """Request permission and verify the resulting state as one operation.

        Holding the process-wide UserService gate across both Android activities
        prevents another Shizuku command from being inserted between the prompt
        and the verification.  ACBridge trust is also checked once for the
        complete handshake.
        """

        with self._device_operation_guard(cancel_event) as acquired:
            if not acquired:
                return ShizukuState(
                    state="cancelled",
                    message=(
                        "Shizuku permission setup was cancelled while waiting "
                        "for another operation."
                    ),
                )
            installed, message = self._ensure_trusted_bridge(
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                return ShizukuState(
                    state="cancelled",
                    message="Shizuku permission setup was cancelled.",
                )
            if not installed:
                return ShizukuState(
                    state="bridge_unavailable",
                    message=message,
                )

            host = self.bridge.start_permission_host(
                "shizuku",
                # The foreground host ends at the terminal permission callback;
                # the following status verification is intentionally passive.
                timeout=min(
                    900,
                    max(30, min(int(request_timeout), 600)) + 90,
                ),
                cancel_event=cancel_event,
                bridge_is_current=True,
            )
            if not host.started:
                return ShizukuState(
                    state=(
                        "cancelled"
                        if cancel_event is not None and cancel_event.is_set()
                        else "permission_failed"
                    ),
                    message=host.message,
                )
            host_closed = False
            try:
                # The Android implementation checks the existing package grant
                # before invoking Shizuku.requestPermission(), so this direct
                # foreground request is idempotent when permission already exists.
                requested = self._status_operation(
                    "requestPermission",
                    timeout=max(30, min(int(request_timeout), 600)),
                    cancel_event=cancel_event,
                    bridge_is_trusted=True,
                    permission_host_request_id=host.request_id,
                )
                # The permission Activity publishes its terminal result before
                # closing the token-scoped foreground task. Wait for the closed
                # acknowledgement before launching the passive status Activity;
                # otherwise Android can attach it to a task still being removed.
                host_closed = self.bridge.dismiss_permission_host(host.request_id)
                if requested.state == "cancelled" or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    return requested
                if not host_closed:
                    # The authenticated terminal result remains authoritative.
                    # Avoid starting another Activity while task removal could
                    # still be in progress; the finally block retries cleanup.
                    return requested
                return self._status_operation(
                    "status",
                    timeout=max(10, min(int(check_timeout), 90)),
                    cancel_event=cancel_event,
                    bridge_is_trusted=True,
                )
            finally:
                if not host_closed:
                    self.bridge.dismiss_permission_host(host.request_id)

    def prepare_session(
        self,
        *,
        expected_uid: int | None = None,
        timeout: int = 25,
        cancel_event=None,
    ) -> ShizukuExecutionSession:
        """Prepare one immutable trusted-state snapshot for subsequent execution.

        Passing ``expected_uid`` binds the session to an identity the user has
        already reviewed.  Omitting it accepts the freshly reported ready UID.
        Preparing performs the expensive ACBridge trust check once; commands
        executed through the returned session do not repeat it.
        """

        with self._device_operation_guard(cancel_event) as acquired:
            if not acquired:
                state = ShizukuState(
                    state="cancelled",
                    message=(
                        "Shizuku session preparation was cancelled while waiting "
                        "for another operation."
                    ),
                )
            else:
                state = self.check_status(
                    timeout=timeout,
                    cancel_event=cancel_event,
                )
        session_uid = expected_uid
        if session_uid is None and state.ready:
            session_uid = state.uid
        return ShizukuExecutionSession(
            state=state,
            expected_uid=session_uid,
            device_identity=self._device_identity(),
            _client=self,
        )

    def session_from_verified_state(
        self,
        state: ShizukuState,
        *,
        expected_uid: int | None = None,
    ) -> ShizukuExecutionSession:
        """Create an operation session from a current manager-owned ready state.

        This is deliberately strict and performs no device I/O.  The caller
        must own the device-generation and privilege-generation checks.  Every
        command still carries ``expected_uid`` to ACBridge, where permission
        and identity are checked again before execution.
        """

        if not isinstance(state, ShizukuState) or not state.ready:
            raise ValueError("A verified ready Shizuku state is required")
        session_uid = state.uid if expected_uid is None else expected_uid
        if session_uid not in {0, 2000} or state.uid != session_uid:
            raise ValueError("The verified Shizuku UID does not match the requested session UID")
        return ShizukuExecutionSession(
            state=state,
            expected_uid=session_uid,
            device_identity=self._device_identity(),
            _client=self,
        )

    def open_manager(self, *, cancel_event=None) -> CommandResult:
        """Open an already-installed Shizuku manager; never install it silently."""

        last_result: CommandResult | None = None
        for package_name in SHIZUKU_MANAGER_PACKAGES:
            if cancel_event is not None and cancel_event.is_set():
                break
            last_result = self.adb.run_shell(
                f"pm path {shell_quote(package_name)}",
                timeout=10,
                cancel_event=cancel_event,
            )
            if not last_result.success or "package:" not in (last_result.stdout or ""):
                continue
            last_result = self.adb.run_shell(
                "monkey -p "
                f"{shell_quote(package_name)} "
                "-c android.intent.category.LAUNCHER 1",
                timeout=20,
                cancel_event=cancel_event,
            )
            if last_result.success:
                last_result.status = (
                    "Opened Shizuku on Android."
                    if package_name == SHIZUKU_MANAGER_PACKAGE
                    else "Opened Sui on Android."
                )
                return last_result
        if last_result is not None:
            last_result.status = "Neither the Shizuku nor Sui manager could be opened on Android."
            return last_result
        started_at = datetime.now(timezone.utc)
        return self._cancelled_result(started_at, time.monotonic())

    def execute_shell(
        self,
        command: str,
        *,
        timeout: float | None = 120,
        expected_uid: int | None = None,
        cancel_event=None,
    ) -> CommandResult:
        command = str(command or "")
        if not command.strip():
            return self._local_result(
                success=False,
                status="Shizuku command is empty.",
                stderr="Shizuku command is empty.",
                error_type="invalid_command",
            )
        return self.execute_argv(
            ["/system/bin/sh", "-c", command],
            timeout=timeout,
            expected_uid=expected_uid,
            cancel_event=cancel_event,
        )

    def execute_argv(
        self,
        argv: Iterable[str],
        *,
        timeout: float | None = 120,
        expected_uid: int | None = None,
        cancel_event=None,
    ) -> CommandResult:
        started_monotonic = time.monotonic()
        started_at = datetime.now(timezone.utc)
        with self._device_operation_guard(cancel_event) as acquired:
            if not acquired:
                return self._cancelled_result(started_at, started_monotonic)
            return self._execute_argv_serialized(
                argv,
                timeout=timeout,
                expected_uid=expected_uid,
                cancel_event=cancel_event,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

    def _execute_prepared_shell(
        self,
        session: ShizukuExecutionSession,
        command: str,
        *,
        timeout: float | None,
        cancel_event=None,
    ) -> CommandResult:
        command = str(command or "")
        if not command.strip():
            return self._local_result(
                success=False,
                status="Shizuku command is empty.",
                stderr="Shizuku command is empty.",
                error_type="invalid_command",
            )
        return self._execute_prepared_argv(
            session,
            ["/system/bin/sh", "-c", command],
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def _execute_prepared_argv(
        self,
        session: ShizukuExecutionSession,
        argv: Iterable[str],
        *,
        timeout: float | None,
        cancel_event=None,
    ) -> CommandResult:
        started_monotonic = time.monotonic()
        started_at = datetime.now(timezone.utc)
        if session._client is not self or session.device_identity != self._device_identity():
            detail = "The prepared Shizuku session belongs to a different device identity."
            return self._local_result(
                success=False,
                status=detail,
                stderr=detail,
                error_type="shizuku_session_stale",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        with self._device_operation_guard(cancel_event) as acquired:
            if not acquired:
                return self._cancelled_result(started_at, started_monotonic)
            return self._execute_argv_serialized(
                argv,
                timeout=timeout,
                expected_uid=session.expected_uid,
                cancel_event=cancel_event,
                prepared_session=session,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

    def _execute_argv_serialized(
        self,
        argv: Iterable[str],
        *,
        timeout: float | None = 120,
        expected_uid: int | None = None,
        cancel_event=None,
        prepared_session: ShizukuExecutionSession | None = None,
        started_at: datetime | None = None,
        started_monotonic: float | None = None,
    ) -> CommandResult:
        started_monotonic = (
            time.monotonic() if started_monotonic is None else started_monotonic
        )
        started_at = started_at or datetime.now(timezone.utc)
        arguments = [str(value) for value in argv]
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_result(started_at, started_monotonic)
        if expected_uid not in {0, 2000}:
            return self._local_result(
                success=False,
                status="Check Shizuku access before running a command.",
                stderr="A verified Shizuku UID is required before command execution.",
                error_type="shizuku_identity_unverified",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        validation_error = self._validate_arguments(
            arguments,
            expected_uid=expected_uid,
        )
        if validation_error:
            return self._local_result(
                success=False,
                status=validation_error,
                stderr=validation_error,
                error_type="invalid_command",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        current_state = (
            prepared_session.state
            if prepared_session is not None
            else self.check_status(
                timeout=25,
                cancel_event=cancel_event,
            )
        )
        if cancel_event is not None and cancel_event.is_set():
            return self._cancelled_result(started_at, started_monotonic)
        if not current_state.ready:
            detail = current_state.message or "Shizuku is unavailable."
            return self._local_result(
                success=False,
                status=detail,
                stderr=detail,
                error_type=(
                    "shizuku_permission_required"
                    if current_state.state
                    in {"permission_required", "permission_denied"}
                    else "shizuku_unavailable"
                ),
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        if current_state.uid != expected_uid:
            detail = (
                "Shizuku identity changed after access was checked. Review the new "
                "UID and confirm the command again."
            )
            return self._local_result(
                success=False,
                status=detail,
                stderr=detail,
                error_type="shizuku_identity_changed",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

        request_id = uuid.uuid4().hex
        paths = self._execution_paths(request_id)
        timeout_seconds = self._normalize_timeout(timeout)
        payload = self._request_payload(arguments, expected_uid=expected_uid)
        if len(payload) > MAX_REQUEST_BYTES:
            return self._local_result(
                success=False,
                status="Shizuku request exceeds the 128 KiB encoded safety limit.",
                stderr="Shizuku request exceeds the 128 KiB encoded safety limit.",
                error_type="invalid_command",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        prepare = self._write_request(
            payload,
            paths,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            self._cleanup_execution(paths)
            return self._cancelled_result(started_at, started_monotonic)
        if not prepare.success:
            self._cleanup_execution(paths)
            detail = prepare.stderr or prepare.status or "Unable to prepare the Shizuku request."
            return self._local_result(
                success=False,
                status=detail,
                stderr=detail,
                error_type="shizuku_request_failed",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

        status_prepare = self._prepare_status(
            request_id,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            self._cleanup_execution(paths)
            return self._cancelled_result(started_at, started_monotonic)
        status_prepare_warning = ""
        if not status_prepare.success:
            # The app-private provider is authoritative.  Keep the legacy
            # app-owned result as a compatibility fallback for older helpers.
            status_prepare_warning = (
                status_prepare.stderr
                or status_prepare.status
                or "Unable to clear the legacy Shizuku status channel."
            )

        start = self._start_activity(
            "execute",
            request_id,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            self._signal_cancel(paths)
            self._cancel_activity(request_id)
            self._cleanup_execution(paths)
            return self._cancelled_result(started_at, started_monotonic)
        if not start.success:
            self._signal_cancel(paths)
            self._cancel_activity(request_id)
            self._cleanup_execution(paths)
            detail = start.status or start.stderr or "ACBridge could not start Shizuku execution."
            return self._local_result(
                success=False,
                status=detail,
                stderr=detail,
                error_type="shizuku_start_failed",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

        metadata_text = ""
        wait = self._wait_for_execution_result(
            paths,
            timeout=timeout_seconds + 20,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            self._signal_cancel(paths)
            self._cancel_activity(request_id)
            final_wait = self._wait_for_execution_result(paths, timeout=8)
            metadata_text = final_wait.stdout or ""
        else:
            metadata_text = wait.stdout or ""

        if not metadata_text.strip():
            self._signal_cancel(paths)
            self._cancel_activity(request_id)
            self._cleanup_execution(paths)
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_result(started_at, started_monotonic)
            detail = (
                wait.stderr
                or wait.status
                or "ACBridge did not produce a Shizuku result before timeout."
            )
            if status_prepare_warning:
                detail = (
                    f"{detail} Shell-owned status preparation also failed: "
                    f"{status_prepare_warning}"
                )
            return self._local_result(
                success=False,
                status=detail,
                stderr=detail,
                error_type=(
                    "shizuku_status_channel_failed"
                    if status_prepare_warning or wait.exit_code == 13
                    else "shizuku_timeout"
                ),
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

        fields = self._parse_protocol(metadata_text, self.RESULT_PREFIX)
        if fields is not None and not self._valid_result_fields(
            fields,
            request_id=request_id,
            expected_uid=expected_uid,
        ):
            fields = None
        status_fields = (
            None
            if fields is not None
            else self._parse_protocol(metadata_text, self.STATUS_PREFIX)
        )
        if status_fields is not None and not self._valid_status_fields(
            status_fields,
            request_id=request_id,
        ):
            status_fields = None
        if fields is None and status_fields is not None:
            self._cleanup_execution(paths)
            if cancel_event is not None and cancel_event.is_set():
                return self._cancelled_result(started_at, started_monotonic)
            state = self._state_from_fields(status_fields)
            error_type = (
                "shizuku_permission_required"
                if state.state in {"permission_required", "permission_denied"}
                else "shizuku_unavailable"
            )
            detail = state.message or "Shizuku could not start the command service."
            return self._local_result(
                success=False,
                status=detail,
                stderr=detail,
                error_type=error_type,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        stdout, desktop_stdout_truncated = self._read_output_with_truncation(
            paths["stdout"],
            cancel_event=cancel_event,
        )
        stderr, desktop_stderr_truncated = self._read_output_with_truncation(
            paths["stderr"],
            cancel_event=cancel_event,
        )
        self._cleanup_execution(paths)
        if fields is None:
            detail = "ACBridge returned an invalid Shizuku result."
            return self._local_result(
                success=False,
                status=detail,
                stdout=stdout,
                stderr=stderr or detail,
                error_type="shizuku_protocol_error",
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

        exit_code = self._integer(fields.get("exit_code"))
        uid = self._integer(fields.get("uid"))
        mode = self._normalized_mode(fields.get("mode", ""), uid)
        state = fields.get("state", "complete").strip().casefold()
        timed_out = self._truthy(fields.get("timed_out"))
        cancelled = self._truthy(fields.get("cancelled")) or (
            cancel_event is not None and cancel_event.is_set()
        )
        message = self._decode_message(fields.get("message_b64", ""))
        stdout_truncated = self._truthy(fields.get("stdout_truncated"))
        stderr_truncated = self._truthy(fields.get("stderr_truncated"))
        if stdout_truncated:
            stdout = self._append_notice(stdout, "[OpenADB: Shizuku stdout was truncated at the safety limit.]")
        if stderr_truncated:
            stderr = self._append_notice(stderr, "[OpenADB: Shizuku stderr was truncated at the safety limit.]")
        output_truncated = bool(
            stdout_truncated
            or stderr_truncated
            or desktop_stdout_truncated
            or desktop_stderr_truncated
        )

        success = bool(
            state == "complete"
            and exit_code == 0
            and not timed_out
            and not cancelled
            and not output_truncated
        )
        if cancelled:
            error_type = "cancelled"
            status = message or "Shizuku command cancelled."
        elif output_truncated:
            error_type = "shizuku_output_truncated"
            status = (
                "Shizuku command output exceeded a safety limit and was truncated; "
                "OpenADB rejected the incomplete result."
            )
        elif timed_out:
            error_type = "shizuku_timeout"
            status = message or "Shizuku command timed out."
        elif state in {"permission_required", "permission_denied"}:
            error_type = "shizuku_permission_required"
            status = message or "Shizuku permission is required."
        elif state in {"stopped", "binder_dead", "unavailable"}:
            error_type = "shizuku_unavailable"
            status = message or "Shizuku is not running."
        elif success:
            error_type = ""
            status = message or f"Shizuku command completed through {mode} (UID {uid})."
        else:
            error_type = "shizuku_command_failed"
            status = message or f"Shizuku command failed with exit code {exit_code}."
        if not success and not stderr and status:
            stderr = status

        finished_at = datetime.now(timezone.utc)
        return CommandResult(
            command=["shizuku", mode, "<protected request>"],
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=max(0.0, time.monotonic() - started_monotonic),
            started_at=started_at,
            finished_at=finished_at,
            success=success,
            status=status,
            error_type=error_type,
            **self._context_fields(),
        )

    def _status_operation(
        self,
        operation: str,
        *,
        timeout: int,
        cancel_event=None,
        bridge_is_trusted: bool = False,
        permission_host_request_id: str = "",
    ) -> ShizukuState:
        if cancel_event is not None and cancel_event.is_set():
            return ShizukuState(state="cancelled", message="Shizuku check was cancelled.")
        if not bridge_is_trusted:
            installed, message = self._ensure_trusted_bridge(cancel_event=cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                return ShizukuState(state="cancelled", message="Shizuku check was cancelled.")
            if not installed:
                return ShizukuState(state="bridge_unavailable", message=message)
        request_id = uuid.uuid4().hex
        result_path = self._status_path(request_id)
        prepare = self._prepare_status(request_id, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            self._cleanup_status(request_id)
            return ShizukuState(state="cancelled", message="Shizuku check was cancelled.")
        prepare_warning = ""
        if not prepare.success:
            prepare_warning = (
                prepare.stderr
                or prepare.status
                or "ACBridge could not clear the legacy Shizuku status channel."
            )
        start = self._start_activity(
            operation,
            request_id,
            timeout_seconds=timeout,
            cancel_event=cancel_event,
            permission_host_request_id=permission_host_request_id,
        )
        if cancel_event is not None and cancel_event.is_set():
            self._cancel_activity(request_id)
            self._cleanup_status(request_id)
            return ShizukuState(state="cancelled", message="Shizuku check was cancelled.")
        if not start.success:
            self._cancel_activity(request_id)
            self._cleanup_status(request_id)
            detail = start.status or start.stderr or "ACBridge could not start the Shizuku status flow."
            return ShizukuState(state="error", message=detail)
        wait = self._wait_for_file(
            result_path,
            timeout=timeout,
            cancel_event=cancel_event,
            provider_uri=self._status_uri(request_id),
        )
        output = wait.stdout or ""
        if cancel_event is not None and cancel_event.is_set():
            self._cancel_activity(request_id)
            self._cleanup_status(request_id)
            return ShizukuState(state="cancelled", message="Shizuku check was cancelled.")
        fields = self._parse_protocol(output, self.STATUS_PREFIX)
        if fields is None or not self._valid_status_fields(
            fields,
            request_id=request_id,
        ):
            self._cancel_activity(request_id)
            self._cleanup_status(request_id)
            detail = wait.stderr or wait.status or "ACBridge returned an invalid Shizuku status."
            if prepare_warning:
                detail = (
                    f"{detail} Shell-owned status preparation also failed: "
                    f"{prepare_warning}"
                )
            return ShizukuState(state="error", message=detail)
        self._cleanup_status(request_id)
        return self._state_from_fields(fields)

    def _ensure_trusted_bridge(self, *, cancel_event=None) -> tuple[bool, str]:
        return self.bridge.ensure_trusted(cancel_event=cancel_event)

    def _start_activity(
        self,
        operation: str,
        request_id: str,
        *,
        timeout_seconds: int,
        cancel_event=None,
        permission_host_request_id: str = "",
    ) -> CommandResult:
        wait_for_launch = "" if operation.casefold() == "requestpermission" else "-W "
        normalized_host_request_id = str(
            permission_host_request_id or ""
        ).strip().casefold()
        host_argument = (
            " --es permission_host_request_id "
            f"{shell_quote(normalized_host_request_id)}"
            if self.REQUEST_ID_RE.fullmatch(normalized_host_request_id)
            else ""
        )
        command = (
            f"am start {wait_for_launch}-n {shell_quote(SHIZUKU_ACTIVITY)} "
            f"--es operation {shell_quote(operation)} "
            f"--es request_id {shell_quote(request_id)} "
            f"--ei timeout_seconds {int(timeout_seconds)}"
            f"{host_argument}"
        )
        return self.adb.run_shell(
            command,
            timeout=20,
            cancel_event=cancel_event,
        )

    def _cancel_activity(self, request_id: str) -> CommandResult:
        return self._start_activity(
            "cancel",
            request_id,
            timeout_seconds=10,
        )

    def _write_request(self, payload: bytes, paths: dict[str, str], *, cancel_event=None) -> CommandResult:
        quoted = " ".join(shell_quote(path) for path in paths.values())
        script = (
            f"umask 077; rm -f {quoted}; "
            f"cat > {shell_quote(paths['request'])}; "
            f"chmod 0600 {shell_quote(paths['request'])}"
        )

        def write_request(stream: BinaryIO) -> None:
            stream.write(payload)
            stream.flush()

        return self.adb.run_raw_with_input_stream(
            ["shell", script],
            input_writer=write_request,
            timeout=20,
            cancel_event=cancel_event,
        )

    def _wait_for_file(
        self,
        path: str,
        *,
        timeout: int,
        cancel_event=None,
        provider_uri: str = "",
    ) -> CommandResult:
        timeout = max(1, int(timeout))
        script = (
            f"result={shell_quote(path)}; provider={shell_quote(provider_uri)}; "
            f"deadline=$(( $(date +%s) + {timeout} )); delay=0.1; "
            "while [ \"$(date +%s)\" -lt \"$deadline\" ]; do "
            "if [ -n \"$provider\" ]; then "
            "provider_payload=\"$(content read --uri \"$provider\" 2>/dev/null)\"; "
            "case \"$provider_payload\" in "
            "\"OPENADB_SHIZUKU_STATUS 1\"*) "
            "printf '%s\\n' \"$provider_payload\"; exit 0;; esac; fi; "
            "if [ -s \"$result\" ]; then "
            "cat \"$result\" && exit 0; "
            "echo 'Shizuku result exists but ADB shell cannot read it.' >&2; exit 13; "
            "fi; "
            "sleep \"$delay\"; done; "
            "echo 'Shizuku result was not produced before timeout.' >&2; exit 124"
        )
        return self.adb.run_shell(
            script,
            timeout=timeout + 8,
            cancel_event=cancel_event,
        )

    def _wait_for_execution_result(
        self,
        paths: dict[str, str],
        *,
        timeout: int,
        cancel_event=None,
    ) -> CommandResult:
        """Wait for either the shell-owned result or an app-owned terminal status."""

        timeout = max(1, int(timeout))
        try:
            request_id = self._status_request_id_from_path(paths["status"])
        except ValueError:
            request_id = ""
        provider_uri = self._status_uri(request_id) if request_id else ""
        script = (
            f"result={shell_quote(paths['result'])}; "
            f"status={shell_quote(paths['status'])}; "
            f"provider={shell_quote(provider_uri)}; "
            f"deadline=$(( $(date +%s) + {timeout} )); delay=0.1; "
            "while [ \"$(date +%s)\" -lt \"$deadline\" ]; do "
            "if [ -s \"$result\" ]; then "
            "cat \"$result\" && exit 0; "
            "echo 'Shizuku result exists but ADB shell cannot read it.' >&2; exit 13; "
            "fi; "
            "if [ -n \"$provider\" ]; then "
            "provider_payload=\"$(content read --uri \"$provider\" 2>/dev/null)\"; "
            "case \"$provider_payload\" in "
            "\"OPENADB_SHIZUKU_STATUS 1\"*) "
            "printf '%s\\n' \"$provider_payload\"; exit 0;; esac; fi; "
            "if [ -s \"$status\" ]; then "
            "cat \"$status\" && exit 0; "
            "echo 'Shizuku status exists but ADB shell cannot read it.' >&2; exit 13; "
            "fi; "
            "sleep \"$delay\"; done; "
            "echo 'Shizuku result was not produced before timeout.' >&2; exit 124"
        )
        return self.adb.run_shell(
            script,
            timeout=timeout + 8,
            cancel_event=cancel_event,
        )

    def _signal_cancel(self, paths: dict[str, str]) -> None:
        self.adb.run_shell(
            f"umask 077; : > {shell_quote(paths['cancel'])}",
            timeout=5,
        )

    def _cleanup_execution(self, paths: dict[str, str]) -> None:
        quoted = " ".join(shell_quote(path) for path in paths.values())
        command = f"rm -f {quoted}"
        try:
            request_id = self._status_request_id_from_path(paths["status"])
        except (KeyError, ValueError):
            request_id = ""
        if request_id:
            provider_uri = self._status_uri(request_id)
            command += (
                "; content delete --uri "
                f"{shell_quote(provider_uri)} >/dev/null 2>&1 || true"
            )
        self.adb.run_shell(command, timeout=8)

    def _cleanup_status(self, request_id: str) -> None:
        status_path = self._status_path(request_id)
        temporary_path = self._status_temporary_path(request_id)
        provider_uri = self._status_uri(request_id)
        self.adb.run_shell(
            "rm -f "
            f"{shell_quote(status_path)} {shell_quote(temporary_path)} "
            ">/dev/null 2>&1 || true; "
            f"content delete --uri {shell_quote(provider_uri)} >/dev/null 2>&1 || true",
            timeout=5,
        )

    def _prepare_status(self, request_id: str, *, cancel_event=None) -> CommandResult:
        """Clear prior request artifacts without creating cross-UID files.

        ACBridge publishes the authoritative payload from app-private storage
        through its DUMP-protected provider.  Creating the legacy temporary
        file as ADB shell breaks fresh-install app UIDs on Android 16.
        """

        status_path = self._status_path(request_id)
        temporary_path = self._status_temporary_path(request_id)
        provider_uri = self._status_uri(request_id)
        command = (
            f"content delete --uri {shell_quote(provider_uri)} >/dev/null 2>&1 || true; "
            f"rm -f {shell_quote(status_path)} {shell_quote(temporary_path)} "
            ">/dev/null 2>&1 || true"
        )
        return self.adb.run_shell(
            command,
            timeout=8,
            cancel_event=cancel_event,
        )

    def _read_output(self, path: str, *, cancel_event=None) -> str:
        output, _truncated = self._read_output_with_truncation(
            path,
            cancel_event=cancel_event,
        )
        return output

    def _read_output_with_truncation(
        self,
        path: str,
        *,
        cancel_event=None,
    ) -> tuple[str, bool]:
        if cancel_event is not None and cancel_event.is_set():
            return "", False
        run_binary = getattr(self.adb, "run_raw_binary_output", None)
        if callable(run_binary):
            result, payload = run_binary(
                ["exec-out", "cat", path],
                timeout=20,
                cancel_event=cancel_event,
            )
            if result.success:
                return self._bounded_output_text_with_truncation(bytes(payload or b""))
            if cancel_event is not None and cancel_event.is_set():
                return "", False
        result = self.adb.run_shell(
            f"cat {shell_quote(path)} 2>/dev/null",
            timeout=20,
            cancel_event=cancel_event,
        )
        return self._bounded_output_text_with_truncation(
            str(result.stdout or "").encode("utf-8", errors="replace")
        )

    @staticmethod
    def _bounded_output_text(payload: bytes) -> str:
        text, _truncated = ShizukuClient._bounded_output_text_with_truncation(payload)
        return text

    @staticmethod
    def _bounded_output_text_with_truncation(payload: bytes) -> tuple[str, bool]:
        truncated = len(payload) > MAX_DESKTOP_OUTPUT_BYTES
        if truncated:
            payload = payload[:MAX_DESKTOP_OUTPUT_BYTES]
        text = payload.decode("utf-8", errors="replace")
        if truncated:
            text = ShizukuClient._append_notice(
                text,
                "[OpenADB: Shizuku output was shortened to 2 MiB for UI responsiveness.]",
            )
        return text, truncated

    @staticmethod
    def _execution_paths(request_id: str) -> dict[str, str]:
        prefix = f"/data/local/tmp/openadb-shizuku-{request_id}"
        return {
            "request": f"{prefix}.request",
            "stdout": f"{prefix}.stdout",
            "stderr": f"{prefix}.stderr",
            "result": f"{prefix}.result",
            "result_tmp": f"{prefix}.result.tmp",
            "cancel": f"{prefix}.cancel",
            "status": ShizukuClient._status_path(request_id),
            "status_tmp": ShizukuClient._status_temporary_path(request_id),
        }

    @staticmethod
    def _status_path(request_id: str) -> str:
        return f"{ACBridgeClient.REMOTE_APP_DIR}/shizuku_status_{request_id}.txt"

    @staticmethod
    def _status_temporary_path(request_id: str) -> str:
        return f"{ACBridgeClient.REMOTE_APP_DIR}/.shizuku_status_{request_id}.tmp"

    @staticmethod
    def _status_uri(request_id: str) -> str:
        return ACBridgeClient._host_status_uri("shizuku", request_id)

    @staticmethod
    def _status_request_id_from_path(path: str) -> str:
        match = re.search(
            r"/shizuku_status_([0-9a-f]{32})\.txt\Z",
            str(path or ""),
        )
        if match is None:
            raise ValueError("Invalid Shizuku status result path")
        return match.group(1)

    @classmethod
    def _request_payload(
        cls,
        arguments: list[str],
        *,
        expected_uid: int = 2000,
    ) -> bytes:
        lines = [
            f"{cls.REQUEST_PREFIX} {SHIZUKU_PROTOCOL_VERSION}",
            f"expected_uid={expected_uid}",
            f"argv_count={len(arguments)}",
        ]
        lines.extend(
            "arg_b64=" + base64.b64encode(argument.encode("utf-8")).decode("ascii")
            for argument in arguments
        )
        return ("\n".join(lines) + "\n").encode("ascii")

    @staticmethod
    def _validate_arguments(
        arguments: list[str],
        *,
        expected_uid: int = 2000,
    ) -> str:
        if not arguments:
            return "Shizuku command has no executable."
        if len(arguments) > MAX_ARGUMENTS:
            return f"Shizuku command has too many arguments (maximum {MAX_ARGUMENTS})."
        for argument in arguments:
            if "\x00" in argument:
                return "Shizuku command arguments cannot contain NUL bytes."
            encoded = argument.encode("utf-8")
            if len(encoded) > 64 * 1024:
                return "A Shizuku command argument exceeds the 64 KiB safety limit."
        if expected_uid not in {0, 2000}:
            return "Shizuku expected UID must be 0 or 2000."
        if len(
            ShizukuClient._request_payload(
                arguments,
                expected_uid=expected_uid,
            )
        ) > MAX_REQUEST_BYTES:
            return "Shizuku request exceeds the 128 KiB encoded safety limit."
        return ""

    @staticmethod
    def _normalize_timeout(timeout: float | None) -> int:
        if timeout is None:
            return 120
        try:
            value = int(float(timeout))
        except (TypeError, ValueError, OverflowError):
            return 120
        return max(1, min(value, 900))

    @staticmethod
    def _parse_protocol(text: str, prefix: str) -> dict[str, str] | None:
        lines = [line.rstrip("\r") for line in str(text or "").splitlines()]
        marker = f"{prefix} {SHIZUKU_PROTOCOL_VERSION}"
        try:
            marker_index = lines.index(marker)
        except ValueError:
            return None
        fields: dict[str, str] = {}
        for line in lines[marker_index + 1 :]:
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key.replace("_", "").isalnum():
                fields[key] = value.strip()
        return fields

    @classmethod
    def _valid_status_fields(
        cls,
        fields: dict[str, str],
        *,
        request_id: str,
    ) -> bool:
        required = {
            "request_id",
            "state",
            "installed",
            "binder",
            "permission",
            "uid",
            "mode",
            "api",
        }
        if not required.issubset(fields) or fields.get("request_id") != request_id:
            return False
        if cls._integer(fields.get("uid")) is None or cls._integer(fields.get("api")) is None:
            return False
        if not cls._protocol_boolean(fields.get("installed")):
            return False
        if not cls._protocol_boolean(fields.get("binder")):
            return False
        return bool(fields.get("state", "").strip())

    @classmethod
    def _valid_result_fields(
        cls,
        fields: dict[str, str],
        *,
        request_id: str,
        expected_uid: int,
    ) -> bool:
        required = {
            "request_id",
            "state",
            "exit_code",
            "uid",
            "timed_out",
            "cancelled",
        }
        if not required.issubset(fields) or fields.get("request_id") != request_id:
            return False
        if cls._integer(fields.get("exit_code")) is None:
            return False
        if cls._integer(fields.get("uid")) != expected_uid:
            return False
        if not cls._protocol_boolean(fields.get("timed_out")):
            return False
        if not cls._protocol_boolean(fields.get("cancelled")):
            return False
        return bool(fields.get("state", "").strip())

    @staticmethod
    def _protocol_boolean(value: str | None) -> bool:
        return str(value or "").strip().casefold() in {"0", "1", "true", "false"}

    @staticmethod
    def _decode_message(value: str) -> str:
        if not value:
            return ""
        try:
            return base64.b64decode(value, validate=True).decode("utf-8", errors="replace").strip()
        except (ValueError, binascii.Error):
            return ""

    @staticmethod
    def _integer(value: str | None) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _truthy(value: str | None) -> bool:
        return str(value or "").strip().casefold() in {"1", "true", "yes"}

    @staticmethod
    def _normalized_mode(value: str, uid: int | None) -> str:
        if uid == 0:
            return "root"
        if uid == 2000:
            return "shell"
        value = str(value or "").strip().casefold()
        return value if value in {"root", "shell"} else "unavailable"

    @classmethod
    def _state_from_fields(cls, fields: dict[str, str]) -> ShizukuState:
        uid = cls._integer(fields.get("uid"))
        api_version = cls._integer(fields.get("api"))
        state = fields.get("state", "unknown").strip().casefold()
        installed = cls._truthy(fields.get("installed"))
        running = cls._truthy(fields.get("binder"))
        if state == "unavailable" and not running:
            state = "stopped" if installed else "not_installed"
        permission = fields.get("permission", "unknown").strip().casefold()
        if permission not in {
            "unknown",
            "required",
            "denied",
            "granted",
            "unsupported",
        }:
            permission = "unknown"
        if state == "permission_granted" and permission == "granted":
            state = "ready"
        elif state == "permission_required" and permission == "denied":
            state = "permission_denied"
        elif state == "permission_required" and permission == "unsupported":
            state = "unsupported"
        integrity_message = ""
        if state == "ready" and not (
            running and permission == "granted" and uid in {0, 2000}
        ):
            state = "error"
            integrity_message = (
                "ACBridge returned inconsistent Shizuku readiness details; "
                "access was not accepted."
            )
        normalized_message = ""
        if state in {"permission_denied", "unsupported"}:
            normalized_message = cls._default_state_message(state, uid)
        return ShizukuState(
            state=state,
            installed=installed,
            running=running,
            permission=permission,
            uid=uid,
            mode=cls._normalized_mode(fields.get("mode", ""), uid),
            api_version=api_version,
            message=integrity_message
            or normalized_message
            or cls._decode_message(fields.get("message_b64", ""))
            or cls._default_state_message(state, uid),
        )

    @staticmethod
    def _append_notice(output: str, notice: str) -> str:
        output = str(output or "")
        separator = "" if not output or output.endswith("\n") else "\n"
        return f"{output}{separator}{notice}\n"

    @staticmethod
    def _default_state_message(state: str, uid: int | None) -> str:
        messages = {
            "not_installed": "Shizuku is not installed on this device.",
            "stopped": "Shizuku is installed but its service is not running.",
            "permission_required": "Grant OpenADB Bridge access in the Shizuku prompt on the device.",
            "permission_denied": "Shizuku permission was denied for OpenADB Bridge.",
            "unsupported": "The running Shizuku service is too old for OpenADB.",
            "binder_dead": "The Shizuku service disconnected.",
        }
        if state == "ready" and uid == 0:
            return "Shizuku is ready with root identity (UID 0)."
        if state == "ready" and uid == 2000:
            return "Shizuku is ready with Android shell identity (UID 2000)."
        return messages.get(state, "Shizuku is unavailable.")

    def _context_fields(self) -> dict[str, object]:
        context = getattr(self.adb, "device_context", None)
        serial = str(getattr(self.adb, "serial", "") or "")
        return {
            "device_serial": serial,
            "device_generation": getattr(context, "generation", None),
            "logs_folder": str(getattr(context, "logs_path", "") or ""),
        }

    def _device_identity(self) -> ShizukuDeviceIdentity:
        """Return a stable key for the captured transport, never global UI state."""

        context = getattr(self.adb, "device_context", None)
        serial = str(
            getattr(context, "serial", "")
            or getattr(self.adb, "serial", "")
            or ""
        )
        transport_id = str(
            getattr(context, "transport_id", "")
            or getattr(self.adb, "_bound_transport_id", "")
            or ""
        )
        generation_value = getattr(context, "generation", None)
        generation = generation_value if isinstance(generation_value, int) else None
        fallback_identity = 0
        if not serial and not transport_id and generation is None:
            fallback_identity = id(self.adb)
        return serial, transport_id, generation, fallback_identity

    def _device_operation_lock(self) -> threading.RLock:
        return self._USER_SERVICE_GATE

    @contextmanager
    def _device_operation_guard(self, cancel_event=None) -> Iterator[bool]:
        """Acquire the reconnect-safe one-shot UserService gate cancellably."""

        lock = self._device_operation_lock()
        acquired = False
        while not acquired:
            if cancel_event is not None and cancel_event.is_set():
                yield False
                return
            acquired = lock.acquire(timeout=0.1)
        try:
            if cancel_event is not None and cancel_event.is_set():
                yield False
            else:
                yield True
        finally:
            lock.release()

    def _cancelled_result(self, started_at: datetime, started_monotonic: float) -> CommandResult:
        return self._local_result(
            success=False,
            status="Shizuku command cancelled.",
            error_type="cancelled",
            started_at=started_at,
            started_monotonic=started_monotonic,
        )

    def _local_result(
        self,
        *,
        success: bool,
        status: str,
        stdout: str = "",
        stderr: str = "",
        error_type: str = "",
        started_at: datetime | None = None,
        started_monotonic: float | None = None,
    ) -> CommandResult:
        started_at = started_at or datetime.now(timezone.utc)
        duration = 0.0 if started_monotonic is None else max(0.0, time.monotonic() - started_monotonic)
        return CommandResult(
            command=["shizuku", "<protected request>"],
            exit_code=0 if success else None,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            success=success,
            status=status,
            error_type=error_type,
            **self._context_fields(),
        )
