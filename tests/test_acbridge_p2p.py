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
    P2P_AUTH_TAG_SIZE,
    P2P_CONTROL_MAX_LINE_BYTES,
    P2P_CONTROL_SOCKET_PREFIX,
    P2P_ENTRY_CONTROL_CONTEXT,
    P2P_MAGIC,
    P2P_REQUEST_TRANSCRIPT_CONTEXT,
    P2P_RESPONSE_CONTEXT,
    ACBridgeP2PClient,
    P2PEntry,
    P2PTransferResult,
    P2PSession,
    P2PTransferError,
    _P2PControlChannel,
    _parse_forward_port,
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


def text_frame(text: str) -> bytes:
    data = text.encode("utf-8")
    return struct.pack(">I", len(data)) + data


def authenticated_response(
    key: bytes,
    request_tag: bytes,
    *,
    success: bool,
    entry_count: int,
    file_count: int,
    byte_count: int,
    message: str,
) -> bytes:
    payload = (
        P2P_MAGIC
        + (b"\x01" if success else b"\x00")
        + struct.pack(">IIQ", entry_count, file_count, byte_count)
        + text_frame(message)
    )
    tag = hmac.new(
        key,
        P2P_RESPONSE_CONTEXT + request_tag + payload,
        hashlib.sha256,
    ).digest()
    return payload + tag


def consume_authenticated_request(stream, key: bytes) -> tuple[bytes, int, int, int]:
    read_exact(stream, len(P2P_MAGIC) + P2P_AUTH_TAG_SIZE)
    transcript = hmac.new(
        key,
        P2P_REQUEST_TRANSCRIPT_CONTEXT,
        hashlib.sha256,
    )
    count_frame = read_exact(stream, 4)
    transcript.update(count_frame)
    entry_count = struct.unpack(">I", count_frame)[0]
    file_count = 0
    byte_count = 0
    for _index in range(entry_count):
        kind = read_exact(stream, 1)
        transcript.update(kind)
        path_size_frame = read_exact(stream, 4)
        transcript.update(path_size_frame)
        path_size = struct.unpack(">I", path_size_frame)[0]
        path_data = read_exact(stream, path_size)
        transcript.update(path_data)
        if kind == b"\x01":
            size_frame = read_exact(stream, 8)
            transcript.update(size_frame)
            size = struct.unpack(">Q", size_frame)[0]
        else:
            size = 0
        control_tag = read_exact(stream, P2P_AUTH_TAG_SIZE)
        transcript.update(control_tag)
        if kind == b"\x01":
            read_exact(stream, size)
            digest = read_exact(stream, P2P_AUTH_TAG_SIZE)
            authenticator = read_exact(stream, P2P_AUTH_TAG_SIZE)
            transcript.update(digest)
            transcript.update(authenticator)
            file_count += 1
            byte_count += size
    supplied_tag = read_exact(stream, P2P_AUTH_TAG_SIZE)
    if not hmac.compare_digest(supplied_tag, transcript.digest()):
        raise AssertionError("client request transcript tag is invalid")
    return supplied_tag, entry_count, file_count, byte_count


class ProtocolTestClient(ACBridgeP2PClient):
    def __init__(self, client_socket: socket.socket) -> None:
        self.client_socket = client_socket
        self.bridge = SimpleNamespace()
        self.adb = SimpleNamespace(device_ip_addresses=lambda: ["127.0.0.1"])
        self.settings = SimpleNamespace()
        self.cleanup_signals: list[bool] = []

    def _prepare_session(
        self,
        destination: str,
        timeout_seconds: int,
        connect_timeout: float,
        cancel_event=None,
        progress_callback=None,
    ) -> P2PSession:
        return P2PSession(
            "127.0.0.1", 4242, "a" * 64, int(time.time() * 1000) + 60_000, "b" * 32
        )

    def _connect(
        self, session: P2PSession, connect_timeout: float, cancel_event=None
    ) -> socket.socket:
        return self.client_socket

    def _cleanup_session_files(
        self, session_id: str, cancel_event=None, *, signal_cancel=True
    ) -> None:
        self.cleanup_signals.append(bool(signal_cancel))

    def _fetch_session_error(self, session_id: str, cancel_event=None) -> str:
        return ""


class PlanningOnlyClient(ACBridgeP2PClient):
    """Record dispatch decisions without opening a socket or touching ADB."""

    def __init__(self) -> None:
        super().__init__(
            SimpleNamespace(adb=SimpleNamespace(), settings=SimpleNamespace())
        )
        self.selected_parallelism: list[int] = []

    def _upload_entry_batch(self, entries, android_destination, **kwargs):
        self.selected_parallelism.append(1)
        files = [entry for entry in entries if not entry.is_directory]
        return P2PTransferResult(
            True,
            "planned",
            sum(entry.size for entry in files),
            len(files),
            len(entries),
        )

    def _upload_parallel_entries(
        self,
        entries,
        android_destination,
        *,
        parallelism,
        **kwargs,
    ):
        self.selected_parallelism.append(parallelism)
        files = [entry for entry in entries if not entry.is_directory]
        return P2PTransferResult(
            True,
            "planned",
            sum(entry.size for entry in files),
            len(files),
            len(entries),
        )


class ACBridgeP2PTests(unittest.TestCase):
    def test_session_repr_never_exposes_authentication_material(self) -> None:
        session = P2PSession(
            "192.0.2.10",
            42042,
            "c" * 64,
            int(time.time() * 1000) + 60_000,
            "d" * 32,
        )

        rendered = repr(session)

        self.assertNotIn(session.token, rendered)
        self.assertNotIn(session.session_id, rendered)
        self.assertIn("192.0.2.10", rendered)

    def test_android_bootstrap_separates_public_locator_from_secret(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (
            root
            / "openadb/resources/acbridge/src/com/communism420/acbridge/"
            "P2PTransferService.java"
        ).read_text(encoding="utf-8")
        manifest = (
            root / "openadb/resources/acbridge/AndroidManifest.xml"
        ).read_text(encoding="utf-8")

        self.assertIn('getStringExtra("request_id")', source)
        self.assertIn('getStringExtra("cancel_id")', source)
        self.assertIn('CONTROL_SOCKET_PREFIX = "openadb_p2p_control_"', source)
        self.assertIn("new LocalServerSocket(", source)
        self.assertIn("candidate.getPeerCredentials()", source)
        self.assertIn("uid == ADB_SHELL_UID || uid == ROOT_UID", source)
        self.assertIn("CONTROL_ACCEPT_TIMEOUT_SECONDS", source)
        self.assertIn('"OPENADB_P2P_2"', source)
        self.assertIn('"OADBP2P2"', source)
        self.assertIn("request.bootstrapSecret", source)
        self.assertIn("throwIfCancelled(controlSession)", source)
        self.assertIn('"PERMISSION_REQUIRED\\t"', source)
        self.assertIn('writeControlLine(controlSession, "ACCEPTED")', source)
        accepted = source.index('writeControlLine(controlSession, "ACCEPTED")')
        blocking_monitor = source.index("controlSocket.setSoTimeout(0)", accepted)
        self.assertLess(accepted, blocking_monitor)
        self.assertLess(
            blocking_monitor,
            source.index("startControlMonitor(controlSession", blocking_monitor),
        )
        monitor_source = source[
            source.index("private Thread startControlMonitor(") : source.index(
                "private void handleControlDisconnect("
            )
        ]
        self.assertNotIn("SocketTimeoutException", monitor_source)
        self.assertIn('"CANCEL".equals(command)', source)
        self.assertIn('"CLOSE".equals(command)', source)
        self.assertIn("REQUEST_TRANSCRIPT_CONTEXT", source)
        self.assertIn("ENTRY_CONTROL_CONTEXT", source)
        self.assertIn("writeAuthenticatedResponse", source)
        self.assertIn("server.setSoTimeout(250)", source)
        self.assertIn("server.getLocalPort()", source)
        self.assertIn("latestStartId.set(startId)", source)
        self.assertIn("stopSelfResult(startId)", source)
        self.assertNotIn("BOOTSTRAP_REQUEST_PREFIX", source)
        self.assertNotIn("BOOTSTRAP_STATUS_PREFIX", source)
        self.assertNotIn("BOOTSTRAP_CANCEL_PREFIX", source)
        self.assertNotIn("getFilesDir()", source)
        self.assertNotIn("run-as", source)
        self.assertNotIn('getStringExtra("session")', source)
        self.assertNotIn("appOutputDir()", source)
        self.assertIn('android:permission="android.permission.DUMP"', manifest)
        self.assertNotIn("android:debuggable=", manifest)
        control_verified = source.index(
            "P2P entry metadata authentication failed"
        )
        self.assertLess(
            control_verified,
            source.index("ensureDirectDirectory(directDestination", control_verified),
        )
        self.assertLess(
            control_verified,
            source.index("ensureRelativeDirectory(destinationDirectory", control_verified),
        )
        transcript_verified = source.index(
            "P2P request transcript authentication failed"
        )
        self.assertLess(
            transcript_verified,
            source.index("writeAuthenticatedResponse(", transcript_verified),
        )

    def test_ready_metadata_requires_the_bootstrap_hmac(self) -> None:
        bootstrap_secret = "b2" * 32
        token = "c3" * 32
        expires_at = int(time.time() * 1000) + 60_000
        ready = f"READY\t42042\t{token}\t{expires_at}"
        proof = hmac.new(
            bytes.fromhex(bootstrap_secret),
            ready.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        parsed = ACBridgeP2PClient._parse_ready_status(
            f"{ready}\t{proof}",
            expected_port=42042,
            bootstrap_secret=bootstrap_secret,
        )

        self.assertEqual(parsed, (42042, token, expires_at))
        self.assertEqual(
            ACBridgeP2PClient._parse_ready_status(
                f"{ready}\t{proof}",
                expected_port=0,
                bootstrap_secret=bootstrap_secret,
            ),
            parsed,
        )
        with self.assertRaisesRegex(P2PTransferError, "invalid"):
            ACBridgeP2PClient._parse_ready_status(
                f"{ready}\t{'0' * 64}",
                expected_port=42042,
                bootstrap_secret=bootstrap_secret,
            )

    def test_public_request_ids_isolate_parallel_control_sockets(self) -> None:
        first = "01" * 16
        second = "02" * 16
        bootstrap_secret = "ff" * 32

        socket_names = {
            P2P_CONTROL_SOCKET_PREFIX + first,
            P2P_CONTROL_SOCKET_PREFIX + second,
        }

        self.assertEqual(len(socket_names), 2)
        self.assertTrue(
            all(bootstrap_secret not in socket_name for socket_name in socket_names)
        )
        self.assertTrue(
            all(
                socket_name.startswith("openadb_p2p_control_")
                for socket_name in socket_names
            )
        )
        self.assertTrue(all(len(socket_name) < 108 for socket_name in socket_names))

    def test_control_channel_rejects_an_oversized_status_line(self) -> None:
        class OneChunkSocket:
            def __init__(self) -> None:
                self.chunk = b"x" * (P2P_CONTROL_MAX_LINE_BYTES + 1) + b"\n"

            def settimeout(self, _timeout):
                return None

            def recv(self, _size):
                chunk, self.chunk = self.chunk, b""
                return chunk

        channel = _P2PControlChannel(OneChunkSocket(), 43120)  # type: ignore[arg-type]

        with self.assertRaisesRegex(P2PTransferError, "oversized"):
            channel.read_line(deadline=time.monotonic() + 1)

    def test_control_forward_port_parser_rejects_unusable_output(self) -> None:
        self.assertEqual(_parse_forward_port("43123\r\n"), 43123)
        for value in ("", "not-a-port", "0", "65536", "43123 extra"):
            with self.subTest(value=value):
                with self.assertRaises(P2PTransferError):
                    _parse_forward_port(value)

    def test_control_connection_retries_until_android_listener_is_ready(self) -> None:
        attempts: list[tuple[tuple[str, int], float]] = []
        sent = bytearray()
        acknowledgement_reads: list[int] = []

        class RecordingSocket:
            def __init__(self) -> None:
                self.response = b"ACCEPTED\n"

            def settimeout(self, _timeout):
                return None

            def send(self, payload):
                sent.extend(payload)
                return len(payload)

            def recv(self, _size):
                acknowledgement_reads.append(_size)
                response, self.response = self.response, b""
                return response

            def shutdown(self, _how):
                return None

            def close(self):
                return None

        outcomes = iter(
            [
                OSError("listener not registered yet"),
                OSError("listener still starting"),
                RecordingSocket(),
            ]
        )

        def connect(address, timeout):
            attempts.append((address, timeout))
            outcome = next(outcomes)
            if isinstance(outcome, OSError):
                raise outcome
            return outcome

        client = ACBridgeP2PClient(
            SimpleNamespace(adb=SimpleNamespace(), settings=SimpleNamespace())
        )
        payload = b"OPENADB_P2P_2\n0\n120\n/storage/emulated/0\nsecret\n"
        with (
            patch(
                "openadb.core.acbridge_p2p.socket.create_connection",
                side_effect=connect,
            ),
            patch("openadb.core.acbridge_p2p.time.sleep"),
        ):
            channel = client._connect_control_channel(
                43123,
                payload,
                connect_timeout=5,
            )
        channel.close()

        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(address == ("127.0.0.1", 43123) for address, _ in attempts))
        self.assertEqual(bytes(sent), payload)
        self.assertEqual(len(acknowledgement_reads), 1)

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
            ensure_installed=lambda require_current=True, cancel_event=None: (
                True,
                "ready",
            ),
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

    def test_bridge_cancellation_after_setup_does_not_start_storage_activity(
        self,
    ) -> None:
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

    def test_cancelled_app_export_does_not_start_diagnostics_or_cache_import(
        self,
    ) -> None:
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
            request_id = "a1" * 16
            bootstrap_secret = "b2" * 32
            commands: list[str] = []
            pushed_text: list[str] = []
            control_commands: list[str] = []
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
                if request_id in command:
                    self.assertGreater(runner.active, 0)

            class FakeAdb:
                def __init__(self) -> None:
                    self.runner = runner

                def device_ip_addresses(self, cancel_event=None) -> list[str]:
                    return ["192.168.1.42"]

                def run_shell(self, command: str, timeout=None, cancel_event=None):
                    record_private_command(command)
                    return SimpleNamespace(
                        success=True, stdout="", stderr="", status=""
                    )

                def run_raw(self, args, timeout=None, cancel_event=None):
                    record_private_command(" ".join(args))
                    stdout = "43123\n" if args[:2] == ["forward", "tcp:0"] else ""
                    return SimpleNamespace(
                        success=True, stdout=stdout, stderr="", status=""
                    )

            class FakeControlChannel:
                forward_port = 43123

                def read_line(self, **_kwargs):
                    nonlocal status_reads
                    status_reads += 1
                    if status_reads == 1:
                        return "PERMISSION_REQUIRED\t/storage/ABCD-1234/Movies"
                    ready = (
                        f"READY\t42042\t{'c' * 64}\t"
                        f"{int(time.time() * 1000) + 60_000}"
                    )
                    proof = hmac.new(
                        bytes.fromhex(bootstrap_secret),
                        ready.encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()
                    return f"{ready}\t{proof}"

                def send_command(self, command):
                    control_commands.append(command)
                    return True

                def close(self):
                    return None

            class TestClient(ACBridgeP2PClient):
                def _connect_control_channel(
                    self,
                    forward_port,
                    request_payload,
                    **_kwargs,
                ):
                    if forward_port != 43123:
                        raise AssertionError("unexpected forwarded control port")
                    pushed_text.append(request_payload.decode("utf-8"))
                    return FakeControlChannel()

            cancel_event = threading.Event()

            def grant_storage_access(path: str, timeout: int, cancel_event=None):
                granted_paths.append(path)
                self.assertIs(cancel_event, cancel_event_from_prepare)
                return SimpleNamespace(
                    success=True, stdout="", stderr="", status="granted"
                )

            cancel_event_from_prepare = cancel_event

            bridge = SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path(temp)),
                ensure_installed=lambda require_current=True, cancel_event=None: (
                    True,
                    "ready",
                ),
                grant_storage_access=grant_storage_access,
            )
            client = TestClient(bridge)
            updates: list[dict] = []
            with (
                patch(
                    "openadb.core.acbridge_p2p.uuid.uuid4",
                    return_value=SimpleNamespace(hex=request_id),
                ),
                patch(
                    "openadb.core.acbridge_p2p.secrets.token_hex",
                    return_value=bootstrap_secret,
                ),
            ):
                session = client._prepare_session(
                    "/storage/ABCD-1234/Movies",
                    timeout_seconds=120,
                    connect_timeout=2,
                    cancel_event=cancel_event,
                    progress_callback=updates.append,
                )
                client._cleanup_session_files(
                    session.session_id,
                    signal_cancel=False,
                )

        self.assertEqual(session.port, 42042)
        self.assertEqual(session.token, "c" * 64)
        self.assertEqual(
            pushed_text[0].splitlines(),
            [
                "OPENADB_P2P_2",
                "0",
                "120",
                "/storage/ABCD-1234/Movies",
                bootstrap_secret,
            ],
        )
        self.assertNotIn("c" * 64, "\n".join(commands))
        self.assertNotIn(bootstrap_secret, "\n".join(commands))
        self.assertNotIn("/storage/ABCD-1234/Movies", "\n".join(commands))
        self.assertNotIn("run-as", "\n".join(commands))
        self.assertNotIn(bootstrap_secret, repr(session))
        self.assertNotIn(bootstrap_secret, repr(updates))
        self.assertEqual(session.session_id, request_id)
        self.assertNotIn("--es session", "\n".join(commands))
        self.assertIn("--es request_id", "\n".join(commands))
        self.assertIn(
            f"forward tcp:0 localabstract:{P2P_CONTROL_SOCKET_PREFIX}{request_id}",
            commands,
        )
        self.assertIn("forward --remove tcp:43123", commands)
        self.assertEqual(control_commands, ["CLOSE"])
        service_commands = [
            command for command in commands if "--es request_id" in command
        ]
        self.assertEqual(len(service_commands), 1)
        self.assertIn("getprop ro.build.version.sdk", service_commands[0])
        self.assertIn("startservice", service_commands[0])
        self.assertIn("start-foreground-service", service_commands[0])
        self.assertNotIn(ACBridgeClient.REMOTE_APP_DIR, "\n".join(commands))
        self.assertTrue(runner.scopes)
        self.assertTrue(
            all(session.session_id in values for _display, values in runner.scopes)
        )
        self.assertTrue(
            all(
                session.session_id not in " ".join(display)
                for display, _values in runner.scopes
            )
        )
        self.assertFalse(
            any(bootstrap_secret in values for _display, values in runner.scopes)
        )
        self.assertEqual(granted_paths, ["/storage/ABCD-1234/Movies"])
        self.assertTrue(
            any(
                "Waiting for MicroSD/USB access" in update.get("activity", "")
                for update in updates
            )
        )

    def test_control_forward_failures_redact_one_shot_values(self) -> None:
        request_id = "d4" * 16
        bootstrap_secret = "e5" * 32
        for failure_mode in ("result", "exception"):
            with self.subTest(failure_mode=failure_mode):
                scopes: list[tuple[str, ...]] = []
                raw_calls: list[list[str]] = []
                remote_spec = (
                    f"localabstract:{P2P_CONTROL_SOCKET_PREFIX}{request_id}"
                )

                class RecordingRunner:
                    @contextmanager
                    def scoped_log_command(self, _command, *, sensitive_values=()):
                        scopes.append(tuple(sensitive_values))
                        yield

                class FakeAdb:
                    runner = RecordingRunner()

                    @staticmethod
                    def device_ip_addresses(cancel_event=None):
                        return ["192.0.2.10"]

                    @staticmethod
                    def run_raw(args, **_kwargs):
                        raw_calls.append(list(args))
                        if args == ["forward", "--list"]:
                            return SimpleNamespace(
                                success=True,
                                stdout=f"device tcp:43129 {remote_spec}\n",
                                stderr="",
                                status="",
                            )
                        if args == ["forward", "--remove", "tcp:43129"]:
                            return SimpleNamespace(
                                success=True,
                                stdout="",
                                stderr="",
                                status="",
                            )
                        if failure_mode == "exception":
                            raise RuntimeError(
                                f"forward rejected {request_id} {bootstrap_secret}"
                            )
                        return SimpleNamespace(
                            success=False,
                            stdout="",
                            stderr=f"forward rejected {request_id} {bootstrap_secret}",
                            status="Command failed with exit code 1",
                        )

                bridge = SimpleNamespace(
                    adb=FakeAdb(),
                    settings=SimpleNamespace(temp_folder=Path("unused")),
                    ensure_installed=lambda **_kwargs: (True, "ready"),
                )
                client = ACBridgeP2PClient(bridge)
                with (
                    patch(
                        "openadb.core.acbridge_p2p.uuid.uuid4",
                        return_value=SimpleNamespace(hex=request_id),
                    ),
                    patch(
                        "openadb.core.acbridge_p2p.secrets.token_hex",
                        return_value=bootstrap_secret,
                    ),
                    self.assertRaises(P2PTransferError) as raised,
                ):
                    client._prepare_session(
                        "/storage/emulated/0/Download",
                        timeout_seconds=120,
                        connect_timeout=2,
                    )

                self.assertNotIn(bootstrap_secret, str(raised.exception))
                self.assertNotIn(request_id, str(raised.exception))
                self.assertIn("[private]", str(raised.exception))
                self.assertTrue(any(request_id in values for values in scopes))
                self.assertFalse(any(bootstrap_secret in values for values in scopes))
                self.assertIn(["forward", "--list"], raw_calls)
                self.assertIn(
                    ["forward", "--remove", "tcp:43129"],
                    raw_calls,
                )

    def test_control_forward_failure_prefers_actionable_stderr(self) -> None:
        request_id = "d5" * 16
        bootstrap_secret = "e6" * 32

        class FakeAdb:
            runner = SimpleNamespace()

            @staticmethod
            def device_ip_addresses(cancel_event=None):
                return ["192.0.2.10"]

            @staticmethod
            def run_raw(_args, **_kwargs):
                return SimpleNamespace(
                    success=False,
                    stdout="",
                    stderr="error: cannot bind the requested local control listener",
                    status="Command failed with exit code 1",
                )

        bridge = SimpleNamespace(
            adb=FakeAdb(),
            settings=SimpleNamespace(temp_folder=Path("unused")),
            ensure_installed=lambda **_kwargs: (True, "ready"),
        )
        client = ACBridgeP2PClient(bridge)
        with (
            patch(
                "openadb.core.acbridge_p2p.uuid.uuid4",
                return_value=SimpleNamespace(hex=request_id),
            ),
            patch(
                "openadb.core.acbridge_p2p.secrets.token_hex",
                return_value=bootstrap_secret,
            ),
            self.assertRaises(P2PTransferError) as raised,
        ):
            client._prepare_session(
                "/storage/emulated/0/Download",
                timeout_seconds=120,
                connect_timeout=2,
            )

        message = str(raised.exception)
        self.assertIn("cannot bind the requested local control listener", message)
        self.assertNotIn("Command failed with exit code 1", message)
        self.assertNotIn(request_id, message)
        self.assertNotIn(bootstrap_secret, message)

    def test_cancel_immediately_after_forward_removes_exact_port(self) -> None:
        request_id = "d7" * 16
        cancel_event = threading.Event()
        raw_calls: list[list[str]] = []

        class FakeAdb:
            @staticmethod
            def device_ip_addresses(cancel_event=None):
                return ["192.0.2.10"]

            @staticmethod
            def run_raw(args, **_kwargs):
                raw_calls.append(list(args))
                if args[:2] == ["forward", "tcp:0"]:
                    cancel_event.set()
                    return SimpleNamespace(
                        success=True,
                        stdout="43128\n",
                        stderr="",
                        status="",
                    )
                return SimpleNamespace(
                    success=True,
                    stdout="",
                    stderr="",
                    status="",
                )

            @staticmethod
            def run_shell(*_args, **_kwargs):
                raise AssertionError("cancelled bootstrap must not start ACBridge")

        bridge = SimpleNamespace(
            adb=FakeAdb(),
            settings=SimpleNamespace(temp_folder=Path("unused")),
            ensure_installed=lambda **_kwargs: (True, "ready"),
        )
        client = ACBridgeP2PClient(bridge)
        with (
            patch(
                "openadb.core.acbridge_p2p.uuid.uuid4",
                return_value=SimpleNamespace(hex=request_id),
            ),
            self.assertRaisesRegex(P2PTransferError, "cancelled"),
        ):
            client._prepare_session(
                "/storage/emulated/0/Download",
                timeout_seconds=120,
                connect_timeout=2,
                cancel_event=cancel_event,
            )

        self.assertEqual(
            raw_calls,
            [
                [
                    "forward",
                    "tcp:0",
                    f"localabstract:{P2P_CONTROL_SOCKET_PREFIX}{request_id}",
                ],
                ["forward", "--remove", "tcp:43128"],
            ],
        )
        self.assertNotIn(request_id, client._control_handles)

    def test_malformed_forward_output_removes_only_matching_forward(self) -> None:
        request_id = "d8" * 16
        remote_spec = f"localabstract:{P2P_CONTROL_SOCKET_PREFIX}{request_id}"
        unrelated_spec = f"localabstract:{P2P_CONTROL_SOCKET_PREFIX}{'f9' * 16}"
        raw_calls: list[list[str]] = []

        class FakeAdb:
            @staticmethod
            def device_ip_addresses(cancel_event=None):
                return ["192.0.2.10"]

            @staticmethod
            def run_raw(args, **_kwargs):
                raw_calls.append(list(args))
                if args[:2] == ["forward", "tcp:0"]:
                    return SimpleNamespace(
                        success=True,
                        stdout="unexpected dynamic-forward response",
                        stderr="",
                        status="",
                    )
                if args == ["forward", "--list"]:
                    return SimpleNamespace(
                        success=True,
                        stdout=(
                            f"device-1 tcp:43129 {unrelated_spec}\n"
                            f"device-1 tcp:43130 {remote_spec}\n"
                        ),
                        stderr="",
                        status="",
                    )
                return SimpleNamespace(
                    success=True,
                    stdout="",
                    stderr="",
                    status="",
                )

            @staticmethod
            def run_shell(*_args, **_kwargs):
                raise AssertionError("malformed forward must not start ACBridge")

        bridge = SimpleNamespace(
            adb=FakeAdb(),
            settings=SimpleNamespace(temp_folder=Path("unused")),
            ensure_installed=lambda **_kwargs: (True, "ready"),
        )
        client = ACBridgeP2PClient(bridge)
        with (
            patch(
                "openadb.core.acbridge_p2p.uuid.uuid4",
                return_value=SimpleNamespace(hex=request_id),
            ),
            self.assertRaisesRegex(
                P2PTransferError,
                "did not return a valid local port",
            ),
        ):
            client._prepare_session(
                "/storage/emulated/0/Download",
                timeout_seconds=120,
                connect_timeout=2,
            )

        self.assertEqual(
            raw_calls,
            [
                ["forward", "tcp:0", remote_spec],
                ["forward", "--list"],
                ["forward", "--remove", "tcp:43130"],
            ],
        )
        self.assertNotIn(["forward", "--remove", "tcp:43129"], raw_calls)
        self.assertNotIn(request_id, client._control_handles)

    def test_control_status_error_redacts_request_id_and_secret(self) -> None:
        request_id = "d6" * 16
        bootstrap_secret = "e7" * 32

        class FakeAdb:
            @staticmethod
            def device_ip_addresses(cancel_event=None):
                return ["192.0.2.10"]

            @staticmethod
            def run_raw(args, **_kwargs):
                stdout = "43127\n" if args[:2] == ["forward", "tcp:0"] else ""
                return SimpleNamespace(
                    success=True,
                    stdout=stdout,
                    stderr="",
                    status="",
                )

            @staticmethod
            def run_shell(_command, **_kwargs):
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        class ErrorChannel:
            def read_line(self, **_kwargs):
                return f"ERROR\tbootstrap rejected {request_id} {bootstrap_secret}"

        class TestClient(ACBridgeP2PClient):
            def _connect_control_channel(self, *_args, **_kwargs):
                return ErrorChannel()

            def _cleanup_session_files(
                self, session_id, cancel_event=None, *, signal_cancel=True
            ):
                return None

        bridge = SimpleNamespace(
            adb=FakeAdb(),
            settings=SimpleNamespace(temp_folder=Path("unused")),
            ensure_installed=lambda **_kwargs: (True, "ready"),
        )
        client = TestClient(bridge)
        with (
            patch(
                "openadb.core.acbridge_p2p.uuid.uuid4",
                return_value=SimpleNamespace(hex=request_id),
            ),
            patch(
                "openadb.core.acbridge_p2p.secrets.token_hex",
                return_value=bootstrap_secret,
            ),
            self.assertRaises(P2PTransferError) as raised,
        ):
            client._prepare_session(
                "/storage/emulated/0/Download",
                timeout_seconds=120,
                connect_timeout=2,
            )

        message = str(raised.exception)
        self.assertIn("bootstrap rejected", message)
        self.assertIn("[private]", message)
        self.assertNotIn(request_id, message)
        self.assertNotIn(bootstrap_secret, message)

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
            key = bytes.fromhex("a" * 64)

            def server() -> None:
                stream = server_socket.makefile("rwb", buffering=1024 * 1024)
                try:
                    observed["magic"] = read_exact(stream, len(P2P_MAGIC))
                    observed["proof"] = read_exact(stream, 32)
                    transcript = hmac.new(
                        key,
                        P2P_REQUEST_TRANSCRIPT_CONTEXT,
                        hashlib.sha256,
                    )
                    count_frame = read_exact(stream, 4)
                    transcript.update(count_frame)
                    observed["count"] = struct.unpack(">I", count_frame)[0]
                    kind_frame = read_exact(stream, 1)
                    transcript.update(kind_frame)
                    observed["kind"] = kind_frame
                    path_size_frame = read_exact(stream, 4)
                    path_size = struct.unpack(">I", path_size_frame)[0]
                    path_data = read_exact(stream, path_size)
                    path_frame = path_size_frame + path_data
                    transcript.update(path_frame)
                    observed["path"] = path_data.decode("utf-8")
                    size_frame = read_exact(stream, 8)
                    transcript.update(size_frame)
                    size = struct.unpack(">Q", size_frame)[0]
                    control_tag = read_exact(stream, P2P_AUTH_TAG_SIZE)
                    transcript.update(control_tag)
                    observed["control_tag"] = control_tag
                    data = read_exact(stream, size)
                    observed["data"] = data
                    digest = read_exact(stream, 32)
                    transcript.update(digest)
                    observed["digest"] = digest
                    authenticator = read_exact(stream, 32)
                    transcript.update(authenticator)
                    observed["authenticator"] = authenticator
                    request_tag = read_exact(stream, P2P_AUTH_TAG_SIZE)
                    observed["request_tag"] = request_tag
                    observed["expected_request_tag"] = transcript.digest()
                    stream.write(
                        authenticated_response(
                            key,
                            request_tag,
                            success=True,
                            entry_count=1,
                            file_count=1,
                            byte_count=len(payload),
                            message="verified",
                        )
                    )
                    stream.flush()
                finally:
                    stream.close()
                    server_socket.close()

            thread = threading.Thread(target=server)
            thread.start()
            updates: list[dict] = []
            client = ProtocolTestClient(client_socket)
            result = client.upload(
                [source],
                "/storage/ABCD-1234/Movies",
                progress_callback=updates.append,
            )
            thread.join(timeout=5)

        self.assertTrue(result.success)
        self.assertEqual(result.message, "verified")
        self.assertEqual(client.cleanup_signals, [False])
        self.assertEqual(observed["magic"], P2P_MAGIC)
        self.assertEqual(
            observed["proof"], hmac.new(key, P2P_MAGIC, hashlib.sha256).digest()
        )
        self.assertEqual(observed["path"], "movie.bin")
        self.assertEqual(observed["data"], payload)
        self.assertEqual(observed["digest"], hashlib.sha256(payload).digest())
        authenticator = hmac.new(key, digestmod=hashlib.sha256)
        authenticator.update(b"movie.bin\x00")
        authenticator.update(struct.pack(">Q", len(payload)))
        authenticator.update(payload)
        self.assertEqual(observed["authenticator"], authenticator.digest())
        expected_control = hmac.new(
            key,
            P2P_ENTRY_CONTROL_CONTEXT
            + struct.pack(">I", 0)
            + b"\x01"
            + text_frame("movie.bin")
            + struct.pack(">Q", len(payload)),
            hashlib.sha256,
        ).digest()
        self.assertEqual(observed["control_tag"], expected_control)
        self.assertEqual(observed["request_tag"], observed["expected_request_tag"])
        self.assertTrue(
            any(
                update.get("activity") == "Direct ACBridge P2P upload"
                for update in updates
            )
        )

    def test_file_growth_after_planning_is_rejected_before_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "growing.bin"
            source.write_bytes(b"planned bytes")
            original_size = source.stat().st_size
            left, right = socket.socketpair()

            def drain_server() -> None:
                try:
                    while right.recv(65_536):
                        pass
                finally:
                    right.close()

            thread = threading.Thread(target=drain_server)
            thread.start()
            grew = False

            def grow_after_payload(update: dict) -> None:
                nonlocal grew
                if not grew and update.get("done_bytes") == original_size:
                    with source.open("ab") as output:
                        output.write(b" appended")
                    grew = True

            try:
                with self.assertRaisesRegex(
                    P2PTransferError,
                    "grew or changed size",
                ):
                    ProtocolTestClient(left).upload(
                        [source],
                        "/storage/emulated/0/Download",
                        progress_callback=grow_after_payload,
                    )
            finally:
                left.close()
                thread.join(timeout=3)

        self.assertTrue(grew)
        self.assertFalse(thread.is_alive())

    def test_forged_truncated_and_miscounted_responses_are_rejected(self) -> None:
        key = bytes.fromhex("a" * 64)
        cases = (
            ("forged", "failed authentication"),
            ("truncated", "interrupted"),
            ("miscounted", "inconsistent authenticated transfer counts"),
        )
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "payload.bin"
            source.write_bytes(b"authenticated payload")
            for case, expected_error in cases:
                with self.subTest(case=case):
                    client_socket, server_socket = socket.socketpair()

                    def server() -> None:
                        stream = server_socket.makefile("rwb")
                        try:
                            request_tag, entries, files, byte_count = (
                                consume_authenticated_request(stream, key)
                            )
                            response = authenticated_response(
                                key,
                                request_tag,
                                success=True,
                                entry_count=entries,
                                file_count=(0 if case == "miscounted" else files),
                                byte_count=byte_count,
                                message="stored",
                            )
                            if case == "forged":
                                response = response[:-1] + bytes([response[-1] ^ 1])
                            elif case == "truncated":
                                response = response[:-8]
                            stream.write(response)
                            stream.flush()
                        finally:
                            stream.close()
                            server_socket.close()

                    thread = threading.Thread(target=server)
                    thread.start()
                    client = ProtocolTestClient(client_socket)
                    try:
                        with self.assertRaisesRegex(P2PTransferError, expected_error):
                            client.upload([source], "/storage/emulated/0/Download")
                    finally:
                        client_socket.close()
                        thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(client.cleanup_signals, [True])

    def test_cancelled_transfer_never_opens_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "file.bin"
            source.write_bytes(b"data")
            cancel = threading.Event()
            cancel.set()
            left, right = socket.socketpair()
            try:
                with self.assertRaisesRegex(P2PTransferError, "cancelled"):
                    ProtocolTestClient(left).upload(
                        [source], "/storage/ABCD-1234", cancel_event=cancel
                    )
            finally:
                left.close()
                right.close()

    def test_cleanup_failure_never_overrides_primary_transfer_cancellation(
        self,
    ) -> None:
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

            def _cleanup_session_files(
                self, session_id, cancel_event=None, *, signal_cancel=True
            ):
                raise RuntimeError("cleanup exploded with private state")

        with self.assertRaisesRegex(P2PTransferError, "cancelled by user"):
            CleanupFailureClient()._upload_entry_batch(
                [],
                "/storage/emulated/0/Download",
                progress_callback=updates.append,
            )

        self.assertTrue(
            any("cleanup could not finish" in str(update) for update in updates)
        )
        self.assertNotIn("cleanup exploded", str(updates))

    def test_cleanup_warning_callback_failure_never_overrides_primary_error(
        self,
    ) -> None:
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

            def _cleanup_session_files(
                self, session_id, cancel_event=None, *, signal_cancel=True
            ):
                raise RuntimeError("cleanup exploded with one-time private state")

            def _fetch_session_error(self, session_id, cancel_event=None):
                return ""

        def rejecting_callback(_update: dict) -> None:
            raise RuntimeError("warning callback rejected the update")

        with self.assertRaisesRegex(
            P2PTransferError, "authoritative protocol failure"
        ) as raised:
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

            def run_raw(self, args, **_kwargs):
                self.assert_forward(args)
                return SimpleNamespace(
                    success=True,
                    status="",
                    stdout="43126\n",
                    stderr="",
                )

            @staticmethod
            def assert_forward(args):
                if args[:2] != ["forward", "tcp:0"]:
                    raise AssertionError(f"unexpected ADB command: {args!r}")

            def run_shell(self, _command, **_kwargs):
                return SimpleNamespace(
                    success=False,
                    status="primary bootstrap failure",
                    stdout="",
                    stderr="",
                )

        class CleanupFailureClient(ACBridgeP2PClient):
            def _cleanup_session_files(
                self, session_id, cancel_event=None, *, signal_cancel=True
            ):
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

        self.assertTrue(
            any("cleanup could not finish" in str(update) for update in updates)
        )
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
                    consume_authenticated_request(stream, bytes.fromhex("a" * 64))
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

    def test_cancelled_session_cleanup_closes_one_private_forward(self) -> None:
        raw_calls: list[tuple[list[str], float]] = []
        shell_calls: list[str] = []
        channel_commands: list[str] = []

        class FakeAdb:
            def run_shell(self, command, timeout=None):
                shell_calls.append(command)
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def run_raw(self, args, timeout=None):
                raw_calls.append((list(args), timeout))
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        class FakeChannel:
            def send_command(self, command):
                channel_commands.append(command)
                return True

            def close(self):
                return None

        client = ACBridgeP2PClient(
            SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path("unused")),
            )
        )
        session_id = "a" * 32
        client._control_handles[session_id] = SimpleNamespace(
            forward_port=43123,
            channel=FakeChannel(),
            service_started=True,
        )
        cancel = threading.Event()
        cancel.set()

        client._cleanup_session_files(session_id, cancel_event=cancel)

        self.assertEqual(channel_commands, ["CANCEL"])
        self.assertEqual(raw_calls, [(["forward", "--remove", "tcp:43123"], 1.5)])
        self.assertEqual(len(shell_calls), 1)
        self.assertIn("--es cancel_id", shell_calls[0])
        self.assertIn(session_id, shell_calls[0])

    def test_cleanup_falls_back_to_service_cancel_before_removing_forward(
        self,
    ) -> None:
        operations: list[tuple[str, object]] = []

        class FakeAdb:
            def run_shell(self, command, timeout=None):
                operations.append(("shell", command))
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def run_raw(self, args, timeout=None):
                operations.append(("raw", list(args)))
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        client = ACBridgeP2PClient(
            SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path("unused")),
            )
        )
        session_id = "04" * 16
        client._control_handles[session_id] = SimpleNamespace(
            forward_port=43104,
            channel=None,
            service_started=True,
        )

        client._cleanup_session_files(session_id)

        self.assertEqual([kind for kind, _value in operations], ["shell", "raw"])
        cancel_command = str(operations[0][1])
        self.assertIn("--es cancel_id", cancel_command)
        self.assertIn(session_id, cancel_command)
        self.assertIn("getprop ro.build.version.sdk", cancel_command)
        self.assertIn("startservice", cancel_command)
        self.assertIn("start-foreground-service", cancel_command)
        self.assertNotIn("run-as", cancel_command)
        self.assertEqual(
            operations[1],
            ("raw", ["forward", "--remove", "tcp:43104"]),
        )

    def test_parallel_session_control_cleanup_never_crosses_forwards(self) -> None:
        removed_forwards: list[str] = []
        cancelled_sessions: list[str] = []
        channel_commands: dict[str, list[str]] = {}

        class FakeAdb:
            def run_shell(self, command, timeout=None):
                for session_id in (first, second):
                    if session_id in command:
                        cancelled_sessions.append(session_id)
                        break
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def run_raw(self, args, timeout=None):
                removed_forwards.append(args[-1])
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        class FakeChannel:
            def __init__(self, session_id):
                self.session_id = session_id
                channel_commands[session_id] = []

            def send_command(self, command):
                channel_commands[self.session_id].append(command)
                return True

            def close(self):
                return None

        client = ACBridgeP2PClient(
            SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path("unused")),
            )
        )
        first = "01" * 16
        second = "02" * 16
        client._control_handles[first] = SimpleNamespace(
            forward_port=43101,
            channel=FakeChannel(first),
            service_started=True,
        )
        client._control_handles[second] = SimpleNamespace(
            forward_port=43102,
            channel=FakeChannel(second),
            service_started=True,
        )

        client._cleanup_session_files(first)
        client._cleanup_session_files(second)

        self.assertEqual(channel_commands[first], ["CANCEL"])
        self.assertEqual(channel_commands[second], ["CANCEL"])
        self.assertEqual(cancelled_sessions, [first, second])
        self.assertEqual(removed_forwards, ["tcp:43101", "tcp:43102"])

    def test_success_cleanup_closes_channel_without_remote_cancel(self) -> None:
        raw_commands: list[list[str]] = []
        channel_commands: list[str] = []

        class FakeAdb:
            def run_raw(self, args, timeout=None):
                raw_commands.append(list(args))
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

        class FakeChannel:
            def send_command(self, command):
                channel_commands.append(command)
                return True

            def close(self):
                return None

        client = ACBridgeP2PClient(
            SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path("unused")),
            )
        )

        session_id = "03" * 16
        client._control_handles[session_id] = SimpleNamespace(
            forward_port=43103,
            channel=FakeChannel(),
            service_started=True,
        )

        client._cleanup_session_files(session_id, signal_cancel=False)

        self.assertEqual(channel_commands, ["CLOSE"])
        self.assertEqual(
            raw_commands,
            [["forward", "--remove", "tcp:43103"]],
        )

    def test_cancellation_during_control_read_cleans_remote_session(self) -> None:
        cancel_event = threading.Event()
        cleaned_sessions: list[str] = []

        class FakeAdb:
            def device_ip_addresses(self, cancel_event=None):
                return ["192.0.2.10"]

            def run_shell(self, _command, **_kwargs):
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def run_raw(self, args, **_kwargs):
                stdout = "43124\n" if args[:2] == ["forward", "tcp:0"] else ""
                return SimpleNamespace(success=True, stdout=stdout, stderr="", status="")

        class CancelDuringReadChannel:
            def read_line(self, **_kwargs):
                cancel_event.set()
                return ""

        class CancelDuringStatusClient(ACBridgeP2PClient):
            def _connect_control_channel(self, *_args, **_kwargs):
                return CancelDuringReadChannel()

            def _cleanup_session_files(
                self, session_id, cancel_event=None, *, signal_cancel=True
            ) -> None:
                cleaned_sessions.append(session_id)

        with tempfile.TemporaryDirectory() as temp:
            bridge = SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path(temp)),
                ensure_installed=lambda require_current=True, cancel_event=None: (
                    True,
                    "ready",
                ),
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

            def run_raw_binary_output(self, *_args, **_kwargs):
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

    def test_connect_timeout_never_depends_on_android_wall_clock(self) -> None:
        class SessionWithUnusableWallClock:
            host = "192.0.2.10"
            port = 42042

            @property
            def expires_at_ms(self):
                raise AssertionError("cross-device wall clock must not be read")

        client = ACBridgeP2PClient(
            SimpleNamespace(
                adb=SimpleNamespace(
                    device_ip_addresses=lambda cancel_event=None: ["192.0.2.10"]
                ),
                settings=SimpleNamespace(temp_folder=Path("unused")),
            )
        )
        connected = MagicMock()

        with patch(
            "openadb.core.acbridge_p2p.socket.create_connection",
            return_value=connected,
        ):
            result = client._connect(
                SessionWithUnusableWallClock(),  # type: ignore[arg-type]
                connect_timeout=5,
            )

        self.assertIs(result, connected)

    def test_local_control_read_error_still_cleans_remote_session(self) -> None:
        cleaned_sessions: list[str] = []

        class FakeAdb:
            def device_ip_addresses(self, cancel_event=None):
                return ["192.0.2.10"]

            def run_shell(self, _command, **_kwargs):
                return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def run_raw(self, args, **_kwargs):
                stdout = "43125\n" if args[:2] == ["forward", "tcp:0"] else ""
                return SimpleNamespace(success=True, stdout=stdout, stderr="", status="")

        class FailingControlChannel:
            def read_line(self, **_kwargs):
                raise OSError("temporary control channel unavailable")

        class FailingStatusClient(ACBridgeP2PClient):
            def _connect_control_channel(self, *_args, **_kwargs):
                return FailingControlChannel()

            def _cleanup_session_files(
                self, session_id, cancel_event=None, *, signal_cancel=True
            ) -> None:
                cleaned_sessions.append(session_id)

        with tempfile.TemporaryDirectory() as temp:
            bridge = SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path(temp)),
                ensure_installed=lambda require_current=True, cancel_event=None: (
                    True,
                    "ready",
                ),
            )
            client = FailingStatusClient(bridge)

            with self.assertRaisesRegex(OSError, "control channel"):
                client._prepare_session(
                    "/storage/emulated/0/Download",
                    timeout_seconds=120,
                    connect_timeout=2,
                )

        self.assertEqual(len(cleaned_sessions), 1)

    def test_auto_parallelism_is_selected_after_entries_are_collected(self) -> None:
        file_size = 8 * 1024 * 1024
        entries = [
            P2PEntry(Path(f"file-{index}.bin"), f"file-{index}.bin", file_size, False)
            for index in range(32)
        ]
        updates: list[dict] = []
        client = PlanningOnlyClient()

        with patch(
            "openadb.core.acbridge_p2p.socket.create_connection",
            side_effect=AssertionError("planning must not open the network"),
        ):
            result = client.upload_entries(
                entries,
                "/sdcard/Download",
                parallelism=None,
                parallelism_mode="auto",
                progress_callback=updates.append,
            )

        self.assertTrue(result.success)
        self.assertEqual(client.selected_parallelism, [4])
        self.assertTrue(
            any(
                update.get("message") == "Auto selected 4 streams for 32 files"
                for update in updates
            )
        )
        self.assertEqual(updates[0]["parallelism"], 4)
        self.assertEqual(updates[0]["parallelism_mode"], "auto")

    def test_legacy_parallelism_api_remains_a_manual_override(self) -> None:
        entries = [
            P2PEntry(Path(f"file-{index}.bin"), f"file-{index}.bin", 1, False)
            for index in range(3)
        ]
        updates: list[dict] = []
        client = PlanningOnlyClient()

        client.upload_entries(
            entries,
            "/sdcard/Download",
            parallelism=3,
            progress_callback=updates.append,
        )

        self.assertEqual(client.selected_parallelism, [3])
        self.assertTrue(
            any(
                update.get("message") == "Manual selected 3 streams for 3 files"
                for update in updates
            )
        )

    def test_legacy_default_remains_one_manual_stream(self) -> None:
        entries = [
            P2PEntry(Path(f"file-{index}.bin"), f"file-{index}.bin", 1, False)
            for index in range(3)
        ]
        updates: list[dict] = []
        client = PlanningOnlyClient()

        client.upload_entries(
            entries,
            "/sdcard/Download",
            progress_callback=updates.append,
        )

        self.assertEqual(client.selected_parallelism, [1])
        self.assertTrue(
            any(
                update.get("message") == "Manual selected 1 streams for 3 files"
                for update in updates
            )
        )

    def test_directory_only_plan_reports_one_meaningful_stream(self) -> None:
        entries = [
            P2PEntry(None, "Empty", 0, True),
            P2PEntry(None, "Empty/Nested", 0, True),
        ]
        updates: list[dict] = []
        client = PlanningOnlyClient()

        result = client.upload_entries(
            entries,
            "/storage/emulated/0/Download",
            parallelism=None,
            parallelism_mode="auto",
            progress_callback=updates.append,
        )

        self.assertTrue(result.success)
        self.assertEqual(client.selected_parallelism, [1])
        rendered = repr(updates)
        self.assertIn("One P2P stream selected for directory entries", rendered)
        self.assertNotIn("for 0 files", rendered)

    def test_parallel_upload_balances_files_without_extra_directory_session(
        self,
    ) -> None:
        class RecordingParallelClient(ACBridgeP2PClient):
            def __init__(self) -> None:
                super().__init__(
                    SimpleNamespace(adb=SimpleNamespace(), settings=SimpleNamespace())
                )
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
        self.assertEqual(len(client.batches), 3)
        self.assertTrue(any(entry.is_directory for entry in client.batches[0]))
        self.assertTrue(
            all(
                not entry.is_directory
                for batch in client.batches[1:]
                for entry in batch
            )
        )
        transferred = [
            entry.relative_path
            for batch in client.batches
            for entry in batch
            if not entry.is_directory
        ]
        self.assertCountEqual(
            transferred,
            ["Folder/large.bin", "Folder/medium.bin", "Folder/small.bin"],
        )
        transferred_directories = [
            entry.relative_path
            for batch in client.batches
            for entry in batch
            if entry.is_directory
        ]
        self.assertCountEqual(
            transferred_directories,
            ["Folder"],
        )
        self.assertIn("3 parallel", result.message)


if __name__ == "__main__":
    unittest.main()
