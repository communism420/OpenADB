from __future__ import annotations

from openadb.models.device_info import DeviceInfo

from .adb import ADBClient
from .fastboot import FastbootClient
from .settings_manager import SettingsManager


class DeviceManager:
    def __init__(self, adb: ADBClient, fastboot: FastbootClient, settings: SettingsManager) -> None:
        self.adb = adb
        self.fastboot = fastboot
        self.settings = settings
        self.active = DeviceInfo()
        self.devices: list[DeviceInfo] = []

    def refresh(self) -> DeviceInfo:
        adb_devices = self.adb.list_devices()
        fastboot_devices = [] if adb_devices else self.fastboot.list_devices()
        all_devices = adb_devices + fastboot_devices
        self.devices = all_devices
        if not all_devices:
            self._set_active(DeviceInfo(mode="No device", state="none"))
            return self.active

        saved_serial = str(self.settings.get("active_device_serial", "") or "")
        selected = next((device for device in all_devices if device.serial == saved_serial), None)
        if selected is None:
            selected = all_devices[0]

        if selected.mode == "ADB":
            self.adb.set_serial(selected.serial)
            detailed = self.adb.get_device_info(selected.serial)
            detailed.transport_id = selected.transport_id
            detailed.product = selected.product
            selected = detailed
        else:
            self.fastboot.set_serial(selected.serial)

        self._set_active(selected)
        return self.active

    def choose(self, serial: str) -> DeviceInfo:
        self.settings.set("active_device_serial", serial)
        for device in self.devices:
            if device.serial == serial:
                self._set_active(device)
                if device.mode == "Fastboot":
                    self.fastboot.set_serial(serial)
                else:
                    self.adb.set_serial(serial)
                return self.active
        return self.refresh()

    def _set_active(self, device: DeviceInfo) -> None:
        self.active = device
        if device.serial:
            self.adb.set_serial(device.serial if device.mode != "Fastboot" else "")
            self.fastboot.set_serial(device.serial if device.mode == "Fastboot" else "")
            if self.settings.get("last_connected_device_serial", "") != device.serial:
                self.settings.set("last_connected_device_serial", device.serial)
