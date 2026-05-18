from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class DeviceInfo:
    serial: str = ""
    model: str = ""
    manufacturer: str = ""
    android_version: str = ""
    sdk_version: str = ""
    mode: str = "No device"
    state: str = "none"
    transport_id: str = ""
    product: str = ""

    @property
    def is_adb(self) -> bool:
        return self.mode == "ADB"

    @property
    def is_fastboot(self) -> bool:
        return self.mode == "Fastboot"

    @property
    def is_available_for_adb(self) -> bool:
        return self.mode in {"ADB", "Recovery"}

    @property
    def title(self) -> str:
        if not self.serial:
            return "No Android device detected"
        parts = [self.model or self.serial, self.mode]
        return " - ".join(part for part in parts if part)
