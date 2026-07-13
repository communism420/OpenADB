from __future__ import annotations

import threading
from pathlib import Path

from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo

from .command_runner import CommandRunner
from .device_context import DeviceContext
from .platform_tools import PlatformToolsManager


class FastbootClient:
    def __init__(self, platform_tools: PlatformToolsManager, runner: CommandRunner) -> None:
        self.platform_tools = platform_tools
        self.runner = runner
        self._serial = ""

    @property
    def serial(self) -> str:
        return self._serial

    @serial.setter
    def serial(self, value: str) -> None:
        self._serial = str(value or "")

    def set_serial(self, serial: str) -> None:
        self.serial = serial or ""

    def for_context(self, context: DeviceContext) -> BoundFastbootClient:
        if not context.serial:
            raise ValueError("A device serial is required to bind fastboot")
        return BoundFastbootClient(self, context.serial, context)

    def for_serial(self, serial: str) -> BoundFastbootClient:
        serial = str(serial or "").strip()
        if not serial:
            raise ValueError("A device serial is required to bind fastboot")
        return BoundFastbootClient(self, serial, None)

    def _base(self, serial: str | None = None) -> list[str]:
        fastboot = self.platform_tools.fastboot_path
        command = [str(fastboot) if fastboot else "fastboot"]
        selected = serial if serial is not None else self.serial
        if selected:
            command.extend(["-s", selected])
        return command

    def run_raw(
        self,
        args: list[str],
        timeout: int | float | None = 120,
        use_serial: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        command = self._base() if use_serial else self._base(serial="")
        command.extend(args)
        if cancel_event is not None:
            return self.runner.run_streaming(command, timeout=timeout, cancel_event=cancel_event)
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


class BoundFastbootClient(FastbootClient):
    """Fastboot facade permanently bound to one captured target serial."""

    def __init__(self, source: FastbootClient, serial: str, context: DeviceContext | None) -> None:
        self.platform_tools = source.platform_tools
        self.runner = source.runner.for_context(context) if context is not None else source.runner
        self._bound_serial = str(serial)
        self.device_context = context

    @property
    def serial(self) -> str:
        return self._bound_serial

    @serial.setter
    def serial(self, _value: str) -> None:
        raise RuntimeError("A bound fastboot client cannot change serial")

    def _base(self, serial: str | None = None) -> list[str]:
        if serial not in (None, self._bound_serial):
            raise RuntimeError("A bound fastboot client cannot target another serial")
        return super()._base(serial=serial)

    def set_serial(self, serial: str) -> None:
        if str(serial or "") != self._bound_serial:
            raise RuntimeError("A bound fastboot client cannot change serial")

    def for_context(self, context: DeviceContext) -> BoundFastbootClient:
        if context.serial != self._bound_serial:
            raise RuntimeError("A bound fastboot client cannot be rebound to another serial")
        return BoundFastbootClient(self, self._bound_serial, context)

    def for_serial(self, serial: str) -> BoundFastbootClient:
        if str(serial or "") != self._bound_serial:
            raise RuntimeError("A bound fastboot client cannot be rebound to another serial")
        return self
