from __future__ import annotations

import hashlib
import hmac
import secrets
import socket
import struct
import threading
import time
import uuid
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

from openadb.core.acbridge import ACBridgeClient
from openadb.core.path_utils import ensure_dir, shell_quote
from openadb.core.p2p_parallelism import (
    AUTO_PARALLELISM_MODE,
    MANUAL_PARALLELISM_MODE,
    P2P_MAX_PARALLELISM as P2P_MAX_PARALLELISM,
    choose_p2p_parallelism,
    normalize_p2p_parallelism_preference,
)


P2P_MAGIC = b"OADBP2P2"
P2P_REQUEST_TRANSCRIPT_CONTEXT = b"OpenADB-P2P-request-v2\x00"
P2P_ENTRY_CONTROL_CONTEXT = b"OpenADB-P2P-entry-v2\x00"
P2P_RESPONSE_CONTEXT = b"OpenADB-P2P-response-v2\x00"
P2P_AUTH_TAG_SIZE = hashlib.sha256().digest_size
P2P_TRANSPORT = "acbridge_p2p"
ADB_TRANSPORT = "adb"
P2P_BUFFER_SIZE = 1024 * 1024
P2P_MAX_ENTRIES = 100_000
P2P_CONNECT_ATTEMPT_TIMEOUT = 0.5
P2P_BOOTSTRAP_REQUEST_PREFIX = "p2p_request_"
P2P_BOOTSTRAP_STATUS_PREFIX = "p2p_status_"
P2P_BOOTSTRAP_CANCEL_PREFIX = "p2p_cancel_"


class P2PTransferError(RuntimeError):
    """A safe, user-facing ACBridge peer transfer failure."""


class _CombinedCancelEvent:
    def __init__(self, *events) -> None:
        self.events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return any(event.is_set() for event in self.events)


class _CancellableSocketStream:
    """Minimal socket stream with cooperative cancellation and idle timeout."""

    def __init__(self, sock: socket.socket, cancel_event, idle_timeout: float) -> None:
        self._socket = sock
        self._cancel_event = cancel_event
        self._idle_timeout = max(5.0, float(idle_timeout))
        self._last_progress = time.monotonic()
        self._closed = False
        self._socket.settimeout(0.25)

    def _check_ready(self) -> None:
        if self._closed:
            raise OSError("P2P socket stream is closed")
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise P2PTransferError("P2P transfer cancelled by user.")
        if time.monotonic() - self._last_progress > self._idle_timeout:
            raise TimeoutError(
                "ACBridge P2P connection timed out while waiting for network data"
            )

    def write(self, data: bytes | bytearray | memoryview) -> int:
        view = memoryview(data)
        total = len(view)
        sent = 0
        while sent < total:
            self._check_ready()
            try:
                count = self._socket.send(view[sent:])
            except socket.timeout:
                continue
            if count <= 0:
                raise EOFError("ACBridge closed the P2P connection during upload")
            sent += count
            self._last_progress = time.monotonic()
        return total

    def read(self, size: int) -> bytes:
        while True:
            self._check_ready()
            try:
                chunk = self._socket.recv(size)
            except socket.timeout:
                continue
            if chunk:
                self._last_progress = time.monotonic()
            return chunk

    def flush(self) -> None:
        self._check_ready()

    def close(self) -> None:
        self._closed = True


@dataclass(slots=True, frozen=True)
class P2PEntry:
    source: Path | None
    relative_path: str
    size: int
    is_directory: bool


@dataclass(slots=True, frozen=True)
class P2PSession:
    host: str
    port: int
    token: str = field(repr=False)
    expires_at_ms: int
    # Public control-file correlation id, not an authentication credential.
    # It is still omitted from repr so diagnostics expose no one-shot metadata.
    session_id: str = field(repr=False)


@dataclass(slots=True, frozen=True)
class P2PTransferResult:
    success: bool
    message: str
    bytes_sent: int
    files_sent: int
    entries_sent: int


class ACBridgeP2PClient:
    """Send PC files directly over the LAN into ACBridge's SAF grant.

    ADB remains the control plane: it installs the bundled bridge, streams a
    secret-bearing request into ACBridge's private files directory, starts the
    foreground service with a public correlation id, and reads authenticated
    READY metadata in memory. File bytes never pass through ADB.
    """

    SERVICE = f"{ACBridgeClient.PACKAGE}/.P2PTransferService"

    def __init__(
        self, bridge: ACBridgeClient, temp_folder: str | Path | None = None
    ) -> None:
        self.bridge = bridge
        self.adb = bridge.adb
        self.settings = bridge.settings
        self._temp_folder = (
            Path(temp_folder).expanduser() if temp_folder is not None else None
        )
        self._session_prepare_lock = threading.Lock()

    def upload(
        self,
        local_paths: Iterable[str | Path],
        android_destination: str,
        *,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
        connect_timeout: float = 15.0,
        session_timeout: int = 120,
        parallelism: int | None = 1,
        parallelism_mode: str | None = None,
    ) -> P2PTransferResult:
        entries = collect_p2p_entries(local_paths, cancel_event=cancel_event)
        return self.upload_entries(
            entries,
            android_destination,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            connect_timeout=connect_timeout,
            session_timeout=session_timeout,
            parallelism=parallelism,
            parallelism_mode=parallelism_mode,
        )

    def upload_entries(
        self,
        entries: Iterable[P2PEntry],
        android_destination: str,
        *,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
        connect_timeout: float = 15.0,
        session_timeout: int = 120,
        parallelism: int | None = 1,
        parallelism_mode: str | None = None,
    ) -> P2PTransferResult:
        entries = list(entries)
        if not entries:
            raise P2PTransferError("No local files were selected for P2P transfer.")
        total_bytes = sum(entry.size for entry in entries if not entry.is_directory)
        total_files = sum(1 for entry in entries if not entry.is_directory)
        largest_file_bytes = max(
            (entry.size for entry in entries if not entry.is_directory),
            default=0,
        )
        preference = normalize_p2p_parallelism_preference(
            MANUAL_PARALLELISM_MODE if parallelism_mode is None else parallelism_mode,
            parallelism,
        )
        selected_parallelism = choose_p2p_parallelism(
            total_files,
            total_bytes,
            largest_file_bytes,
            preference.mode,
            preference.manual_value,
        )
        selection_label = (
            "Auto" if preference.mode == AUTO_PARALLELISM_MODE else "Manual"
        )
        if total_files:
            selection_message = (
                f"{selection_label} selected {selected_parallelism} streams "
                f"for {total_files} files"
            )
        else:
            selection_message = "One P2P stream selected for directory entries"
        self._emit(
            progress_callback,
            {
                "type": "plan",
                "title": "ACBridge P2P transfer started",
                "direction": "PC → Android",
                "total_files": total_files,
                "total_bytes": total_bytes,
                "destination": android_destination,
                "parallelism": selected_parallelism,
                "parallelism_mode": preference.mode,
                "parallelism_message": selection_message,
                "message": (
                    "Platform Tools is preparing a one-time ACBridge session. "
                    "File data will travel directly over the local network and be written through Android SAF access."
                ),
            },
        )
        self._emit(
            progress_callback,
            {
                "type": "progress",
                "total_files": total_files,
                "total_bytes": total_bytes,
                "done_files": 0,
                "done_bytes": 0,
                "parallelism": selected_parallelism,
                "parallelism_mode": preference.mode,
                "message": selection_message,
                "activity": selection_message,
            },
        )
        self._check_cancelled(cancel_event)
        if selected_parallelism <= 1 or total_files <= 1:
            return self._upload_entry_batch(
                entries,
                android_destination,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                connect_timeout=connect_timeout,
                session_timeout=session_timeout,
            )
        return self._upload_parallel_entries(
            entries,
            android_destination,
            parallelism=selected_parallelism,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            connect_timeout=connect_timeout,
            session_timeout=session_timeout,
        )

    def _upload_entry_batch(
        self,
        entries: list[P2PEntry],
        android_destination: str,
        *,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
        connect_timeout: float = 15.0,
        session_timeout: int = 120,
    ) -> P2PTransferResult:
        total_bytes = sum(entry.size for entry in entries if not entry.is_directory)
        total_files = sum(1 for entry in entries if not entry.is_directory)
        session = self._prepare_session(
            android_destination,
            timeout_seconds=session_timeout,
            connect_timeout=connect_timeout,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        sent_bytes = 0
        sent_files = 0
        started = time.monotonic()
        sock = None
        completed = False
        remote_terminal = False
        try:
            sock = self._connect(
                session, connect_timeout=connect_timeout, cancel_event=cancel_event
            )
            stream = _CancellableSocketStream(
                sock,
                cancel_event,
                idle_timeout=float(session_timeout),
            )
            try:
                stream.write(P2P_MAGIC)
                session_key = bytes.fromhex(session.token)
                stream.write(hmac.new(session_key, P2P_MAGIC, hashlib.sha256).digest())
                request_transcript = hmac.new(
                    session_key,
                    P2P_REQUEST_TRANSCRIPT_CONTEXT,
                    hashlib.sha256,
                )
                _write_authenticated(
                    stream,
                    request_transcript,
                    struct.pack(">I", len(entries)),
                )
                for entry_index, entry in enumerate(entries):
                    self._check_cancelled(cancel_event)
                    self._emit(
                        progress_callback,
                        {
                            "type": "file_start",
                            "current_file": entry.relative_path,
                            "message": f"P2P: {entry.relative_path}",
                        },
                    )
                    kind_frame = b"\x00" if entry.is_directory else b"\x01"
                    path_frame = _text_frame(entry.relative_path)
                    size_frame = (
                        b"" if entry.is_directory else struct.pack(">Q", entry.size)
                    )
                    control_frame = kind_frame + path_frame + size_frame
                    control_tag = hmac.new(
                        session_key,
                        P2P_ENTRY_CONTROL_CONTEXT
                        + struct.pack(">I", entry_index)
                        + control_frame,
                        hashlib.sha256,
                    ).digest()
                    _write_authenticated(
                        stream,
                        request_transcript,
                        control_frame,
                    )
                    _write_authenticated(
                        stream,
                        request_transcript,
                        control_tag,
                    )
                    if entry.is_directory:
                        continue
                    digest = hashlib.sha256()
                    authenticator = hmac.new(session_key, digestmod=hashlib.sha256)
                    authenticator.update(entry.relative_path.encode("utf-8"))
                    authenticator.update(b"\x00")
                    authenticator.update(size_frame)
                    assert entry.source is not None
                    with entry.source.open("rb") as source_file:
                        remaining = entry.size
                        while remaining:
                            self._check_cancelled(cancel_event)
                            chunk = source_file.read(min(P2P_BUFFER_SIZE, remaining))
                            if not chunk:
                                raise P2PTransferError(
                                    f"Local file changed or became unreadable during transfer: {entry.source}"
                                )
                            stream.write(chunk)
                            digest.update(chunk)
                            authenticator.update(chunk)
                            sent_bytes += len(chunk)
                            remaining -= len(chunk)
                            self._emit(
                                progress_callback,
                                {
                                    "type": "progress",
                                    "done_bytes": sent_bytes,
                                    "total_bytes": total_bytes,
                                    "done_files": sent_files,
                                    "total_files": total_files,
                                    "current_file": entry.relative_path,
                                    "speed": _speed_text(sent_bytes, started),
                                    "activity": "Direct ACBridge P2P upload",
                                },
                            )
                        if (
                            source_file.read(1)
                            or entry.source.stat().st_size != entry.size
                        ):
                            raise P2PTransferError(
                                "Local file grew or changed size during transfer: "
                                f"{entry.source}"
                            )
                    _write_authenticated(
                        stream,
                        request_transcript,
                        digest.digest(),
                    )
                    _write_authenticated(
                        stream,
                        request_transcript,
                        authenticator.digest(),
                    )
                    stream.flush()
                    sent_files += 1
                    self._emit(
                        progress_callback,
                        {
                            "type": "progress",
                            "done_bytes": sent_bytes,
                            "total_bytes": total_bytes,
                            "done_files": sent_files,
                            "total_files": total_files,
                            "current_file": entry.relative_path,
                            "speed": _speed_text(sent_bytes, started),
                            "activity": "Direct ACBridge P2P upload",
                        },
                    )
                request_tag = request_transcript.digest()
                stream.write(request_tag)
                stream.flush()
                success, message = _read_authenticated_response(
                    stream,
                    session_key=session_key,
                    request_tag=request_tag,
                    expected_entries=len(entries),
                    expected_files=total_files,
                    expected_bytes=total_bytes,
                )
                if not success:
                    raise P2PTransferError(message or "ACBridge rejected the P2P upload.")
                completed = True
            finally:
                stream.close()
        except P2PTransferError as exc:
            if "cancel" in str(exc).lower():
                raise
            remote_error = self._fetch_session_error_safely(
                session.session_id,
                cancel_event=cancel_event,
            )
            if remote_error:
                remote_terminal = True
                raise P2PTransferError(remote_error) from exc
            raise
        except (OSError, EOFError, ValueError) as exc:
            remote_error = self._fetch_session_error_safely(
                session.session_id,
                cancel_event=cancel_event,
            )
            remote_terminal = bool(remote_error)
            raise P2PTransferError(
                remote_error or _friendly_network_error(exc)
            ) from exc
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self._cleanup_session_files_safely(
                session.session_id,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                signal_cancel=not (completed or remote_terminal),
            )

        return P2PTransferResult(True, message, sent_bytes, sent_files, len(entries))

    def _upload_parallel_entries(
        self,
        entries: list[P2PEntry],
        android_destination: str,
        *,
        parallelism: int,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
        connect_timeout: float,
        session_timeout: int,
    ) -> P2PTransferResult:
        directories = [entry for entry in entries if entry.is_directory]
        files = [entry for entry in entries if not entry.is_directory]

        worker_count = min(parallelism, len(files))
        batches: list[list[P2PEntry]] = [[] for _index in range(worker_count)]
        batch_sizes = [0] * worker_count
        for entry in sorted(files, key=lambda item: item.size, reverse=True):
            target = min(range(worker_count), key=batch_sizes.__getitem__)
            batches[target].append(entry)
            batch_sizes[target] += entry.size
        # Directory entries share one of the already-selected file sessions,
        # so an N-stream plan opens exactly N data sessions rather than N+1.
        # ACBridge serializes directory lookup/creation across those sessions;
        # file batches may therefore safely create a parent first.
        if directories:
            batches[0] = [*directories, *batches[0]]

        progress_lock = threading.Lock()
        batch_progress = [0] * worker_count
        batch_files = [0] * worker_count
        parallel_started = time.monotonic()
        total_bytes = sum(entry.size for entry in files)
        abort_event = threading.Event()
        combined_cancel = _CombinedCancelEvent(cancel_event, abort_event)

        def on_progress(index: int, update: dict) -> None:
            forwarded = dict(update)
            with progress_lock:
                if update.get("type") == "progress":
                    batch_progress[index] = max(
                        batch_progress[index], int(update.get("done_bytes", 0) or 0)
                    )
                    batch_files[index] = max(
                        batch_files[index], int(update.get("done_files", 0) or 0)
                    )
                    forwarded["done_bytes"] = sum(batch_progress)
                    forwarded["total_bytes"] = total_bytes
                    forwarded["done_files"] = sum(batch_files)
                    forwarded["total_files"] = len(files)
                    forwarded["speed"] = _speed_text(
                        forwarded["done_bytes"], parallel_started
                    )
                forwarded["activity"] = (
                    f"Direct ACBridge P2P upload ({worker_count} streams)"
                )
                self._emit(progress_callback, forwarded)

        def run_batch(index: int, batch: list[P2PEntry]) -> P2PTransferResult:
            return self._upload_entry_batch(
                batch,
                android_destination,
                cancel_event=combined_cancel,
                progress_callback=lambda update: on_progress(index, update),
                connect_timeout=connect_timeout,
                session_timeout=session_timeout,
            )

        results: list[P2PTransferResult] = []
        primary_error: Exception | None = None
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="OpenADB-P2P"
        ) as executor:
            futures = [
                executor.submit(run_batch, index, batch)
                for index, batch in enumerate(batches)
            ]
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    if primary_error is None or "cancel" not in str(exc).lower():
                        primary_error = exc
                    abort_event.set()

        if primary_error is not None:
            if isinstance(primary_error, P2PTransferError):
                raise primary_error
            raise P2PTransferError(str(primary_error)) from primary_error
        self._check_cancelled(cancel_event)
        sent_bytes = sum(result.bytes_sent for result in results)
        sent_files = sum(result.files_sent for result in results)
        return P2PTransferResult(
            True,
            f"Stored {sent_files} file(s) through {worker_count} parallel ACBridge P2P streams",
            sent_bytes,
            sent_files,
            len(entries),
        )

    def _prepare_session(
        self,
        destination: str,
        timeout_seconds: int,
        connect_timeout: float,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> P2PSession:
        while not self._session_prepare_lock.acquire(timeout=0.1):
            self._check_cancelled(cancel_event)
        try:
            self._check_cancelled(cancel_event)
            return self._prepare_session_locked(
                destination,
                timeout_seconds,
                connect_timeout,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
            )
        finally:
            self._session_prepare_lock.release()

    def _prepare_session_locked(
        self,
        destination: str,
        timeout_seconds: int,
        connect_timeout: float,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> P2PSession:
        self._check_cancelled(cancel_event)
        destination = self._normalize_destination(destination)
        installed, install_message = self.bridge.ensure_installed(
            require_current=True,
            cancel_event=cancel_event,
        )
        self._check_cancelled(cancel_event)
        if not installed:
            raise P2PTransferError(install_message)
        addresses = self.adb.device_ip_addresses(cancel_event=cancel_event)
        self._check_cancelled(cancel_event)
        if not addresses:
            raise P2PTransferError(
                "ACBridge P2P could not determine the Android device's local IPv4 address. "
                "Keep Platform Tools connected, and connect the PC and Android TV to the same local network."
            )

        # The request id is a public correlation locator. Authentication does
        # not depend on it: the one-shot bootstrap secret remains inside the
        # request payload and is never placed in argv or a filename.
        request_id = uuid.uuid4().hex
        bootstrap_secret = secrets.token_hex(32)
        # Port 0 lets Android choose an actually free ephemeral port. The
        # authenticated READY response carries the selected value back.
        port = 0
        timeout_seconds = max(30, min(600, int(timeout_seconds)))
        remote_request = self._remote_request_path(request_id)
        remote_cancel = self._remote_cancel_path(request_id)
        request_text = (
            f"OPENADB_P2P_2\n{port}\n{timeout_seconds}\n{destination}\n"
            f"{bootstrap_secret}\n"
        )
        remote_bootstrap_started = False
        service_started = False
        remote_terminal_status = False
        try:
            self._check_cancelled(cancel_event)
            remote_bootstrap_started = True
            with self._private_adb_log("prepare request", request_id):
                prepare_command = (
                    "mkdir -p files && "
                    f"rm -f {shell_quote(remote_request)} {shell_quote(remote_cancel)}"
                )
                prepared = self.adb.run_shell(
                    f"run-as {shell_quote(ACBridgeClient.PACKAGE)} "
                    f"sh -c {shell_quote(prepare_command)}",
                    timeout=15,
                    cancel_event=cancel_event,
                )
            self._check_cancelled(cancel_event)
            if not prepared.success:
                detail = prepared.stderr or prepared.status
                raise P2PTransferError(
                    _redact_exact(detail, request_id, bootstrap_secret)
                    or "Could not prepare the ACBridge P2P request folder."
                )
            self._remove_status_file(request_id, cancel_event=cancel_event)
            self._check_cancelled(cancel_event)

            def write_request(stream: BinaryIO) -> None:
                stream.write(request_text.encode("utf-8"))
                stream.flush()

            # `adb shell` reconstructs multiple argv items as one remote shell
            # command. Passing run-as/sh/-c/redirection as separate items lets
            # Android's outer shell consume `>` before run-as is entered. Keep
            # the complete nested command in the single argument after
            # `shell`, so the request is written inside ACBridge's private
            # files directory rather than the shell user's working directory.
            write_script = f"cat > {shell_quote(remote_request)}"
            remote_write_command = (
                f"run-as {shell_quote(ACBridgeClient.PACKAGE)} "
                f"sh -c {shell_quote(write_script)}"
            )
            try:
                with self._private_adb_log(
                    "write request",
                    request_id,
                    bootstrap_secret,
                ):
                    pushed = self.adb.run_raw_with_input_stream(
                        ["shell", remote_write_command],
                        input_writer=write_request,
                        timeout=30,
                        cancel_event=cancel_event,
                    )
            except Exception as exc:
                detail = _redact_exact(str(exc), request_id, bootstrap_secret)
                raise P2PTransferError(
                    detail or "Could not pass the P2P request to ACBridge."
                ) from None
            self._check_cancelled(cancel_event)
            if not pushed.success:
                detail = pushed.stderr or pushed.status
                raise P2PTransferError(
                    _redact_exact(detail, request_id, bootstrap_secret)
                    or "Could not pass the P2P request to ACBridge."
                )
            self._check_cancelled(cancel_event)

            with self._private_adb_log("start service", request_id):
                started = self.adb.run_shell(
                    f"am start-foreground-service -n {shell_quote(self.SERVICE)} "
                    f"--es request_id {shell_quote(request_id)}",
                    timeout=20,
                    cancel_event=cancel_event,
                )
            service_started = bool(started.success)
            self._check_cancelled(cancel_event)
            if not started.success:
                detail = started.stderr or started.status
                raise P2PTransferError(
                    _redact_exact(detail, request_id, bootstrap_secret)
                    or "Android refused to start the ACBridge P2P foreground service."
                )
        except Exception:
            if remote_bootstrap_started:
                self._cleanup_session_files_safely(
                    request_id,
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                    signal_cancel=service_started,
                )
            raise

        deadline = time.monotonic() + max(5.0, connect_timeout)
        permission_requested = False
        try:
            while time.monotonic() < deadline:
                self._check_cancelled(cancel_event)
                probe = self._status_file_probe(request_id, cancel_event=cancel_event)
                if "READY" not in (probe.stdout or ""):
                    time.sleep(0.2)
                    continue
                pulled, status = self._read_status(
                    request_id,
                    cancel_event=cancel_event,
                )
                self._check_cancelled(cancel_event)
                if not pulled.success or not status:
                    time.sleep(0.2)
                    continue
                if status.startswith("ERROR\t"):
                    remote_terminal_status = True
                    raise P2PTransferError(status.split("\t", 1)[1].strip())
                if status.startswith("PERMISSION_REQUIRED\t"):
                    if permission_requested:
                        time.sleep(0.2)
                        continue
                    permission_requested = True
                    self._emit(
                        progress_callback,
                        {
                            "type": "progress",
                            "done_bytes": 0,
                            "total_bytes": 0,
                            "done_files": 0,
                            "total_files": 0,
                            "activity": "Waiting for MicroSD/USB access on Android",
                            "output": (
                                "ACBridge requires Android storage access. Select the requested MicroSD/USB "
                                "location on the Android device and confirm it. No file data will be sent before access is granted."
                            ),
                        },
                    )
                    grant_result = self.bridge.grant_storage_access(
                        destination,
                        timeout=600,
                        cancel_event=cancel_event,
                    )
                    self._check_cancelled(cancel_event)
                    if not grant_result.success:
                        message = (
                            grant_result.status
                            or grant_result.stderr
                            or "Android storage access was not granted."
                        )
                        raise P2PTransferError(
                            f"Android storage access was not granted: {message}"
                        )
                    deadline = time.monotonic() + max(5.0, connect_timeout)
                    self._emit(
                        progress_callback,
                        {
                            "type": "progress",
                            "done_bytes": 0,
                            "total_bytes": 0,
                            "done_files": 0,
                            "total_files": 0,
                            "activity": "Storage access granted; preparing P2P",
                            "output": "Android granted ACBridge storage access. Preparing the direct P2P connection.",
                        },
                    )
                    time.sleep(0.2)
                    continue
                if status.startswith("READY\t"):
                    ready_port, token, expires_at_ms = self._parse_ready_status(
                        status,
                        expected_port=port,
                        bootstrap_secret=bootstrap_secret,
                    )
                    self._remove_status_file(
                        request_id,
                        cancel_event=cancel_event,
                    )
                    self._check_cancelled(cancel_event)
                    return P2PSession(
                        addresses[0], ready_port, token, expires_at_ms, request_id
                    )
                time.sleep(0.2)
        except Exception:
            self._cleanup_session_files_safely(
                request_id,
                cancel_event=cancel_event,
                progress_callback=progress_callback,
                signal_cancel=service_started and not remote_terminal_status,
            )
            raise
        self._cleanup_session_files_safely(
            request_id,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            signal_cancel=service_started,
        )
        raise P2PTransferError(
            "ACBridge did not open the one-time P2P session before timeout. "
            "Check the TV notification and local-network connectivity."
        )

    def _connect(
        self, session: P2PSession, connect_timeout: float, cancel_event=None
    ) -> socket.socket:
        self._check_cancelled(cancel_event)
        addresses = self.adb.device_ip_addresses(cancel_event=cancel_event)
        self._check_cancelled(cancel_event)
        candidates = list(dict.fromkeys([session.host, *addresses]))
        # Android and Windows wall clocks may differ substantially. Use the
        # local monotonic timeout captured by this operation; ACBridge enforces
        # its own server-side expiry independently.
        deadline = max(1.0, min(600.0, float(connect_timeout)))
        deadline_at = time.monotonic() + deadline
        last_error: OSError | None = None
        while time.monotonic() < deadline_at:
            self._check_cancelled(cancel_event)
            for host in candidates:
                self._check_cancelled(cancel_event)
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    connected = socket.create_connection(
                        (host, session.port),
                        timeout=min(P2P_CONNECT_ATTEMPT_TIMEOUT, remaining),
                    )
                except OSError as exc:
                    last_error = exc
                    self._check_cancelled(cancel_event)
                    continue
                try:
                    self._check_cancelled(cancel_event)
                except P2PTransferError:
                    try:
                        connected.close()
                    except OSError:
                        pass
                    raise
                return connected
            self._check_cancelled(cancel_event)
            time.sleep(0.2)
        detail = f" ({last_error})" if last_error else ""
        raise P2PTransferError(
            "The PC could not reach ACBridge directly on the local network"
            f"{detail}. Confirm that both devices are on the same LAN and that client isolation is disabled."
        )

    def _cleanup_session_files(
        self,
        session_id: str,
        cancel_event=None,
        *,
        signal_cancel: bool = True,
    ) -> None:
        if not session_id or len(session_id) != 32:
            return
        cancelled = cancel_event is not None and cancel_event.is_set()
        relative = self._status_relative_path(session_id)
        cancel_command = (
            f"touch {shell_quote(self._remote_cancel_path(session_id))}"
            if signal_cancel
            else f"rm -f {shell_quote(self._remote_cancel_path(session_id))}"
        )
        cleanup_command = (
            f"{cancel_command}; "
            f"rm -f {shell_quote(self._remote_request_path(session_id))} "
            f"{shell_quote(relative)}"
        )
        with self._private_adb_log("clean session", session_id):
            self.adb.run_shell(
                f"run-as {shell_quote(ACBridgeClient.PACKAGE)} "
                f"sh -c {shell_quote(cleanup_command)} >/dev/null 2>&1 || true",
                timeout=1.5 if cancelled else 10,
            )

    def _cleanup_session_files_safely(
        self,
        session_id: str,
        *,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
        signal_cancel: bool = True,
    ) -> None:
        """Best-effort cleanup that never hides the transfer's primary result."""

        try:
            self._cleanup_session_files(
                session_id,
                cancel_event=cancel_event,
                signal_cancel=signal_cancel,
            )
        except Exception:
            try:
                self._emit(
                    progress_callback,
                    {
                        "type": "progress",
                        "activity": "P2P session cleanup warning",
                        "output": (
                            "ACBridge P2P session cleanup could not finish. "
                            "The one-time session will expire automatically."
                        ),
                    },
                )
            except Exception:
                # Cleanup and its warning are both best-effort. Neither may
                # replace the transfer's authoritative result or exception.
                pass

    def _fetch_session_error_safely(self, session_id: str, cancel_event=None) -> str:
        """Return optional ACBridge diagnostics without replacing the primary error."""

        try:
            return self._fetch_session_error(session_id, cancel_event=cancel_event)
        except Exception:
            # Remote diagnostics are supplementary. Avoid exposing diagnostic
            # internals (which can include one-time session data) and preserve
            # the network/protocol/cancellation error that triggered this call.
            return ""

    def _fetch_session_error(self, session_id: str, cancel_event=None) -> str:
        if cancel_event is not None and cancel_event.is_set():
            return ""
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return ""
            probe = self._status_file_probe(
                session_id,
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                return ""
            if "READY" in (probe.stdout or ""):
                pulled, status = self._read_status(
                    session_id,
                    cancel_event=cancel_event,
                )
                if cancel_event is not None and cancel_event.is_set():
                    return ""
                if pulled.success and status.startswith("ERROR\t"):
                    return status.split("\t", 1)[1].strip()
                return ""
            time.sleep(0.1)
        return ""

    def _local_temp_dir(self) -> Path:
        base = self._temp_folder
        if base is None:
            base = Path(self.settings.temp_folder).expanduser()
        return ensure_dir(base / "acbridge" / "p2p")

    @staticmethod
    def _parse_ready_status(
        status: str,
        *,
        expected_port: int,
        bootstrap_secret: str,
    ) -> tuple[int, str, int]:
        """Validate authenticated ACBridge bootstrap metadata without logging it."""

        fields = status.split("\t")
        if len(fields) != 5 or fields[0] != "READY":
            raise P2PTransferError("ACBridge returned malformed P2P session metadata.")
        try:
            ready_port = int(fields[1])
            token = fields[2]
            expires_at_ms = int(fields[3])
            token_bytes = bytes.fromhex(token)
            proof_bytes = bytes.fromhex(fields[4])
            bootstrap_key = bytes.fromhex(bootstrap_secret)
        except (TypeError, ValueError) as exc:
            raise P2PTransferError(
                "ACBridge returned malformed P2P session metadata."
            ) from exc
        ready_payload = "\t".join(fields[:4]).encode("utf-8")
        expected_proof = hmac.new(
            bootstrap_key,
            ready_payload,
            hashlib.sha256,
        ).digest()
        if (
            (expected_port and ready_port != expected_port)
            or ready_port < 1024
            or ready_port > 65535
            or len(token_bytes) != 32
            or len(bootstrap_key) != 32
            or len(proof_bytes) != 32
            or not hmac.compare_digest(expected_proof, proof_bytes)
        ):
            raise P2PTransferError("ACBridge returned invalid P2P session metadata.")
        return ready_port, token, expires_at_ms

    @staticmethod
    def _normalize_destination(destination: str) -> str:
        clean = str(destination or "").replace("\\", "/").rstrip("/")
        if clean == "/sdcard" or clean.startswith("/sdcard/"):
            clean = "/storage/emulated/0" + clean[len("/sdcard") :]
        elif clean == "/storage/self/primary" or clean.startswith(
            "/storage/self/primary/"
        ):
            clean = "/storage/emulated/0" + clean[len("/storage/self/primary") :]
        parts = [part for part in clean.split("/") if part]
        if (
            not clean.startswith("/storage/")
            or any(character in clean for character in ("\x00", "\r", "\n"))
            or any(part in {".", ".."} for part in parts)
        ):
            raise P2PTransferError(
                "ACBridge P2P requires /sdcard or a valid Android storage path under /storage/."
            )
        return clean

    @staticmethod
    def _check_cancelled(cancel_event) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise P2PTransferError("P2P transfer cancelled by user.")

    @staticmethod
    def _emit(callback: Callable[[dict], None] | None, update: dict) -> None:
        if callback is not None:
            callback(update)

    @staticmethod
    def _remote_request_path(request_id: str) -> str:
        return f"files/{P2P_BOOTSTRAP_REQUEST_PREFIX}{request_id}.txt"

    @staticmethod
    def _status_relative_path(request_id: str) -> str:
        return f"files/{P2P_BOOTSTRAP_STATUS_PREFIX}{request_id}.txt"

    @staticmethod
    def _remote_cancel_path(request_id: str) -> str:
        return f"files/{P2P_BOOTSTRAP_CANCEL_PREFIX}{request_id}"

    def _status_file_probe(self, session_id: str, cancel_event=None):
        relative = self._status_relative_path(session_id)
        command = f"test -f {shell_quote(relative)} && echo READY"
        with self._private_adb_log("check session status", session_id):
            return self.adb.run_shell(
                f"run-as {shell_quote(ACBridgeClient.PACKAGE)} sh -c {shell_quote(command)}",
                timeout=8,
                cancel_event=cancel_event,
            )

    def _read_status(self, session_id: str, cancel_event=None):
        """Return bootstrap metadata in memory so session keys never touch PC disk."""

        with self._private_adb_log("read session status", session_id):
            result, payload = self.adb.run_raw_binary_output(
                [
                    "exec-out",
                    "run-as",
                    ACBridgeClient.PACKAGE,
                    "cat",
                    self._status_relative_path(session_id),
                ],
                timeout=20,
                cancel_event=cancel_event,
            )
        return result, payload.decode("utf-8", errors="replace").strip()

    def _remove_status_file(self, session_id: str, cancel_event=None) -> None:
        relative = self._status_relative_path(session_id)
        with self._private_adb_log("remove session status", session_id):
            self.adb.run_shell(
                f"run-as {shell_quote(ACBridgeClient.PACKAGE)} rm -f {shell_quote(relative)}",
                timeout=8,
                cancel_event=cancel_event,
            )

    def _private_adb_log(
        self,
        operation: str,
        session_id: str,
        *additional_sensitive_values: str,
    ):
        """Keep one-time P2P locators out of command history and log files."""

        runner = getattr(self.adb, "runner", None)
        scoped_log_command = getattr(runner, "scoped_log_command", None)
        if not callable(scoped_log_command):
            return nullcontext()
        return scoped_log_command(
            ["adb", f"<ACBridge P2P {operation}>"],
            sensitive_values=(session_id, *additional_sensitive_values),
        )


def collect_p2p_entries(
    local_paths: Iterable[str | Path],
    *,
    cancel_event=None,
) -> list[P2PEntry]:
    entries: list[P2PEntry] = []
    for raw_path in local_paths:
        ACBridgeP2PClient._check_cancelled(cancel_event)
        path = Path(raw_path).expanduser()
        if path.is_symlink():
            raise P2PTransferError(
                f"P2P transfer does not follow symbolic links: {path}"
            )
        if path.is_file():
            entries.append(
                P2PEntry(
                    path, _safe_relative_name(path.name), path.stat().st_size, False
                )
            )
            continue
        if not path.is_dir():
            raise P2PTransferError(f"Local transfer source does not exist: {path}")
        root_name = _safe_relative_name(path.name)
        entries.append(P2PEntry(None, root_name, 0, True))
        children: list[Path] = []
        for child in path.rglob("*"):
            ACBridgeP2PClient._check_cancelled(cancel_event)
            children.append(child)
        children.sort(key=lambda item: item.as_posix().casefold())
        for child in children:
            ACBridgeP2PClient._check_cancelled(cancel_event)
            if child.is_symlink():
                raise P2PTransferError(
                    f"P2P transfer does not follow symbolic links: {child}"
                )
            relative = _safe_relative_name(
                (Path(root_name) / child.relative_to(path)).as_posix()
            )
            if child.is_dir():
                entries.append(P2PEntry(None, relative, 0, True))
            elif child.is_file():
                entries.append(P2PEntry(child, relative, child.stat().st_size, False))
            if len(entries) > P2P_MAX_ENTRIES:
                raise P2PTransferError(
                    f"P2P transfer is limited to {P2P_MAX_ENTRIES:,} entries per session."
                )
    if not entries:
        raise P2PTransferError("No local files were selected for P2P transfer.")
    relative_paths = [entry.relative_path for entry in entries]
    if len(set(relative_paths)) != len(relative_paths):
        raise P2PTransferError(
            "Selected sources contain duplicate destination names in the P2P session."
        )
    return entries


def _safe_relative_name(value: str) -> str:
    clean = str(value or "").replace("\\", "/").strip("/")
    parts = clean.split("/") if clean else []
    if not parts or any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise P2PTransferError(f"Unsafe relative path in P2P transfer: {value!r}")
    encoded = clean.encode("utf-8")
    if len(encoded) > 65_536:
        raise P2PTransferError("A P2P transfer path is too long for the protocol.")
    return clean


def _text_frame(value: str) -> bytes:
    data = value.encode("utf-8")
    if len(data) > 65_536:
        raise P2PTransferError("P2P protocol text is too long.")
    return struct.pack(">I", len(data)) + data


def _write_authenticated(stream, transcript, data: bytes) -> None:
    """Write one canonical frame and bind it to the request transcript."""

    stream.write(data)
    transcript.update(data)


def _read_authenticated_response(
    stream,
    *,
    session_key: bytes,
    request_tag: bytes,
    expected_entries: int,
    expected_files: int,
    expected_bytes: int,
) -> tuple[bool, str]:
    """Read and authenticate ACBridge's terminal result and accounting.

    The response MAC binds the terminal status and exact server-side entry,
    file, and byte counts to the request transcript.  A network peer cannot
    therefore turn a truncated or rejected transfer into a visible success.
    """

    response_magic = _read_exact(stream, len(P2P_MAGIC))
    status_frame = _read_exact(stream, 1)
    counts_frame = _read_exact(stream, 16)
    message_size_frame = _read_exact(stream, 4)
    message_size = struct.unpack(">I", message_size_frame)[0]
    if message_size > 65_536:
        raise P2PTransferError("ACBridge returned oversized protocol text.")
    message_data = _read_exact(stream, message_size)
    response_tag = _read_exact(stream, P2P_AUTH_TAG_SIZE)
    response_payload = (
        response_magic
        + status_frame
        + counts_frame
        + message_size_frame
        + message_data
    )
    expected_tag = hmac.new(
        session_key,
        P2P_RESPONSE_CONTEXT + request_tag + response_payload,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_tag, response_tag):
        raise P2PTransferError(
            "ACBridge returned a P2P response that failed authentication."
        )
    if response_magic != P2P_MAGIC:
        raise P2PTransferError("ACBridge returned an unsupported P2P response.")

    status = status_frame[0]
    if status not in {0, 1}:
        raise P2PTransferError("ACBridge returned an invalid P2P response status.")
    received_entries, received_files, received_bytes = struct.unpack(">IIQ", counts_frame)
    message = message_data.decode("utf-8", errors="replace")
    if status == 1 and (
        received_entries != expected_entries
        or received_files != expected_files
        or received_bytes != expected_bytes
    ):
        raise P2PTransferError(
            "ACBridge returned inconsistent authenticated transfer counts."
        )
    return status == 1, message


def _read_exact(stream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _speed_text(bytes_sent: int, started: float) -> str:
    elapsed = max(0.001, time.monotonic() - started)
    speed = bytes_sent / elapsed
    units = ["B/s", "KB/s", "MB/s", "GB/s"]
    value = speed
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return "0 B/s"


def _friendly_network_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return (
        "ACBridge P2P transfer was interrupted. No incomplete file is committed by ACBridge. "
        f"Details: {text}"
    )


def _redact_exact(value: object, *secrets_to_remove: str) -> str:
    """Remove caller-known one-shot values without mutating unrelated data."""

    text = str(value or "")
    for secret in secrets_to_remove:
        if secret:
            text = text.replace(secret, "[private]")
    return text
