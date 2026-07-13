"""Immutable identity snapshots for device-bound and wireless operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class DeviceContextUnavailable(RuntimeError):
    """Raised when an operation requires a device but none can be captured."""


class StaleDeviceContext(RuntimeError):
    """Raised when an operation tries to use a context from an older generation."""


@dataclass(frozen=True, slots=True)
class DeviceContext:
    """Complete immutable target identity for a device-bound operation."""

    serial: str
    mode: str
    transport_id: str
    profile_key: str
    profile_kind: str
    profile_path: Path
    backups_path: Path
    temp_path: Path
    logs_path: Path
    generation: int

    @property
    def is_adb(self) -> bool:
        return self.mode in {"ADB", "Recovery", "Sideload", "Unauthorized", "Offline"}

    @property
    def is_fastboot(self) -> bool:
        return self.mode == "Fastboot"


@dataclass(frozen=True, slots=True)
class WirelessConnectionAttempt:
    """Identity of one pairing/connect flow, independent of active-device changes."""

    attempt_id: str
    action: str
    scenario: str
    expected_host: str
    expected_pair_port: int | None
    expected_connect_port: int | None
    pairing_target: str
    connect_target: str
    expected_ready_serials: tuple[str, ...]
    started_generation: int

    def expects_host(self, host: str) -> bool:
        return self.expected_host.strip().casefold() == str(host or "").strip().casefold()

    def accepts_ready_serial(self, serial: str) -> bool:
        serial_key = str(serial or "").strip().casefold()
        if not serial_key:
            return False
        if not self.expected_ready_serials:
            return False
        return serial_key in {value.strip().casefold() for value in self.expected_ready_serials}

    def accepts_transport(self, serial: str, state: str) -> bool:
        """Return true only for an expected transport that is fully ready."""

        return str(state or "").strip().casefold() == "device" and self.accepts_ready_serial(serial)
