from __future__ import annotations

import unittest

from openadb.ui.workers import Worker


class WorkerSecretRedactionTests(unittest.TestCase):
    def test_exception_message_and_traceback_are_redacted_before_signal(self) -> None:
        qr_password = "QrPassword12"
        pairing_code = "739201"
        url = "https://alice:private-password@example.test/run?sig=private-signature"
        observed: list[tuple[str, str]] = []

        def fail() -> None:
            raise RuntimeError(
                f"qr_password={qr_password}; adb pair host:37123 {pairing_code}; {url}"
            )

        worker = Worker(fail)
        worker.signals.error.connect(lambda message, trace: observed.append((message, trace)))
        worker.run()

        self.assertEqual(len(observed), 1)
        rendered = "\n".join(observed[0])
        for secret in (
            qr_password,
            pairing_code,
            "private-password",
            "private-signature",
        ):
            self.assertNotIn(secret, rendered)

    def test_finalizer_error_is_redacted_before_signal(self) -> None:
        secret = "FinalizerSecret12"
        observed: list[tuple[str, str]] = []

        def fail_cleanup() -> None:
            raise RuntimeError(f"qr_password={secret}")

        worker = Worker(lambda: None)
        worker.add_finalizer(fail_cleanup)
        worker.signals.error.connect(lambda message, trace: observed.append((message, trace)))
        worker.run()

        self.assertEqual(len(observed), 1)
        self.assertNotIn(secret, "\n".join(observed[0]))

    def test_progress_and_item_callback_payloads_are_recursively_redacted(self) -> None:
        secret = "WorkerPairSecret12"
        url = "sftp://alice:private-password@example.test/upload"
        progress: list[str] = []
        items: list[object] = []

        def emit_payloads(progress_callback, item_callback) -> None:
            progress_callback.emit(f"qr_password={secret}; {url}")
            item_callback.emit(
                {
                    "session_key": secret,
                    "nested": [f"pairing_secret={secret}", url],
                    "ordinary": {"key": "ordinary-value"},
                }
            )

        worker = Worker(emit_payloads)
        worker.signals.progress.connect(progress.append)
        worker.signals.item.connect(items.append)
        worker.run()

        self.assertEqual(len(progress), 1)
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], dict)
        rendered = repr((progress, items))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("private-password", rendered)
        self.assertEqual(items[0]["ordinary"], {"key": "ordinary-value"})


if __name__ == "__main__":
    unittest.main()
