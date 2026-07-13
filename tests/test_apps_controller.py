from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from openadb.core.apps_controller import AppsController
from openadb.core.device_context import (
    DeviceContext,
    DeviceContextUnavailable,
    StaleDeviceContext,
)
from openadb.core.operations import OperationRegistry
from openadb.models.device_info import DeviceInfo


def context(root: Path, generation: int = 4) -> DeviceContext:
    return DeviceContext(
        serial="device-a",
        mode="ADB",
        transport_id="transport-3",
        profile_key="device-a",
        profile_kind="Phone",
        profile_path=root,
        backups_path=root / "backups",
        temp_path=root / "temp",
        logs_path=root / "logs",
        generation=generation,
    )


class Settings:
    def __init__(self, root: Path) -> None:
        self.config_dir = root
        self.active_profile_kind = "Phone"
        self.values = {
            "backups_folder": str(root / "backups"),
            "temp_folder": str(root / "temp"),
            "logs_folder": str(root / "logs"),
            "show_system_apps": True,
        }
        self.on_get = None

    def get(self, key, default=None):
        if self.on_get is not None:
            self.on_get(key)
        return self.values.get(key, default)


class Devices:
    def __init__(self, captured: DeviceContext) -> None:
        self.context = captured
        self.active = DeviceInfo(
            serial=captured.serial,
            mode=captured.mode,
            transport_id=captured.transport_id,
        )
        self.current_generation = captured.generation
        self.operations = OperationRegistry()
        self.on_active_snapshot = None

    def require_context(self, allowed_modes):
        if self.context.mode not in allowed_modes:
            raise RuntimeError("mode rejected")
        return self.context

    def is_context_current(self, captured):
        return captured == self.context

    def require_current(self, captured):
        if not self.is_context_current(captured):
            raise StaleDeviceContext("stale")

    def active_snapshot(self):
        snapshot = replace(self.active)
        generation = self.current_generation
        if self.on_active_snapshot is not None:
            self.on_active_snapshot()
        return snapshot, generation


class LegacyDevices:
    """Context-API-free manager used to exercise the compatibility branch."""

    def __init__(self, captured: DeviceContext) -> None:
        self.active = DeviceInfo(
            serial=captured.serial,
            mode=captured.mode,
            transport_id=captured.transport_id,
        )
        self.current_generation = captured.generation
        self.operations = OperationRegistry()


class ADB:
    def __init__(self) -> None:
        self.bound: list[DeviceContext] = []

    def for_context(self, captured):
        self.bound.append(captured)
        return SimpleNamespace(
            context=captured,
            device_context=captured,
            serial=captured.serial,
        )


class AppsControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "profile-a"
        self.root.mkdir()
        self.context = context(self.root)
        self.settings = Settings(self.root)
        self.devices = Devices(self.context)
        self.adb = ADB()
        self.controller = AppsController(
            self.adb,  # type: ignore[arg-type]
            self.devices,
            self.settings,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_profile_services_are_anchored_to_captured_profile(self) -> None:
        services = self.controller.profile_services(self.context)

        self.assertEqual(services.settings.config_dir, self.root)
        self.assertEqual(services.app_cache.cache_dir, self.root / "app-cache")
        self.assertEqual(services.icon_extractor.cache_dir, self.root / "icon-cache")
        self.assertTrue(services.include_system)

        self.settings.values["show_system_apps"] = False
        self.assertTrue(services.include_system)

    def test_explicit_profile_option_never_reads_mutable_settings(self) -> None:
        def fail_on_read(key: str) -> None:
            if key == "show_system_apps":
                self.fail("explicit include_system unexpectedly read mutable settings")

        self.settings.on_get = fail_on_read
        services = self.controller.profile_services(
            self.context,
            include_system=False,
        )

        self.assertFalse(services.include_system)
        self.assertEqual(services.app_cache.cache_dir, self.root / "app-cache")

    def test_default_profile_option_rejects_switch_during_settings_read(self) -> None:
        def switch_profile(key: str) -> None:
            if key != "show_system_apps":
                return
            self.settings.on_get = None
            self.devices.context = context(self.root, generation=5)
            self.devices.current_generation = 5

        self.settings.on_get = switch_profile

        with self.assertRaises(StaleDeviceContext):
            self.controller.profile_services(self.context)

    def test_context_binding_snapshot_and_view_identity_use_one_target(self) -> None:
        captured = self.controller.require_context()
        bound = self.controller.bound_adb(captured)
        snapshot = self.controller.device_snapshot(captured)
        self.controller.set_view_identity(captured.serial, captured)

        self.assertIs(bound.context, captured)
        self.assertEqual(snapshot.serial, captured.serial)
        self.assertEqual(snapshot.transport_id, captured.transport_id)
        self.assertTrue(self.controller.view_matches(captured))

    def test_device_snapshot_rejects_switch_after_atomic_capture(self) -> None:
        def switch_device() -> None:
            switched = context(self.root, generation=5)
            self.devices.context = switched
            self.devices.current_generation = switched.generation
            self.devices.active = DeviceInfo(
                serial="device-b",
                model="Device B",
                mode="ADB",
                transport_id="transport-b",
            )

        self.devices.on_active_snapshot = switch_device

        with self.assertRaises(StaleDeviceContext):
            self.controller.device_snapshot(self.context)

    def test_legacy_snapshot_revalidates_after_copying_device_fields(self) -> None:
        legacy = LegacyDevices(self.context)

        class SwitchingActive:
            serial = self.context.serial
            mode = self.context.mode
            transport_id = self.context.transport_id
            switched = False

            @property
            def model(inner_self) -> str:
                if not inner_self.switched:
                    inner_self.switched = True
                    legacy.current_generation += 1
                return "Old device details"

        legacy.active = SwitchingActive()  # type: ignore[assignment]
        controller = AppsController(
            None,
            legacy,
            self.settings,
        )
        captured = controller.require_context()

        with self.assertRaises(StaleDeviceContext):
            controller.device_snapshot(captured)

    def test_legacy_fallback_compares_complete_immutable_identity(self) -> None:
        legacy = LegacyDevices(self.context)
        controller = AppsController(
            SimpleNamespace(serial=self.context.serial),  # type: ignore[arg-type]
            legacy,
            self.settings,
        )
        captured = controller.require_context()
        self.assertTrue(controller.is_current(captured))

        for name in ("serial", "mode", "transport", "generation", "profile"):
            with self.subTest(name=name):
                legacy = LegacyDevices(self.context)
                controller = AppsController(
                    SimpleNamespace(serial=self.context.serial),  # type: ignore[arg-type]
                    legacy,
                    self.settings,
                )
                captured = controller.require_context()
                original_root = self.settings.config_dir
                if name == "profile":
                    self.settings.config_dir = self.root / "other"
                elif name == "serial":
                    legacy.active.serial = "device-b"
                elif name == "mode":
                    legacy.active.mode = "Recovery"
                elif name == "transport":
                    legacy.active.transport_id = "transport-4"
                else:
                    legacy.current_generation = 5
                try:
                    self.assertFalse(controller.is_current(captured))
                finally:
                    self.settings.config_dir = original_root

    def test_legacy_adb_without_context_binding_fails_closed_on_reconnect(self) -> None:
        legacy = LegacyDevices(self.context)
        mutable_adb = SimpleNamespace(serial=self.context.serial)
        controller = AppsController(
            mutable_adb,  # type: ignore[arg-type]
            legacy,
            self.settings,
        )
        captured = controller.require_context()

        with self.assertRaises(DeviceContextUnavailable):
            controller.bound_adb(captured)

        legacy.active.transport_id = "transport-reconnected"
        legacy.current_generation += 1
        self.assertFalse(controller.is_current(captured))

    def test_serial_only_for_context_result_is_rejected(self) -> None:
        adb = SimpleNamespace(
            for_context=lambda captured: SimpleNamespace(serial=captured.serial)
        )
        controller = AppsController(
            adb,  # type: ignore[arg-type]
            self.devices,
            self.settings,
        )

        with self.assertRaises(DeviceContextUnavailable):
            controller.bound_adb(self.context)

    def test_wrong_or_spoofed_bound_context_is_rejected(self) -> None:
        class SpoofedContext:
            def __eq__(self, _other) -> bool:
                return True

        for bound_context in (
            replace(self.context, transport_id="other-transport"),
            SpoofedContext(),
        ):
            with self.subTest(bound_context=type(bound_context).__name__):
                adb = SimpleNamespace(
                    for_context=lambda captured, value=bound_context: SimpleNamespace(
                        serial=captured.serial,
                        device_context=value,
                    )
                )
                controller = AppsController(
                    adb,  # type: ignore[arg-type]
                    self.devices,
                    self.settings,
                )

                with self.assertRaises(DeviceContextUnavailable):
                    controller.bound_adb(self.context)

    def test_registration_rejects_context_changed_during_insert(self) -> None:
        original_register = self.controller.operations.register

        def switch_then_register(*args, **kwargs):
            token = original_register(*args, **kwargs)
            self.devices.context = context(self.root, generation=5)
            return token

        self.controller.operations.register = switch_then_register  # type: ignore[method-assign]

        with self.assertRaises(StaleDeviceContext):
            self.controller.register_operation(
                self.context,
                "metadata",
                "apps-metadata",
            )

        self.assertEqual(self.controller.operations.active_count, 0)

    def test_profile_reset_cancels_all_owned_operations(self) -> None:
        tokens = [
            self.controller.operations.register(
                owner,
                device_context=self.context,
                conflict_group=f"test-{index}",
            )
            for index, owner in enumerate(self.controller.OWNER_KEYS)
        ]
        self.controller.set_view_identity(self.context.serial, self.context)

        self.controller.cancel_profile_operations("profile switched")

        self.assertTrue(all(token.cancelled for token in tokens))
        self.assertTrue(
            all(token.cancellation_reason == "profile switched" for token in tokens)
        )
        self.assertEqual(self.controller.view.serial, "")


if __name__ == "__main__":
    unittest.main()
