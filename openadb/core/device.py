from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from openadb.models.device_info import DeviceInfo

from .adb import ADBClient
from .device_context import DeviceContext, DeviceContextUnavailable, StaleDeviceContext
from .fastboot import FastbootClient
from .operations import OperationRegistry
from .path_utils import safe_filename
from .settings_manager import SettingsManager


class DeviceManager:
    def __init__(
        self,
        adb: ADBClient,
        fastboot: FastbootClient,
        settings: SettingsManager,
        operations: OperationRegistry | None = None,
    ) -> None:
        self.adb = adb
        self.fastboot = fastboot
        self.settings = settings
        self.operations = operations or OperationRegistry()
        self.active = DeviceInfo()
        self.devices: list[DeviceInfo] = []
        self._context_lock = threading.RLock()
        self._generation = 0
        self._profile_tracking_supported = hasattr(settings, "active_profile_serial")
        self._profile_serial = str(getattr(settings, "active_profile_serial", "") or "")
        self._profile_kind = str(getattr(settings, "active_profile_kind", "") or "")
        self._profile_key = safe_filename(self._profile_serial) if self._profile_serial else ""
        self._profile_paths = self._read_profile_paths()

    @property
    def current_generation(self) -> int:
        with self._context_lock:
            return self._generation

    def active_snapshot(self) -> tuple[DeviceInfo, int]:
        """Return one atomic copy of the active identity and its generation."""

        with self._context_lock:
            return replace(self.active), self._generation

    def capture_context(self) -> DeviceContext:
        with self._context_lock:
            device = self.active
            if not device.serial:
                raise DeviceContextUnavailable("No active Android device is available")
            if self._profile_tracking_supported and self._profile_serial != device.serial:
                raise DeviceContextUnavailable("The active device profile is not ready")
            profile_key = self._profile_key or safe_filename(device.serial)
            profile_path, backups_path, temp_path, logs_path = self._profile_paths
            return DeviceContext(
                serial=device.serial,
                mode=device.mode,
                transport_id=device.transport_id,
                profile_key=profile_key,
                profile_kind=self._profile_kind or self._profile_kind_for_device(device),
                profile_path=profile_path,
                backups_path=backups_path,
                temp_path=temp_path,
                logs_path=logs_path,
                generation=self._generation,
            )

    def require_context(self, allowed_modes: Iterable[str] | None = None) -> DeviceContext:
        context = self.capture_context()
        if allowed_modes is not None:
            allowed = {str(mode) for mode in allowed_modes}
            if context.mode not in allowed:
                expected = ", ".join(sorted(allowed)) or "an available device mode"
                raise DeviceContextUnavailable(
                    f"Current device mode is {context.mode}; expected {expected}"
                )
        return context

    def is_context_current(self, context: DeviceContext | None) -> bool:
        if context is None:
            return True
        with self._context_lock:
            if context.generation != self._generation or not self.active.serial:
                return False
            profile_path, backups_path, temp_path, logs_path = self._profile_paths
            return (
                context.serial == self.active.serial
                and context.mode == self.active.mode
                and context.transport_id == self.active.transport_id
                and context.profile_key == (self._profile_key or safe_filename(self.active.serial))
                and context.profile_kind
                == (self._profile_kind or self._profile_kind_for_device(self.active))
                and context.profile_path == profile_path
                and context.backups_path == backups_path
                and context.temp_path == temp_path
                and context.logs_path == logs_path
            )

    def require_current(self, context: DeviceContext) -> DeviceContext:
        if not self.is_context_current(context):
            raise StaleDeviceContext(
                "The active device or profile changed while the operation was running"
            )
        return context

    def notify_profile_changed(self, serial: str, profile_kind: str = "") -> bool:
        """Synchronize the active profile and invalidate operations if it changed."""

        serial = str(serial or "").strip()
        profile_kind = str(profile_kind or getattr(self.settings, "active_profile_kind", "") or "")
        profile_key = safe_filename(serial) if serial else ""
        profile_paths = self._read_profile_paths()
        with self._context_lock:
            changed = (
                serial != self._profile_serial
                or profile_kind != self._profile_kind
                or profile_key != self._profile_key
                or profile_paths != self._profile_paths
            )
            self._profile_serial = serial
            self._profile_kind = profile_kind
            self._profile_key = profile_key
            self._profile_paths = profile_paths
            generation = self._advance_generation_locked() if changed else self._generation
        if changed:
            self.operations.cancel_stale(generation, "device profile changed")
        return changed

    def invalidate_profile(self, reason: str = "device profile reset") -> int:
        with self._context_lock:
            self._profile_serial = ""
            self._profile_kind = ""
            self._profile_key = ""
            self._profile_paths = self._read_profile_paths()
            generation = self._advance_generation_locked()
        self.operations.cancel_stale(generation, reason)
        return generation

    def refresh(self) -> DeviceInfo:
        starting_generation = self.current_generation
        adb_devices = self.adb.list_devices()
        fastboot_devices = [] if adb_devices else self.fastboot.list_devices()
        all_devices = adb_devices + fastboot_devices
        with self._context_lock:
            if self._generation != starting_generation:
                return self.active
            self.devices = all_devices
        if not all_devices:
            return self._commit_refresh_device(
                DeviceInfo(mode="No device", state="none"), starting_generation
            )

        saved_serial = str(self.settings.get("active_device_serial", "") or "")
        selected = next((device for device in all_devices if device.serial == saved_serial), None)
        if selected is None:
            if len(all_devices) == 1 and not saved_serial:
                selected = all_devices[0]
            else:
                return self._commit_refresh_device(
                    DeviceInfo(mode="No device", state="selection_required"),
                    starting_generation,
                )

        if selected.mode == "ADB":
            refresh_context = self._capture_refresh_context(selected, starting_generation)
            if refresh_context is None:
                return self.active
            bind_context = getattr(self.adb, "for_context", None)
            if callable(bind_context):
                detailed = bind_context(refresh_context).get_device_info()
            elif hasattr(self.adb, "for_serial"):
                detailed = self.adb.for_serial(selected.serial).get_device_info()
            else:  # Compatibility for lightweight integrations and existing test doubles.
                detailed = self.adb.get_device_info(selected.serial)
            detailed.transport_id = selected.transport_id
            detailed.product = selected.product
            selected = detailed

        return self._commit_refresh_device(selected, starting_generation)

    def _commit_refresh_device(self, device: DeviceInfo, starting_generation: int) -> DeviceInfo:
        self._set_active(
            device,
            reason="device refresh changed target",
            expected_generation=starting_generation,
        )
        return self.active

    def reconnect_offline(
        self,
        serial: str = "",
        attempts: int = 4,
        progress_callback=None,
    ) -> DeviceInfo:
        target_serial = (serial or self.active.serial or "").strip()
        if target_serial:
            self.settings.set("active_device_serial", target_serial)
        attempts = max(1, int(attempts))
        for attempt in range(1, attempts + 1):
            self._emit_progress(
                progress_callback,
                f"Device offline. Reconnect attempt {attempt}/{attempts}...",
            )
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
                self._set_active(device, reason="active device selected")
                return self.active
        return self.refresh()

    def _set_active(
        self,
        device: DeviceInfo,
        reason: str = "active device changed",
        *,
        expected_generation: int | None = None,
    ) -> bool:
        with self._context_lock:
            if expected_generation is not None and self._generation != expected_generation:
                return False
            old_identity = self._device_identity(self.active)
            new_identity = self._device_identity(device)
            self.active = device
            changed = old_identity != new_identity
            generation = self._advance_generation_locked() if changed else self._generation
            if device.serial:
                self.adb.set_serial(device.serial if device.mode != "Fastboot" else "")
                self.fastboot.set_serial(device.serial if device.mode == "Fastboot" else "")
                if self.settings.get("last_connected_device_serial", "") != device.serial:
                    self.settings.set("last_connected_device_serial", device.serial)
            else:
                self.adb.set_serial("")
                self.fastboot.set_serial("")
        if changed:
            self.operations.cancel_stale(generation, reason)
        return True

    def _device_identity(self, device: DeviceInfo) -> tuple[str, str, str]:
        if not device.serial:
            return ("", device.mode, "")
        return (device.serial, device.mode, device.transport_id)

    def _advance_generation_locked(self) -> int:
        self._generation += 1
        return self._generation

    def _capture_refresh_context(
        self,
        device: DeviceInfo,
        starting_generation: int,
    ) -> DeviceContext | None:
        """Capture a stable target and log destination for one detail query."""

        with self._context_lock:
            if self._generation != starting_generation:
                return None
            profile_matches = (
                not self._profile_tracking_supported
                or self._profile_serial == device.serial
            )
            if profile_matches:
                profile_path, backups_path, temp_path, logs_path = self._profile_paths
                profile_key = self._profile_key or safe_filename(device.serial)
                profile_kind = self._profile_kind or self._profile_kind_for_device(device)
            else:
                # A newly discovered target has no activated profile yet. Keep
                # discovery output in the stable global folders instead of the
                # unrelated profile that happened to be active at refresh time.
                profile_path = Path(
                    getattr(
                        self.settings,
                        "base_config_dir",
                        getattr(self.settings, "config_dir", Path.cwd()),
                    )
                ).expanduser()
                backups_path = profile_path / "backups"
                temp_path = profile_path / "temp"
                logs_path = profile_path / "logs"
                profile_key = safe_filename(device.serial)
                profile_kind = self._profile_kind_for_device(device)
            return DeviceContext(
                serial=device.serial,
                mode=device.mode,
                transport_id=device.transport_id,
                profile_key=profile_key,
                profile_kind=profile_kind,
                profile_path=profile_path,
                backups_path=backups_path,
                temp_path=temp_path,
                logs_path=logs_path,
                generation=starting_generation,
            )

    def _read_profile_paths(self) -> tuple[Path, Path, Path, Path]:
        profile_path = Path(getattr(self.settings, "config_dir", Path.cwd())).expanduser()

        def configured_path(key: str, default_name: str) -> Path:
            value = str(self.settings.get(key, "") or "").strip()
            return Path(value).expanduser() if value else profile_path / default_name

        return (
            profile_path,
            configured_path("backups_folder", "backups"),
            configured_path("temp_folder", "temp"),
            configured_path("logs_folder", "logs"),
        )

    @staticmethod
    def _profile_kind_for_device(device: DeviceInfo) -> str:
        return "TV" if "tv" in str(device.form_factor or "").casefold() else "Phone"

    @staticmethod
    def _emit_progress(progress_callback, message: str) -> None:
        if progress_callback is not None:
            progress_callback.emit(message)
