from __future__ import annotations

import hashlib
import hmac
import socket
import struct
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openadb.core.acbridge import ACBridgeClient, ACBridgeExportState
from openadb.core.acbridge_p2p import (
    P2P_MAGIC,
    ACBridgeP2PClient,
    P2PTransferResult,
    P2PSession,
    P2PTransferError,
    collect_p2p_entries,
)


def read_exact(stream, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = stream.read(size - len(result))
        if not chunk:
            raise EOFError
        result.extend(chunk)
    return bytes(result)


def read_text(stream) -> str:
    size = struct.unpack(">I", read_exact(stream, 4))[0]
    return read_exact(stream, size).decode("utf-8")


def write_text(stream, text: str) -> None:
    data = text.encode("utf-8")
    stream.write(struct.pack(">I", len(data)))
    stream.write(data)


class ProtocolTestClient(ACBridgeP2PClient):
    def __init__(self, client_socket: socket.socket) -> None:
        self.client_socket = client_socket
        self.bridge = SimpleNamespace()
        self.adb = SimpleNamespace(device_ip_addresses=lambda: ["127.0.0.1"])
        self.settings = SimpleNamespace()

    def _prepare_session(
        self,
        destination: str,
        timeout_seconds: int,
        connect_timeout: float,
        cancel_event=None,
        progress_callback=None,
    ) -> P2PSession:
        return P2PSession("127.0.0.1", 4242, "a" * 64, int(time.time() * 1000) + 60_000, "b" * 32)

    def _connect(self, session: P2PSession, connect_timeout: float, cancel_event=None) -> socket.socket:
        return self.client_socket

    def _cleanup_session_files(self, session_id: str, cancel_event=None) -> None:
        return None

    def _fetch_session_error(self, session_id: str, cancel_event=None) -> str:
        return ""


class ACBridgeP2PTests(unittest.TestCase):
    def test_bridge_control_plane_uses_captured_temp_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            captured = root / "profile-a"
            settings = SimpleNamespace(temp_folder=root / "mutable-profile")
            bridge = ACBridgeClient(
                SimpleNamespace(),  # type: ignore[arg-type]
                settings,  # type: ignore[arg-type]
                temp_folder=captured,
            )
            settings.temp_folder = root / "profile-b"

            local_dir = bridge._local_temp_dir()

        self.assertEqual(local_dir, captured / "acbridge")

    def test_explicit_temp_folder_is_immutable_for_the_client_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "profile-a"
            captured = root / "captured-profile"
            settings = SimpleNamespace(temp_folder=original)
            bridge = SimpleNamespace(adb=SimpleNamespace(), settings=settings)
            client = ACBridgeP2PClient(bridge, temp_folder=captured)
            settings.temp_folder = root / "profile-b"

            local_dir = client._local_temp_dir()

        self.assertEqual(local_dir, captured / "acbridge" / "p2p")

    def test_cancelled_waiter_does_not_block_on_session_prepare_lock(self) -> None:
        bridge = SimpleNamespace(adb=SimpleNamespace(), settings=SimpleNamespace())
        client = ACBridgeP2PClient(bridge)
        cancel_event = threading.Event()
        client._session_prepare_lock.acquire()
        cancel_event.set()
        try:
            with self.assertRaisesRegex(P2PTransferError, "cancelled"):
                client._prepare_session(
                    "/sdcard/Download",
                    timeout_seconds=120,
                    connect_timeout=2,
                    cancel_event=cancel_event,
                )
        finally:
            client._session_prepare_lock.release()

    def test_cancelled_bridge_storage_operations_do_not_start_adb(self) -> None:
        bridge = ACBridgeClient(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(temp_folder=Path("unused")),  # type: ignore[arg-type]
        )
        cancel_event = threading.Event()
        cancel_event.set()

        grant = bridge.grant_storage_access(
            "/storage/ABCD-1234/Movies",
            cancel_event=cancel_event,
        )
        delete = bridge.delete_path(
            "/storage/ABCD-1234/Movies/file.bin",
            cancel_event=cancel_event,
        )

        self.assertEqual(grant.error_type, "cancelled")
        self.assertEqual(delete.error_type, "cancelled")
        self.assertFalse(grant.success)
        self.assertFalse(delete.success)

    def test_cancellation_during_ip_discovery_does_not_bootstrap_p2p(self) -> None:
        cancel_event = threading.Event()
        adb_calls: list[str] = []

        class FakeAdb:
            def device_ip_addresses(self, cancel_event=None):
                cancel_event.set()
                return ["192.0.2.10"]

            def run_shell(self, command, **_kwargs):
                adb_calls.append(f"shell:{command}")
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def push_streaming(self, source, destination, **_kwargs):
                adb_calls.append(f"push:{source}:{destination}")
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        bridge = SimpleNamespace(
            adb=FakeAdb(),
            settings=SimpleNamespace(temp_folder=Path("unused")),
            ensure_installed=lambda require_current=True, cancel_event=None: (True, "ready"),
        )
        client = ACBridgeP2PClient(bridge)

        with self.assertRaisesRegex(P2PTransferError, "cancelled"):
            client._prepare_session(
                "/storage/emulated/0/Download",
                timeout_seconds=120,
                connect_timeout=2,
                cancel_event=cancel_event,
            )

        self.assertEqual(adb_calls, [])

    def test_bridge_cancellation_after_setup_does_not_start_storage_activity(self) -> None:
        cancel_event = threading.Event()
        bridge = ACBridgeClient(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(temp_folder=Path("unused")),  # type: ignore[arg-type]
        )

        def cancel_during_setup(*, require_current=True, cancel_event=None):
            self.assertTrue(require_current)
            self.assertIsNotNone(cancel_event)
            cancel_event.set()
            return True, "ready"

        bridge.ensure_installed = cancel_during_setup  # type: ignore[method-assign]
        bridge._prepare_delete = MagicMock()  # type: ignore[method-assign]
        bridge._start_storage_grant = MagicMock()  # type: ignore[method-assign]
        bridge._start_delete = MagicMock()  # type: ignore[method-assign]

        grant = bridge.grant_storage_access(
            "/storage/ABCD-1234",
            cancel_event=cancel_event,
        )
        self.assertEqual(grant.error_type, "cancelled")
        bridge._prepare_delete.assert_not_called()
        bridge._start_storage_grant.assert_not_called()

        cancel_event.clear()
        delete = bridge.delete_path(
            "/storage/ABCD-1234/file.bin",
            cancel_event=cancel_event,
        )
        self.assertEqual(delete.error_type, "cancelled")
        bridge._prepare_delete.assert_not_called()
        bridge._start_delete.assert_not_called()

    def test_cancelled_app_export_does_not_start_diagnostics_or_cache_import(self) -> None:
        cancel_event = threading.Event()
        icon_extractor = SimpleNamespace(import_pre_rendered_icon_batch=MagicMock())
        bridge = ACBridgeClient(
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(temp_folder=Path("unused")),  # type: ignore[arg-type]
            icon_extractor,  # type: ignore[arg-type]
        )
        bridge.ensure_installed = MagicMock(return_value=(True, "ready"))  # type: ignore[method-assign]
        bridge._prepare_run = MagicMock()  # type: ignore[method-assign]
        bridge._start_bridge = MagicMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(success=True, status="", stderr="")
        )

        def cancel_during_wait(*_args, **_kwargs):
            cancel_event.set()
            return ACBridgeExportState(False, False, False, 0.1)

        bridge._wait_for_export = MagicMock(  # type: ignore[method-assign]
            side_effect=cancel_during_wait
        )
        bridge._download_remote_text = MagicMock()  # type: ignore[method-assign]
        bridge._acbridge_diagnostic = MagicMock()  # type: ignore[method-assign]

        result = bridge.load_app_data(
            {"com.example.app": ("1.0", "1")},
            cancel_event=cancel_event,
        )

        self.assertFalse(result.available)
        self.assertIn("cancelled", result.message.lower())
        bridge._download_remote_text.assert_not_called()
        bridge._acbridge_diagnostic.assert_not_called()
        icon_extractor.import_pre_rendered_icon_batch.assert_not_called()

    def test_internal_storage_aliases_are_normalized(self) -> None:
        self.assertEqual(
            ACBridgeP2PClient._normalize_destination("/sdcard/Download/"),
            "/storage/emulated/0/Download",
        )
        self.assertEqual(
            ACBridgeP2PClient._normalize_destination("/storage/self/primary/Documents"),
            "/storage/emulated/0/Documents",
        )
        self.assertEqual(
            ACBridgeP2PClient._normalize_destination("/storage/emulated/0"),
            "/storage/emulated/0",
        )

    def test_adb_bootstrap_never_places_session_key_on_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            commands: list[str] = []
            pushed_text: list[str] = []
            granted_paths: list[str] = []
            status_reads = 0

            class RecordingRunner:
                def __init__(self) -> None:
                    self.active = 0
                    self.scopes: list[tuple[list[str], tuple[str, ...]]] = []

                @contextmanager
                def scoped_log_command(self, command, *, sensitive_values=()):
                    self.scopes.append((list(command), tuple(sensitive_values)))
                    self.active += 1
                    try:
                        yield
                    finally:
                        self.active -= 1

            runner = RecordingRunner()

            def record_private_command(command: str) -> None:
                commands.append(command)
                if "p2p_request_" in command or "p2p_status_" in command or "--es session" in command:
                    self.assertGreater(runner.active, 0)

            class FakeAdb:
                def __init__(self) -> None:
                    self.runner = runner

                def device_ip_addresses(self, cancel_event=None) -> list[str]:
                    return ["192.168.1.50"]

                def run_shell(self, command: str, timeout=None, cancel_event=None):
                    record_private_command(command)
                    stdout = "READY\n" if "test -f" in command else ""
                    return SimpleNamespace(success=True, stdout=stdout, stderr="", status="")

                def push_streaming(
                    self,
                    source,
                    destination,
                    timeout=None,
                    cancel_event=None,
                ):
                    record_private_command(f"push {source} {destination}")
                    pushed_text.append(Path(source).read_text(encoding="utf-8"))
                    return SimpleNamespace(success=True, stdout="", stderr="", status="")

                def run_raw_binary_output_to_file(
                    self,
                    args,
                    destination,
                    timeout=None,
                    cancel_event=None,
                ):
                    nonlocal status_reads
                    record_private_command(" ".join(args))
                    status_reads += 1
                    status = "PERMISSION_REQUIRED\t/storage/ABCD-1234/Movies"
                    if status_reads > 1:
                        status = f"READY\t42042\t{'c' * 64}\t{int(time.time() * 1000) + 60_000}"
                    Path(destination).write_text(status, encoding="utf-8")
                    return SimpleNamespace(success=True, stdout="", stderr="", status="")

            cancel_event = threading.Event()

            def grant_storage_access(path: str, timeout: int, cancel_event=None):
                granted_paths.append(path)
                self.assertIs(cancel_event, cancel_event_from_prepare)
                return SimpleNamespace(success=True, stdout="", stderr="", status="granted")

            cancel_event_from_prepare = cancel_event

            bridge = SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path(temp)),
                ensure_installed=lambda require_current=True, cancel_event=None: (True, "ready"),
                grant_storage_access=grant_storage_access,
            )
            client = ACBridgeP2PClient(bridge)
            updates: list[dict] = []
            with patch("openadb.core.acbridge_p2p.secrets.randbelow", return_value=6042):
                session = client._prepare_session(
                    "/storage/ABCD-1234/Movies",
                    timeout_seconds=120,
                    connect_timeout=2,
                    cancel_event=cancel_event,
                    progress_callback=updates.append,
                )

        self.assertEqual(session.port, 42042)
        self.assertEqual(session.token, "c" * 64)
        self.assertTrue(pushed_text[0].startswith("OPENADB_P2P_1\n42042\n"))
        self.assertNotIn("c" * 64, "\n".join(commands))
        self.assertTrue(runner.scopes)
        self.assertTrue(all(session.session_id in values for _display, values in runner.scopes))
        self.assertTrue(all(session.session_id not in " ".join(display) for display, _values in runner.scopes))
        self.assertEqual(granted_paths, ["/storage/ABCD-1234/Movies"])
        self.assertTrue(any("Waiting for MicroSD/USB access" in update.get("activity", "") for update in updates))

    def test_collects_files_and_empty_directories_without_flattening(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Media"
            (root / "Season 1").mkdir(parents=True)
            (root / "Empty").mkdir()
            (root / "Season 1" / "episode.mkv").write_bytes(b"video")

            entries = collect_p2p_entries([root])

        by_path = {entry.relative_path: entry for entry in entries}
        self.assertTrue(by_path["Media"].is_directory)
        self.assertTrue(by_path["Media/Empty"].is_directory)
        self.assertEqual(by_path["Media/Season 1/episode.mkv"].size, 5)

    def test_rejects_missing_and_parent_traversal_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(P2PTransferError):
                collect_p2p_entries([Path(temp) / "missing.bin"])

    def test_rejects_duplicate_destination_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            left = Path(temp) / "left"
            right = Path(temp) / "right"
            left.mkdir()
            right.mkdir()
            (left / "same.bin").write_bytes(b"left")
            (right / "same.bin").write_bytes(b"right")
            with self.assertRaisesRegex(P2PTransferError, "duplicate"):
                collect_p2p_entries([left / "same.bin", right / "same.bin"])

    def test_stream_protocol_sends_bytes_and_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "movie.bin"
            payload = b"OpenADB-P2P" * 1000
            source.write_bytes(payload)
            client_socket, server_socket = socket.socketpair()
            observed: dict[str, object] = {}

            def server() -> None:
                stream = server_socket.makefile("rwb", buffering=1024 * 1024)
                try:
                    observed["magic"] = read_exact(stream, len(P2P_MAGIC))
                    observed["proof"] = read_exact(stream, 32)
                    observed["count"] = struct.unpack(">I", read_exact(stream, 4))[0]
                    observed["kind"] = read_exact(stream, 1)
                    observed["path"] = read_text(stream)
                    size = struct.unpack(">Q", read_exact(stream, 8))[0]
                    data = read_exact(stream, size)
                    observed["data"] = data
                    observed["digest"] = read_exact(stream, 32)
                    observed["authenticator"] = read_exact(stream, 32)
                    stream.write(P2P_MAGIC)
                    stream.write(b"\x01")
                    write_text(stream, "verified")
                    stream.flush()
                finally:
                    stream.close()
                    server_socket.close()

            thread = threading.Thread(target=server)
            thread.start()
            updates: list[dict] = []
            result = ProtocolTestClient(client_socket).upload(
                [source],
                "/storage/ABCD-1234/Movies",
                progress_callback=updates.append,
            )
            thread.join(timeout=5)

        self.assertTrue(result.success)
        self.assertEqual(result.message, "verified")
        self.assertEqual(observed["magic"], P2P_MAGIC)
        key = bytes.fromhex("a" * 64)
        self.assertEqual(observed["proof"], hmac.new(key, P2P_MAGIC, hashlib.sha256).digest())
        self.assertEqual(observed["path"], "movie.bin")
        self.assertEqual(observed["data"], payload)
        self.assertEqual(observed["digest"], hashlib.sha256(payload).digest())
        authenticator = hmac.new(key, digestmod=hashlib.sha256)
        authenticator.update(b"movie.bin\x00")
        authenticator.update(struct.pack(">Q", len(payload)))
        authenticator.update(payload)
        self.assertEqual(observed["authenticator"], authenticator.digest())
        self.assertTrue(any(update.get("activity") == "Direct ACBridge P2P upload" for update in updates))

    def test_cancelled_transfer_never_opens_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "file.bin"
            source.write_bytes(b"data")
            cancel = threading.Event()
            cancel.set()
            left, right = socket.socketpair()
            try:
                with self.assertRaisesRegex(P2PTransferError, "cancelled"):
                    ProtocolTestClient(left).upload([source], "/storage/ABCD-1234", cancel_event=cancel)
            finally:
                left.close()
                right.close()

    def test_cleanup_failure_never_overrides_primary_transfer_cancellation(self) -> None:
        updates: list[dict] = []

        class CleanupFailureClient(ACBridgeP2PClient):
            def __init__(self) -> None:
                self.bridge = SimpleNamespace()
                self.adb = SimpleNamespace()
                self.settings = SimpleNamespace()

            def _prepare_session(self, *args, **kwargs):
                return P2PSession(
                    "127.0.0.1",
                    4242,
                    "a" * 64,
                    int(time.time() * 1000) + 60_000,
                    "b" * 32,
                )

            def _connect(self, *args, **kwargs):
                raise P2PTransferError("Transfer cancelled by user")

            def _cleanup_session_files(self, session_id, cancel_event=None):
                raise RuntimeError("cleanup exploded with private state")

        with self.assertRaisesRegex(P2PTransferError, "cancelled by user"):
            CleanupFailureClient()._upload_entry_batch(
                [],
                "/storage/emulated/0/Download",
                progress_callback=updates.append,
            )

        self.assertTrue(any("cleanup could not finish" in str(update) for update in updates))
        self.assertNotIn("cleanup exploded", str(updates))

    def test_cleanup_warning_callback_failure_never_overrides_primary_error(self) -> None:
        class CleanupFailureClient(ACBridgeP2PClient):
            def __init__(self) -> None:
                self.bridge = SimpleNamespace()
                self.adb = SimpleNamespace()
                self.settings = SimpleNamespace()

            def _prepare_session(self, *args, **kwargs):
                return P2PSession(
                    "127.0.0.1",
                    4242,
                    "a" * 64,
                    int(time.time() * 1000) + 60_000,
                    "b" * 32,
                )

            def _connect(self, *args, **kwargs):
                raise P2PTransferError("authoritative protocol failure")

            def _cleanup_session_files(self, session_id, cancel_event=None):
                raise RuntimeError("cleanup exploded with one-time private state")

            def _fetch_session_error(self, session_id, cancel_event=None):
                return ""

        def rejecting_callback(_update: dict) -> None:
            raise RuntimeError("warning callback rejected the update")

        with self.assertRaisesRegex(P2PTransferError, "authoritative protocol failure") as raised:
            CleanupFailureClient()._upload_entry_batch(
                [],
                "/storage/emulated/0/Download",
                progress_callback=rejecting_callback,
            )

        self.assertNotIn("private state", str(raised.exception))
        self.assertNotIn("callback", str(raised.exception))

    def test_diagnostic_failure_never_overrides_primary_protocol_error(self) -> None:
        class DiagnosticFailureClient(ProtocolTestClient):
            def _connect(self, *args, **kwargs):
                raise P2PTransferError("authoritative protocol failure")

            def _fetch_session_error(self, session_id, cancel_event=None):
                raise RuntimeError("diagnostic failed with one-time secret")

        left, right = socket.socketpair()
        try:
            with self.assertRaisesRegex(
                P2PTransferError,
                "authoritative protocol failure",
            ) as raised:
                DiagnosticFailureClient(left)._upload_entry_batch(
                    [],
                    "/storage/emulated/0/Download",
                )
        finally:
            left.close()
            right.close()

        self.assertNotIn("one-time secret", str(raised.exception))

    def test_diagnostic_failure_never_overrides_primary_network_error(self) -> None:
        class DiagnosticFailureClient(ProtocolTestClient):
            def _connect(self, *args, **kwargs):
                raise OSError("authoritative socket failure")

            def _fetch_session_error(self, session_id, cancel_event=None):
                raise RuntimeError("diagnostic failed with one-time secret")

        left, right = socket.socketpair()
        try:
            with self.assertRaises(P2PTransferError) as raised:
                DiagnosticFailureClient(left)._upload_entry_batch(
                    [],
                    "/storage/emulated/0/Download",
                )
        finally:
            left.close()
            right.close()

        self.assertIn("authoritative socket failure", str(raised.exception))
        self.assertNotIn("one-time secret", str(raised.exception))

    def test_bootstrap_cleanup_failure_never_overrides_primary_error(self) -> None:
        updates: list[dict] = []

        class FakeAdb:
            def device_ip_addresses(self, cancel_event=None):
                return ["192.0.2.10"]

            def run_shell(self, _command, **_kwargs):
                return SimpleNamespace(
                    success=False,
                    status="primary bootstrap failure",
                    stdout="",
                    stderr="",
                )

        class CleanupFailureClient(ACBridgeP2PClient):
            def _cleanup_session_files(self, session_id, cancel_event=None):
                raise RuntimeError("cleanup exploded with private state")

        with tempfile.TemporaryDirectory() as temp:
            bridge = SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path(temp)),
                ensure_installed=lambda **_kwargs: (True, "ready"),
            )
            client = CleanupFailureClient(bridge)

            with self.assertRaisesRegex(P2PTransferError, "primary bootstrap failure"):
                client._prepare_session(
                    "/storage/emulated/0/Download",
                    timeout_seconds=120,
                    connect_timeout=2,
                    progress_callback=updates.append,
                )

        self.assertTrue(any("cleanup could not finish" in str(update) for update in updates))
        self.assertNotIn("cleanup exploded", str(updates))

    def test_cancel_while_waiting_for_peer_response_closes_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "file.bin"
            payload = b"data awaiting acknowledgement"
            source.write_bytes(payload)
            cancel = threading.Event()
            left, right = socket.socketpair()

            def server() -> None:
                stream = right.makefile("rb")
                try:
                    read_exact(stream, len(P2P_MAGIC) + 32)
                    self.assertEqual(struct.unpack(">I", read_exact(stream, 4))[0], 1)
                    self.assertEqual(read_exact(stream, 1), b"\x01")
                    read_text(stream)
                    size = struct.unpack(">Q", read_exact(stream, 8))[0]
                    read_exact(stream, size + 64)
                    cancel.set()
                    time.sleep(0.5)
                finally:
                    stream.close()
                    right.close()

            thread = threading.Thread(target=server)
            thread.start()
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(P2PTransferError, "cancelled"):
                    ProtocolTestClient(left).upload(
                        [source],
                        "/storage/ABCD-1234",
                        cancel_event=cancel,
                        session_timeout=120,
                    )
            finally:
                left.close()
                thread.join(timeout=3)

        self.assertLess(time.monotonic() - started, 2.0)

    def test_cancelled_session_cleanup_uses_one_short_bounded_adb_call(self) -> None:
        calls: list[tuple[str, float]] = []

        class FakeAdb:
            def run_shell(self, command, timeout=None):
                calls.append((command, timeout))
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        client = ACBridgeP2PClient(
            SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path("unused")),
            )
        )
        cancel = threading.Event()
        cancel.set()

        client._cleanup_session_files("a" * 32, cancel_event=cancel)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 1.5)
        self.assertIn("p2p_request_", calls[0][0])
        self.assertIn("p2p_status_", calls[0][0])

    def test_cancellation_during_status_read_cleans_remote_session(self) -> None:
        cancel_event = threading.Event()
        cleaned_sessions: list[str] = []

        class FakeAdb:
            def device_ip_addresses(self, cancel_event=None):
                return ["192.0.2.10"]

            def run_shell(self, _command, **_kwargs):
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def push_streaming(self, _source, _destination, **_kwargs):
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        class CancelDuringStatusClient(ACBridgeP2PClient):
            def _remove_status_file(self, session_id, cancel_event=None) -> None:
                return None

            def _status_file_probe(self, session_id, cancel_event=None):
                return SimpleNamespace(stdout="READY")

            def _read_status_file(self, session_id, destination, cancel_event=None):
                cancel_event.set()
                return SimpleNamespace(success=False)

            def _cleanup_session_files(self, session_id, cancel_event=None) -> None:
                cleaned_sessions.append(session_id)

        with tempfile.TemporaryDirectory() as temp:
            bridge = SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path(temp)),
                ensure_installed=lambda require_current=True, cancel_event=None: (True, "ready"),
            )
            client = CancelDuringStatusClient(bridge)

            with self.assertRaisesRegex(P2PTransferError, "cancelled"):
                client._prepare_session(
                    "/storage/emulated/0/Download",
                    timeout_seconds=120,
                    connect_timeout=2,
                    cancel_event=cancel_event,
                )

        self.assertEqual(len(cleaned_sessions), 1)
        self.assertEqual(len(cleaned_sessions[0]), 32)

    def test_cancelled_protocol_error_skips_remote_diagnostics(self) -> None:
        class NoDiagnosticAdb:
            def run_shell(self, *_args, **_kwargs):
                raise AssertionError("cancelled transfer must not probe remote status")

            def run_raw_binary_output_to_file(self, *_args, **_kwargs):
                raise AssertionError("cancelled transfer must not read remote status")

        client = ACBridgeP2PClient(
            SimpleNamespace(
                adb=NoDiagnosticAdb(),
                settings=SimpleNamespace(temp_folder=Path("unused")),
            )
        )
        cancel_event = threading.Event()
        cancel_event.set()

        self.assertEqual(
            client._fetch_session_error("a" * 32, cancel_event=cancel_event),
            "",
        )

    def test_connect_checks_cancellation_between_address_candidates(self) -> None:
        cancel_event = threading.Event()
        attempted: list[tuple[tuple[str, int], float]] = []

        class FakeAdb:
            @staticmethod
            def device_ip_addresses(cancel_event=None):
                return ["192.0.2.20", "192.0.2.30"]

        client = ACBridgeP2PClient(
            SimpleNamespace(adb=FakeAdb(), settings=SimpleNamespace())
        )
        session = P2PSession(
            "192.0.2.10",
            4242,
            "a" * 64,
            int(time.time() * 1000) + 60_000,
            "b" * 32,
        )

        def cancelled_first_attempt(address, timeout):
            attempted.append((address, timeout))
            cancel_event.set()
            raise OSError("deterministic connection failure")

        with patch(
            "openadb.core.acbridge_p2p.socket.create_connection",
            side_effect=cancelled_first_attempt,
        ):
            with self.assertRaisesRegex(P2PTransferError, "cancelled"):
                client._connect(session, connect_timeout=15, cancel_event=cancel_event)

        self.assertEqual([item[0] for item in attempted], [("192.0.2.10", 4242)])
        self.assertLessEqual(attempted[0][1], 0.5)

    def test_local_status_read_error_still_cleans_remote_session(self) -> None:
        cleaned_sessions: list[str] = []

        class FakeAdb:
            def device_ip_addresses(self, cancel_event=None):
                return ["192.0.2.10"]

            def run_shell(self, _command, **_kwargs):
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def push_streaming(self, _source, _destination, **_kwargs):
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        class FailingStatusClient(ACBridgeP2PClient):
            def _remove_status_file(self, session_id, cancel_event=None) -> None:
                return None

            def _status_file_probe(self, session_id, cancel_event=None):
                return SimpleNamespace(stdout="READY")

            def _read_status_file(self, session_id, destination, cancel_event=None):
                raise OSError("temporary status drive unavailable")

            def _cleanup_session_files(self, session_id, cancel_event=None) -> None:
                cleaned_sessions.append(session_id)

        with tempfile.TemporaryDirectory() as temp:
            bridge = SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path(temp)),
                ensure_installed=lambda require_current=True, cancel_event=None: (True, "ready"),
            )
            client = FailingStatusClient(bridge)

            with self.assertRaisesRegex(OSError, "status drive"):
                client._prepare_session(
                    "/storage/emulated/0/Download",
                    timeout_seconds=120,
                    connect_timeout=2,
                )

        self.assertEqual(len(cleaned_sessions), 1)

    def test_parallel_upload_balances_files_and_prepares_directories_first(self) -> None:
        class RecordingParallelClient(ACBridgeP2PClient):
            def __init__(self) -> None:
                super().__init__(SimpleNamespace(adb=SimpleNamespace(), settings=SimpleNamespace()))
                self.barrier = threading.Barrier(3)
                self.lock = threading.Lock()
                self.active = 0
                self.max_active = 0
                self.batches: list[list] = []

            def _upload_entry_batch(self, entries, android_destination, **kwargs):
                with self.lock:
                    self.batches.append(list(entries))
                files = [entry for entry in entries if not entry.is_directory]
                if files:
                    with self.lock:
                        self.active += 1
                        self.max_active = max(self.max_active, self.active)
                    self.barrier.wait(timeout=2)
                    time.sleep(0.02)
                    with self.lock:
                        self.active -= 1
                return P2PTransferResult(
                    True,
                    "recorded",
                    sum(entry.size for entry in files),
                    len(files),
                    len(entries),
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "Folder"
            root.mkdir()
            (root / "large.bin").write_bytes(b"a" * 30)
            (root / "medium.bin").write_bytes(b"b" * 20)
            (root / "small.bin").write_bytes(b"c" * 10)
            entries = collect_p2p_entries([root])
            client = RecordingParallelClient()
            result = client.upload_entries(entries, "/sdcard/Download", parallelism=3)

        self.assertTrue(result.success)
        self.assertEqual(result.bytes_sent, 60)
        self.assertEqual(result.files_sent, 3)
        self.assertEqual(client.max_active, 3)
        self.assertTrue(all(entry.is_directory for entry in client.batches[0]))
        transferred = [entry.relative_path for batch in client.batches[1:] for entry in batch]
        self.assertCountEqual(
            transferred,
            ["Folder/large.bin", "Folder/medium.bin", "Folder/small.bin"],
        )
        self.assertIn("3 parallel", result.message)


if __name__ == "__main__":
    unittest.main()
