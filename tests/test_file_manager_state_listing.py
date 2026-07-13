from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from openadb.core.device_context import DeviceContext, DeviceContextUnavailable
from openadb.core.file_listing_controller import (
    FileListingController,
    StaleFileListing,
)
from openadb.core.file_manager_errors import (
    FileManagerErrorCode,
    TransferCancelled,
    map_file_manager_error,
)
from openadb.core.file_manager_state import (
    FileManagerState,
    FileManagerUIState,
    StaleFileManagerProfile,
)


def make_context(root: Path, *, serial: str = "device-a", generation: int = 1) -> DeviceContext:
    profile = root / serial
    return DeviceContext(
        serial=serial,
        mode="ADB",
        transport_id=f"transport-{serial}",
        profile_key=serial,
        profile_kind="Phone",
        profile_path=profile,
        backups_path=profile / "backups",
        temp_path=profile / "temp",
        logs_path=profile / "logs",
        generation=generation,
    )


class FakeDeviceManager:
    def __init__(self, context: DeviceContext) -> None:
        self.context = context

    def require_context(self, allowed_modes) -> DeviceContext:
        if self.context.mode not in allowed_modes:
            raise RuntimeError("mode rejected")
        return self.context

    def is_context_current(self, context: DeviceContext) -> bool:
        return context == self.context


class FakeBoundADB:
    def __init__(self, context: DeviceContext) -> None:
        self.device_context = context
        self.serial = context.serial
        self.before_files_return = None
        self.before_storage_return = None
        self.before_volumes_return = None
        self.list_calls: list[str] = []
        self.storage_calls: list[str] = []
        self.volume_calls = 0

    def list_files(self, path, *, use_root=False, cancel_event=None):
        self.list_calls.append(path)
        if self.before_files_return is not None:
            self.before_files_return()
        return [f"item:{path}"]

    def storage_info(self, path, *, use_root=False, cancel_event=None):
        self.storage_calls.append(path)
        if self.before_storage_return is not None:
            self.before_storage_return()
        return {"free_bytes": 123}

    def storage_volumes(self, *, use_root=False, cancel_event=None):
        self.volume_calls += 1
        if self.before_volumes_return is not None:
            self.before_volumes_return()
        return ["internal", "usb"]


class FakeADB:
    def __init__(self, bound: FakeBoundADB) -> None:
        self.bound = bound
        self.bound_contexts: list[DeviceContext] = []

    def for_context(self, context: DeviceContext) -> FakeBoundADB:
        self.bound_contexts.append(context)
        return self.bound


class ProfileSettings:
    def __init__(self) -> None:
        self._save_lock = threading.RLock()
        self.active_profile_serial = "device-a"
        self.active_profile_kind = "Phone"
        self.config_dir = Path("C:/OpenADB/Phones/device-a")
        self.profiles = {
            "device-a": {"file_manager_android_path": "/sdcard/A"},
            "device-b": {"file_manager_android_path": "/sdcard/B"},
        }
        self.global_values = {
            "file_manager_windows_path": "C:/Users/Public",
            "file_manager_splitter_sizes": [300, 170, 500],
        }

    def get(self, key, default=None):
        return self.profiles[self.active_profile_serial].get(key, default)

    def set(self, key, value):
        self.profiles[self.active_profile_serial][key] = value

    def get_global(self, key, default=None):
        return self.global_values.get(key, default)

    def set_global_values(self, values):
        self.global_values.update(values)

    def switch_profile(self, serial: str, kind: str = "Phone") -> None:
        with self._save_lock:
            self.active_profile_serial = serial
            self.active_profile_kind = kind
            self.config_dir = Path(f"C:/OpenADB/{kind}s/{serial}")


class RacingProfileSettings(ProfileSettings):
    def __init__(self) -> None:
        super().__init__()
        self.switch_attempted = threading.Event()
        self.switch_finished = threading.Event()
        self.switch_thread: threading.Thread | None = None

    def set(self, key, value):
        def switch() -> None:
            self.switch_attempted.set()
            self.switch_profile("device-b")
            self.switch_finished.set()

        self.switch_thread = threading.Thread(target=switch)
        self.switch_thread.start()
        self.assert_switch_attempt_started()
        super().set(key, value)

    def assert_switch_attempt_started(self) -> None:
        if not self.switch_attempted.wait(timeout=2):
            raise RuntimeError("profile switch test thread did not start")


class FileListingControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = make_context(self.root)
        self.devices = FakeDeviceManager(self.context)
        self.bound = FakeBoundADB(self.context)
        self.controller = FileListingController(
            FakeADB(self.bound),
            self.devices,
            android_path="/sdcard/one",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_stale_device_listing_is_rejected(self) -> None:
        prepared = self.controller.begin_android_listing()
        result = self.controller.load_android_listing(prepared)
        self.devices.context = make_context(
            self.root,
            serial="device-b",
            generation=2,
        )

        with self.assertRaises(StaleFileListing):
            self.controller.accept_android_listing(result)
        self.assertFalse(self.controller.is_listing_current(prepared.request))
        self.assertEqual(self.bound.list_calls, ["/sdcard/one"])

    def test_path_switch_during_listing_rejects_late_result(self) -> None:
        prepared = self.controller.begin_android_listing("/sdcard/one")
        self.bound.before_files_return = lambda: self.controller.set_android_path(
            "/sdcard/two"
        )

        with self.assertRaisesRegex(StaleFileListing, "folder changed"):
            self.controller.load_android_listing(prepared)

        self.assertEqual(self.bound.list_calls, ["/sdcard/one"])
        self.assertEqual(self.bound.storage_calls, [])
        self.assertEqual(self.controller.requested_android_path, "/sdcard/two")

    def test_result_is_revalidated_immediately_before_ui_apply(self) -> None:
        prepared = self.controller.begin_android_listing("/sdcard/one")
        result = self.controller.load_android_listing(prepared)
        self.controller.set_android_path("/sdcard/two")

        with self.assertRaises(StaleFileListing):
            self.controller.accept_android_listing(result)

    def test_storage_volume_generation_rejects_older_request(self) -> None:
        older = self.controller.begin_storage_volumes()
        older_result = self.controller.load_storage_volumes(older)
        current = self.controller.begin_storage_volumes()

        with self.assertRaisesRegex(StaleFileListing, "newer"):
            self.controller.accept_storage_volumes(older_result)

        result = self.controller.load_storage_volumes(current)
        self.assertIs(self.controller.accept_storage_volumes(result), result)
        self.assertEqual(result.volumes, ("internal", "usb"))
        self.assertEqual(self.bound.volume_calls, 2)

    def test_request_captures_bound_context_path_generation_and_options(self) -> None:
        prepared = self.controller.begin_android_listing(
            "/storage/USB/Movies",
            use_root=True,
        )

        self.assertIs(prepared.request.device_context, self.context)
        self.assertEqual(prepared.request.requested_path, "/storage/USB/Movies")
        self.assertEqual(prepared.request.generation, self.controller.listing_generation)
        self.assertTrue(prepared.request.use_root)
        self.assertIs(prepared.adb, self.bound)
        with self.assertRaises(FrozenInstanceError):
            prepared.request.requested_path = "/changed"  # type: ignore[misc]

    def test_wrong_bound_context_is_rejected_before_listing_or_storage_calls(self) -> None:
        wrong_context = make_context(self.root, serial="device-b", generation=2)
        wrong_bound = FakeBoundADB(wrong_context)
        controller = FileListingController(
            FakeADB(wrong_bound),
            self.devices,
            android_path="/sdcard/one",
        )

        with self.assertRaisesRegex(
            DeviceContextUnavailable,
            "captured file-listing context",
        ):
            controller.begin_android_listing()
        with self.assertRaisesRegex(
            DeviceContextUnavailable,
            "captured file-listing context",
        ):
            controller.begin_storage_volumes()

        self.assertEqual(wrong_bound.list_calls, [])
        self.assertEqual(wrong_bound.storage_calls, [])
        self.assertEqual(wrong_bound.volume_calls, 0)

    def test_wrong_bound_serial_is_rejected_before_listing_or_storage_calls(self) -> None:
        wrong_bound = FakeBoundADB(self.context)
        wrong_bound.serial = "device-b"
        controller = FileListingController(
            FakeADB(wrong_bound),
            self.devices,
            android_path="/sdcard/one",
        )

        with self.assertRaisesRegex(DeviceContextUnavailable, "another device"):
            controller.begin_android_listing()
        with self.assertRaisesRegex(DeviceContextUnavailable, "another device"):
            controller.begin_storage_volumes()

        self.assertEqual(wrong_bound.list_calls, [])
        self.assertEqual(wrong_bound.storage_calls, [])
        self.assertEqual(wrong_bound.volume_calls, 0)

    def test_bound_identity_is_revalidated_before_every_adb_read(self) -> None:
        prepared = self.controller.begin_android_listing()
        self.bound.serial = "device-b"

        with self.assertRaisesRegex(DeviceContextUnavailable, "another device"):
            self.controller.load_android_listing(prepared)
        self.assertEqual(self.bound.list_calls, [])
        self.assertEqual(self.bound.storage_calls, [])

        self.bound.serial = self.context.serial
        prepared = self.controller.begin_android_listing()
        self.bound.before_files_return = lambda: setattr(
            self.bound,
            "device_context",
            make_context(self.root, serial="device-b", generation=2),
        )

        with self.assertRaisesRegex(
            DeviceContextUnavailable,
            "captured file-listing context",
        ):
            self.controller.load_android_listing(prepared)
        self.assertEqual(self.bound.list_calls, ["/sdcard/one"])
        self.assertEqual(self.bound.storage_calls, [])

        self.bound.before_files_return = None
        self.bound.device_context = self.context
        prepared_volumes = self.controller.begin_storage_volumes()
        self.bound.serial = "device-b"

        with self.assertRaisesRegex(DeviceContextUnavailable, "another device"):
            self.controller.load_storage_volumes(prepared_volumes)
        self.assertEqual(self.bound.volume_calls, 0)

    def test_bound_identity_is_revalidated_after_the_final_adb_read(self) -> None:
        prepared = self.controller.begin_android_listing()
        self.bound.before_storage_return = lambda: setattr(
            self.bound,
            "serial",
            "device-b",
        )

        with self.assertRaisesRegex(DeviceContextUnavailable, "another device"):
            self.controller.load_android_listing(prepared)
        self.assertEqual(self.bound.storage_calls, ["/sdcard/one"])

        self.bound.before_storage_return = None
        self.bound.serial = self.context.serial
        prepared_volumes = self.controller.begin_storage_volumes()
        self.bound.before_volumes_return = lambda: setattr(
            self.bound,
            "device_context",
            make_context(self.root, serial="device-b", generation=2),
        )

        with self.assertRaisesRegex(
            DeviceContextUnavailable,
            "captured file-listing context",
        ):
            self.controller.load_storage_volumes(prepared_volumes)
        self.assertEqual(self.bound.volume_calls, 1)

    def test_windows_listing_and_navigation_need_no_android(self) -> None:
        folder = self.root / "windows"
        child = folder / "Folder"
        child.mkdir(parents=True)
        (folder / "file.txt").write_text("hello", encoding="utf-8")
        controller = FileListingController()

        navigated = controller.navigate_windows(folder)
        result = controller.list_windows(navigated)

        self.assertEqual(result.requested_path, str(folder.resolve()))
        self.assertEqual([entry.name for entry in result.entries], ["Folder", "file.txt"])
        self.assertTrue(result.entries[0].is_dir)
        self.assertEqual(result.entries[1].size, 5)

    def test_cancelled_listing_stops_before_adb_and_maps_as_cancelled(self) -> None:
        prepared = self.controller.begin_android_listing("/sdcard/")
        cancel_event = threading.Event()
        cancel_event.set()

        with self.assertRaises(TransferCancelled) as raised:
            self.controller.load_android_listing(
                prepared,
                cancel_event=cancel_event,
            )

        self.assertEqual(self.bound.list_calls, [])
        mapped = map_file_manager_error(raised.exception, operation="File listing")
        self.assertEqual(mapped.code, FileManagerErrorCode.CANCELLED)
        self.assertTrue(mapped.cancelled)


class FileManagerStateTests(unittest.TestCase):
    def test_profile_paths_and_global_splitter_serialize_independently(self) -> None:
        settings = ProfileSettings()
        state = FileManagerState(settings)
        self.assertEqual(state.profile_key, "device-a")
        self.assertEqual(state.android_path, "/sdcard/A")

        state.save_android_path("/storage/AAAA/Movies")
        state.save_windows_path("C:/Media")
        state.save_splitter_sizes([240, 160, 600])
        serialized = state.snapshot.to_mapping()
        restored = FileManagerUIState.from_mapping(serialized)

        self.assertEqual(restored, state.snapshot)
        self.assertEqual(
            settings.profiles["device-a"]["file_manager_android_path"],
            "/storage/AAAA/Movies",
        )
        self.assertEqual(settings.global_values["file_manager_windows_path"], "C:\\Media")
        self.assertEqual(
            settings.global_values["file_manager_splitter_sizes"],
            [240, 160, 600],
        )

        settings.switch_profile("device-b")
        with self.assertRaises(StaleFileManagerProfile):
            state.save_android_path("/wrong-profile")
        state.reload()
        self.assertEqual(state.profile_key, "device-b")
        self.assertEqual(state.android_path, "/sdcard/B")
        self.assertEqual(state.splitter_sizes, (240, 160, 600))

    def test_profile_switch_cannot_interleave_with_android_path_save(self) -> None:
        settings = RacingProfileSettings()
        state = FileManagerState(settings)

        state.save_android_path("/sdcard/saved-for-a")
        self.assertIsNotNone(settings.switch_thread)
        settings.switch_thread.join(timeout=2)  # type: ignore[union-attr]

        self.assertTrue(settings.switch_finished.is_set())
        self.assertEqual(
            settings.profiles["device-a"]["file_manager_android_path"],
            "/sdcard/saved-for-a",
        )
        self.assertEqual(
            settings.profiles["device-b"]["file_manager_android_path"],
            "/sdcard/B",
        )

    def test_same_serial_in_another_profile_kind_or_path_is_stale(self) -> None:
        settings = ProfileSettings()
        state = FileManagerState(settings)
        settings.switch_profile("device-a", kind="TV")

        with self.assertRaisesRegex(StaleFileManagerProfile, "kind changed"):
            state.save_android_path("/storage/tv")


if __name__ == "__main__":
    unittest.main()
