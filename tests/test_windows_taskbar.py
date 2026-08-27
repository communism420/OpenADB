from __future__ import annotations

import unittest
from unittest.mock import patch

from openadb.ui.windows_taskbar import (
    TaskbarProgressState,
    WindowsTaskbarProgress,
)


class _FakeTaskbarApi:
    def __init__(self) -> None:
        self.states: list[tuple[int, TaskbarProgressState]] = []
        self.values: list[tuple[int, int, int]] = []
        self.close_calls = 0

    def set_state(self, hwnd: int, state: TaskbarProgressState) -> bool:
        self.states.append((hwnd, state))
        return True

    def set_value(self, hwnd: int, completed: int, total: int) -> bool:
        self.values.append((hwnd, completed, total))
        return True

    def close(self) -> None:
        self.close_calls += 1


class _FailingTaskbarApi:
    def set_state(self, hwnd: int, state: TaskbarProgressState) -> bool:
        raise OSError("cosmetic state failure")

    def set_value(self, hwnd: int, completed: int, total: int) -> bool:
        raise OSError("cosmetic value failure")

    def close(self) -> None:
        raise OSError("cosmetic close failure")


class WindowsTaskbarProgressTests(unittest.TestCase):
    HWND = 42042

    def _progress(self) -> tuple[WindowsTaskbarProgress, _FakeTaskbarApi]:
        native = _FakeTaskbarApi()
        progress = WindowsTaskbarProgress(
            lambda: self.HWND,
            native_api=native,
        )
        return progress, native

    def test_unknown_total_stays_indeterminate(self) -> None:
        progress, native = self._progress()

        progress.begin("pull-1")
        progress.apply_update(
            "pull-1",
            {"type": "plan", "done_bytes": 0, "total_bytes": 0},
        )

        self.assertEqual(progress.active_operation_id, "pull-1")
        self.assertEqual(native.values, [])
        self.assertTrue(native.states)
        self.assertTrue(
            all(state is TaskbarProgressState.INDETERMINATE for _hwnd, state in native.states)
        )

    def test_known_byte_progress_is_normal_monotonic_and_capped_before_success(
        self,
    ) -> None:
        progress, native = self._progress()
        progress.begin("push-1")

        progress.apply_update(
            "push-1",
            {"type": "plan", "done_bytes": 0, "total_bytes": 100},
        )
        progress.apply_update(
            "push-1",
            {"type": "progress", "done_bytes": 25, "total_bytes": 100},
        )
        progress.apply_update(
            "push-1",
            {"type": "progress", "done_bytes": 5, "total_bytes": 100},
        )
        progress.apply_update(
            "push-1",
            {
                "type": "heartbeat",
                "phase": "finalizing",
                "done_bytes": 100,
                "total_bytes": 100,
            },
        )

        self.assertEqual(
            [completed for _hwnd, completed, _total in native.values],
            [0, 250, 250, 999],
        )
        self.assertTrue(
            all(total == progress.SCALE for _hwnd, _completed, total in native.values)
        )
        self.assertEqual(native.states[-1], (self.HWND, TaskbarProgressState.NORMAL))

    def test_file_count_fallback_becomes_determinate(self) -> None:
        progress, native = self._progress()
        progress.begin("pull-files")

        progress.apply_update(
            "pull-files",
            {"type": "progress", "done_files": 1, "total_files": 4},
        )

        self.assertEqual(native.values[-1], (self.HWND, 250, progress.SCALE))
        self.assertEqual(native.states[-1], (self.HWND, TaskbarProgressState.NORMAL))

    def test_cancelled_transfer_remains_paused_when_done_false_follows(self) -> None:
        progress, native = self._progress()
        progress.begin("push-cancel")
        progress.apply_update(
            "push-cancel",
            {"type": "progress", "done_bytes": 40, "total_bytes": 100},
        )

        progress.apply_update("push-cancel", {"type": "cancelled"})
        progress.apply_update(
            "push-cancel",
            {"type": "done", "success": False, "message": "Cancelled by user"},
        )

        terminal_states = [state for _hwnd, state in native.states[-2:]]
        self.assertEqual(
            terminal_states,
            [TaskbarProgressState.PAUSED, TaskbarProgressState.PAUSED],
        )
        self.assertNotIn(TaskbarProgressState.ERROR, terminal_states)
        self.assertEqual(native.values[-1], (self.HWND, 400, progress.SCALE))

    def test_failure_is_error_and_success_reaches_full_normal_progress(self) -> None:
        progress, native = self._progress()
        progress.begin("failed")
        progress.apply_update(
            "failed",
            {"type": "progress", "done_bytes": 10, "total_bytes": 100},
        )
        progress.apply_update("failed", {"type": "done", "success": False})

        self.assertEqual(native.values[-1], (self.HWND, 100, progress.SCALE))
        self.assertEqual(native.states[-1], (self.HWND, TaskbarProgressState.ERROR))

        progress.finish("failed")
        progress.begin("succeeded")
        progress.apply_update("succeeded", {"type": "done", "success": True})

        self.assertEqual(native.values[-1], (self.HWND, progress.SCALE, progress.SCALE))
        self.assertEqual(native.states[-1], (self.HWND, TaskbarProgressState.NORMAL))

    def test_stale_operation_updates_and_finish_are_ignored(self) -> None:
        progress, native = self._progress()
        progress.begin("current")
        states_before = list(native.states)

        progress.apply_update(
            "stale",
            {"type": "progress", "done_bytes": 90, "total_bytes": 100},
        )
        progress.finish("stale")

        self.assertEqual(progress.active_operation_id, "current")
        self.assertEqual(native.states, states_before)
        self.assertEqual(native.values, [])

    def test_finish_clears_matching_operation(self) -> None:
        progress, native = self._progress()
        progress.begin("current")

        progress.finish("current")

        self.assertEqual(progress.active_operation_id, "")
        self.assertEqual(native.states[-1], (self.HWND, TaskbarProgressState.NONE))

    def test_close_clears_and_is_idempotent(self) -> None:
        progress, native = self._progress()
        progress.begin("current")

        progress.close()
        calls_after_first_close = (list(native.states), list(native.values))
        progress.close()
        progress.begin("late")
        progress.apply_update(
            "current",
            {"type": "progress", "done_bytes": 1, "total_bytes": 2},
        )

        self.assertEqual(native.close_calls, 1)
        self.assertEqual(native.states[-1], (self.HWND, TaskbarProgressState.NONE))
        self.assertEqual((native.states, native.values), calls_after_first_close)

    def test_non_windows_backend_is_a_silent_noop(self) -> None:
        with patch("openadb.ui.windows_taskbar._NativeWindowsTaskbarApi") as native_type:
            progress = WindowsTaskbarProgress(lambda: self.HWND, platform="linux")
            progress.begin("noop")
            progress.apply_update(
                "noop",
                {"type": "progress", "done_bytes": 1, "total_bytes": 2},
            )
            progress.finish("noop")
            progress.close()

        native_type.assert_not_called()
        self.assertEqual(progress.active_operation_id, "")

    def test_native_failures_and_invalid_hwnd_never_escape(self) -> None:
        failing = WindowsTaskbarProgress(
            lambda: self.HWND,
            native_api=_FailingTaskbarApi(),
        )
        failing.begin("failure")
        failing.apply_update(
            "failure",
            {"type": "progress", "done_bytes": 1, "total_bytes": 2},
        )
        failing.finish("failure")
        failing.close()

        native = _FakeTaskbarApi()
        no_handle = WindowsTaskbarProgress(lambda: 0, native_api=native)
        no_handle.begin("headless")
        no_handle.apply_update(
            "headless",
            {"type": "progress", "done_bytes": 1, "total_bytes": 2},
        )
        no_handle.finish("headless")
        no_handle.close()

        self.assertEqual(native.states, [])
        self.assertEqual(native.values, [])
        self.assertEqual(native.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
