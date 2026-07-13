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

    def test_nested_scopes_redact_every_secret_from_streaming_callbacks_and_logs(self) -> None:
        outer_secret = "outer-pairing-secret"
        inner_secret = "inner-session-secret"
        qr_secret = "QrPassword12"
        authenticated_url = "https://alice:url-user-secret@example.test/upload?sig=private-signature"
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / "logs"
            runner = CommandRunner(logs)
            observed: list[str] = []
            command = [
                sys.executable,
                "-c",
                "import sys; print(' '.join(sys.argv[1:]))",
                outer_secret,
                inner_secret,
                f"qr_password={qr_secret}",
                authenticated_url,
            ]

            with runner.scoped_log_command(
                ["outer-private-operation"],
                sensitive_values=(outer_secret,),
            ):
                with runner.scoped_log_command(
                    ["inner-private-operation"],
                    sensitive_values=(inner_secret,),
                ):
                    result = runner.run_streaming(
                        command,
                        output_callback=lambda _channel, text: observed.append(text),
                    )

            persisted = (logs / "openadb.log").read_text(encoding="utf-8")
            persisted += (logs / "openadb.commands.jsonl").read_text(encoding="utf-8")
            rendered = result.command_text + result.stdout + result.stderr + "".join(observed) + persisted

        for secret in (outer_secret, inner_secret, qr_secret, "url-user-secret", "private-signature"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(result.command, ["inner-private-operation"])

    def test_input_stream_echo_is_redacted_before_output_callback(self) -> None:
        secret = "pairing-secret-from-stdin"
        with tempfile.TemporaryDirectory() as temporary:
            runner = CommandRunner(Path(temporary) / "logs")
            observed: list[str] = []

            def write_secret(stream) -> None:
                stream.write((secret + "\n").encode())
                stream.flush()

            with runner.scoped_log_command(
                ["adb", "pair", "host:37123"],
                sensitive_values=(secret,),
            ):
                result = runner.run_with_input_stream(
                    [sys.executable, "-c", "import sys; print(sys.stdin.readline().strip())"],
                    input_writer=write_secret,
                    output_callback=lambda _channel, text: observed.append(text),
                )

        self.assertTrue(result.success)
        self.assertNotIn(secret, "".join(observed))
        self.assertNotIn(secret, result.stdout)
        self.assertIn("[private]", "".join(observed))

    def test_legitimate_hex_and_ordinary_json_survive_streaming_and_logs(self) -> None:
        serial = "0123456789abcdef0123456789abcdef"
        checksum = "a" * 64
        ordinary_json = '{"key":"ordinary-value"}'
        with tempfile.TemporaryDirectory() as temporary:
            logs = Path(temporary) / "logs"
            runner = CommandRunner(logs)
            observed: list[str] = []
            result = runner.run_streaming(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('\\n'.join(sys.argv[1:]))",
                    serial,
                    checksum,
                    ordinary_json,
                ],
                output_callback=lambda _channel, text: observed.append(text),
            )
            persisted = (logs / "openadb.log").read_text(encoding="utf-8")

        for value in (serial, checksum, ordinary_json):
            self.assertIn(value, result.stdout)
            self.assertIn(value, "".join(observed))
            self.assertIn(value, persisted)

    def test_cancelled_blocked_input_writer_does_not_emit_after_result(self) -> None:
        writer_started = threading.Event()
        release_writer = threading.Event()
        writer_finished = threading.Event()
        cancel_event = threading.Event()
        results = []

        def blocked_writer(stream) -> None:
            writer_started.set()
            try:
                release_writer.wait(timeout=10)
                stream.write(b"late input")
                stream.flush()
            finally:
                writer_finished.set()

        with tempfile.TemporaryDirectory() as temporary:
            runner = CommandRunner(Path(temporary) / "logs")
            run_thread = threading.Thread(
                target=lambda: results.append(
                    runner.run_with_input_stream(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        input_writer=blocked_writer,
                        timeout=30,
                        cancel_event=cancel_event,
                    )
                )
            )
            run_thread.start()
            self.assertTrue(writer_started.wait(timeout=5))
            cancel_event.set()
            run_thread.join(timeout=8)
            self.assertFalse(run_thread.is_alive())
            self.assertEqual(len(results), 1)
            before_release = (results[0].stdout, results[0].stderr, results[0].status)
            release_writer.set()
            self.assertTrue(writer_finished.wait(timeout=5))
            self.assertEqual(
                (results[0].stdout, results[0].stderr, results[0].status),
                before_release,
            )

        self.assertEqual(results[0].error_type, "cancelled")


if __name__ == "__main__":
    unittest.main()
