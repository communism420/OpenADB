from __future__ import annotations

import logging
import posixpath
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from openadb.core.adb import ADBClient, _device_form_factor
from openadb.core.device_context import DeviceContextUnavailable, StaleDeviceContext
from openadb.core.path_utils import is_probably_writable_android_path
from openadb.core.shizuku import ShizukuClient, ShizukuState
from openadb.models.device_info import DeviceInfo

if TYPE_CHECKING:
    from openadb.core.device_context import DeviceContext


LOGGER = logging.getLogger(__name__)
SHIZUKU_VERIFIED_STATE_TTL_SECONDS = 60.0


class PrivilegeBackend(str, Enum):
    STANDARD = "standard"
    ROOT = "root"
    SHIZUKU = "shizuku"

    @classmethod
    def normalize(cls, value: object) -> PrivilegeBackend:
        if isinstance(value, cls):
            return value
        normalized = str(value or "").strip().casefold()
        normalized = {
            "adb": cls.STANDARD.value,
            "none": cls.STANDARD.value,
            "su": cls.ROOT.value,
            "sui": cls.SHIZUKU.value,
        }.get(normalized, normalized)
        try:
            return cls(normalized)
        except ValueError:
            return cls.STANDARD


@dataclass(frozen=True, slots=True)
class PrivilegeOperationLease:
    """Immutable access-mode generation captured before an operation starts."""

    backend: PrivilegeBackend
    _cancel_event: threading.Event

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()


SHIZUKU_INVALIDATING_ERRORS = frozenset(
    {
        "shizuku_identity_unverified",
        "shizuku_identity_changed",
        "shizuku_permission_required",
        "shizuku_unavailable",
        "shizuku_bridge_unavailable",
        "shizuku_start_failed",
        "shizuku_protocol_error",
        "shizuku_session_stale",
    }
)

SHIZUKU_INFRASTRUCTURE_ERRORS = SHIZUKU_INVALIDATING_ERRORS | frozenset(
    {
        "shizuku_output_truncated",
        "shizuku_request_failed",
        "shizuku_timeout",
    }
)

_SHIZUKU_TRUNCATION_MARKERS = (
    "stdout was truncated",
    "stderr was truncated",
    "output was shortened to 2 mib",
    "stdout was shortened to 2 mib",
    "stderr was shortened to 2 mib",
)


class RootExecutionStrategy(str, Enum):
    """How an operation-scoped root client reaches UID 0 on Android."""

    DIRECT = "direct"
    SU = "su"


class _LinkedCancellationEvent:
    """Event-like view cancelled by either an operation or backend reset."""

    def __init__(
        self,
        operation_event: threading.Event | None,
        backend_event: threading.Event,
    ) -> None:
        self._operation_event = operation_event
        self._backend_event = backend_event

    def is_set(self) -> bool:
        return self._backend_event.is_set() or bool(
            self._operation_event is not None and self._operation_event.is_set()
        )

    def set(self) -> None:
        if self._operation_event is not None:
            self._operation_event.set()
        else:
            self._backend_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            self._backend_event.wait(
                0.05 if remaining is None else min(0.05, remaining)
            )
        return self.is_set()


def _delegate_prepared_raw(
    client,
    method_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    cancel_index: int,
):
    """Keep the data plane direct while invalidating an old prepared lease."""

    client._manager._raise_if_cancelled(
        client._backend_cancel_event,
        "Android operation was cancelled because the selected access mode changed.",
    )
    client._manager._require_selected_backend(
        client.backend,
        "The selected access mode changed before the Android operation started.",
    )
    client._manager._require_context_current(client.device_context)
    call_args = list(args)
    call_kwargs = dict(kwargs)
    if len(call_args) > cancel_index:
        operation_event = call_args[cancel_index]
        call_args[cancel_index] = _LinkedCancellationEvent(
            operation_event,
            client._backend_cancel_event,
        )
    else:
        operation_event = call_kwargs.get("cancel_event")
        call_kwargs["cancel_event"] = _LinkedCancellationEvent(
            operation_event,
            client._backend_cancel_event,
        )
    client._manager._raise_if_cancelled(
        call_args[cancel_index]
        if len(call_args) > cancel_index
        else call_kwargs["cancel_event"],
        "Android operation was cancelled before it started.",
    )
    result = getattr(client._direct_adb, method_name)(*call_args, **call_kwargs)
    client._manager._raise_if_cancelled(
        client._backend_cancel_event,
        "Android operation was cancelled because the selected access mode changed.",
    )
    client._manager._require_selected_backend(
        client.backend,
        "The selected access mode changed while the Android operation was running.",
    )
    client._manager._require_context_current(client.device_context)
    return result


class RootAwareADBClient(ADBClient):
    """ADB facade whose Android-shell plane is pinned to verified UID 0.

    A root adbd (common in recovery/userdebug environments) must execute the
    command directly: wrapping it in ``su -c`` can fail when ``su`` is absent.
    A production adbd normally needs the separately verified ``su`` route.
    Host-side ADB operations and byte streams remain on the immutable direct
    client; only the Android shell plane is elevated automatically.
    """

    def __init__(
        self,
        direct_adb,
        manager: PrivilegeManager,
        context: DeviceContext,
        strategy: RootExecutionStrategy,
        backend_cancel_event: threading.Event,
    ) -> None:
        # The captured direct client already owns the immutable serial,
        # transport id, bound logger, and cancellation-capable data plane.
        self._direct_adb = direct_adb
        self._manager = manager
        self.device_context = context
        self.root_strategy = strategy
        self._backend_cancel_event = backend_cancel_event
        self.backend = PrivilegeBackend.ROOT
        self.requested_privilege_backend = PrivilegeBackend.ROOT
        self.effective_privilege_backend = PrivilegeBackend.ROOT
        self.verified_uid = 0
        self.privilege_fallback_message = ""
        self.platform_tools = getattr(direct_adb, "platform_tools", None)
        self.runner = getattr(direct_adb, "runner", None)

    @property
    def serial(self) -> str:
        return str(getattr(self._direct_adb, "serial", "") or "")

    @serial.setter
    def serial(self, _value: str) -> None:
        raise RuntimeError("A prepared root ADB client cannot change serial")

    @property
    def direct_adb(self):
        """Return the immutable host/control-plane client for this operation."""

        return self._direct_adb

    def set_serial(self, serial: str) -> None:
        if str(serial or "") != self.serial:
            raise RuntimeError("A prepared root ADB client cannot change serial")

    def for_context(self, context: DeviceContext) -> RootAwareADBClient:
        if context != self.device_context:
            raise RuntimeError("A prepared root ADB client cannot be rebound")
        self._manager._require_context_current(context)
        return self

    def for_serial(self, serial: str) -> RootAwareADBClient:
        if str(serial or "") != self.serial:
            raise RuntimeError("A prepared root ADB client cannot be rebound")
        self._manager._require_context_current(self.device_context)
        return self

    def _base(self, serial: str | None = None) -> list[str]:
        return self._direct_adb._base(serial=serial)

    # A prepared backend never rewrites raw ADB protocol operations. In
    # particular push/pull/install/reboot/connect stay on Platform Tools.
    def run_raw(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw", args, kwargs, cancel_index=3
        )

    def run_raw_binary_output(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw_binary_output", args, kwargs, cancel_index=3
        )

    def run_raw_binary_output_with_writer(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self,
            "run_raw_binary_output_with_writer",
            args,
            kwargs,
            cancel_index=5,
        )

    def run_raw_streaming(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw_streaming", args, kwargs, cancel_index=4
        )

    def run_raw_with_input_stream(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw_with_input_stream", args, kwargs, cancel_index=5
        )

    def run_raw_binary_output_to_file(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw_binary_output_to_file", args, kwargs, cancel_index=6
        )

    def run_shell(
        self,
        shell_command: str,
        timeout: float | None = 120,
        cancel_event=None,
    ):
        self._manager._raise_if_cancelled(
            self._backend_cancel_event,
            "Root command was cancelled because the selected access mode changed.",
        )
        self._manager._require_selected_backend(
            PrivilegeBackend.ROOT,
            "The selected access mode changed before the root command completed.",
        )
        self._manager._require_context_current(self.device_context)
        linked_cancel = _LinkedCancellationEvent(
            cancel_event,
            self._backend_cancel_event,
        )
        self._manager._raise_if_cancelled(
            linked_cancel,
            "Root command was cancelled before it started.",
        )
        executor = (
            self._direct_adb.run_shell
            if self.root_strategy is RootExecutionStrategy.DIRECT
            else self._direct_adb.run_root_shell
        )
        result = executor(
            str(shell_command or ""),
            timeout=timeout,
            cancel_event=linked_cancel,
        )
        self._manager._raise_if_cancelled(
            self._backend_cancel_event,
            "Root command was cancelled because the selected access mode changed.",
        )
        self._manager._require_selected_backend(
            PrivilegeBackend.ROOT,
            "The selected access mode changed while the root command was running.",
        )
        self._manager._require_context_current(self.device_context)
        return result

    def run_root_shell(
        self,
        shell_command: str,
        timeout: float | None = 120,
        cancel_event=None,
    ):
        # Do not call ADBClient.run_root_shell here: it would wrap the command
        # a second time when this facade already uses the verified su route.
        return self.run_shell(
            shell_command,
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def root_available(self, cancel_event=None) -> bool:
        linked_cancel = _LinkedCancellationEvent(
            cancel_event,
            self._backend_cancel_event,
        )
        if linked_cancel.is_set():
            return False
        self._manager._raise_if_cancelled(
            self._backend_cancel_event,
            "Root availability changed with the selected access mode.",
        )
        self._manager._require_selected_backend(
            PrivilegeBackend.ROOT,
            "The selected access mode changed before root availability was used.",
        )
        self._manager._require_context_current(self.device_context)
        return True

    def root_shell_script(self, shell_command: str) -> str:
        self._manager._raise_if_cancelled(
            self._backend_cancel_event,
            "Root command preparation was cancelled because the access mode changed.",
        )
        self._manager._require_selected_backend(
            PrivilegeBackend.ROOT,
            "The selected access mode changed before a root command was prepared.",
        )
        self._manager._raise_if_cancelled(
            self._backend_cancel_event,
            "Root command preparation was cancelled because the access mode changed.",
        )
        self._manager._require_context_current(self.device_context)
        if self.root_strategy is RootExecutionStrategy.DIRECT:
            return str(shell_command or "")
        return self._direct_adb.root_shell_script(shell_command)


class ShizukuAwareADBClient(ADBClient):
    """ADB facade whose Android-shell plane is pinned to one Shizuku session.

    The original immutable bound ADB remains the control and data plane.  This
    distinction is deliberate: Shizuku itself uses that direct ADB client for
    its authenticated request protocol, and host-side operations such as
    push, pull, install, connect, and forwarding are not Android shell calls.
    """

    PACKAGE_DETAILS_CHUNK_SIZE = 8

    def __init__(
        self,
        direct_adb,
        session,
        manager: PrivilegeManager,
        context: DeviceContext,
        verified_uid: int,
        backend_cancel_event: threading.Event,
    ) -> None:
        # Do not call ADBClient.__init__: the direct client is already bound to
        # the captured serial *and* transport id and owns its bound log runner.
        self._direct_adb = direct_adb
        self._session = session
        self._manager = manager
        self.device_context = context
        self.backend = PrivilegeBackend.SHIZUKU
        self.requested_privilege_backend = PrivilegeBackend.SHIZUKU
        self.effective_privilege_backend = PrivilegeBackend.SHIZUKU
        self.verified_uid = verified_uid
        self._backend_cancel_event = backend_cancel_event
        self.privilege_fallback_message = ""
        self.platform_tools = getattr(direct_adb, "platform_tools", None)
        self.runner = getattr(direct_adb, "runner", None)

    @property
    def serial(self) -> str:
        return str(getattr(self._direct_adb, "serial", "") or "")

    @serial.setter
    def serial(self, _value: str) -> None:
        raise RuntimeError("A prepared Shizuku ADB client cannot change serial")

    def set_serial(self, serial: str) -> None:
        if str(serial or "") != self.serial:
            raise RuntimeError("A prepared Shizuku ADB client cannot change serial")

    @property
    def direct_adb(self):
        """Return the immutable control/data-plane ADB captured for this operation."""

        return self._direct_adb

    def for_context(self, context: DeviceContext) -> ShizukuAwareADBClient:
        if context != self.device_context:
            raise RuntimeError("A prepared Shizuku ADB client cannot be rebound")
        self._manager._require_context_current(context)
        return self

    def for_serial(self, serial: str) -> ShizukuAwareADBClient:
        if str(serial or "") != self.serial:
            raise RuntimeError("A prepared Shizuku ADB client cannot be rebound")
        self._manager._require_context_current(self.device_context)
        return self

    def _base(self, serial: str | None = None) -> list[str]:
        return self._direct_adb._base(serial=serial)

    # Raw/data-plane calls must never enter the Shizuku request protocol.
    def run_raw(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw", args, kwargs, cancel_index=3
        )

    def run_raw_binary_output(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw_binary_output", args, kwargs, cancel_index=3
        )

    def run_raw_binary_output_with_writer(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self,
            "run_raw_binary_output_with_writer",
            args,
            kwargs,
            cancel_index=5,
        )

    def run_raw_streaming(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw_streaming", args, kwargs, cancel_index=4
        )

    def run_raw_with_input_stream(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw_with_input_stream", args, kwargs, cancel_index=5
        )

    def run_raw_binary_output_to_file(self, *args, **kwargs):
        return _delegate_prepared_raw(
            self, "run_raw_binary_output_to_file", args, kwargs, cancel_index=6
        )

    def _direct_public_storage_shell(self, path: str, *, use_root: bool) -> bool:
        """Return whether a listing is equivalent on the captured ADB shell.

        A Shizuku UserService running as UID 2000 has the same public-storage
        visibility as the direct ADB shell, so every public path can use the
        low-latency data plane.  UID 0 keeps Shizuku semantics for protected
        and removable-storage paths, but ordinary internal shared storage is
        equivalent and must not start a new Activity/UserService for every
        folder navigation.
        """

        if use_root or not is_probably_writable_android_path(path):
            return False
        if self.verified_uid == 2000:
            return True
        return bool(
            self.verified_uid == 0
            and self._ordinary_internal_shared_storage_path(path)
        )

    @staticmethod
    def _ordinary_internal_shared_storage_path(path: str) -> bool:
        raw = str(path or "").replace("\\", "/")
        clean = posixpath.normpath(f"/{raw.lstrip('/')}")
        roots = ("/sdcard", "/storage/emulated/0")
        for root in roots:
            if clean != root and not clean.startswith(f"{root}/"):
                continue
            relative = clean[len(root) :].strip("/")
            parts = [part.casefold() for part in relative.split("/") if part]
            return not (
                len(parts) >= 2
                and parts[0] == "android"
                and parts[1] in {"data", "obb"}
            )
        return False

    def list_files(
        self,
        android_path: str,
        use_root: bool = False,
        cancel_event=None,
    ):
        if self._direct_public_storage_shell(android_path, use_root=use_root):
            return _delegate_prepared_raw(
                self,
                "list_files",
                (android_path,),
                {"use_root": False, "cancel_event": cancel_event},
                cancel_index=2,
            )
        return super().list_files(
            android_path,
            use_root=use_root,
            cancel_event=cancel_event,
        )

    def list_files_with_storage(
        self,
        android_path: str,
        use_root: bool = False,
        cancel_event=None,
    ):
        if self._direct_public_storage_shell(android_path, use_root=use_root):
            return _delegate_prepared_raw(
                self,
                "list_files_with_storage",
                (android_path,),
                {"use_root": False, "cancel_event": cancel_event},
                cancel_index=2,
            )
        return super().list_files_with_storage(
            android_path,
            use_root=use_root,
            cancel_event=cancel_event,
        )

    def storage_info(
        self,
        android_path: str,
        use_root: bool = False,
        cancel_event=None,
    ):
        if self._direct_public_storage_shell(android_path, use_root=use_root):
            return _delegate_prepared_raw(
                self,
                "storage_info",
                (android_path,),
                {"use_root": False, "cancel_event": cancel_event},
                cancel_index=2,
            )
        return super().storage_info(
            android_path,
            use_root=use_root,
            cancel_event=cancel_event,
        )

    def storage_volumes(self, use_root: bool = False, cancel_event=None):
        if not use_root and self.verified_uid == 2000:
            return _delegate_prepared_raw(
                self,
                "storage_volumes",
                (),
                {"use_root": False, "cancel_event": cancel_event},
                cancel_index=1,
            )
        return super().storage_volumes(
            use_root=use_root,
            cancel_event=cancel_event,
        )

    def run_public_storage_shell(
        self,
        android_path: str,
        shell_command: str,
        timeout: float | None = 120,
        cancel_event=None,
    ):
        """Run a public-storage data-plane transaction on direct ADB shell.

        File streaming always uses the captured direct ADB transport because a
        Shizuku Activity/UserService cannot preserve ``exec-in`` byte streams.
        The temporary file is therefore created by the captured direct ADB shell
        even when Shizuku itself was started as UID 0.  Finalize and cleanup must
        use that same data plane for both verified Shizuku identities.
        """

        if self.verified_uid not in {0, 2000} or not is_probably_writable_android_path(
            android_path
        ):
            raise RuntimeError(
                "Direct Shizuku storage routing is limited to verified UID 0/2000 "
                "public-storage transactions."
            )
        return _delegate_prepared_raw(
            self,
            "run_shell",
            (shell_command,),
            {"timeout": timeout, "cancel_event": cancel_event},
            cancel_index=2,
        )

    def run_shell(
        self,
        shell_command: str,
        timeout: float | None = 120,
        cancel_event=None,
    ):
        self._manager._raise_if_cancelled(
            self._backend_cancel_event,
            "Shizuku command was cancelled because the selected access mode changed.",
        )
        self._manager._require_selected_backend(
            PrivilegeBackend.SHIZUKU,
            "The selected access mode changed before the Shizuku command completed.",
        )
        self._manager._require_context_current(self.device_context)
        linked_cancel = _LinkedCancellationEvent(
            cancel_event,
            self._backend_cancel_event,
        )
        self._manager._raise_if_cancelled(
            linked_cancel,
            "Shizuku command was cancelled before it started.",
        )
        try:
            result = self._session.execute_shell(
                str(shell_command or ""),
                timeout=timeout,
                cancel_event=linked_cancel,
            )
        except Exception:
            self._reset_backend_if_current()
            raise
        self._manager._raise_if_cancelled(
            self._backend_cancel_event,
            "Shizuku command was cancelled because the selected access mode changed.",
        )
        result = self._prepare_logical_result(result)
        self._record_result(result)
        self._manager._require_selected_backend(
            PrivilegeBackend.SHIZUKU,
            "The selected access mode changed while the Shizuku command was running.",
        )
        self._manager._require_context_current(self.device_context)
        if str(getattr(result, "error_type", "") or "") in SHIZUKU_INFRASTRUCTURE_ERRORS:
            raise RuntimeError(
                str(
                    getattr(result, "status", "")
                    or getattr(result, "stderr", "")
                    or "Shizuku infrastructure failed."
                )
            )
        return result

    def get_device_info(self, serial: str | None = None) -> DeviceInfo:
        """Read device properties through the prepared Android-shell plane."""

        if serial not in (None, "", self.serial):
            raise RuntimeError("A prepared Shizuku ADB client cannot target another serial")
        props = [
            "ro.product.model",
            "ro.product.manufacturer",
            "ro.build.version.release",
            "ro.build.version.sdk",
            "ro.build.characteristics",
        ]
        result = self.run_shell(
            "; ".join(f"getprop {prop}" for prop in props),
            timeout=15,
        )
        values = [line.strip() for line in (result.stdout or "").splitlines()]
        while len(values) < len(props):
            values.append("")
        return DeviceInfo(
            serial=self.serial,
            model=values[0],
            manufacturer=values[1],
            android_version=values[2],
            sdk_version=values[3],
            mode="ADB",
            state="device",
            form_factor=_device_form_factor(values[4]),
        )

    def run_root_shell(
        self,
        shell_command: str,
        timeout: float | None = 120,
        cancel_event=None,
    ):
        if self.verified_uid != 0:
            raise RuntimeError(
                "The prepared Shizuku session has Android shell access only; root UID 0 is required."
            )
        return self.run_shell(
            shell_command,
            timeout=timeout,
            cancel_event=cancel_event,
        )

    def root_available(self, cancel_event=None) -> bool:
        linked_cancel = _LinkedCancellationEvent(
            cancel_event,
            self._backend_cancel_event,
        )
        if linked_cancel.is_set():
            return False
        self._manager._raise_if_cancelled(
            self._backend_cancel_event,
            "Shizuku availability changed with the selected access mode.",
        )
        self._manager._require_selected_backend(
            PrivilegeBackend.SHIZUKU,
            "The selected access mode changed before Shizuku availability was used.",
        )
        self._manager._require_context_current(self.device_context)
        return self.verified_uid == 0

    def root_shell_script(self, _shell_command: str) -> str:
        raise RuntimeError(
            "A Shizuku root command cannot be embedded in a direct ADB stream; "
            "use run_root_shell or an explicit staging workflow."
        )

    def get_package_details_many(
        self,
        package_names: list[str],
        max_workers: int = 4,
        progress_callback=None,
        cancel_event=None,
    ) -> dict[str, dict[str, str]]:
        """Load compact package metadata in bounded Shizuku batches.

        Starting one Activity/UserService request per package is both slow and
        unsafe with the current remove-on-unbind lifecycle.  Extracting only
        the three fields OpenADB consumes also keeps structured output far
        below the Shizuku output cap.
        """

        del max_workers  # Shizuku requests are deliberately serialized.
        packages = [str(package).strip() for package in package_names if str(package).strip()]
        total = len(packages)
        if not packages or (cancel_event is not None and cancel_event.is_set()):
            return {}
        results: dict[str, dict[str, str]] = {}
        completed = 0
        for chunk in self._chunks(packages, self.PACKAGE_DETAILS_CHUNK_SIZE):
            if cancel_event is not None and cancel_event.is_set():
                return {}
            command = self._package_details_script(chunk)
            command_result = self.run_shell(
                command,
                timeout=max(30, 12 * len(chunk)),
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                return {}
            if not bool(getattr(command_result, "success", False)):
                detail = str(
                    getattr(command_result, "status", "")
                    or getattr(command_result, "stderr", "")
                    or "Shizuku package metadata request failed."
                )
                raise RuntimeError(detail)
            parsed = self._parse_package_details(command_result.stdout, chunk)
            for package in chunk:
                details = parsed.get(package, {})
                results[package] = details
                completed += 1
                if progress_callback:
                    progress_callback(completed, total, package, details)
        return results

    def _prepare_logical_result(self, result):
        if result is None or not hasattr(result, "success"):
            raise RuntimeError("Shizuku returned an invalid command result.")
        error_type = str(getattr(result, "error_type", "") or "")
        if error_type in SHIZUKU_INVALIDATING_ERRORS:
            self._reset_backend_if_current()
        if self._result_is_truncated(result):
            result.success = False
            if getattr(result, "exit_code", None) in (None, 0):
                result.exit_code = 1
            result.error_type = "shizuku_output_truncated"
            result.status = (
                "Shizuku output was truncated; OpenADB rejected the incomplete result."
            )
            existing_stderr = str(getattr(result, "stderr", "") or "").strip()
            result.stderr = "\n".join(
                part for part in (existing_stderr, result.status) if part
            )
            result.stdout = ""
        elif not bool(result.success):
            # Shell-only parsers must never consume a partial payload from a
            # failed privileged request as if it were authoritative data.
            result.stdout = ""
        return result

    def _reset_backend_if_current(self) -> bool:
        return self._manager._reset_if_lease_current(
            PrivilegeOperationLease(
                PrivilegeBackend.SHIZUKU,
                self._backend_cancel_event,
            )
        )

    def _record_result(self, result) -> None:
        record = getattr(self.runner, "record_result", None)
        if not callable(record):
            raise TypeError("The bound ADB runner cannot record a Shizuku result safely.")
        record(result)

    @staticmethod
    def _result_is_truncated(result) -> bool:
        if any(
            bool(getattr(result, attribute, False))
            for attribute in ("truncated", "stdout_truncated", "stderr_truncated")
        ):
            return True
        text = "\n".join(
            str(getattr(result, field, "") or "")
            for field in ("status", "stdout", "stderr")
        ).casefold()
        return any(marker in text for marker in _SHIZUKU_TRUNCATION_MARKERS)

    @staticmethod
    def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
        for start in range(0, len(values), max(1, int(size))):
            yield values[start : start + size]

    @staticmethod
    def _package_details_script(package_names: list[str]) -> str:
        from openadb.core.path_utils import shell_quote

        package_args = " ".join(shell_quote(package) for package in package_names)
        return (
            f"for p in {package_args}; do "
            "printf 'OPENADB_PACKAGE:%s\\n' \"$p\"; "
            "dumpsys package \"$p\" 2>/dev/null | sed -n "
            "-e 's/^[[:space:]]*versionName=/OPENADB_VERSION_NAME:/p' "
            "-e 's/^[[:space:]]*versionCode=\\([0-9][0-9]*\\).*/OPENADB_VERSION_CODE:\\1/p' "
            "-e 's/^[[:space:]]*nonLocalizedLabel=/OPENADB_LABEL:/p'; "
            "printf 'OPENADB_END\\n'; done"
        )

    @staticmethod
    def _parse_package_details(
        output: str,
        expected_packages: list[str],
    ) -> dict[str, dict[str, str]]:
        expected = set(expected_packages)
        parsed: dict[str, dict[str, str]] = {}
        current = ""
        for raw_line in str(output or "").splitlines():
            line = raw_line.strip()
            if line.startswith("OPENADB_PACKAGE:"):
                candidate = line.split(":", 1)[1].strip()
                current = candidate if candidate in expected else ""
                if current:
                    parsed.setdefault(current, {})
                continue
            if line == "OPENADB_END":
                current = ""
                continue
            if not current:
                continue
            details = parsed[current]
            if line.startswith("OPENADB_VERSION_NAME:") and "versionName" not in details:
                details["versionName"] = line.split(":", 1)[1].strip()
            elif line.startswith("OPENADB_VERSION_CODE:") and "versionCode" not in details:
                value = line.split(":", 1)[1].strip()
                if value.isdigit():
                    details["versionCode"] = value
            elif line.startswith("OPENADB_LABEL:") and "appLabel" not in details:
                value = line.split(":", 1)[1].strip().strip('"')
                if value and value.casefold() != "null":
                    details["appLabel"] = value
        return parsed


@dataclass(frozen=True, slots=True)
class PrivilegeStatus:
    """Runtime privilege result bound to one immutable device generation."""

    backend: PrivilegeBackend = PrivilegeBackend.STANDARD
    state: str = "unknown"
    uid: int | None = None
    level: str = "standard"
    message: str = "Standard ADB access"
    device_serial: str = ""
    device_generation: int | None = None
    shizuku: ShizukuState | None = None

    @property
    def available(self) -> bool:
        return self.state == "ready"

    @property
    def root(self) -> bool:
        return self.available and self.uid == 0 and self.level == "root"

    @property
    def shell(self) -> bool:
        return self.available and self.uid == 2000 and self.level == "shell"

    @classmethod
    def standard(cls, *, serial: str = "", generation: int | None = None) -> PrivilegeStatus:
        return cls.standard_with_uid(
            2000,
            serial=serial,
            generation=generation,
        )

    @classmethod
    def standard_with_uid(
        cls,
        uid: int | None,
        *,
        serial: str = "",
        generation: int | None = None,
    ) -> PrivilegeStatus:
        direct_root = uid == 0
        ready = uid == 2000
        if direct_root:
            message = (
                "Standard ADB blocked Android shell execution because this adbd is "
                "already running as UID 0. Select Root to acknowledge elevated execution."
            )
        elif ready:
            message = f"Standard ADB shell (UID {uid}; no Root or Shizuku)"
        elif uid is not None:
            message = (
                f"Standard ADB blocked Android shell execution because adbd returned "
                f"unexpected UID {uid}. Standard mode requires Android shell UID 2000."
            )
        else:
            message = "Standard ADB shell identity could not be verified."
        return cls(
            backend=PrivilegeBackend.STANDARD,
            state="ready" if ready else ("blocked" if uid is not None else "unavailable"),
            uid=uid,
            level=(
                "shell"
                if ready
                else ("blocked-root" if direct_root else ("blocked" if uid is not None else "unavailable"))
            ),
            message=message,
            device_serial=serial,
            device_generation=generation,
        )

    @classmethod
    def from_root_check(
        cls,
        available: bool,
        *,
        serial: str = "",
        generation: int | None = None,
    ) -> PrivilegeStatus:
        return cls(
            backend=PrivilegeBackend.ROOT,
            state="ready" if available else "unavailable",
            uid=0 if available else None,
            level="root" if available else "unavailable",
            message=(
                "Existing su/root access is ready (UID 0)."
                if available
                else "Existing su/root access was denied or is unavailable."
            ),
            device_serial=serial,
            device_generation=generation,
        )

    @classmethod
    def from_shizuku(
        cls,
        state: ShizukuState,
        *,
        serial: str = "",
        generation: int | None = None,
    ) -> PrivilegeStatus:
        if state.ready:
            identity = (
                "Shizuku root (UID 0)"
                if state.root
                else "Shizuku shell (UID 2000, not root)"
            )
            detail = str(state.message or "").strip()
            message = f"{identity} is ready."
            if detail and detail.casefold() not in message.casefold():
                message = f"{message} {detail}"
        else:
            message = state.message
        return cls(
            backend=PrivilegeBackend.SHIZUKU,
            state="ready" if state.ready else state.state,
            uid=state.uid,
            level="root" if state.root else ("shell" if state.shell else "unavailable"),
            message=message,
            device_serial=serial,
            device_generation=generation,
            shizuku=state,
        )


class PrivilegeManager:
    """Resolve and cache privilege state without crossing device generations."""

    def __init__(self, adb, settings, device_manager) -> None:
        self.adb = adb
        self.settings = settings
        self.device_manager = device_manager
        self._lock = threading.RLock()
        self._cached: PrivilegeStatus | None = None
        self._cached_verified_at = 0.0
        self._backend_cancel_event = threading.Event()
        self._status_listeners: list[Callable[[PrivilegeStatus | None], None]] = []
        self._invalidation_listeners: list[Callable[[], None]] = []

    @property
    def selected_backend(self) -> PrivilegeBackend:
        return PrivilegeBackend.normalize(
            self.settings.get("privilege_backend", PrivilegeBackend.STANDARD.value)
        )

    def cached_status(self) -> PrivilegeStatus | None:
        with self._lock:
            status = self._cached
        if status is None or not self._status_is_current(status):
            return None
        if status.backend is not self.selected_backend:
            return None
        return status

    def status_is_current(self, status: PrivilegeStatus | None) -> bool:
        """Return whether a queued status still belongs to the active mode/device."""

        return bool(
            status is not None
            and status.backend is self.selected_backend
            and self._status_is_current(status)
        )

    def reset(self) -> None:
        with self._lock:
            stale_backend_event = self._backend_cancel_event
            self._backend_cancel_event = threading.Event()
            changed = self._cached is not None
            self._cached = None
            self._cached_verified_at = 0.0
            listeners = tuple(self._status_listeners) if changed else ()
        stale_backend_event.set()
        self._notify_status_listeners(listeners, None)

    def capture_operation_lease(self) -> PrivilegeOperationLease:
        """Capture the selected backend and its cancellation generation atomically."""

        with self._lock:
            return PrivilegeOperationLease(
                self.selected_backend,
                self._backend_cancel_event,
            )

    def validate_operation_lease(
        self,
        lease: PrivilegeOperationLease,
        message: str = "The selected access mode changed while the operation was running.",
    ) -> None:
        """Public fail-closed validation for non-shell control-plane operations."""

        self._require_operation_lease(lease, message)

    def linked_cancellation_event(
        self,
        lease: PrivilegeOperationLease,
        cancel_event=None,
    ) -> _LinkedCancellationEvent:
        """Link an operation cancellation source to one selected-mode generation."""

        self._require_operation_lease(
            lease,
            "The selected access mode changed before the operation started.",
        )
        return _LinkedCancellationEvent(cancel_event, lease._cancel_event)

    def add_status_listener(
        self,
        callback: Callable[[PrivilegeStatus | None], None],
    ) -> None:
        with self._lock:
            if callback not in self._status_listeners:
                self._status_listeners.append(callback)

    def remove_status_listener(
        self,
        callback: Callable[[PrivilegeStatus | None], None],
    ) -> None:
        with self._lock:
            if callback in self._status_listeners:
                self._status_listeners.remove(callback)

    def add_invalidation_listener(self, callback: Callable[[], None]) -> None:
        """Observe runtime backend invalidation, excluding deliberate resets."""

        with self._lock:
            if callback not in self._invalidation_listeners:
                self._invalidation_listeners.append(callback)

    def remove_invalidation_listener(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback in self._invalidation_listeners:
                self._invalidation_listeners.remove(callback)

    def prepare_adb(
        self,
        context: DeviceContext,
        cancel_event=None,
        privilege_lease: PrivilegeOperationLease | None = None,
    ):
        """Prepare one immutable ADB view for an operation's privilege backend.

        Standard ADB keeps using the original context-bound client. Root and
        Shizuku replace only Android shell execution with a freshly verified,
        operation-scoped backend; the original bound client remains the
        non-recursive host/control/data plane.
        """

        lease = privilege_lease or self.capture_operation_lease()
        self._require_operation_lease(
            lease,
            "The selected access mode changed before privilege preparation started.",
        )
        self._require_context_current(context)
        self._raise_if_cancelled(cancel_event, "Privilege preparation was cancelled.")
        backend_cancel_event = lease._cancel_event
        preparation_cancel = _LinkedCancellationEvent(
            cancel_event,
            backend_cancel_event,
        )
        direct = self._immutable_bound_adb(context)
        self._require_operation_lease(
            lease,
            "The selected access mode changed while ADB was being bound.",
        )
        self._require_context_current(context)
        selected = lease.backend
        if selected is PrivilegeBackend.STANDARD:
            direct_uid = self._probe_direct_uid(
                direct,
                cancel_event=preparation_cancel,
            )
            self._raise_if_cancelled(
                preparation_cancel,
                "Standard ADB preparation was cancelled.",
            )
            self._require_operation_lease(
                lease,
                "The selected access mode changed while Standard ADB was being prepared.",
            )
            status = PrivilegeStatus.standard_with_uid(
                direct_uid,
                serial=context.serial,
                generation=context.generation,
            )
            self._cache_if_current(status, privilege_lease=lease)
            if not status.available:
                raise RuntimeError(status.message)
            self._mark_direct_backend(direct, PrivilegeBackend.STANDARD)
            direct.verified_uid = direct_uid
            return direct
        if selected is PrivilegeBackend.ROOT:
            return self._prepare_root_adb(
                direct,
                context,
                cancel_event=preparation_cancel,
                backend_cancel_event=backend_cancel_event,
                privilege_lease=lease,
            )

        # Shizuku is an Android app/service and cannot exist in Recovery,
        # Fastboot, Offline, Unauthorized, or Sideload modes. Do not silently
        # execute an operation through Standard ADB when Shizuku was selected.
        if context.mode != "ADB":
            self._require_operation_lease(
                lease,
                "The selected access mode changed while Shizuku was being prepared.",
            )
            self._require_context_current(context)
            raise RuntimeError(
                f"Shizuku is unavailable while the device is in {context.mode} mode. "
                "Select Standard ADB or Root explicitly to run this operation."
            )

        cached = self.cached_status()
        cached_is_recent = self._cached_status_is_recent(
            cached,
            SHIZUKU_VERIFIED_STATE_TTL_SECONDS,
        )
        cached_uid = (
            cached.uid
            if cached is not None
            and cached_is_recent
            and cached.backend is PrivilegeBackend.SHIZUKU
            and cached.available
            and cached.uid in {0, 2000}
            else None
        )
        client = ShizukuClient(
            direct,
            self.settings,
            temp_folder=context.temp_path,
        )
        prepare_session = getattr(client, "prepare_session", None)
        if not callable(prepare_session):
            self._reset_if_lease_current(lease)
            raise TypeError(
                "This OpenADB Bridge integration does not support prepared Shizuku sessions."
            )
        cached_state = (
            cached.shizuku
            if cached_uid is not None
            and cached is not None
            and isinstance(cached.shizuku, ShizukuState)
            and cached.shizuku.ready
            and cached.shizuku.uid == cached_uid
            else None
        )
        used_cached_state = False
        session_from_verified_state = getattr(
            client,
            "session_from_verified_state",
            None,
        )
        try:
            if cached_state is not None and callable(session_from_verified_state):
                session = session_from_verified_state(
                    cached_state,
                    expected_uid=cached_uid,
                )
                used_cached_state = True
            else:
                session = prepare_session(cancel_event=preparation_cancel)
        except Exception:
            if not preparation_cancel.is_set():
                self._reset_if_lease_current(lease)
            raise
        self._raise_if_cancelled(preparation_cancel, "Shizuku preparation was cancelled.")
        self._require_context_current(context)
        self._require_operation_lease(
            lease,
            "The selected privilege backend changed while Shizuku was being prepared.",
        )

        state, verified_uid, ready = self._prepared_session_identity(session)
        if state is not None and state.state == "cancelled":
            raise RuntimeError(state.message or "Shizuku preparation was cancelled.")
        if not ready or verified_uid not in {0, 2000}:
            self._reset_if_lease_current(lease)
            detail = str(getattr(state, "message", "") or "").strip()
            raise RuntimeError(detail or "Shizuku is unavailable or permission was not granted.")
        if cached_uid is not None and verified_uid != cached_uid:
            self._reset_if_lease_current(lease)
            raise RuntimeError(
                "Shizuku identity changed while the operation was being prepared; "
                "review the new UID before retrying."
            )
        if not callable(getattr(session, "execute_shell", None)):
            self._reset_if_lease_current(lease)
            raise TypeError("The prepared Shizuku session cannot execute shell commands.")

        if isinstance(state, ShizukuState):
            status = PrivilegeStatus.from_shizuku(
                state,
                serial=context.serial,
                generation=context.generation,
            )
        else:
            status = PrivilegeStatus(
                backend=PrivilegeBackend.SHIZUKU,
                state="ready",
                uid=verified_uid,
                level="root" if verified_uid == 0 else "shell",
                message=(
                    "Shizuku root (UID 0) is ready."
                    if verified_uid == 0
                    else "Shizuku shell (UID 2000, not root) is ready."
                ),
                device_serial=context.serial,
                device_generation=context.generation,
            )
        self._raise_if_cancelled(preparation_cancel, "Shizuku preparation was cancelled.")
        self._require_context_current(context)
        if not used_cached_state:
            self._cache_if_current(status, privilege_lease=lease)
        self._require_context_current(context)
        self._require_operation_lease(
            lease,
            "The selected privilege backend changed while Shizuku was being prepared.",
        )
        prepared = ShizukuAwareADBClient(
            direct,
            session,
            self,
            context,
            verified_uid,
            backend_cancel_event,
        )
        self._require_context_current(context)
        return prepared

    def _prepare_root_adb(
        self,
        direct,
        context: DeviceContext,
        *,
        cancel_event=None,
        backend_cancel_event: threading.Event,
        privilege_lease: PrivilegeOperationLease,
    ):
        """Verify one direct-adbd or su route and pin it to this operation."""

        if context.mode not in {"ADB", "Recovery"}:
            self._mark_direct_fallback(
                direct,
                PrivilegeBackend.ROOT,
                f"Root shell is unavailable while the device is in {context.mode} mode; "
                "this operation uses the captured direct ADB transport.",
            )
            self._require_context_current(context)
            return direct

        strategy: RootExecutionStrategy | None = None
        direct_uid: int | None = None
        try:
            direct_result = direct.run_shell(
                "id -u",
                timeout=8,
                cancel_event=cancel_event,
            )
            self._raise_if_cancelled(cancel_event, "Root preparation was cancelled.")
            self._require_context_current(context)
            self._require_operation_lease(
                privilege_lease,
                "The selected privilege backend changed while root access was being prepared.",
            )
            direct_uid = self._uid_from_result(direct_result)
            if self._result_has_root_uid(direct_result):
                strategy = RootExecutionStrategy.DIRECT
            else:
                su_result = direct.run_root_shell(
                    "id -u",
                    timeout=12,
                    cancel_event=cancel_event,
                )
                self._raise_if_cancelled(cancel_event, "Root preparation was cancelled.")
                self._require_context_current(context)
                self._require_operation_lease(
                    privilege_lease,
                    "The selected privilege backend changed while root access was being prepared.",
                )
                if self._result_has_root_uid(su_result):
                    strategy = RootExecutionStrategy.SU
        except Exception:
            if cancel_event is None or not cancel_event.is_set():
                self._reset_if_lease_current(privilege_lease)
            raise

        status = PrivilegeStatus.from_root_check(
            strategy is not None,
            serial=context.serial,
            generation=context.generation,
        )
        self._raise_if_cancelled(cancel_event, "Root preparation was cancelled.")
        self._require_context_current(context)
        self._require_operation_lease(
            privilege_lease,
            "The selected privilege backend changed while root access was being prepared.",
        )
        self._cache_if_current(status, privilege_lease=privilege_lease)
        self._require_context_current(context)
        self._require_operation_lease(
            privilege_lease,
            "The selected privilege backend changed while root access was being prepared.",
        )
        if strategy is None:
            fallback_status = PrivilegeStatus.standard_with_uid(
                direct_uid,
                serial=context.serial,
                generation=context.generation,
            )
            if not fallback_status.available:
                raise RuntimeError(
                    "Root access is unavailable and OpenADB could not safely fall back to "
                    f"Standard ADB. {fallback_status.message}"
                )
            self._mark_direct_fallback(
                direct,
                PrivilegeBackend.ROOT,
                "Root access is unavailable for this device; this operation uses "
                "the captured standard ADB shell.",
            )
            direct.verified_uid = direct_uid
            return direct

        prepared = RootAwareADBClient(
            direct,
            self,
            context,
            strategy,
            backend_cancel_event,
        )
        self._require_context_current(context)
        return prepared

    def check(
        self,
        context: DeviceContext,
        *,
        backend: PrivilegeBackend | str | None = None,
        cancel_event=None,
        privilege_lease: PrivilegeOperationLease | None = None,
    ) -> PrivilegeStatus:
        selected = PrivilegeBackend.normalize(backend or self.selected_backend)
        lease = privilege_lease or self.capture_operation_lease()
        if selected is not lease.backend:
            raise RuntimeError(
                "The requested access check does not match the captured access mode."
            )
        self._require_operation_lease(
            lease,
            "The selected access mode changed before the access check started.",
        )
        linked_cancel = _LinkedCancellationEvent(cancel_event, lease._cancel_event)
        bound = self._bound_adb(context)
        if selected is PrivilegeBackend.STANDARD:
            status = PrivilegeStatus.standard_with_uid(
                self._probe_direct_uid(
                    bound,
                    cancel_event=linked_cancel,
                    fallback_uid=2000,
                ),
                serial=context.serial,
                generation=context.generation,
            )
        elif selected is PrivilegeBackend.ROOT:
            status = PrivilegeStatus.from_root_check(
                bool(bound.root_available(cancel_event=linked_cancel)),
                serial=context.serial,
                generation=context.generation,
            )
        else:
            state = ShizukuClient(
                bound,
                self.settings,
                temp_folder=context.temp_path,
            ).check_status(cancel_event=linked_cancel)
            status = PrivilegeStatus.from_shizuku(
                state,
                serial=context.serial,
                generation=context.generation,
            )
        if lease.cancelled:
            raise RuntimeError(
                "The selected access mode changed while the access check was running."
            )
        if cancel_event is not None and cancel_event.is_set():
            return status
        self._require_operation_lease(
            lease,
            "The selected access mode changed while the access check was running.",
        )
        if not linked_cancel.is_set():
            self._cache_if_current(status, privilege_lease=lease)
        return status

    def request_shizuku(
        self,
        context: DeviceContext,
        *,
        cancel_event=None,
        privilege_lease: PrivilegeOperationLease | None = None,
    ) -> PrivilegeStatus:
        lease = privilege_lease or self.capture_operation_lease()
        if lease.backend is not PrivilegeBackend.SHIZUKU:
            raise RuntimeError("Select Shizuku before requesting its Android permission.")
        self._require_operation_lease(
            lease,
            "The selected access mode changed before the Shizuku permission request started.",
        )
        self._require_context_current(context)
        linked_cancel = _LinkedCancellationEvent(cancel_event, lease._cancel_event)
        bound = self._bound_adb(context)
        state = ShizukuClient(
            bound,
            self.settings,
            temp_folder=context.temp_path,
        ).request_permission(cancel_event=linked_cancel)
        status = PrivilegeStatus.from_shizuku(
            state,
            serial=context.serial,
            generation=context.generation,
        )
        self._require_operation_lease(
            lease,
            "The selected access mode changed while Shizuku permission was being requested.",
        )
        self._require_context_current(context)
        if not linked_cancel.is_set():
            self._cache_if_current(status, privilege_lease=lease)
        return status

    def request_and_check_shizuku(
        self,
        context: DeviceContext,
        *,
        cancel_event=None,
        privilege_lease: PrivilegeOperationLease | None = None,
    ) -> PrivilegeStatus:
        """Request Shizuku permission and cache only the verified final state."""

        lease = privilege_lease or self.capture_operation_lease()
        if lease.backend is not PrivilegeBackend.SHIZUKU:
            raise RuntimeError("Select Shizuku before requesting its Android permission.")
        self._require_operation_lease(
            lease,
            "The selected access mode changed before Shizuku setup started.",
        )
        self._require_context_current(context)
        linked_cancel = _LinkedCancellationEvent(cancel_event, lease._cancel_event)
        state = ShizukuClient(
            self._bound_adb(context),
            self.settings,
            temp_folder=context.temp_path,
        ).request_permission_then_check(cancel_event=linked_cancel)
        status = PrivilegeStatus.from_shizuku(
            state,
            serial=context.serial,
            generation=context.generation,
        )
        self._require_operation_lease(
            lease,
            "The selected access mode changed while Shizuku setup was running.",
        )
        self._require_context_current(context)
        if not linked_cancel.is_set():
            self._cache_if_current(status, privilege_lease=lease)
        return status

    def execute_shizuku_shell(
        self,
        context: DeviceContext,
        command: str,
        *,
        timeout: float | None = 120,
        expected_uid: int | None = None,
        cancel_event=None,
        privilege_lease: PrivilegeOperationLease | None = None,
    ):
        lease = privilege_lease or self.capture_operation_lease()
        if lease.backend is not PrivilegeBackend.SHIZUKU:
            raise RuntimeError("Select Shizuku before executing a Shizuku shell command.")
        self._require_operation_lease(
            lease,
            "The selected access mode changed before the Shizuku command started.",
        )
        self._require_context_current(context)
        linked_cancel = _LinkedCancellationEvent(cancel_event, lease._cancel_event)
        bound = self._bound_adb(context)
        status = self.cached_status()
        verified_uid = (
            expected_uid
            if (
                expected_uid in {0, 2000}
                and status is not None
                and status.backend is PrivilegeBackend.SHIZUKU
                and status.available
                and status.uid == expected_uid
                and self._status_is_current(status)
            )
            else None
        )
        result = ShizukuClient(
            bound,
            self.settings,
            temp_folder=context.temp_path,
        ).execute_shell(
            command,
            timeout=timeout,
            expected_uid=verified_uid,
            cancel_event=linked_cancel,
        )
        self._require_operation_lease(
            lease,
            "The selected access mode changed while the Shizuku command was running.",
        )
        self._require_context_current(context)
        if result.error_type in SHIZUKU_INVALIDATING_ERRORS:
            self._reset_if_lease_current(lease)
        return result

    def open_shizuku_manager(
        self,
        context: DeviceContext,
        *,
        cancel_event=None,
        privilege_lease: PrivilegeOperationLease | None = None,
    ):
        lease = privilege_lease or self.capture_operation_lease()
        if lease.backend is not PrivilegeBackend.SHIZUKU:
            raise RuntimeError("Select Shizuku before opening its Android manager.")
        self._require_operation_lease(
            lease,
            "The selected access mode changed before Shizuku could be opened.",
        )
        self._require_context_current(context)
        linked_cancel = _LinkedCancellationEvent(cancel_event, lease._cancel_event)
        result = ShizukuClient(
            self._bound_adb(context),
            self.settings,
            temp_folder=context.temp_path,
        ).open_manager(cancel_event=linked_cancel)
        self._require_operation_lease(
            lease,
            "The selected access mode changed while Shizuku was being opened.",
        )
        self._require_context_current(context)
        return result

    def _bound_adb(self, context: DeviceContext):
        if hasattr(self.adb, "for_context"):
            return self.adb.for_context(context)
        return self.adb

    def _immutable_bound_adb(self, context: DeviceContext):
        binder = getattr(self.adb, "for_context", None)
        if not callable(binder):
            raise DeviceContextUnavailable(
                "This ADB client cannot be immutably bound to the active device context."
            )
        bound = binder(context)
        if bound is self.adb:
            raise DeviceContextUnavailable(
                "ADB context binding returned the mutable source client."
            )
        bound_context = getattr(bound, "device_context", None)
        if bound_context != context:
            raise DeviceContextUnavailable(
                "ADB context binding did not preserve the complete device identity."
            )
        if str(getattr(bound, "serial", "") or "") != context.serial:
            raise DeviceContextUnavailable(
                "ADB context binding selected a different device serial."
            )
        return bound

    def _require_context_current(self, context: DeviceContext) -> None:
        require_current = getattr(self.device_manager, "require_current", None)
        if callable(require_current):
            require_current(context)
            return
        is_current = getattr(self.device_manager, "is_context_current", None)
        if callable(is_current):
            if not is_current(context):
                raise StaleDeviceContext(
                    "The active device or profile changed while the operation was running"
                )
            return
        active = getattr(self.device_manager, "active", None)
        active_serial = str(getattr(active, "serial", "") or "")
        generation = getattr(self.device_manager, "current_generation", None)
        if (
            not active_serial
            or active_serial != context.serial
            or (generation is not None and generation != context.generation)
        ):
            raise StaleDeviceContext(
                "The active device or profile changed while the operation was running"
            )

    @staticmethod
    def _prepared_session_identity(
        session: Any,
    ) -> tuple[ShizukuState | None, int | None, bool]:
        if session is None:
            return None, None, False
        state = getattr(session, "state", None)
        uid = getattr(session, "expected_uid", None)
        if uid not in {0, 2000}:
            uid = getattr(session, "uid", None)
        if uid not in {0, 2000} and state is not None:
            uid = getattr(state, "uid", None)
        session_ready = getattr(session, "ready", None)
        if callable(session_ready):
            session_ready = session_ready()
        ready = bool(session_ready)
        if session_ready is None and state is not None:
            ready = bool(getattr(state, "ready", False))
        return (
            state if isinstance(state, ShizukuState) else None,
            uid if uid in {0, 2000} else None,
            ready,
        )

    @staticmethod
    def _mark_direct_backend(direct, backend: PrivilegeBackend) -> None:
        direct.requested_privilege_backend = backend
        direct.effective_privilege_backend = backend
        direct.privilege_fallback_message = ""

    @staticmethod
    def _mark_direct_fallback(
        direct,
        requested: PrivilegeBackend,
        message: str,
    ) -> None:
        direct.requested_privilege_backend = requested
        direct.effective_privilege_backend = PrivilegeBackend.STANDARD
        direct.privilege_fallback_message = str(message or "").strip()

    def _require_selected_backend(
        self,
        expected: PrivilegeBackend,
        message: str,
    ) -> None:
        if self.selected_backend is not expected:
            raise RuntimeError(message)

    def _require_operation_lease(
        self,
        lease: PrivilegeOperationLease,
        message: str,
    ) -> None:
        if not isinstance(lease, PrivilegeOperationLease):
            raise TypeError("A valid privilege operation lease is required.")
        with self._lock:
            current_event = self._backend_cancel_event
        if (
            lease._cancel_event is not current_event
            or lease.cancelled
            or self.selected_backend is not lease.backend
        ):
            raise RuntimeError(message)

    def _reset_if_lease_current(self, lease: PrivilegeOperationLease) -> bool:
        """Invalidate only the backend generation owned by ``lease``.

        A worker from an older mode must never reset a newer backend or erase
        its cached status after the user changes the global selector.
        """

        with self._lock:
            if (
                lease._cancel_event is not self._backend_cancel_event
                or lease.cancelled
                or self.selected_backend is not lease.backend
            ):
                return False
            stale_backend_event = self._backend_cancel_event
            self._backend_cancel_event = threading.Event()
            changed = self._cached is not None
            self._cached = None
            listeners = tuple(self._status_listeners) if changed else ()
            invalidation_listeners = tuple(self._invalidation_listeners)
        stale_backend_event.set()
        self._notify_status_listeners(listeners, None)
        self._notify_invalidation_listeners(invalidation_listeners)
        return True

    @staticmethod
    def _uid_from_result(result: Any) -> int | None:
        if not bool(getattr(result, "success", False)):
            return None
        for line in str(getattr(result, "stdout", "") or "").splitlines():
            value = line.strip()
            if value.isdigit():
                return int(value)
        return None

    def _probe_direct_uid(
        self,
        adb,
        *,
        cancel_event=None,
        fallback_uid: int | None = None,
    ) -> int | None:
        run_shell = getattr(adb, "run_shell", None)
        if not callable(run_shell):
            return fallback_uid
        result = run_shell(
            "id -u",
            timeout=8,
            cancel_event=cancel_event,
        )
        return self._uid_from_result(result)

    @staticmethod
    def _result_has_root_uid(result: Any) -> bool:
        if not bool(getattr(result, "success", False)):
            return False
        return any(
            line.strip() == "0"
            for line in str(getattr(result, "stdout", "") or "").splitlines()
        )

    @staticmethod
    def _raise_if_cancelled(cancel_event, message: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError(message)

    def _cache_if_current(
        self,
        status: PrivilegeStatus,
        *,
        privilege_lease: PrivilegeOperationLease | None = None,
    ) -> None:
        if status.state == "cancelled":
            return
        with self._lock:
            if status.backend is not self.selected_backend:
                return
            if privilege_lease is not None and (
                privilege_lease._cancel_event is not self._backend_cancel_event
                or privilege_lease.cancelled
                or privilege_lease.backend is not status.backend
            ):
                return
            if not self._status_is_current(status):
                return
            changed = self._cached != status
            self._cached = status
            self._cached_verified_at = time.monotonic()
            listeners = tuple(self._status_listeners) if changed else ()
        self._notify_status_listeners(listeners, status)

    def _cached_status_is_recent(
        self,
        status: PrivilegeStatus | None,
        ttl_seconds: float,
    ) -> bool:
        if status is None:
            return False
        with self._lock:
            return bool(
                self._cached is status
                and self._cached_verified_at > 0
                and time.monotonic() - self._cached_verified_at
                <= max(0.0, float(ttl_seconds))
            )

    @staticmethod
    def _notify_status_listeners(
        listeners: tuple[Callable[[PrivilegeStatus | None], None], ...],
        status: PrivilegeStatus | None,
    ) -> None:
        for callback in listeners:
            try:
                callback(status)
            except Exception:
                LOGGER.exception("Privilege status listener failed")

    @staticmethod
    def _notify_invalidation_listeners(
        listeners: tuple[Callable[[], None], ...],
    ) -> None:
        for callback in listeners:
            try:
                callback()
            except Exception:
                LOGGER.exception("Privilege invalidation listener failed")

    def _status_is_current(self, status: PrivilegeStatus) -> bool:
        active = getattr(self.device_manager, "active", None)
        if active is None or status.device_serial != str(getattr(active, "serial", "") or ""):
            return False
        current_generation = getattr(self.device_manager, "current_generation", None)
        if status.device_generation is not None and current_generation is not None:
            return status.device_generation == current_generation
        return True
