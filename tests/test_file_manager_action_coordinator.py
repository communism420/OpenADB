from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from unittest.mock import patch

from openadb.core.device_context import (
    DeviceContext,
    DeviceContextUnavailable,
    StaleDeviceContext,
)
from openadb.core.file_manager_controller import (
    AndroidActionRequest,
    FileManagerAction,
    FileManagerActionCancelled,
    FileManagerActionCoordinator,
    FileManagerRequestError,
    WindowsActionRequest,
    WindowsNavigationHistory,
)


def make_context(
    root: Path,
    *,
    serial: str = "device-a",
    generation: int = 7,
) -> DeviceContext:
    profile = root / serial
    return DeviceContext(
        serial=serial,
        mode="ADB",
        transport_id=f"transport-{serial}",
        profile_key=serial,
        profile_kind="adb",
        profile_path=profile,
        backups_path=profile / "backups",
        temp_path=profile / "temp",
        logs_path=profile / "logs",
        generation=generation,
    )


@dataclass
class Result:
    success: bool
    status: str = ""
    stderr: str = ""
    stdout: str = ""


class FakeDeviceManager:
    def __init__(self) -> None:
        self.current = True
        self.checked: list[DeviceContext] = []

    def require_current(self, captured: DeviceContext) -> DeviceContext:
        self.checked.append(captured)
        if not self.current:
            raise StaleDeviceContext("selector changed")
        return captured


class BoundADB:
    def __init__(self, captured: DeviceContext) -> None:
        self.device_context = captured
        self.serial = captured.serial
        self.calls: list[tuple] = []
        self.delete_result = Result(True, "deleted")
        self.after_delete = None

    def root_available(self, *, cancel_event=None) -> bool:
        self.calls.append(("root",))
        return True

    def mkdir(self, path, *, use_root, cancel_event):
        self.calls.append(("mkdir", path, use_root))
        return Result(True, "created")

    def delete(self, path, *, recursive, use_root, cancel_event):
        self.calls.append(("delete", path, recursive, use_root))
        result = self.delete_result
        if self.after_delete is not None:
            self.after_delete()
        return result

    def rename(self, source, target, *, use_root, cancel_event):
        self.calls.append(("rename", source, target, use_root))
        return Result(True, "renamed")

    def stat(self, path, *, use_root, cancel_event):
        self.calls.append(("stat", path, use_root))
        return Result(True, stdout="mode: file")

    def install_apk(self, path, *, cancel_event):
        self.calls.append(("install", Path(path)))
        return Result(True, "installed")


class SharedADB:
    def __init__(self, captured: DeviceContext) -> None:
        self.bound = BoundADB(captured)
        self.bound_contexts: list[DeviceContext] = []

    def for_context(self, captured: DeviceContext) -> BoundADB:
        self.bound_contexts.append(captured)
        return self.bound


class FakeBridge:
    def __init__(self) -> None:
        self.delete_calls = 0
        self.grant_calls = 0

    def delete_path(self, path, **kwargs):
        self.delete_calls += 1
        if self.delete_calls == 1:
            return Result(False, "SAF_PERMISSION_REQUIRED: grant MicroSD/USB access")
        return Result(True, "deleted through SAF")

    def grant_storage_access(self, path, **kwargs):
        self.grant_calls += 1
        return Result(True, "granted")


class FileManagerActionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix=".file-manager-actions-",
            dir=Path.cwd(),
        )
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_windows_actions_never_touch_android_collaborators(self) -> None:
        class NoAndroidAccess:
            def __getattribute__(self, name):
                raise AssertionError(f"unexpected Android access: {name}")

        coordinator = FileManagerActionCoordinator(
            NoAndroidAccess(),
            NoAndroidAccess(),
        )
        folder = self.root / "local only"

        created = coordinator.execute_windows(
            WindowsActionRequest.create_folder(self.root, folder.name)
        )
        inspected = coordinator.execute_windows(
            WindowsActionRequest.properties(folder)
        )

        self.assertTrue(created.success)
        self.assertTrue(inspected.success)
        self.assertTrue(folder.is_dir())
        self.assertIn("Type: Folder", inspected.messages[0])

    def test_windows_delete_is_cancellable_between_items(self) -> None:
        first = self.root / "first.txt"
        second = self.root / "second.txt"
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")
        event = threading.Event()
        coordinator = FileManagerActionCoordinator(object(), object())
        original_unlink = Path.unlink

        def unlink_and_cancel(path: Path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            if path == first:
                event.set()

        with patch.object(Path, "unlink", unlink_and_cancel):
            result = coordinator.execute_windows(
                WindowsActionRequest.delete((first, second)),
                cancel_event=event,
            )

        self.assertTrue(result.cancelled)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_windows_recursive_delete_checks_cancel_between_children(self) -> None:
        folder = self.root / "large"
        folder.mkdir()
        first = folder / "first.txt"
        second = folder / "second.txt"
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")
        event = threading.Event()
        coordinator = FileManagerActionCoordinator(object(), object())
        original_unlink = Path.unlink
        deleted: list[Path] = []

        def unlink_and_cancel(path: Path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            deleted.append(path)
            if len(deleted) == 1:
                event.set()

        with patch.object(Path, "unlink", unlink_and_cancel):
            result = coordinator.execute_windows(
                WindowsActionRequest.delete((folder,)),
                cancel_event=event,
            )

        self.assertTrue(result.cancelled)
        self.assertEqual(sum(path.exists() for path in (first, second)), 1)
        self.assertTrue(folder.exists())

    def test_windows_delete_never_descends_into_directory_reparse_points(self) -> None:
        junction = self.root / "junction"
        event = threading.Event()
        coordinator = FileManagerActionCoordinator(object(), object())

        with (
            patch.object(
                FileManagerActionCoordinator,
                "_is_windows_directory_reparse_point",
                return_value=True,
            ),
            patch.object(Path, "rmdir") as rmdir,
            patch("openadb.core.file_manager_controller.os.scandir") as scandir,
        ):
            result = coordinator.execute_windows(
                WindowsActionRequest.delete((junction,)),
                cancel_event=event,
            )

        self.assertTrue(result.success)
        rmdir.assert_called_once_with()
        scandir.assert_not_called()

    def test_android_request_is_frozen_and_bound_to_complete_context(self) -> None:
        captured = make_context(self.root)
        request = AndroidActionRequest.create_folder(
            captured,
            "/sdcard/Download",
            "new folder",
            use_root_requested=True,
        )
        manager = FakeDeviceManager()
        adb = SharedADB(captured)
        coordinator = FileManagerActionCoordinator(adb, manager)

        with self.assertRaises(FrozenInstanceError):
            request.target = "/sdcard/other"  # type: ignore[misc]
        result = coordinator.execute_android(request)

        self.assertTrue(result.success)
        self.assertEqual(adb.bound_contexts, [captured])
        self.assertEqual(
            adb.bound.calls,
            [
                ("root",),
                ("mkdir", "/sdcard/Download/new folder", True),
            ],
        )
        self.assertTrue(all(checked == captured for checked in manager.checked))

    def test_selector_change_after_first_delete_blocks_remaining_mutations(self) -> None:
        captured = make_context(self.root)
        manager = FakeDeviceManager()
        adb = SharedADB(captured)
        adb.bound.after_delete = lambda: setattr(manager, "current", False)
        request = AndroidActionRequest.delete(
            captured,
            ("/sdcard/one", "/sdcard/two"),
        )

        with self.assertRaisesRegex(StaleDeviceContext, "selector changed"):
            FileManagerActionCoordinator(adb, manager).execute_android(request)

        self.assertEqual(
            [call[1] for call in adb.bound.calls if call[0] == "delete"],
            ["/sdcard/one"],
        )

    def test_batch_delete_returns_completed_items_and_the_first_failure(self) -> None:
        captured = make_context(self.root)

        class PartiallyFailingADB(BoundADB):
            def delete(self, path, *, recursive, use_root, cancel_event):
                self.calls.append(("delete", path, recursive, use_root))
                if path == "/sdcard/two":
                    raise PermissionError("permission denied")
                return Result(True, "deleted")

        class SharedPartiallyFailingADB:
            def __init__(self) -> None:
                self.bound = PartiallyFailingADB(captured)

            def for_context(self, context):
                self.assert_context = context
                return self.bound

        adb = SharedPartiallyFailingADB()
        request = AndroidActionRequest.delete(
            captured,
            ("/sdcard/one", "/sdcard/two", "/sdcard/three"),
        )

        result = FileManagerActionCoordinator(
            adb,
            FakeDeviceManager(),
        ).execute_android(request)

        self.assertFalse(result.success)
        self.assertEqual([item.success for item in result.items], [True, False])
        self.assertIn("Permission denied", result.items[-1].message)
        self.assertEqual(
            [call[1] for call in adb.bound.calls if call[0] == "delete"],
            ["/sdcard/one", "/sdcard/two"],
        )

    def test_android_cancel_event_stops_before_binding(self) -> None:
        captured = make_context(self.root)
        adb = SharedADB(captured)
        event = threading.Event()
        event.set()

        with self.assertRaises(FileManagerActionCancelled):
            FileManagerActionCoordinator(adb, FakeDeviceManager()).execute_android(
                AndroidActionRequest.properties(captured, "/sdcard/file"),
                cancel_event=event,
            )

        self.assertEqual(adb.bound_contexts, [])

    def test_removable_delete_does_not_request_saf_without_consent(self) -> None:
        self._assert_removable_delete(
            allow_grant=False,
            expected_grants=0,
            expected_deletes=1,
            success=False,
        )

    def test_removable_delete_waits_for_saf_after_explicit_consent(self) -> None:
        self._assert_removable_delete(
            allow_grant=True,
            expected_grants=1,
            expected_deletes=2,
            success=True,
        )

    def _assert_removable_delete(
        self,
        *,
        allow_grant: bool,
        expected_grants: int,
        expected_deletes: int,
        success: bool,
    ) -> None:
        captured = make_context(self.root)
        adb = SharedADB(captured)
        adb.bound.delete_result = Result(False, "permission denied")
        bridges: list[FakeBridge] = []

        def bridge_factory(*args, **kwargs):
            bridge = FakeBridge()
            bridges.append(bridge)
            return bridge

        coordinator = FileManagerActionCoordinator(
            adb,
            FakeDeviceManager(),
            settings=object(),
            bridge_factory=bridge_factory,
        )
        result = coordinator.execute_android(
            AndroidActionRequest.delete(
                captured,
                ("/storage/1234-ABCD/movie.mkv",),
                allow_storage_grant=allow_grant,
            )
        )

        self.assertEqual(result.success, success)
        self.assertEqual(bridges[0].grant_calls, expected_grants)
        self.assertEqual(bridges[0].delete_calls, expected_deletes)

    def test_strict_binding_rejects_mutable_shared_client(self) -> None:
        captured = make_context(self.root)

        class MutableADB:
            def for_context(self, ignored):
                return self

        with self.assertRaisesRegex(
            DeviceContextUnavailable,
            "mutable shared client",
        ):
            FileManagerActionCoordinator(
                MutableADB(),
                FakeDeviceManager(),
            ).execute_android(
                AndroidActionRequest.properties(captured, "/sdcard/file")
            )

    def test_install_apk_uses_captured_context_and_local_snapshot(self) -> None:
        captured = make_context(self.root)
        apk = self.root / "Example.APK"
        apk.write_bytes(b"not a real apk")
        adb = SharedADB(captured)

        result = FileManagerActionCoordinator(
            adb,
            FakeDeviceManager(),
        ).execute_android(AndroidActionRequest.install_apk(captured, apk))

        self.assertTrue(result.success)
        self.assertEqual(adb.bound.calls, [("install", apk)])

    def test_requests_reject_unsafe_names_and_android_traversal(self) -> None:
        captured = make_context(self.root)
        with self.assertRaisesRegex(FileManagerRequestError, "path separators"):
            WindowsActionRequest.rename(self.root / "old", "../new")
        with self.assertRaisesRegex(FileManagerRequestError, "traversal"):
            AndroidActionRequest.properties(captured, "/sdcard/../data")
        with self.assertRaisesRegex(FileManagerRequestError, "traversal"):
            AndroidActionRequest(
                FileManagerAction.DELETE,
                captured,
                paths=("/sdcard/../data",),
            )
        with self.assertRaisesRegex(FileManagerRequestError, "Only APK"):
            AndroidActionRequest.install_apk(captured, self.root / "app.exe")

    def test_properties_preserves_successful_stat_output(self) -> None:
        captured = make_context(self.root)
        adb = SharedADB(captured)

        result = FileManagerActionCoordinator(
            adb,
            FakeDeviceManager(),
        ).execute_android(AndroidActionRequest.properties(captured, "/sdcard/item.txt"))

        self.assertTrue(result.success)
        self.assertIn("mode: file", result.items[0].message)

    def test_windows_navigation_history_discards_forward_branch(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        branch = self.root / "branch"
        history = WindowsNavigationHistory(first)
        history.push(second)

        self.assertEqual(history.back(), str(first.resolve(strict=False)))
        history.push(branch)

        self.assertEqual(
            history.snapshot.current,
            str(branch.resolve(strict=False)),
        )
        self.assertTrue(history.snapshot.can_go_back)
        self.assertFalse(history.snapshot.can_go_forward)
        self.assertIsNone(history.forward())
        self.assertEqual(
            history.snapshot.entries,
            (
                str(first.resolve(strict=False)),
                str(branch.resolve(strict=False)),
            ),
        )


if __name__ == "__main__":
    unittest.main()
