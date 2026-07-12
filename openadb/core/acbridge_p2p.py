from __future__ import annotations

import hashlib
import hmac
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from openadb.core.acbridge import ACBridgeClient
from openadb.core.path_utils import ensure_dir, shell_quote


P2P_MAGIC = b"OADBP2P1"
P2P_TRANSPORT = "acbridge_p2p"
ADB_TRANSPORT = "adb"
P2P_BUFFER_SIZE = 1024 * 1024
P2P_MAX_ENTRIES = 100_000


class P2PTransferError(RuntimeError):
    """A safe, user-facing ACBridge peer transfer failure."""


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
    token: str
    expires_at_ms: int
    session_id: str


@dataclass(slots=True, frozen=True)
class P2PTransferResult:
    success: bool
    message: str
    bytes_sent: int
    files_sent: int
    entries_sent: int


class ACBridgeP2PClient:
    """Send PC files directly over the LAN into ACBridge's SAF grant.

    ADB remains the control plane: it installs the bundled bridge, pushes an
    unguessable one-shot bootstrap request, starts the foreground service, and
    retrieves the generated session token. File bytes never pass through ADB.
    """

    SERVICE = f"{ACBridgeClient.PACKAGE}/.P2PTransferService"

    def __init__(self, bridge: ACBridgeClient) -> None:
        self.bridge = bridge
        self.adb = bridge.adb
        self.settings = bridge.settings

    def upload(
        self,
        local_paths: Iterable[str | Path],
        android_destination: str,
        *,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
        connect_timeout: float = 15.0,
        session_timeout: int = 120,
    ) -> P2PTransferResult:
        entries = collect_p2p_entries(local_paths)
        total_bytes = sum(entry.size for entry in entries if not entry.is_directory)
        total_files = sum(1 for entry in entries if not entry.is_directory)
        self._emit(
            progress_callback,
            {
                "type": "plan",
                "title": "ACBridge P2P transfer started",
                "direction": "PC → Android",
                "total_files": total_files,
                "total_bytes": total_bytes,
                "destination": android_destination,
                "message": (
                    "Platform Tools is preparing a one-time ACBridge session. "
                    "File data will travel directly over the local network and be written through Android SAF access."
                ),
            },
        )
        self._check_cancelled(cancel_event)
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
        try:
            sock = self._connect(session, connect_timeout=connect_timeout, cancel_event=cancel_event)
            sock.settimeout(max(30.0, float(session_timeout)))
            stream = sock.makefile("rwb", buffering=P2P_BUFFER_SIZE)
            try:
                stream.write(P2P_MAGIC)
                session_key = bytes.fromhex(session.token)
                stream.write(hmac.new(session_key, P2P_MAGIC, hashlib.sha256).digest())
                stream.write(struct.pack(">I", len(entries)))
                for entry in entries:
                    self._check_cancelled(cancel_event)
                    self._emit(
                        progress_callback,
                        {
                            "type": "file_start",
                            "current_file": entry.relative_path,
                            "message": f"P2P: {entry.relative_path}",
                        },
                    )
                    stream.write(b"\x00" if entry.is_directory else b"\x01")
                    _write_text(stream, entry.relative_path)
                    if entry.is_directory:
                        continue
                    stream.write(struct.pack(">Q", entry.size))
                    digest = hashlib.sha256()
                    authenticator = hmac.new(session_key, digestmod=hashlib.sha256)
                    authenticator.update(entry.relative_path.encode("utf-8"))
                    authenticator.update(b"\x00")
                    authenticator.update(struct.pack(">Q", entry.size))
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
                    stream.write(digest.digest())
                    stream.write(authenticator.digest())
                    stream.flush()
                    sent_files += 1
                stream.flush()
                response_magic = _read_exact(stream, len(P2P_MAGIC))
                if response_magic != P2P_MAGIC:
                    raise P2PTransferError("ACBridge returned an unsupported P2P response.")
                success = _read_exact(stream, 1) == b"\x01"
                message = _read_text(stream)
                if not success:
                    raise P2PTransferError(message or "ACBridge rejected the P2P upload.")
            finally:
                stream.close()
        except P2PTransferError as exc:
            if "cancel" in str(exc).lower():
                raise
            remote_error = self._fetch_session_error(session.session_id)
            if remote_error:
                raise P2PTransferError(remote_error) from exc
            raise
        except (OSError, EOFError, ValueError) as exc:
            remote_error = self._fetch_session_error(session.session_id)
            raise P2PTransferError(remote_error or _friendly_network_error(exc)) from exc
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            self._cleanup_session_files(session.session_id)

        return P2PTransferResult(True, message, sent_bytes, sent_files, len(entries))

    def _prepare_session(
        self,
        destination: str,
        timeout_seconds: int,
        connect_timeout: float,
        cancel_event=None,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> P2PSession:
        destination = self._normalize_destination(destination)
        installed, install_message = self.bridge.ensure_installed(require_current=True)
        if not installed:
            raise P2PTransferError(install_message)
        addresses = self.adb.device_ip_addresses()
        if not addresses:
            raise P2PTransferError(
                "ACBridge P2P could not determine the Android device's local IPv4 address. "
                "Keep Platform Tools connected, and connect the PC and Android TV to the same local network."
            )

        session_id = secrets.token_hex(16)
        port = secrets.randbelow(20_000) + 36_000
        timeout_seconds = max(30, min(600, int(timeout_seconds)))
        local_dir = ensure_dir(self.settings.temp_folder / "acbridge" / "p2p")
        request_path = local_dir / f"p2p_request_{session_id}.txt"
        remote_request = self._remote_request_path(session_id)
        request_text = f"OPENADB_P2P_1\n{port}\n{timeout_seconds}\n{destination}\n"
        try:
            request_path.write_text(request_text, encoding="utf-8")
            self.adb.run_shell(
                f"mkdir -p {shell_quote(ACBridgeClient.REMOTE_APP_DIR)}; "
                f"rm -f {shell_quote(remote_request)}",
                timeout=15,
            )
            self._remove_status_file(session_id)
            pushed = self.adb.push(request_path, remote_request, timeout=30)
            if not pushed.success:
                self.adb.run_shell(f"rm -f {shell_quote(remote_request)}", timeout=10)
                raise P2PTransferError(pushed.status or pushed.stderr or "Could not pass the P2P request to ACBridge.")
        finally:
            try:
                request_path.unlink(missing_ok=True)
            except OSError:
                pass

        started = self.adb.run_shell(
            f"am start-foreground-service -n {shell_quote(self.SERVICE)} --es session {shell_quote(session_id)}",
            timeout=20,
        )
        if not started.success:
            self._cleanup_session_files(session_id)
            raise P2PTransferError(
                started.status or started.stderr or "Android refused to start the ACBridge P2P foreground service."
            )

        deadline = time.monotonic() + max(5.0, connect_timeout)
        permission_requested = False
        status_path = local_dir / f"p2p_status_{session_id}.txt"
        try:
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    self._cleanup_session_files(session_id)
                    raise P2PTransferError("P2P transfer cancelled by user.")
                probe = self._status_file_probe(session_id)
                if "READY" not in (probe.stdout or ""):
                    time.sleep(0.2)
                    continue
                pulled = self._read_status_file(session_id, status_path)
                if not pulled.success or not status_path.is_file():
                    time.sleep(0.2)
                    continue
                status = status_path.read_text(encoding="utf-8", errors="replace").strip()
                if status.startswith("ERROR\t"):
                    self._cleanup_session_files(session_id)
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
                    grant_result = self.bridge.grant_storage_access(destination, timeout=600)
                    if not grant_result.success:
                        self._cleanup_session_files(session_id)
                        message = grant_result.status or grant_result.stderr or "Android storage access was not granted."
                        raise P2PTransferError(f"Android storage access was not granted: {message}")
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
                    fields = status.split("\t")
                    if len(fields) != 4:
                        self._cleanup_session_files(session_id)
                        raise P2PTransferError("ACBridge returned malformed P2P session metadata.")
                    try:
                        ready_port = int(fields[1])
                        token = fields[2]
                        expires_at_ms = int(fields[3])
                    except (TypeError, ValueError) as exc:
                        self._cleanup_session_files(session_id)
                        raise P2PTransferError("ACBridge returned malformed P2P session metadata.") from exc
                    if ready_port != port or len(token) != 64:
                        self._cleanup_session_files(session_id)
                        raise P2PTransferError("ACBridge returned invalid P2P session metadata.")
                    self._remove_status_file(session_id)
                    return P2PSession(addresses[0], ready_port, token, expires_at_ms, session_id)
                time.sleep(0.2)
        finally:
            try:
                status_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._cleanup_session_files(session_id)
        raise P2PTransferError(
            "ACBridge did not open the one-time P2P session before timeout. "
            "Check the TV notification and local-network connectivity."
        )

    def _connect(self, session: P2PSession, connect_timeout: float, cancel_event=None) -> socket.socket:
        addresses = self.adb.device_ip_addresses()
        candidates = list(dict.fromkeys([session.host, *addresses]))
        deadline = min(session.expires_at_ms / 1000.0 - time.time(), max(3.0, connect_timeout))
        deadline_at = time.monotonic() + max(1.0, deadline)
        last_error: OSError | None = None
        while time.monotonic() < deadline_at:
            self._check_cancelled(cancel_event)
            for host in candidates:
                try:
                    return socket.create_connection((host, session.port), timeout=1.5)
                except OSError as exc:
                    last_error = exc
            time.sleep(0.2)
        detail = f" ({last_error})" if last_error else ""
        raise P2PTransferError(
            "The PC could not reach ACBridge directly on the local network"
            f"{detail}. Confirm that both devices are on the same LAN and that client isolation is disabled."
        )

    def _cleanup_session_files(self, session_id: str) -> None:
        if not session_id or len(session_id) != 32:
            return
        self.adb.run_shell(
            f"rm -f {shell_quote(self._remote_request_path(session_id))}",
            timeout=10,
        )
        self._remove_status_file(session_id)
        self.adb.run_shell(f"am stopservice -n {shell_quote(self.SERVICE)} >/dev/null 2>&1 || true", timeout=10)

    def _fetch_session_error(self, session_id: str) -> str:
        local_dir = ensure_dir(self.settings.temp_folder / "acbridge" / "p2p")
        local_status = local_dir / f"p2p_error_{session_id}.txt"
        try:
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                probe = self._status_file_probe(session_id)
                if "READY" in (probe.stdout or ""):
                    pulled = self._read_status_file(session_id, local_status)
                    if pulled.success and local_status.is_file():
                        status = local_status.read_text(encoding="utf-8", errors="replace").strip()
                        if status.startswith("ERROR\t"):
                            return status.split("\t", 1)[1].strip()
                        return ""
                time.sleep(0.1)
        finally:
            try:
                local_status.unlink(missing_ok=True)
            except OSError:
                pass
        return ""

    @staticmethod
    def _normalize_destination(destination: str) -> str:
        clean = str(destination or "").replace("\\", "/").rstrip("/")
        if clean == "/sdcard" or clean.startswith("/sdcard/"):
            clean = "/storage/emulated/0" + clean[len("/sdcard") :]
        elif clean == "/storage/self/primary" or clean.startswith("/storage/self/primary/"):
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
    def _remote_request_path(session_id: str) -> str:
        return f"{ACBridgeClient.REMOTE_APP_DIR}/p2p_request_{session_id}.txt"

    @staticmethod
    def _status_relative_path(session_id: str) -> str:
        return f"files/p2p_status_{session_id}.txt"

    def _status_file_probe(self, session_id: str):
        relative = self._status_relative_path(session_id)
        command = f"test -f {shell_quote(relative)} && echo READY"
        return self.adb.run_shell(
            f"run-as {shell_quote(ACBridgeClient.PACKAGE)} sh -c {shell_quote(command)}",
            timeout=8,
        )

    def _read_status_file(self, session_id: str, destination: Path):
        return self.adb.run_raw_binary_output_to_file(
            [
                "exec-out",
                "run-as",
                ACBridgeClient.PACKAGE,
                "cat",
                self._status_relative_path(session_id),
            ],
            destination,
            timeout=20,
        )

    def _remove_status_file(self, session_id: str) -> None:
        relative = self._status_relative_path(session_id)
        self.adb.run_shell(
            f"run-as {shell_quote(ACBridgeClient.PACKAGE)} rm -f {shell_quote(relative)}",
            timeout=8,
        )


def collect_p2p_entries(local_paths: Iterable[str | Path]) -> list[P2PEntry]:
    entries: list[P2PEntry] = []
    for raw_path in local_paths:
        path = Path(raw_path).expanduser()
        if path.is_symlink():
            raise P2PTransferError(f"P2P transfer does not follow symbolic links: {path}")
        if path.is_file():
            entries.append(P2PEntry(path, _safe_relative_name(path.name), path.stat().st_size, False))
            continue
        if not path.is_dir():
            raise P2PTransferError(f"Local transfer source does not exist: {path}")
        root_name = _safe_relative_name(path.name)
        entries.append(P2PEntry(None, root_name, 0, True))
        for child in sorted(path.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if child.is_symlink():
                raise P2PTransferError(f"P2P transfer does not follow symbolic links: {child}")
            relative = _safe_relative_name((Path(root_name) / child.relative_to(path)).as_posix())
            if child.is_dir():
                entries.append(P2PEntry(None, relative, 0, True))
            elif child.is_file():
                entries.append(P2PEntry(child, relative, child.stat().st_size, False))
            if len(entries) > P2P_MAX_ENTRIES:
                raise P2PTransferError(f"P2P transfer is limited to {P2P_MAX_ENTRIES:,} entries per session.")
    if not entries:
        raise P2PTransferError("No local files were selected for P2P transfer.")
    relative_paths = [entry.relative_path for entry in entries]
    if len(set(relative_paths)) != len(relative_paths):
        raise P2PTransferError("Selected sources contain duplicate destination names in the P2P session.")
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


def _write_text(stream, value: str) -> None:
    data = value.encode("utf-8")
    if len(data) > 65_536:
        raise P2PTransferError("P2P protocol text is too long.")
    stream.write(struct.pack(">I", len(data)))
    stream.write(data)


def _read_text(stream) -> str:
    length = struct.unpack(">I", _read_exact(stream, 4))[0]
    if length > 65_536:
        raise P2PTransferError("ACBridge returned oversized protocol text.")
    return _read_exact(stream, length).decode("utf-8", errors="replace")


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
