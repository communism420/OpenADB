"""Canonical OpenADB and ACBridge release metadata."""

from __future__ import annotations

VERSION = "3.1.0"
VERSION_PARTS = (3, 1, 0)

# Android versionCode policy: major * 10_000 + minor * 1_000 + patch * 100
# + build. This preserves the established sequence 20004 (2.0.0 build 4),
# 20101 (2.0.1 build 1), and 30002 (3.0.0 build 2). Minor release 3.1.0
# starts its helper build sequence at 1. Build 2 contains the Android 15/OEM
# lifecycle fix for non-interactive Shizuku operations. Build 3 adds the
# request-scoped ACBridge privilege handshake. Build 4 preserves ADB-shell
# ownership for atomic Root/Shizuku result files on Android 16. Build 5 moves
# those terminal status payloads behind a DUMP-protected app-private provider,
# avoiding cross-UID scoped-storage ownership after a clean helper install.
# Build 6 keeps a foreground permission-host task alive and defers Root/Shizuku
# requests until the Android activity is resumed and focused. Build 7 binds
# that host to the terminal permission result, acknowledges ready/closed state,
# and prevents stale cleanup from closing a newer access-mode request. The
# OpenADB desktop release remains 3.1.0 while the bundled helper can be upgraded
# independently.
# Build 8 introduced exact-token foreground-host closure. Build 9 keeps the
# token in a closing state until Android has destroyed its task, then publishes
# the closed acknowledgement used as the Shizuku verification barrier.
# Build 10 adds the project's license and third-party notices to the APK and
# verifies that every legal asset matches its source byte-for-byte.
ACBRIDGE_BUILD = 10
ACBRIDGE_VERSION_CODE = 31010
ACBRIDGE_PACKAGE = "com.communism420.acbridge"
ACBRIDGE_APK_FILENAME = f"ACBridge-{VERSION}.apk"
ACBRIDGE_SIGNER_SHA256 = "57d0f9154b24fa9e5aebf40e4e4b8f83c42b281e08e22d4cc34ee842c030ecd7"
RELEASE_EXE_FILENAME = f"OpenADB-{VERSION}.exe"


def android_version_code(version_parts: tuple[int, int, int], build: int) -> int:
    """Return the documented ACBridge versionCode for a semantic version."""

    major, minor, patch = version_parts
    if min(version_parts) < 0 or minor > 9 or patch > 9 or not 1 <= build <= 99:
        raise ValueError("Version parts must be non-negative, minor/patch single-digit, and build 1..99")
    return major * 10_000 + minor * 1_000 + patch * 100 + build


if android_version_code(VERSION_PARTS, ACBRIDGE_BUILD) != ACBRIDGE_VERSION_CODE:
    raise RuntimeError("OpenADB and ACBridge release metadata are inconsistent")
