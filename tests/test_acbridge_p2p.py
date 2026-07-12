from __future__ import annotations

import hashlib
import hmac
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openadb.core.acbridge_p2p import (
    P2P_MAGIC,
    ACBridgeP2PClient,
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

    def _cleanup_session_files(self, session_id: str) -> None:
        return None

    def _fetch_session_error(self, session_id: str) -> str:
        return ""


class ACBridgeP2PTests(unittest.TestCase):
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

            class FakeAdb:
                def device_ip_addresses(self) -> list[str]:
                    return ["192.168.1.50"]

                def run_shell(self, command: str, timeout=None):
                    commands.append(command)
                    stdout = "READY\n" if "test -f" in command else ""
                    return SimpleNamespace(success=True, stdout=stdout, stderr="", status="")

                def push(self, source, destination, timeout=None):
                    pushed_text.append(Path(source).read_text(encoding="utf-8"))
                    return SimpleNamespace(success=True, stdout="", stderr="", status="")

                def run_raw_binary_output_to_file(self, args, destination, timeout=None):
                    nonlocal status_reads
                    commands.append(" ".join(args))
                    status_reads += 1
                    status = "PERMISSION_REQUIRED\t/storage/ABCD-1234/Movies"
                    if status_reads > 1:
                        status = f"READY\t42042\t{'c' * 64}\t{int(time.time() * 1000) + 60_000}"
                    Path(destination).write_text(status, encoding="utf-8")
                    return SimpleNamespace(success=True, stdout="", stderr="", status="")

            def grant_storage_access(path: str, timeout: int):
                granted_paths.append(path)
                return SimpleNamespace(success=True, stdout="", stderr="", status="granted")

            bridge = SimpleNamespace(
                adb=FakeAdb(),
                settings=SimpleNamespace(temp_folder=Path(temp)),
                ensure_installed=lambda require_current=True: (True, "ready"),
                grant_storage_access=grant_storage_access,
            )
            client = ACBridgeP2PClient(bridge)
            updates: list[dict] = []
            with patch("openadb.core.acbridge_p2p.secrets.randbelow", return_value=6042):
                session = client._prepare_session(
                    "/storage/ABCD-1234/Movies",
                    timeout_seconds=120,
                    connect_timeout=2,
                    progress_callback=updates.append,
                )

        self.assertEqual(session.port, 42042)
        self.assertEqual(session.token, "c" * 64)
        self.assertTrue(pushed_text[0].startswith("OPENADB_P2P_1\n42042\n"))
        self.assertNotIn("c" * 64, "\n".join(commands))
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


if __name__ == "__main__":
    unittest.main()
