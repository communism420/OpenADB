from __future__ import annotations

import threading
import unittest
from dataclasses import FrozenInstanceError

from openadb.core.transfer_progress import (
    TransferProgressStatus,
    TransferProgressTracker,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TransferProgressTests(unittest.TestCase):
    def test_totals_speed_and_common_update_are_deterministic(self) -> None:
        clock = FakeClock()
        tracker = TransferProgressTracker(total_bytes=4096, total_files=2, clock=clock)
        clock.advance(2)
        snapshot = tracker.ingest(
            {
                "type": "progress",
                "done_bytes": 2048,
                "done_files": 1,
                "current_file": "first.bin",
                "activity": "ADB upload",
            }
        )

        self.assertEqual(snapshot.percent, 50)
        self.assertEqual(snapshot.bytes_per_second, 1024)
        self.assertEqual(snapshot.to_update()["speed"], "1.0 KB/s")
        self.assertEqual(snapshot.to_update()["done_files"], 1)

    def test_parallel_p2p_deltas_share_one_thread_safe_account(self) -> None:
        tracker = TransferProgressTracker(total_bytes=800, total_files=8)

        def send_stream() -> None:
            for _index in range(100):
                tracker.update(byte_delta=1)
            tracker.update(file_delta=1)

        threads = [threading.Thread(target=send_stream) for _index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        snapshot = tracker.finish()
        self.assertEqual(snapshot.done_bytes, 800)
        self.assertEqual(snapshot.done_files, 8)
        self.assertEqual(snapshot.status, TransferProgressStatus.SUCCEEDED)

    def test_out_of_order_absolute_progress_never_moves_backwards(self) -> None:
        tracker = TransferProgressTracker(total_bytes=100, total_files=2)
        tracker.update(done_bytes=80, done_files=1)
        snapshot = tracker.update(done_bytes=30, done_files=0)
        self.assertEqual(snapshot.done_bytes, 80)
        self.assertEqual(snapshot.done_files, 1)

    def test_incomplete_finish_is_partial_not_success(self) -> None:
        tracker = TransferProgressTracker(total_bytes=100, total_files=2)
        tracker.update(done_bytes=50, done_files=1)
        snapshot = tracker.finish(message="Transport returned early")
        self.assertEqual(snapshot.status, TransferProgressStatus.PARTIAL)
        self.assertTrue(snapshot.is_partial)
        self.assertFalse(snapshot.is_success)

    def test_failure_without_committed_progress_is_failed(self) -> None:
        tracker = TransferProgressTracker(total_bytes=100, total_files=1)
        snapshot = tracker.fail("Connection refused")
        self.assertEqual(snapshot.status, TransferProgressStatus.FAILED)
        self.assertFalse(snapshot.is_partial)

    def test_item_failure_prevents_false_success(self) -> None:
        tracker = TransferProgressTracker(total_bytes=10, total_files=2)
        tracker.update(done_bytes=10, done_files=1)
        tracker.record_failure("second.bin", "permission denied")
        snapshot = tracker.finish()
        self.assertEqual(snapshot.status, TransferProgressStatus.PARTIAL)
        self.assertEqual(snapshot.failures[0].item, "second.bin")

    def test_completion_and_late_progress_are_terminal(self) -> None:
        clock = FakeClock()
        tracker = TransferProgressTracker(total_bytes=10, total_files=1, clock=clock)
        clock.advance(1)
        finished = tracker.update(done_bytes=10, done_files=1)
        finished = tracker.finish(message="Stored")
        clock.advance(20)
        late = tracker.update(done_bytes=999, done_files=99, message="late")
        self.assertEqual(late, finished)
        self.assertEqual(late.elapsed_seconds, 1)

    def test_legacy_done_respects_success_and_cancelled_flags(self) -> None:
        succeeded = TransferProgressTracker(total_bytes=1, total_files=1)
        succeeded.update(done_bytes=1, done_files=1)
        success_snapshot = succeeded.ingest(
            {"type": "done", "success": True, "message": "Complete"}
        )
        self.assertTrue(success_snapshot.is_success)
        self.assertTrue(success_snapshot.is_terminal)

        failed = TransferProgressTracker(total_bytes=1, total_files=1)
        failure_snapshot = failed.ingest(
            {"type": "done", "success": False, "message": "Incomplete"}
        )
        self.assertEqual(failure_snapshot.status, TransferProgressStatus.FAILED)

        cancelled = TransferProgressTracker()
        cancelled_snapshot = cancelled.ingest(
            {"type": "done", "success": True, "cancelled": True}
        )
        self.assertEqual(cancelled_snapshot.status, TransferProgressStatus.CANCELLED)

    def test_cancelled_progress_is_never_success(self) -> None:
        tracker = TransferProgressTracker(total_bytes=10, total_files=1)
        tracker.update(done_bytes=5)
        snapshot = tracker.ingest({"type": "cancelled", "message": "Cancelled by user"})
        self.assertEqual(snapshot.status, TransferProgressStatus.CANCELLED)
        self.assertFalse(snapshot.is_success)

    def test_all_snapshot_text_and_failures_redact_p2p_secrets(self) -> None:
        token = "a" * 64
        session_id = "b" * 32
        tracker = TransferProgressTracker(total_bytes=1, total_files=1)
        tracker.update(
            current_file=f"token={token}",
            activity=f"session_id={session_id}",
            message="pairing code: 123456",
        )
        tracker.record_failure("payload", f"Bearer {token}")
        snapshot = tracker.fail(f"READY\t42042\t{token}\t999")
        rendered = repr(snapshot) + repr(snapshot.to_update())
        self.assertNotIn(token, rendered)
        self.assertNotIn(session_id, rendered)
        self.assertNotIn("123456", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_snapshot_is_immutable(self) -> None:
        snapshot = TransferProgressTracker().snapshot()
        with self.assertRaises(FrozenInstanceError):
            snapshot.done_bytes = 1  # type: ignore[misc]

    def test_negative_or_ambiguous_updates_are_rejected(self) -> None:
        tracker = TransferProgressTracker()
        with self.assertRaises(ValueError):
            tracker.update(byte_delta=-1)
        with self.assertRaises(ValueError):
            tracker.update(done_bytes=1, byte_delta=1)
        with self.assertRaises(ValueError):
            tracker.update(done_bytes=1.5)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
