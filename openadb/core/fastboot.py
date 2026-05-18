from __future__ import annotations

from pathlib import Path

from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo

from .command_runner import CommandRunner
from .platform_tools import PlatformToolsManager


class FastbootClient:
    def __init__(self, platform_tools: PlatformToolsManager, runner: CommandRunner) -> None:
        self.platform_tools = platform_tools
        self.runner = runner
        self.serial: str = ""

    def set_serial(self, serial: str) -> None:
        self.serial = serial or ""

    def _base(self, serial: str | None = None) -> list[str]:
        fastboot = self.platform_tools.fastboot_path
        command = [str(fastboot) if fastboot else "fastboot"]
        selected = serial if serial is not None else self.serial
        if selected:
            command.extend(["-s", selected])
        return command

    def run_raw(self, args: list[str], timeout: int | float | None = 120, use_serial: bool = True) -> CommandResult:
        command = self._base() if use_serial else self._base(serial="")
        command.extend(args)
        return self.runner.run(command, timeout=timeout)

    def list_devices(self) -> list[DeviceInfo]:
        result = self.run_raw(["devices"], timeout=15, use_serial=False)
        devices: list[DeviceInfo] = []
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            serial = parts[0]
            state = parts[1] if len(parts) > 1 else "fastboot"
            devices.append(DeviceInfo(serial=serial, mode="Fastboot", state=state))
        return devices

    def getvar_all(self) -> CommandResult:
        return self.run_raw(["getvar", "all"], timeout=60)

    def reboot(self) -> CommandResult:
        return self.run_raw(["reboot"], timeout=60)

    def reboot_bootloader(self) -> CommandResult:
        return self.run_raw(["reboot-bootloader"], timeout=60)

    def flashing_unlock(self) -> CommandResult:
        return self.run_raw(["flashing", "unlock"], timeout=60)

    def flashing_lock(self) -> CommandResult:
        return self.run_raw(["flashing", "lock"], timeout=60)

    def oem_unlock(self) -> CommandResult:
        return self.run_raw(["oem", "unlock"], timeout=60)

    def oem_lock(self) -> CommandResult:
        return self.run_raw(["oem", "lock"], timeout=60)

    def boot_image(self, image_path: str | Path) -> CommandResult:
        return self.run_raw(["boot", str(image_path)], timeout=300)

    def flash_partition(self, partition: str, image_path: str | Path) -> CommandResult:
        return self.run_raw(["flash", partition, str(image_path)], timeout=300)

    def erase_partition(self, partition: str) -> CommandResult:
        return self.run_raw(["erase", partition], timeout=120)

    def format_partition(self, partition: str) -> CommandResult:
        return self.run_raw(["format", partition], timeout=120)
