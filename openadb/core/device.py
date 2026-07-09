from __future__ import annotations

import time

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

    def reconnect_offline(self, serial: str = "", attempts: int = 4, progress_callback=None) -> DeviceInfo:
        target_serial = (serial or self.active.serial or "").strip()
        if target_serial:
            self.settings.set("active_device_serial", target_serial)
        attempts = max(1, int(attempts))
        for attempt in range(1, attempts + 1):
            self._emit_progress(progress_callback, f"Device offline. Reconnect attempt {attempt}/{attempts}...")
            self.adb.reconnect_offline_device(target_serial)

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                device = self.refresh()
                if device.mode not in {"Offline", "No device", "Checking"}:
                    self._emit_progress(progress_callback, f"Reconnect finished: {device.mode}")
                    return device
                time.sleep(0.6)

        device = self.refresh()
        self._emit_progress(progress_callback, "Reconnect attempts finished.")
        return device

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

    @staticmethod
    def _emit_progress(progress_callback, message: str) -> None:
        if progress_callback is not None:
            progress_callback.emit(message)
