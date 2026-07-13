"""Canonical OpenADB and ACBridge release metadata."""

from __future__ import annotations


VERSION = "3.0.0"
VERSION_PARTS = (3, 0, 0)

# Android versionCode policy: major * 10_000 + minor * 1_000 + patch * 100
# + build. This preserves the established sequence 20004 (2.0.0 build 4) and
# 20101 (2.0.1 build 1). OpenADB 3.0.0 starts at build 1, hence 30001.
ACBRIDGE_BUILD = 1
ACBRIDGE_VERSION_CODE = 30001
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
