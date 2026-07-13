from __future__ import annotations

import io
import tempfile
import tarfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openadb.core.adb_transfer_strategy import ADBTransferStrategy


class FakePullADB:
    def __init__(self, payload: bytes, *, success: bool) -> None:
        self.payload = payload
        self.success = success
        self.destinations: list[Path] = []

    def pull_file_streaming_to_file(
        self,
        _source,
        destination,
        *,
        progress_callback,
        **_kwargs,
    ):
        target = Path(destination)
        self.destinations.append(target)
        target.write_bytes(self.payload)
        progress_callback(len(self.payload))
        return SimpleNamespace(
            success=self.success,
            status="Success" if self.success else "Simulated pull failure",
            error_type="" if self.success else "pull_failed",
            stdout="",
            stderr="",
        )


class FakeTarPullADB:
    def __init__(
        self,
        archive_payload: bytes,
        *,
        success: bool,
        cancel_after_stream: threading.Event | None = None,
    ) -> None:
        self.archive_payload = archive_payload
        self.success = success
        self.cancel_after_stream = cancel_after_stream

    def pull_tar_streaming(self, *, output_writer, **_kwargs):
        output_writer(io.BytesIO(self.archive_payload))
        if self.cancel_after_stream is not None:
            self.cancel_after_stream.set()
        return SimpleNamespace(
            success=self.success,
            status="Success" if self.success else "Simulated TAR failure",
            error_type="" if self.success else "pull_failed",
            stdout="",
            stderr="",
        )


class FakePushADB:
    def __init__(
        self,
        *,
        stream_error: BaseException | None = None,
        finalize_error: BaseException | None = None,
        before_input_writer=None,
    ) -> None:
        self.stream_error = stream_error
        self.finalize_error = finalize_error
        self.before_input_writer = before_input_writer
        self.shell_scripts: list[str] = []

    def run_raw_with_input_stream(self, _args, *, input_writer, **_kwargs):
        if self.before_input_writer is not None:
            self.before_input_writer()
        input_writer(io.BytesIO())
        if self.stream_error is not None:
            raise self.stream_error
        return SimpleNamespace(
            success=True,
            status="Success",
            error_type="",
            stdout="",
            stderr="",
        )

    def run_shell(self, script, **_kwargs):
        self.shell_scripts.append(script)
        if "mv -f" in script and self.finalize_error is not None:
            raise self.finalize_error
        return SimpleNamespace(
            success=True,
            status="Success",
            error_type="",
            stdout="",
            stderr="",
        )


class ADBTransferStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = ADBTransferStrategy()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _pull(
        self,
        adb: FakePullADB,
        cancel_event: threading.Event | None = None,
        *,
        expected_size: int | None = None,
    ):
        event = cancel_event or threading.Event()
        return self.strategy._run_single_file_pull_with_progress(
            adb=adb,  # type: ignore[arg-type]
            source="/sdcard/movie.bin",
            display_source="/sdcard/movie.bin",
            destination=self.root,
            cancel_event=event,
            output_callback=None,
            item_callback=None,
            entry_size=len(adb.payload) if expected_size is None else expected_size,
            done_bytes=0,
            total_bytes=len(adb.payload),
            total_files=1,
            done_files=0,
            started=time.monotonic(),
        )

    def _tar_pull(
        self,
        *,
        success: bool,
        cancel_event: threading.Event | None = None,
        payloads: dict[str, bytes] | None = None,
        directories: tuple[str, ...] = (),
    ):
        if payloads is None:
            payloads = {"folder/movie.bin": b"replacement from archive"}
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name in directories:
                member = tarfile.TarInfo(name)
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            for name, payload in payloads.items():
                member = tarfile.TarInfo(name)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        event = cancel_event or threading.Event()
        adb = FakeTarPullADB(
            stream.getvalue(),
            success=success,
            cancel_after_stream=event if cancel_event is not None else None,
        )
        result = self.strategy._run_fast_tar_pull_with_progress(
            adb=adb,  # type: ignore[arg-type]
            source="/sdcard/folder",
            destination=self.root,
            tar_command="tar",
            cancel_event=event,
            output_callback=None,
            item_callback=None,
            entry_size=sum(len(payload) for payload in payloads.values()),
            done_bytes=0,
            total_bytes=sum(len(payload) for payload in payloads.values()),
            total_files=len(payloads) + len(directories),
            done_files=0,
            started=time.monotonic(),
            entry_count=len(payloads) + len(directories),
        )
        return result

    def _push(self, adb: FakePushADB):
        source = self.root / "local-movie.bin"
        source.write_bytes(b"complete local payload")
        return self.strategy._stream_push_file_to_android_target(
            adb=adb,  # type: ignore[arg-type]
            source=source,
            target="/sdcard/movie.bin",
            cancel_event=threading.Event(),
            output_callback=None,
            item_callback=None,
            base_done_bytes=0,
            base_done_files=0,
            total_bytes=source.stat().st_size,
            total_files=1,
            started=time.monotonic(),
            expected_size=source.stat().st_size,
        )

    def test_failed_pull_removes_temporary_file_and_preserves_destination(self) -> None:
        destination = self.root / "movie.bin"
        destination.write_bytes(b"existing complete file")
        adb = FakePullADB(b"partial replacement", success=False)

        result = self._pull(adb)

        self.assertFalse(result["result"].success)
        self.assertEqual(destination.read_bytes(), b"existing complete file")
        self.assertEqual(list(self.root.glob(".openadb-part-*")), [])
        self.assertNotEqual(adb.destinations, [destination])

    def test_successful_pull_atomically_replaces_existing_destination(self) -> None:
        destination = self.root / "movie.bin"
        destination.write_bytes(b"old")
        adb = FakePullADB(b"verified replacement", success=True)

        result = self._pull(adb)

        self.assertTrue(result["result"].success)
        self.assertEqual(destination.read_bytes(), b"verified replacement")
        self.assertEqual(list(self.root.glob(".openadb-part-*")), [])

    def test_short_successful_pull_preserves_existing_destination(self) -> None:
        destination = self.root / "movie.bin"
        destination.write_bytes(b"existing complete file")

        result = self._pull(
            FakePullADB(b"short", success=True),
            expected_size=10,
        )

        self.assertFalse(result["result"].success)
        self.assertEqual(result["result"].error_type, "source_changed")
        self.assertEqual(result["observed_files"], 0)
        self.assertEqual(destination.read_bytes(), b"existing complete file")
        self.assertEqual(list(self.root.glob(".openadb-part-*")), [])

    def test_pull_finalize_exception_preserves_target_and_removes_temp(self) -> None:
        destination = self.root / "movie.bin"
        destination.write_bytes(b"old")
        adb = FakePullADB(b"verified replacement", success=True)

        with patch(
            "openadb.core.adb_transfer_strategy.os.replace",
            side_effect=OSError("simulated finalize failure"),
        ):
            result = self._pull(adb)

        self.assertFalse(result["result"].success)
        self.assertEqual(result["result"].error_type, "local_rename_failed")
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(list(self.root.glob(".openadb-part-*")), [])

    def test_cancel_after_stream_cleans_partial_pull_without_replacing_target(self) -> None:
        destination = self.root / "movie.bin"
        destination.write_bytes(b"old")
        cancel_event = threading.Event()

        class CancelAfterWriteADB(FakePullADB):
            def pull_file_streaming_to_file(self, *args, **kwargs):
                result = super().pull_file_streaming_to_file(*args, **kwargs)
                cancel_event.set()
                return result

        result = self._pull(
            CancelAfterWriteADB(b"new but cancelled", success=True),
            cancel_event,
        )

        self.assertFalse(result["result"].success)
        self.assertEqual(result["result"].error_type, "cancelled")
        self.assertEqual(destination.read_bytes(), b"old")
        self.assertEqual(list(self.root.glob(".openadb-part-*")), [])

    def test_raised_pull_stream_exception_removes_temporary_file(self) -> None:
        destination = self.root / "movie.bin"
        destination.write_bytes(b"existing complete file")

        class RaisingPullADB(FakePullADB):
            def pull_file_streaming_to_file(self, *args, **kwargs):
                target = Path(args[1])
                self.destinations.append(target)
                target.write_bytes(b"partial stream")
                kwargs["progress_callback"](len(b"partial stream"))
                raise RuntimeError("simulated stream failure")

        with self.assertRaisesRegex(RuntimeError, "simulated stream failure"):
            self._pull(RaisingPullADB(b"partial stream", success=True))

        self.assertEqual(destination.read_bytes(), b"existing complete file")
        self.assertEqual(list(self.root.glob(".openadb-part-*")), [])

    def test_raised_push_stream_exception_requests_remote_temp_cleanup(self) -> None:
        adb = FakePushADB(stream_error=RuntimeError("simulated stream failure"))

        with self.assertRaisesRegex(RuntimeError, "simulated stream failure"):
            self._push(adb)

        self.assertEqual(len(adb.shell_scripts), 1)
        self.assertIn("rm -f", adb.shell_scripts[0])
        self.assertIn(".openadb-part-", adb.shell_scripts[0])

    def test_shrunk_push_source_is_not_finalized_or_reported_complete(self) -> None:
        source = self.root / "local-movie.bin"
        adb = FakePushADB(before_input_writer=lambda: source.write_bytes(b"short"))

        result, sent_bytes = self._push(adb)

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "source_changed")
        self.assertEqual(sent_bytes, 0)
        self.assertEqual(len(adb.shell_scripts), 1)
        self.assertNotIn("mv -f", adb.shell_scripts[0])
        self.assertIn("rm -f", adb.shell_scripts[0])

    def test_grown_push_source_is_not_finalized_or_reported_complete(self) -> None:
        source = self.root / "local-movie.bin"

        def grow_source() -> None:
            source.write_bytes(b"complete local payload plus concurrent growth")

        adb = FakePushADB(before_input_writer=grow_source)

        result, sent_bytes = self._push(adb)

        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "source_changed")
        self.assertEqual(sent_bytes, 0)
        self.assertNotIn("mv -f", "\n".join(adb.shell_scripts))

    def test_raised_push_finalize_exception_requests_remote_temp_cleanup(self) -> None:
        adb = FakePushADB(finalize_error=RuntimeError("simulated finalize failure"))

        with self.assertRaisesRegex(RuntimeError, "simulated finalize failure"):
            self._push(adb)

        self.assertEqual(len(adb.shell_scripts), 2)
        self.assertIn("mv -f", adb.shell_scripts[0])
        self.assertIn("rm -f", adb.shell_scripts[1])
        self.assertIn(".openadb-part-", adb.shell_scripts[1])

    def test_push_finalize_rejects_an_existing_remote_directory_target(self) -> None:
        adb = FakePushADB()

        result, _sent_bytes = self._push(adb)

        self.assertTrue(result.success)
        finalize_script = adb.shell_scripts[0]
        self.assertIn('[ -d "$target" ]', finalize_script)
        self.assertIn("destination is a directory", finalize_script)
        self.assertIn('stat -c %s "$tmp"', finalize_script)
        self.assertIn('"22"', finalize_script)
        self.assertLess(finalize_script.index('[ -d "$target" ]'), finalize_script.index("mv -f"))

    def test_failed_tar_pull_preserves_existing_files_and_removes_staging(self) -> None:
        destination = self.root / "folder" / "movie.bin"
        destination.parent.mkdir()
        destination.write_bytes(b"existing complete file")

        result = self._tar_pull(success=False)

        self.assertFalse(result["result"].success)
        self.assertEqual(destination.read_bytes(), b"existing complete file")
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])

    def test_successful_tar_pull_replaces_target_and_removes_transaction_files(self) -> None:
        destination = self.root / "folder" / "movie.bin"
        destination.parent.mkdir()
        destination.write_bytes(b"existing complete file")

        result = self._tar_pull(success=True)

        self.assertTrue(result["result"].success)
        self.assertEqual(destination.read_bytes(), b"replacement from archive")
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])
        self.assertEqual(list(self.root.rglob(".openadb-backup-*")), [])

    def test_cancelled_tar_pull_preserves_existing_files_and_removes_staging(self) -> None:
        destination = self.root / "folder" / "movie.bin"
        destination.parent.mkdir()
        destination.write_bytes(b"existing complete file")
        cancel_event = threading.Event()

        result = self._tar_pull(success=True, cancel_event=cancel_event)

        self.assertFalse(result["result"].success)
        self.assertEqual(result["result"].error_type, "cancelled")
        self.assertEqual(destination.read_bytes(), b"existing complete file")
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])

    def test_second_tar_replace_failure_rolls_back_all_targets_and_cleans_files(self) -> None:
        first = self.root / "folder" / "first.bin"
        second = self.root / "folder" / "second.bin"
        first.parent.mkdir()
        first.write_bytes(b"original first")
        second.write_bytes(b"original second")
        real_install = self.strategy._install_local_staged_file
        staging_replacements = 0

        def fail_second_staging_replace(source, target):
            nonlocal staging_replacements
            staging_replacements += 1
            if staging_replacements == 2:
                raise OSError("simulated second replace failure")
            return real_install(source, target)

        with patch.object(
            self.strategy,
            "_install_local_staged_file",
            side_effect=fail_second_staging_replace,
        ):
            result = self._tar_pull(
                success=True,
                payloads={
                    "folder/first.bin": b"replacement first",
                    "folder/second.bin": b"replacement second",
                },
            )

        self.assertFalse(result["result"].success)
        self.assertEqual(result["result"].error_type, "local_rename_failed")
        self.assertEqual(staging_replacements, 2)
        self.assertEqual(first.read_bytes(), b"original first")
        self.assertEqual(second.read_bytes(), b"original second")
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])
        self.assertEqual(list(self.root.rglob(".openadb-backup-*")), [])

    def test_tar_rollback_does_not_delete_an_uncommitted_concurrent_target(self) -> None:
        first = self.root / "folder" / "first.bin"
        second = self.root / "folder" / "second.bin"
        real_install = self.strategy._install_local_staged_file
        staging_replacements = 0

        def fail_after_concurrent_create(source, target):
            nonlocal staging_replacements
            target_path = Path(target)
            staging_replacements += 1
            if staging_replacements == 2:
                target_path.write_bytes(b"created by another process")
            return real_install(source, target)

        with patch.object(
            self.strategy,
            "_install_local_staged_file",
            side_effect=fail_after_concurrent_create,
        ):
            result = self._tar_pull(
                success=True,
                payloads={
                    "folder/first.bin": b"replacement first",
                    "folder/second.bin": b"replacement second",
                },
            )

        self.assertFalse(result["result"].success)
        self.assertFalse(first.exists())
        self.assertEqual(second.read_bytes(), b"created by another process")
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])
        self.assertEqual(list(self.root.rglob(".openadb-backup-*")), [])

    def test_tar_rollback_preserves_a_target_changed_after_commit(self) -> None:
        first = self.root / "folder" / "first.bin"
        second = self.root / "folder" / "second.bin"
        first.parent.mkdir()
        first.write_bytes(b"original first")
        second.write_bytes(b"original second")
        real_install = self.strategy._install_local_staged_file
        staging_replacements = 0

        def change_first_then_fail_second(source, target):
            nonlocal staging_replacements
            staging_replacements += 1
            if staging_replacements == 2:
                first.write_bytes(b"concurrent first change")
                raise OSError("simulated later commit failure")
            return real_install(source, target)

        with patch.object(
            self.strategy,
            "_install_local_staged_file",
            side_effect=change_first_then_fail_second,
        ):
            result = self._tar_pull(
                success=True,
                payloads={
                    "folder/first.bin": b"replacement first",
                    "folder/second.bin": b"replacement second",
                },
            )

        self.assertFalse(result["result"].success)
        self.assertIn("destination changed concurrently", result["result"].stderr)
        self.assertEqual(first.read_bytes(), b"concurrent first change")
        self.assertEqual(second.read_bytes(), b"original second")
        backups = list(self.root.rglob(".openadb-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"original first")
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])

    def test_tar_commit_detects_a_target_replaced_before_post_publish_validation(self) -> None:
        target = self.root / "folder" / "movie.bin"
        target.parent.mkdir()
        target.write_bytes(b"original")
        real_install = self.strategy._install_local_staged_file

        def replace_before_return(source, destination):
            real_install(source, destination)
            target.write_bytes(b"concurrent replacement")

        with patch.object(
            self.strategy,
            "_install_local_staged_file",
            side_effect=replace_before_return,
        ):
            result = self._tar_pull(success=True)

        self.assertFalse(result["result"].success)
        self.assertIn("Destination changed", result["result"].stderr)
        self.assertEqual(target.read_bytes(), b"concurrent replacement")
        backups = list(self.root.rglob(".openadb-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"original")
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])

    def test_failed_tar_pull_removes_only_new_empty_destination_directories(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()

        result = self._tar_pull(
            success=False,
            payloads={"existing/new/sub/movie.bin": b"partial"},
        )

        self.assertFalse(result["result"].success)
        self.assertTrue(existing.exists())
        self.assertFalse((existing / "new").exists())
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])

    def test_tar_empty_directory_cannot_succeed_over_an_existing_file(self) -> None:
        collision = self.root / "folder" / "empty"
        collision.parent.mkdir()
        collision.write_bytes(b"keep me")

        with self.assertRaisesRegex(OSError, "non-directory"):
            self._tar_pull(
                success=True,
                payloads={},
                directories=("folder/empty/",),
            )

        self.assertEqual(collision.read_bytes(), b"keep me")
        self.assertEqual(list(self.root.rglob(".openadb-part-*")), [])

    def test_root_denial_falls_back_to_normal_adb(self) -> None:
        adb = SimpleNamespace(root_available=lambda **_kwargs: False)
        self.assertFalse(
            self.strategy._root_available_for_worker(
                adb,  # type: ignore[arg-type]
                True,
                threading.Event(),
            )
        )
        self.assertFalse(
            self.strategy._root_available_for_worker(
                adb,  # type: ignore[arg-type]
                False,
                threading.Event(),
            )
        )

    def test_long_single_file_uses_streaming_fallback(self) -> None:
        long_source = Path("C:/" + "/".join(["nested-folder"] * 30) + "/movie.bin")
        self.assertGreater(len(str(long_source)), 260)
        with patch.object(Path, "is_file", autospec=True, return_value=True):
            self.assertTrue(
                self.strategy._should_use_single_file_stream(
                    long_source,
                    is_pull=False,
                    entry_count=1,
                    entry_is_dir=False,
                )
            )

    def test_small_multi_file_directory_keeps_tar_optimization(self) -> None:
        source = self.root / "many-small-files"
        source.mkdir()
        markers = [
            ((index + 1) * 1024, f"file-{index}.bin")
            for index in range(300)
        ]
        self.assertTrue(
            self.strategy._should_use_fast_tar_push(
                source,
                entry_size=300 * 1024,
                entry_count=300,
                file_markers=markers,
                tar_command="tar",
                is_pull=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
