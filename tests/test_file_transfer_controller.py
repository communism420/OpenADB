from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openadb.core.acbridge_p2p import P2PTransferError
from openadb.core.device_context import DeviceContext, StaleDeviceContext
from openadb.core.file_transfer_controller import (
    FileTransferController,
    FileTransferExecutionError,
)
from openadb.core.p2p_transfer_strategy import P2PTransferStrategy
from openadb.core.transfer_plan import (
    ADB_TRANSFER,
    P2P_TRANSFER,
    PUSH_DIRECTION,
    TransferPlan,
)


class _SignalSink:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def emit(self, update: dict) -> None:
        self.updates.append(update)


class _BoundADB:
    def __init__(self, context: DeviceContext) -> None:
        self.device_context = context
        self.serial = context.serial


class _FakeStrategies:
    def __init__(self) -> None:
        self.controller: FileTransferController | None = None
        self.calls: list[tuple[str, object]] = []
        self.result: dict = {"success": True, "summary": "done"}
        self.progress: list[dict] = []
        self.error: Exception | None = None

    def _publish(self, item_callback) -> None:
        for update in self.progress:
            if item_callback is not None:
                item_callback.emit(update)
        if self.error is not None:
            raise self.error

    def _run_pull_transfer(self, *args, **kwargs) -> dict:
        self.calls.append(("pull", args[1]))
        self._publish(args[4])
        return dict(self.result)

    def _run_push_transfer(
        self,
        adb,
        local_paths,
        android_destination,
        cancel_event,
        item_callback,
        use_root_requested,
        *,
        transport,
        p2p_parallelism,
        p2p_parallelism_mode,
        temp_path,
    ) -> dict:
        self.calls.append(
            (
                "compat_push",
                (
                    tuple(local_paths),
                    android_destination,
                    use_root_requested,
                    transport,
                    p2p_parallelism,
                    p2p_parallelism_mode,
                    temp_path,
                ),
            )
        )
        assert self.controller is not None
        return self.controller.execute_push(
            adb=adb,
            local_paths=local_paths,
            android_destination=android_destination,
            cancel_event=cancel_event,
            item_callback=item_callback,
            use_root_requested=use_root_requested,
            transport=transport,
            p2p_parallelism=p2p_parallelism,
            p2p_parallelism_mode=p2p_parallelism_mode,
            temp_path=temp_path,
        )

    def _run_adb_push_transfer(
        self,
        _adb,
        local_paths,
        _destination,
        _cancel_event,
        item_callback,
        _use_root,
    ) -> dict:
        self.calls.append(("adb", tuple(local_paths)))
        self._publish(item_callback)
        return dict(self.result)

    def _run_p2p_push_transfer(
        self,
        _adb,
        local_paths,
        _destination,
        _cancel_event,
        item_callback,
        *,
        parallelism,
        parallelism_mode,
        temp_path,
    ) -> dict:
        self.calls.append(
            (
                "p2p",
                (tuple(local_paths), parallelism_mode, parallelism, temp_path),
            )
        )
        self._publish(item_callback)
        return dict(self.result)


class _P2PHost(P2PTransferStrategy):
    settings = object()

    @staticmethod
    def _emit_transfer(item_callback, update: dict) -> None:
        if item_callback is not None:
            item_callback.emit(update)


class FileTransferControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.context = DeviceContext(
            serial="transport-7",
            mode="ADB",
            transport_id="7",
            profile_key="device-a",
            profile_kind="adb",
            profile_path=root / "profile",
            backups_path=root / "backups",
            temp_path=root / "temp",
            logs_path=root / "logs",
            generation=4,
        )
        self.strategies = _FakeStrategies()
        self.controller = FileTransferController(self.strategies)
        self.strategies.controller = self.controller
        self.adb = _BoundADB(self.context)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _plan(self, *, transport: str = ADB_TRANSFER) -> TransferPlan:
        return TransferPlan(
            direction=PUSH_DIRECTION,
            transport=transport,
            sources=("C:/source.bin",),
            destination="/sdcard/Download/",
            device_context=self.context,
            use_root=True,
            requested_parallelism=4,
        )

    def test_execute_uses_only_values_captured_by_immutable_plan(self) -> None:
        plan = self._plan(transport=P2P_TRANSFER)

        result = self.controller.execute(
            plan,
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
        )

        self.assertTrue(result["success"])
        self.assertIn(
            (
                "compat_push",
                (
                    ("C:/source.bin",),
                    "/sdcard/Download/",
                    True,
                    P2P_TRANSFER,
                    4,
                    "fixed",
                    self.context.temp_path,
                ),
            ),
            self.strategies.calls,
        )
        self.assertIn(
            (
                "p2p",
                (("C:/source.bin",), "fixed", 4, self.context.temp_path),
            ),
            self.strategies.calls,
        )

    def test_auto_parallelism_remains_deferred_until_entries_are_collected(
        self,
    ) -> None:
        plan = TransferPlan(
            direction=PUSH_DIRECTION,
            transport=P2P_TRANSFER,
            sources=("C:/one.bin", "C:/two.bin"),
            destination="/sdcard/Download/",
            device_context=self.context,
            parallelism_mode="auto",
            requested_parallelism=None,
        )

        result = self.controller.execute(
            plan,
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
        )

        self.assertTrue(result["success"])
        self.assertIn(
            (
                "p2p",
                (
                    ("C:/one.bin", "C:/two.bin"),
                    "auto",
                    None,
                    self.context.temp_path,
                ),
            ),
            self.strategies.calls,
        )

    def test_context_mismatch_is_rejected_before_strategy_dispatch(self) -> None:
        stale_adb = _BoundADB(replace(self.context, serial="other", generation=5))

        with self.assertRaises(StaleDeviceContext):
            self.controller.execute(
                self._plan(),
                adb=stale_adb,  # type: ignore[arg-type]
                cancel_event=threading.Event(),
            )

        self.assertEqual(self.strategies.calls, [])

    def test_unbound_adb_is_rejected_before_strategy_dispatch(self) -> None:
        unbound_adb = type("UnboundADB", (), {"serial": self.context.serial})()

        with self.assertRaises(StaleDeviceContext):
            self.controller.execute(
                self._plan(),
                adb=unbound_adb,  # type: ignore[arg-type]
                cancel_event=threading.Event(),
            )

        self.assertEqual(self.strategies.calls, [])

    def test_progress_is_shared_normalized_and_secret_safe(self) -> None:
        secret = "a" * 64
        self.strategies.progress = [
            {
                "type": "plan",
                "total_bytes": 10,
                "total_files": 1,
                "message": f"token={secret}",
            },
            {
                "type": "file_done",
                "done_bytes": 10,
                "done_files": 1,
                "message": f"session_secret={secret}",
                "session_key": secret,
                "metadata": {
                    "auth_token": secret,
                    "bootstrap_secret": secret,
                    "qr_password": secret,
                },
            },
        ]
        self.strategies.result = {
            "success": True,
            "summary": f"token={secret} uploaded",
            "session_token": secret,
        }
        sink = _SignalSink()

        result = self.controller.execute(
            self._plan(),
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
            item_callback=sink,
        )

        self.assertTrue(result["success"])
        self.assertNotIn(secret, result["summary"])
        self.assertEqual(result["session_token"], "[REDACTED]")
        self.assertEqual(
            [update["type"] for update in sink.updates], ["plan", "file_done"]
        )
        self.assertTrue(all(secret not in str(update) for update in sink.updates))
        self.assertEqual(
            sink.updates[-1]["metadata"]["bootstrap_secret"],
            "[REDACTED]",
        )
        self.assertEqual(
            sink.updates[-1]["metadata"]["qr_password"],
            "[REDACTED]",
        )
        self.assertEqual(sink.updates[-1]["done_bytes"], 10)
        self.assertEqual(sink.updates[-1]["total_bytes"], 10)

    def test_partial_progress_cannot_be_reported_as_success(self) -> None:
        self.strategies.progress = [
            {"type": "plan", "total_bytes": 10, "total_files": 1},
            {"type": "progress", "done_bytes": 5, "done_files": 0},
        ]

        result = self.controller.execute(
            self._plan(),
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
            item_callback=_SignalSink(),
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["progress"].status.value, "partial")

    def test_headless_execution_still_accounts_progress_and_rejects_partial_success(
        self,
    ) -> None:
        self.strategies.progress = [
            {"type": "plan", "total_bytes": 10, "total_files": 1},
            {"type": "progress", "done_bytes": 5, "done_files": 0},
        ]

        result = self.controller.execute(
            self._plan(),
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["progress"].done_bytes, 5)
        self.assertEqual(result["progress"].total_bytes, 10)
        self.assertEqual(result["progress"].status.value, "partial")

    def test_text_free_progress_does_not_repeat_retained_detail_message(self) -> None:
        self.strategies.progress = [
            {
                "type": "file_start",
                "message": "P2P: source.bin",
                "total_bytes": 10,
                "total_files": 1,
            },
            {"type": "progress", "done_bytes": 5, "done_files": 0},
        ]
        sink = _SignalSink()

        self.controller.execute(
            self._plan(),
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
            item_callback=sink,
        )

        chunk_update = sink.updates[-1]
        self.assertEqual(chunk_update["type"], "progress")
        self.assertNotIn("message", chunk_update)
        self.assertNotIn("output", chunk_update)

    def test_strategy_terminal_update_is_accounting_only_and_drops_late_updates(
        self,
    ) -> None:
        self.strategies.progress = [
            {"type": "plan", "total_bytes": 1, "total_files": 1},
            {"type": "file_done", "done_bytes": 1, "done_files": 1},
            {"type": "done", "success": True, "message": "complete"},
            {
                "type": "progress",
                "done_bytes": 999,
                "current_file": "late.bin",
                "output": "late update",
            },
        ]
        sink = _SignalSink()

        self.controller.execute(
            self._plan(),
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
            item_callback=sink,
        )

        self.assertEqual(
            [update["type"] for update in sink.updates],
            ["plan", "file_done", "progress"],
        )
        self.assertNotIn("late.bin", str(sink.updates))

    def test_returned_failure_overrides_a_premature_strategy_success_event(
        self,
    ) -> None:
        self.strategies.progress = [
            {"type": "done", "success": True, "done_bytes": 1, "done_files": 1},
        ]
        self.strategies.result = {"success": False, "summary": "final failure"}

        result = self.controller.execute(
            self._plan(),
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
            item_callback=_SignalSink(),
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["progress"].status.value, "partial")

    def test_returned_success_overrides_a_premature_strategy_failure_event(
        self,
    ) -> None:
        self.strategies.progress = [
            {
                "type": "failed",
                "success": False,
                "done_bytes": 1,
                "total_bytes": 1,
                "done_files": 1,
                "total_files": 1,
            },
        ]
        self.strategies.result = {"success": True, "summary": "final success"}

        result = self.controller.execute(
            self._plan(),
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=threading.Event(),
            item_callback=_SignalSink(),
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["progress"].status.value, "succeeded")

    def test_late_cancellation_overrides_strategy_success(self) -> None:
        cancel_event = threading.Event()
        self.strategies.progress = [
            {"type": "plan", "total_bytes": 1, "total_files": 1},
            {"type": "file_done", "done_bytes": 1, "done_files": 1},
        ]

        original_publish = self.strategies._publish

        def publish_then_cancel(item_callback) -> None:
            original_publish(item_callback)
            cancel_event.set()

        self.strategies._publish = publish_then_cancel  # type: ignore[method-assign]
        result = self.controller.execute(
            self._plan(),
            adb=self.adb,  # type: ignore[arg-type]
            cancel_event=cancel_event,
            item_callback=_SignalSink(),
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["cancelled"])

    def test_strategy_exception_crosses_boundary_only_as_redacted_text(self) -> None:
        secret = "b" * 64
        self.strategies.error = RuntimeError(f"auth_token={secret}")

        with self.assertRaises(FileTransferExecutionError) as raised:
            self.controller.execute(
                self._plan(),
                adb=self.adb,  # type: ignore[arg-type]
                cancel_event=threading.Event(),
            )

        self.assertNotIn(secret, str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))


class P2PTransferStrategyTests(unittest.TestCase):
    @patch("openadb.core.p2p_transfer_strategy.ACBridgeP2PClient")
    @patch("openadb.core.p2p_transfer_strategy.ACBridgeClient")
    def test_legacy_positional_parallelism_and_temp_path_remain_compatible(
        self,
        _bridge_type: MagicMock,
        p2p_type: MagicMock,
    ) -> None:
        p2p_type.return_value.upload.return_value = SimpleNamespace(
            success=True,
            message="uploaded",
            bytes_sent=5,
            files_sent=1,
        )
        temp_path = Path("C:/legacy-temp")

        result = _P2PHost()._run_p2p_push_transfer(
            MagicMock(),
            ["C:/source.bin"],
            "/storage/emulated/0/Download/",
            threading.Event(),
            _SignalSink(),
            4,
            temp_path,
        )

        self.assertTrue(result["success"])
        _bridge_type.assert_called_once_with(
            unittest.mock.ANY,
            _P2PHost.settings,
            temp_folder=temp_path,
        )
        self.assertEqual(p2p_type.return_value.upload.call_args.kwargs["parallelism"], 4)
        self.assertEqual(
            p2p_type.return_value.upload.call_args.kwargs["parallelism_mode"],
            "fixed",
        )

    @patch("openadb.core.p2p_transfer_strategy.ACBridgeP2PClient")
    @patch("openadb.core.p2p_transfer_strategy.ACBridgeClient")
    def test_auto_preference_reaches_entry_aware_client_planning(
        self,
        _bridge_type: MagicMock,
        p2p_type: MagicMock,
    ) -> None:
        p2p_type.return_value.upload.return_value = SimpleNamespace(
            success=True,
            message="uploaded",
            bytes_sent=5,
            files_sent=2,
        )

        result = _P2PHost()._run_p2p_push_transfer(
            MagicMock(),
            ["C:/one.bin", "C:/two.bin"],
            "/storage/emulated/0/Download/",
            threading.Event(),
            _SignalSink(),
            parallelism=None,
            parallelism_mode="auto",
            temp_path=Path("C:/temp"),
        )

        self.assertTrue(result["success"])
        self.assertIsNone(p2p_type.return_value.upload.call_args.kwargs["parallelism"])
        self.assertEqual(
            p2p_type.return_value.upload.call_args.kwargs["parallelism_mode"],
            "auto",
        )

    @patch("openadb.core.p2p_transfer_strategy.ACBridgeP2PClient")
    @patch("openadb.core.p2p_transfer_strategy.ACBridgeClient")
    def test_removable_storage_permission_is_granted_before_retry(
        self,
        bridge_type: MagicMock,
        p2p_type: MagicMock,
    ) -> None:
        bridge = bridge_type.return_value
        bridge.grant_storage_access.return_value = SimpleNamespace(
            success=True,
            status="granted",
            stderr="",
        )
        client = p2p_type.return_value
        client.upload.side_effect = [
            P2PTransferError("Grant MicroSD/USB access before using P2P"),
            SimpleNamespace(
                success=True,
                message="uploaded",
                bytes_sent=5,
                files_sent=1,
            ),
        ]
        cancel_event = threading.Event()
        sink = _SignalSink()

        result = _P2PHost()._run_p2p_push_transfer(
            MagicMock(),
            ["C:/source.bin"],
            "/storage/ABCD/Download/",
            cancel_event,
            sink,
            parallelism=3,
            parallelism_mode="manual",
            temp_path=Path("C:/temp"),
        )

        self.assertTrue(result["success"])
        bridge.grant_storage_access.assert_called_once_with(
            "/storage/ABCD/Download/",
            timeout=600,
            cancel_event=cancel_event,
        )
        self.assertEqual(client.upload.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["parallelism_mode"] == "manual"
                and call.kwargs["parallelism"] == 3
                for call in client.upload.call_args_list
            )
        )
        self.assertEqual(sink.updates[-1]["done_bytes"], 5)


if __name__ == "__main__":
    unittest.main()
