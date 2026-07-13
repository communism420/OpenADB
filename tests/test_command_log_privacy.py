from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from openadb.core.command_runner import CommandRunner


class CommandLogPrivacyTests(unittest.TestCase):
    def test_scoped_display_command_redacts_both_logs_and_result(self) -> None:
        session_id = "a9" * 16
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / "logs"
            runner = CommandRunner(logs)
            observed = []
            runner.add_listener(observed.append)
            actual_command = [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1])",
                f"files/p2p_status_{session_id}.txt",
            ]

            with runner.scoped_log_command(
                ["adb", "<ACBridge P2P session status>"],
                sensitive_values=(session_id,),
            ):
                result = runner.run(actual_command)

            text_log = (logs / "openadb.log").read_text(encoding="utf-8")
            jsonl_text = (logs / "openadb.commands.jsonl").read_text(encoding="utf-8")
            json_entry = json.loads(jsonl_text.strip())

        self.assertTrue(result.success)
        self.assertNotIn(session_id, result.command_text)
        self.assertNotIn(session_id, result.stdout)
        self.assertNotIn(session_id, text_log)
        self.assertNotIn(session_id, jsonl_text)
        self.assertEqual(json_entry["command"], ["adb", "<ACBridge P2P session status>"])
        self.assertIn("[private]", json_entry["stdout"])
        self.assertEqual(len(observed), 1)
        self.assertNotIn(session_id, observed[0].command_text)
        self.assertNotIn(session_id, observed[0].stdout)

    def test_scope_does_not_hide_following_ordinary_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / "logs"
            runner = CommandRunner(logs)
            with runner.scoped_log_command(["private-operation"]):
                runner.run([sys.executable, "-c", "pass"])

            marker = "ordinary-command-marker"
            result = runner.run([sys.executable, "-c", "pass", marker])
            text_log = (logs / "openadb.log").read_text(encoding="utf-8")
            json_entries = [
                json.loads(line)
                for line in (logs / "openadb.commands.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertIn(marker, result.command)
        self.assertIn(marker, text_log)
        self.assertIn(marker, json_entries[-1]["command"])

    def test_scope_is_thread_local_for_concurrent_ordinary_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runner = CommandRunner(Path(temporary) / "logs")
            marker = "concurrent-ordinary-command"
            results = []

            with runner.scoped_log_command(["private-operation"]):
                thread = threading.Thread(
                    target=lambda: results.append(
                        runner.run([sys.executable, "-c", "pass", marker])
                    )
                )
                thread.start()
                thread.join(timeout=10)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(results), 1)
            self.assertIn(marker, results[0].command)


if __name__ == "__main__":
    unittest.main()
