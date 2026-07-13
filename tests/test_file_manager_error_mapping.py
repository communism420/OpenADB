from __future__ import annotations

import unittest

from openadb.core.device_context import DeviceContextUnavailable, StaleDeviceContext
from openadb.core.file_manager_errors import (
    REDACTED,
    FileManagerErrorCode,
    PartialTransferError,
    TransferCancelled,
    map_file_manager_error,
    redact_sensitive_text,
)
from openadb.core.file_manager_controller import FileManagerActionCancelled
from openadb.core.file_manager_state import StaleFileManagerProfile


class P2PTransferError(RuntimeError):
    pass


class FileManagerErrorMappingTests(unittest.TestCase):
    def test_mapping_is_deterministic_for_core_failure_classes(self) -> None:
        cases = (
            (TransferCancelled("cancelled by user"), FileManagerErrorCode.CANCELLED),
            (StaleDeviceContext("stale"), FileManagerErrorCode.STALE_CONTEXT),
            (DeviceContextUnavailable("none"), FileManagerErrorCode.DEVICE_UNAVAILABLE),
            (PartialTransferError("partial transfer"), FileManagerErrorCode.PARTIAL_TRANSFER),
            (TimeoutError("timeout"), FileManagerErrorCode.TIMEOUT),
            (FileNotFoundError("gone"), FileManagerErrorCode.NOT_FOUND),
            (PermissionError("denied"), FileManagerErrorCode.ACCESS_DENIED),
            (ConnectionError("reset"), FileManagerErrorCode.CONNECTION),
            (ValueError("bad plan"), FileManagerErrorCode.INVALID_REQUEST),
            (P2PTransferError("protocol rejected"), FileManagerErrorCode.TRANSFER_FAILED),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(map_file_manager_error(error).code, expected)

    def test_saf_timeout_maps_to_permission_before_generic_timeout(self) -> None:
        mapped = map_file_manager_error(
            P2PTransferError("SAF_PERMISSION_TIMEOUT: storage access was not granted")
        )
        self.assertEqual(mapped.code, FileManagerErrorCode.STORAGE_PERMISSION_REQUIRED)
        self.assertTrue(mapped.retryable)

    def test_profile_race_maps_to_stale_context(self) -> None:
        mapped = map_file_manager_error(
            StaleFileManagerProfile("The active File Manager profile changed"),
            operation="File Manager state",
        )
        self.assertEqual(mapped.code, FileManagerErrorCode.STALE_CONTEXT)

    def test_action_cancellation_and_shutdown_are_not_reported_as_unknown_errors(self) -> None:
        for error in (
            FileManagerActionCancelled("File Manager action was cancelled"),
            "File Manager action was canceled",
            "Application shutdown requested",
        ):
            with self.subTest(error=error):
                mapped = map_file_manager_error(error, operation="Delete")
                self.assertEqual(mapped.code, FileManagerErrorCode.CANCELLED)
                self.assertTrue(mapped.cancelled)

    def test_operational_errors_keep_existing_file_manager_explanations(self) -> None:
        cases = {
            "adb: write failed: No space left on device": (
                FileManagerErrorCode.INSUFFICIENT_SPACE,
                "Insufficient space",
            ),
            "read-only file system": (
                FileManagerErrorCode.ACCESS_DENIED,
                "protected or read-only",
            ),
            "device offline": (
                FileManagerErrorCode.DEVICE_UNAVAILABLE,
                "disconnected",
            ),
            "su: not found": (
                FileManagerErrorCode.ROOT_UNAVAILABLE,
                "Root access",
            ),
            "storage unavailable": (
                FileManagerErrorCode.STORAGE_UNAVAILABLE,
                "storage or path",
            ),
        }
        for raw, (code, explanation) in cases.items():
            with self.subTest(raw=raw):
                mapped = map_file_manager_error(raw, operation="Transfer")
                self.assertEqual(mapped.code, code)
                self.assertIn(explanation, mapped.title + "\n" + mapped.message)

    def test_token_session_id_pairing_code_and_bearer_are_redacted_everywhere(self) -> None:
        token = "a" * 64
        session_id = "b" * 32
        error_type = type(f"P2PTransferError_{token}", (RuntimeError,), {})
        error = error_type(
            f'token={token} session_id="{session_id}" pairing code: 123456 '
            f"Bearer {token}"
        )
        mapped = map_file_manager_error(error, operation=f"Upload token={token}")
        rendered = repr(mapped) + repr(mapped.to_dict())

        self.assertNotIn(token, rendered)
        self.assertNotIn(session_id, rendered)
        self.assertNotIn("123456", rendered)
        self.assertIn(REDACTED, rendered)

    def test_ready_protocol_and_url_query_tokens_are_redacted(self) -> None:
        token = "feedface" * 8
        text = (
            f"READY\t42042\t{token}\t123\n"
            f"connect https://host/path?token={token}&session_id={token}"
        )
        redacted = redact_sensitive_text(text)
        self.assertNotIn(token, redacted)
        self.assertGreaterEqual(redacted.count(REDACTED), 3)

    def test_redaction_is_idempotent(self) -> None:
        once = redact_sensitive_text("token=secret-value")
        twice = redact_sensitive_text(once)
        self.assertEqual(once, "token=[REDACTED]")
        self.assertEqual(twice, once)

    def test_mapped_payload_does_not_retain_original_exception(self) -> None:
        secret = "c" * 64
        error = RuntimeError(f"secret={secret}")
        mapped = map_file_manager_error(error)
        del error
        self.assertFalse(hasattr(mapped, "exception"))
        self.assertNotIn(secret, repr(mapped))


if __name__ == "__main__":
    unittest.main()
