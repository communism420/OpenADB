"""Deterministic ACBridge P2P stream planning.

The automatic planner intentionally prefers a small number of sessions.  A
new ACBridge session has setup and Android SAF overhead, so adding one for
every file can make small transfers slower and creates avoidable pressure on
the device.  Automatic mode therefore tops out at four streams; values five
through eight remain available as an explicit advanced override.

This module is pure: it does not inspect the network, device, filesystem, or
settings.  Callers capture file statistics first and can consequently test or
display the selected stream count before a transfer starts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from operator import index

AUTO_PARALLELISM_MODE = "auto"
MANUAL_PARALLELISM_MODE = "manual"
P2P_MAX_PARALLELISM = 8
P2P_AUTO_MAX_PARALLELISM = min(4, P2P_MAX_PARALLELISM)

_MIB = 1024 * 1024
_THREE_STREAM_MIN_FILES = 6
_THREE_STREAM_MIN_TOTAL = 32 * _MIB
_THREE_STREAM_MIN_AVERAGE = 1 * _MIB
_FOUR_STREAM_MIN_FILES = 24
_FOUR_STREAM_MIN_TOTAL = 256 * _MIB
_FOUR_STREAM_MIN_AVERAGE = 4 * _MIB


@dataclass(frozen=True, slots=True)
class P2PParallelismPreference:
    """Normalized profile preference suitable for transfer planning."""

    mode: str = AUTO_PARALLELISM_MODE
    manual_value: int | None = None

    def __post_init__(self) -> None:
        if self.mode == AUTO_PARALLELISM_MODE and self.manual_value is None:
            return
        normalized_manual = _manual_value(self.manual_value)
        if (
            self.mode == MANUAL_PARALLELISM_MODE
            and normalized_manual is not None
            and normalized_manual == self.manual_value
        ):
            return
        raise ValueError(
            "P2P parallelism preference must be Auto or a manual value "
            f"from 1 to {P2P_MAX_PARALLELISM}"
        )

    def to_setting_value(self) -> str | int:
        """Return the compact backward-compatible value to persist.

        Existing settings store an integer.  Keeping integers for manual
        overrides avoids a settings migration; the new automatic preference
        is represented by the string ``"auto"``.
        """

        if self.mode == MANUAL_PARALLELISM_MODE and self.manual_value is not None:
            return self.manual_value
        return AUTO_PARALLELISM_MODE


def normalize_p2p_parallelism_preference(
    mode: object = AUTO_PARALLELISM_MODE,
    manual_value: object = None,
) -> P2PParallelismPreference:
    """Normalize new or legacy preference fields without raising.

    Legacy integer values from 1 through 8 become manual overrides.  Invalid
    modes and invalid manual values deliberately fall back to Auto rather than
    retaining an unsafe or surprising stream count.
    """

    if manual_value is None:
        legacy_value = _manual_value(mode)
        if legacy_value is not None:
            return P2PParallelismPreference(MANUAL_PARALLELISM_MODE, legacy_value)

    normalized_mode = str(mode or "").strip().casefold()
    if normalized_mode in {
        AUTO_PARALLELISM_MODE,
        "automatic",
        "auto (recommended)",
    }:
        return P2PParallelismPreference()
    if normalized_mode not in {MANUAL_PARALLELISM_MODE, "fixed"}:
        return P2PParallelismPreference()

    normalized_manual = _manual_value(manual_value)
    if normalized_manual is None:
        return P2PParallelismPreference()
    return P2PParallelismPreference(MANUAL_PARALLELISM_MODE, normalized_manual)


def migrate_p2p_parallelism_setting(value: object) -> P2PParallelismPreference:
    """Read either the old scalar setting or a future structured preference.

    The current on-disk format is an integer.  Mapping support keeps this pure
    migration helper useful if profile storage later records the mode and
    value separately, while unknown shapes safely select Auto.
    """

    if isinstance(value, P2PParallelismPreference):
        return normalize_p2p_parallelism_preference(value.mode, value.manual_value)
    if isinstance(value, Mapping):
        mode = value.get("mode", value.get("parallelism_mode", AUTO_PARALLELISM_MODE))
        manual_value = value.get("manual_value", value.get("requested_parallelism"))
        return normalize_p2p_parallelism_preference(mode, manual_value)
    return normalize_p2p_parallelism_preference(value)


def choose_p2p_parallelism(
    file_count: int,
    total_bytes: int,
    largest_file_bytes: int,
    mode: str,
    manual_value: int | None,
) -> int:
    """Choose a conservative P2P session count from captured file statistics.

    Automatic planning uses integer thresholds and is therefore deterministic
    on every supported Python and Windows version:

    * one file, missing/inconsistent statistics, or zero-byte-only input: 1;
    * ordinary multi-file transfers: 2;
    * at least 6 files, 32 MiB total, and 1 MiB average: up to 3;
    * at least 24 files, 256 MiB total, and 4 MiB average: up to 4.

    When one file contains at least 75% of all bytes, Auto caps the result at
    two because extra sessions cannot accelerate that dominant file.  Manual
    values 1 through 8 are honored but still cannot create more sessions than
    there are files.  All invalid runtime inputs fail closed to one stream.
    """

    normalized_files = _non_negative_integer(file_count)
    if normalized_files is None or normalized_files <= 1:
        return 1

    preference = normalize_p2p_parallelism_preference(mode, manual_value)
    if preference.mode == MANUAL_PARALLELISM_MODE:
        assert preference.manual_value is not None
        return min(preference.manual_value, normalized_files)

    normalized_total = _non_negative_integer(total_bytes)
    normalized_largest = _non_negative_integer(largest_file_bytes)
    if (
        normalized_total is None
        or normalized_largest is None
        or normalized_total == 0
        or normalized_largest == 0
        or normalized_largest > normalized_total
    ):
        return 1

    average_bytes = normalized_total // normalized_files
    selected = 2
    if (
        normalized_files >= _FOUR_STREAM_MIN_FILES
        and normalized_total >= _FOUR_STREAM_MIN_TOTAL
        and average_bytes >= _FOUR_STREAM_MIN_AVERAGE
    ):
        selected = P2P_AUTO_MAX_PARALLELISM
    elif (
        normalized_files >= _THREE_STREAM_MIN_FILES
        and normalized_total >= _THREE_STREAM_MIN_TOTAL
        and average_bytes >= _THREE_STREAM_MIN_AVERAGE
    ):
        selected = min(3, P2P_AUTO_MAX_PARALLELISM)

    if normalized_largest * 4 >= normalized_total * 3:
        selected = min(selected, 2)
    return max(1, min(selected, normalized_files, P2P_AUTO_MAX_PARALLELISM))


def _manual_value(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = index(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        if not isinstance(value, str):
            return None
        digits = value.strip()
        if not digits.isdecimal():
            return None
        # Python 3.11+ deliberately rejects extremely long decimal strings,
        # while older runtimes may spend significant time converting them.
        # Only 1..8 can be valid, so remove harmless leading zeroes and bound
        # the conversion before calling int().
        digits = digits.lstrip("0") or "0"
        if len(digits) > 1:
            return None
        try:
            normalized = int(digits)
        except (ValueError, OverflowError):
            return None
    if 1 <= normalized <= P2P_MAX_PARALLELISM:
        return normalized
    return None


def _non_negative_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = index(value)  # type: ignore[arg-type]
    except TypeError:
        return None
    return normalized if normalized >= 0 else None
