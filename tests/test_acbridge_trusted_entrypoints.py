from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from openadb.core.acbridge import ACBridgeClient
from openadb.core.acbridge_p2p import ACBridgeP2PClient, P2PTransferError
from openadb.core.shizuku import ShizukuClient
from openadb.models.command_result import CommandResult


def command_result() -> CommandResult:
    now = datetime.now(timezone.utc)
    return CommandResult(
        command=["adb", "<test>"],
        exit_code=0,
        stdout="",
        stderr="",
        duration=0.0,
        started_at=now,
        finished_at=now,
        success=True,
    )


class ACBridgeTrustedEntrypointTests(unittest.TestCase):
    def _client(self, temporary: str) -> tuple[ACBridgeClient, SimpleNamespace]:
        adb = SimpleNamespace(run_shell=MagicMock(return_value=command_result()))
        client = ACBridgeClient(
            adb,
            SimpleNamespace(temp_folder=Path(temporary)),
            temp_folder=Path(temporary),
        )
        client.ensure_trusted = MagicMock(  # type: ignore[method-assign]
            return_value=(False, "untrusted helper")
        )
        return client, adb

    def test_app_metadata_export_stops_before_starting_untrusted_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, _adb = self._client(temporary)
            client._prepare_run = MagicMock()  # type: ignore[method-assign]
            client._start_bridge = MagicMock()  # type: ignore[method-assign]

            result = client.load_app_data({"com.example.app": ("1", "1")})

        self.assertFalse(result.available)
        self.assertIn("untrusted", result.message)
        client.ensure_trusted.assert_called_once_with(cancel_event=None)
        client._prepare_run.assert_not_called()
        client._start_bridge.assert_not_called()

    def test_delete_stops_before_starting_untrusted_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, _adb = self._client(temporary)
            client._prepare_delete = MagicMock()  # type: ignore[method-assign]
            client._start_delete = MagicMock()  # type: ignore[method-assign]

            result = client.delete_path("/sdcard/Download/example.bin")

        self.assertFalse(result.success)
        self.assertIn("untrusted", result.status)
        client.ensure_trusted.assert_called_once_with(cancel_event=None)
        client._prepare_delete.assert_not_called()
        client._start_delete.assert_not_called()

    def test_storage_grant_stops_before_starting_untrusted_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client, _adb = self._client(temporary)
            client._prepare_storage_grant = MagicMock()  # type: ignore[method-assign]
            client._start_storage_grant = MagicMock()  # type: ignore[method-assign]

            result = client.grant_storage_access("/storage/ABCD-1234")

        self.assertFalse(result.success)
        self.assertIn("untrusted", result.status)
        client.ensure_trusted.assert_called_once_with(cancel_event=None)
        client._prepare_storage_grant.assert_not_called()
        client._start_storage_grant.assert_not_called()

    def test_p2p_stops_before_network_discovery_for_untrusted_helper(self) -> None:
        bridge = SimpleNamespace(
            adb=SimpleNamespace(device_ip_addresses=MagicMock()),
            settings=SimpleNamespace(temp_folder=Path("unused")),
            ensure_trusted=MagicMock(return_value=(False, "untrusted helper")),
        )
        client = ACBridgeP2PClient(bridge)

        with self.assertRaisesRegex(P2PTransferError, "untrusted helper"):
            client._prepare_session(
                "/sdcard/Download",
                timeout_seconds=120,
                connect_timeout=2,
            )

        bridge.ensure_trusted.assert_called_once_with(cancel_event=None)
        bridge.adb.device_ip_addresses.assert_not_called()

    def test_shizuku_delegates_to_the_single_exact_trust_gate(self) -> None:
        client = ShizukuClient(
            SimpleNamespace(),
            SimpleNamespace(temp_folder=Path("unused")),
        )
        bridge = SimpleNamespace(
            ensure_trusted=MagicMock(return_value=(False, "untrusted helper"))
        )
        client.bridge = bridge

        trusted, message = client._ensure_trusted_bridge()

        self.assertFalse(trusted)
        self.assertEqual(message, "untrusted helper")
        bridge.ensure_trusted.assert_called_once_with(cancel_event=None)


if __name__ == "__main__":
    unittest.main()
