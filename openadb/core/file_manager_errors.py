"""Deterministic, secret-safe error mapping for File Manager workflows."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .device_context import DeviceContextUnavailable, StaleDeviceContext


REDACTED = "[REDACTED]"


class FileManagerErrorCode(str, Enum):
    CANCELLED = "cancelled"
    STALE_CONTEXT = "stale_context"
    DEVICE_UNAVAILABLE = "device_unavailable"
    STORAGE_PERMISSION_REQUIRED = "storage_permission_required"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    INSUFFICIENT_SPACE = "insufficient_space"
    ROOT_UNAVAILABLE = "root_unavailable"
    PARTIAL_TRANSFER = "partial_transfer"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    NOT_FOUND = "not_found"
    ACCESS_DENIED = "access_denied"
    INVALID_REQUEST = "invalid_request"
    TRANSFER_FAILED = "transfer_failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FileManagerError:
    """Safe error payload suitable for dialogs, logs, and tests.

    The source exception is intentionally not retained because its ``repr`` or
    chained cause can contain a one-time P2P session token.
    """

    code: FileManagerErrorCode
    title: str
    message: str
    retryable: bool = False
    cancelled: bool = False
    source_type: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "code": self.code.value,
            "title": self.title,
            "message": self.message,
            "retryable": self.retryable,
            "cancelled": self.cancelled,
            "source_type": self.source_type,
        }


class TransferCancelled(RuntimeError):
    """Cooperative cancellation reported by a transfer strategy."""


class PartialTransferError(RuntimeError):
    """A transfer stopped after one or more items were already committed."""


_LABELLED_SECRET_PATTERN = re.compile(
    r"""
    (?P<prefix>
        [\"']?
        (?:p2p[ _-]*)?
        (?:
            (?:session[ _-]*)?(?:token|secret|key)
            |auth(?:entication)?(?:[ _-]*(?:token|key))?
            |pair(?:ing)?[ _-]*(?:code|pin)
            |session[ _-]*id
        )
        [\"']?\s*[:=]\s*
    )
    (?P<quote>[\"']?)
    (?P<value>(?!\[REDACTED\])[^\"'\s,;&}\]]+)
    (?P=quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)
_SECRET_ARGUMENT_PATTERN = re.compile(
    r"(?i)(--?(?:p2p-)?(?:token|secret|auth-token|pairing-code|session-id)\s+)(\S+)"
)
_SECRET_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:token|secret|auth|auth_token|pairing_code|session_id)=)([^&#\s]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
_LONG_HEX_SECRET_PATTERN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,128}(?![0-9A-Fa-f])")
_READY_TOKEN_PATTERN = re.compile(
    r"(?im)^(?P<prefix>(?:SESSION_)?READY\t\d+\t)(?P<token>[^\t\r\n]+)"
)


def redact_sensitive_text(value: object) -> str:
    """Remove known ACBridge/P2P credentials from arbitrary display text."""

    text = str(value or "")
    text = _LABELLED_SECRET_PATTERN.sub(_replace_labelled_secret, text)
    text = _SECRET_ARGUMENT_PATTERN.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _SECRET_QUERY_PATTERN.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _BEARER_PATTERN.sub(lambda match: f"{match.group(1)}{REDACTED}", text)
    text = _READY_TOKEN_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{REDACTED}",
        text,
    )
    return _LONG_HEX_SECRET_PATTERN.sub(REDACTED, text)


def map_file_manager_error(
    error: BaseException | str,
    *,
    operation: str = "File operation",
) -> FileManagerError:
    """Map an internal failure to one stable, sanitized user-facing payload."""

    source_type = error.__class__.__name__ if isinstance(error, BaseException) else ""
    source_types = (
        {base.__name__ for base in error.__class__.__mro__}
        if isinstance(error, BaseException)
        else set()
    )
    raw_message = str(error) if error is not None else ""
    message = redact_sensitive_text(raw_message).strip()
    lowered = raw_message.casefold()
    safe_operation = redact_sensitive_text(operation).strip() or "File operation"

    if (
        isinstance(error, (TransferCancelled, KeyboardInterrupt))
        or source_type == "FileManagerActionCancelled"
        or _contains_any(
            lowered,
            "cancelled by user",
            "canceled by user",
            "operation cancelled",
            "operation canceled",
            "transfer cancelled",
            "transfer canceled",
            "action was cancelled",
            "action was canceled",
            "cancelled before execution",
            "canceled before execution",
            "shutdown requested",
        )
    ):
        return _mapped(
            FileManagerErrorCode.CANCELLED,
            f"{safe_operation} cancelled",
            message or "The operation was cancelled.",
            source_type=source_type,
            cancelled=True,
        )

    if (
        isinstance(error, StaleDeviceContext)
        or source_type == "StaleFileManagerProfile"
        or _contains_any(
            lowered,
            "stale device context",
            "device context changed",
            "device generation changed",
            "active file manager profile changed",
        )
    ):
        return _mapped(
            FileManagerErrorCode.STALE_CONTEXT,
            f"{safe_operation} stopped",
            "The active device changed before the operation finished.",
            source_type=source_type,
        )

    if isinstance(error, DeviceContextUnavailable) or _contains_any(
        lowered,
        "no android device",
        "no device is connected",
        "devicecontext is required",
        "device context is required",
        "device not found",
        "no devices/emulators",
        "device offline",
        "device disconnected",
    ):
        return _mapped(
            FileManagerErrorCode.DEVICE_UNAVAILABLE,
            "Android device unavailable",
            _with_details(
                "The Android device disconnected or is unavailable.",
                message,
            ),
            source_type=source_type,
            retryable=True,
        )

    if _contains_any(
        lowered,
        "saf_permission_required",
        "saf_permission_timeout",
        "storage permission",
        "storage access was not granted",
        "grant acbridge access",
    ):
        return _mapped(
            FileManagerErrorCode.STORAGE_PERMISSION_REQUIRED,
            "Android storage access required",
            message or "Grant ACBridge access to this storage location and try again.",
            source_type=source_type,
            retryable=True,
        )

    if isinstance(error, PartialTransferError) or "partial transfer" in lowered:
        return _mapped(
            FileManagerErrorCode.PARTIAL_TRANSFER,
            f"{safe_operation} partially completed",
            message or "Some items were transferred, but the operation did not complete.",
            source_type=source_type,
        )

    if _contains_any(
        lowered,
        "no space left",
        "insufficient storage",
        "not enough space",
    ):
        return _mapped(
            FileManagerErrorCode.INSUFFICIENT_SPACE,
            "Insufficient space",
            _with_details("Insufficient space on the destination.", message),
            source_type=source_type,
        )

    if _contains_any(
        lowered,
        "root denied",
        "root access",
        "su: not found",
        "root not granted",
    ):
        return _mapped(
            FileManagerErrorCode.ROOT_UNAVAILABLE,
            "Root access unavailable",
            _with_details(
                "Root access was denied or is unavailable; normal ADB may still work.",
                message,
            ),
            source_type=source_type,
        )

    if _contains_any(
        lowered,
        "not mounted",
        "storage unavailable",
        "storage is unavailable",
    ):
        return _mapped(
            FileManagerErrorCode.STORAGE_UNAVAILABLE,
            "Storage unavailable",
            _with_details("The selected storage or path is unavailable.", message),
            source_type=source_type,
            retryable=True,
        )

    if isinstance(error, TimeoutError) or "timed out" in lowered or "timeout" in lowered:
        return _mapped(
            FileManagerErrorCode.TIMEOUT,
            f"{safe_operation} timed out",
            message or "The operation did not finish before its timeout.",
            source_type=source_type,
            retryable=True,
        )

    if isinstance(error, FileNotFoundError) or _contains_any(
        lowered,
        "no such file",
        "cannot lstat",
        "does not exist",
        "not found",
    ):
        return _mapped(
            FileManagerErrorCode.NOT_FOUND,
            "File or folder not found",
            message or "The requested file or folder no longer exists.",
            source_type=source_type,
        )

    if isinstance(error, PermissionError) or _contains_any(
        lowered,
        "permission denied",
        "access is denied",
        "read-only file system",
    ):
        return _mapped(
            FileManagerErrorCode.ACCESS_DENIED,
            "Permission denied",
            _with_details(
                (
                    "The Android path is protected or read-only."
                    if _contains_any(lowered, "read-only", "read only")
                    else "Permission denied for this file or folder."
                ),
                message,
            ),
            source_type=source_type,
        )

    if isinstance(error, ConnectionError) or _contains_any(
        lowered,
        "connection refused",
        "connection reset",
        "network is unreachable",
        "p2p connection",
        "closed the p2p connection",
    ):
        return _mapped(
            FileManagerErrorCode.CONNECTION,
            "Connection failed",
            message or "The connection to the Android device was interrupted.",
            source_type=source_type,
            retryable=True,
        )

    if isinstance(error, (TypeError, ValueError)):
        return _mapped(
            FileManagerErrorCode.INVALID_REQUEST,
            "Invalid file operation",
            message or "The file operation contains invalid input.",
            source_type=source_type,
        )

    if "P2PTransferError" in source_types or isinstance(error, OSError):
        return _mapped(
            FileManagerErrorCode.TRANSFER_FAILED,
            f"{safe_operation} failed",
            message or "The transfer could not be completed.",
            source_type=source_type,
            retryable="P2PTransferError" in source_types,
        )

    return _mapped(
        FileManagerErrorCode.UNKNOWN,
        f"{safe_operation} failed",
        message or "An unexpected error occurred.",
        source_type=source_type,
    )


def _replace_labelled_secret(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{match.group('prefix')}{quote}{REDACTED}{quote}"


def _contains_any(value: str, *needles: str) -> bool:
    return any(needle in value for needle in needles)


def _with_details(explanation: str, detail: str) -> str:
    safe_explanation = redact_sensitive_text(explanation).strip()
    safe_detail = redact_sensitive_text(detail).strip()
    if not safe_detail or safe_detail.rstrip(".").casefold() == safe_explanation.rstrip(".").casefold():
        return safe_explanation
    return f"{safe_explanation}\nDetails: {safe_detail}"


def _mapped(
    code: FileManagerErrorCode,
    title: str,
    message: str,
    *,
    source_type: str,
    retryable: bool = False,
    cancelled: bool = False,
) -> FileManagerError:
    return FileManagerError(
        code=code,
        title=redact_sensitive_text(title),
        message=redact_sensitive_text(message),
        retryable=retryable,
        cancelled=cancelled,
        source_type=redact_sensitive_text(source_type),
    )
