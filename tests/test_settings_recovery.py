from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from openadb.core.settings_manager import DEFAULT_SETTINGS, SettingsManager


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class SettingsRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.primary = self.root / "settings.json"
        self.backup = self.root / "settings.json.bak"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_corrupt_primary_recovers_good_last_known_backup(self) -> None:
        self.primary.write_text('{"theme":', encoding="utf-8")
        write_json(self.backup, {"theme": "Dark", "legacy_option": "kept"})

        settings = IsolatedSettings(self.root)

        self.assertEqual(settings.get("theme"), "Dark")
        self.assertEqual(settings.get("legacy_option"), "kept")
        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8"))["theme"], "Dark")
        self.assertEqual(json.loads(self.backup.read_text(encoding="utf-8"))["theme"], "Dark")
        notice = settings.consume_recovery_notice()
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertTrue(notice.restored_from_backup)
        self.assertFalse(notice.primary_was_missing)

    def test_both_corrupt_files_are_preserved_and_defaults_are_safe(self) -> None:
        self.primary.write_text("broken primary", encoding="utf-8")
        self.backup.write_text("broken backup", encoding="utf-8")

        settings = IsolatedSettings(self.root)

        self.assertEqual(settings.get("theme"), DEFAULT_SETTINGS["theme"])
        self.assertIsInstance(json.loads(self.primary.read_text(encoding="utf-8")), dict)
        self.assertIsInstance(json.loads(self.backup.read_text(encoding="utf-8")), dict)
        notice = settings.consume_recovery_notice()
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertFalse(notice.restored_from_backup)
        self.assertEqual(len(notice.preserved_paths), 2)
        self.assertEqual(notice.preserved_paths[0].read_text(encoding="utf-8"), "broken primary")
        self.assertEqual(notice.preserved_paths[1].read_text(encoding="utf-8"), "broken backup")

    def test_missing_primary_is_restored_from_backup(self) -> None:
        write_json(self.backup, {"theme": "Light", "show_warnings": False})

        settings = IsolatedSettings(self.root)

        self.assertTrue(self.primary.exists())
        self.assertEqual(settings.get("theme"), "Light")
        self.assertFalse(settings.get("show_warnings"))
        notice = settings.consume_recovery_notice()
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertTrue(notice.primary_was_missing)
        self.assertTrue(notice.restored_from_backup)
        self.assertEqual(notice.preserved_paths, ())

    def test_concurrent_saves_leave_valid_complete_json_and_no_temp_files(self) -> None:
        settings = IsolatedSettings(self.root)
        failures: list[BaseException] = []
        start = threading.Barrier(17)

        def save_value(index: int) -> None:
            try:
                start.wait(timeout=5)
                settings.set(f"concurrent_{index}", index)
            except BaseException as exc:  # captured so the test can report worker failures
                failures.append(exc)

        threads = [threading.Thread(target=save_value, args=(index,)) for index in range(16)]
        for thread in threads:
            thread.start()
        start.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        saved = json.loads(self.primary.read_text(encoding="utf-8"))
        self.assertEqual({saved[f"concurrent_{index}"] for index in range(16)}, set(range(16)))
        self.assertIsInstance(json.loads(self.backup.read_text(encoding="utf-8")), dict)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_recovery_preserves_profiles_backups_and_logs(self) -> None:
        backup_marker = self.root / "backups" / "keep.apk"
        profile_marker = self.root / "Phones" / "existing" / "profile-data.bin"
        log_marker = self.root / "logs" / "existing.log"
        for marker, content in (
            (backup_marker, b"apk backup"),
            (profile_marker, b"profile"),
            (log_marker, b"existing log"),
        ):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_bytes(content)
        self.primary.write_text("not JSON", encoding="utf-8")

        IsolatedSettings(self.root)

        self.assertEqual(backup_marker.read_bytes(), b"apk backup")
        self.assertEqual(profile_marker.read_bytes(), b"profile")
        self.assertEqual(log_marker.read_bytes(), b"existing log")

    def test_recovery_notice_is_consumed_once_even_after_reload(self) -> None:
        self.primary.write_text("not JSON", encoding="utf-8")
        settings = IsolatedSettings(self.root)

        first = settings.consume_recovery_notice()
        second = settings.consume_recovery_notice()
        settings.load()
        third = settings.consume_recovery_notice()

        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.title, "Settings recovery")
        self.assertIn("preserved at", first.message)
        self.assertIn(str(first.preserved_paths[0]), first.message)
        self.assertIn(str(first.technical_log_path), first.message)
        self.assertIsNone(second)
        self.assertIsNone(third)

    def test_runtime_recovery_notifies_registered_listeners_once(self) -> None:
        settings = IsolatedSettings(self.root)
        notifications: list[str] = []

        def listener() -> None:
            notifications.append("recovered")

        settings.add_recovery_listener(listener)
        self.primary.write_text("damaged after startup", encoding="utf-8")
        settings.set("theme", "Dark")

        self.assertEqual(notifications, ["recovered"])
        self.assertIsNotNone(settings.consume_recovery_notice())
        self.assertIsNone(settings.consume_recovery_notice())

        settings.remove_recovery_listener(listener)
        self.primary.write_text("damaged again", encoding="utf-8")
        settings.set("theme", "Light")
        self.assertEqual(notifications, ["recovered"])

    def test_corrupt_preservation_never_overwrites_a_timestamp_collision(self) -> None:
        settings = IsolatedSettings(self.root)
        fixed = datetime(2026, 7, 13, 12, 34, 56, tzinfo=timezone.utc)
        first = self.root / "settings.corrupt-20260713-123456.json"
        first.write_bytes(b"first forensic copy")
        self.primary.write_bytes(b"second damaged payload")

        with patch("openadb.core.settings_manager.datetime") as clock:
            clock.now.return_value = fixed
            preserved = settings._preserve_corrupt_file(self.primary)

        self.assertEqual(first.read_bytes(), b"first forensic copy")
        self.assertEqual(preserved.name, "settings.corrupt-20260713-123456-1.json")
        self.assertEqual(preserved.read_bytes(), b"second damaged payload")

    def test_corrupt_preservation_survives_a_collision_created_during_publish(self) -> None:
        settings = IsolatedSettings(self.root)
        fixed = datetime(2026, 7, 13, 12, 34, 56, tzinfo=timezone.utc)
        first = self.root / "settings.corrupt-20260713-123456.json"
        self.primary.write_bytes(b"new damaged payload")
        original_rename = os.rename
        attempts = 0

        def collide_once(source: Path, destination: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                destination.write_bytes(b"other process forensic copy")
                raise FileExistsError(destination)
            original_rename(source, destination)

        with (
            patch("openadb.core.settings_manager.datetime") as clock,
            patch("openadb.core.settings_manager.os.name", "nt"),
            patch("openadb.core.settings_manager.os.rename", side_effect=collide_once),
        ):
            clock.now.return_value = fixed
            preserved = settings._preserve_corrupt_file(self.primary)

        self.assertEqual(first.read_bytes(), b"other process forensic copy")
        self.assertEqual(preserved.name, "settings.corrupt-20260713-123456-1.json")
        self.assertEqual(preserved.read_bytes(), b"new damaged payload")

    def test_atomic_preservation_never_deletes_a_newly_repaired_primary(self) -> None:
        settings = IsolatedSettings(self.root)
        self.primary.write_bytes(b"old damaged payload")
        repaired = {"theme": "Dark", "writer": "other process"}
        original_rename = os.rename

        def rename_then_repair(source: Path, destination: Path) -> None:
            original_rename(source, destination)
            write_json(self.primary, repaired)

        with (
            patch("openadb.core.settings_manager.os.name", "nt"),
            patch(
                "openadb.core.settings_manager.os.rename",
                side_effect=rename_then_repair,
            ),
        ):
            preserved = settings._preserve_corrupt_file(self.primary)

        self.assertEqual(preserved.read_bytes(), b"old damaged payload")
        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8")), repaired)

    def test_non_windows_copy_failure_keeps_the_original_corrupt_source(self) -> None:
        settings = IsolatedSettings(self.root)
        self.primary.write_bytes(b"forensic source must survive")

        with (
            patch("openadb.core.settings_manager.os.name", "posix"),
            patch(
                "openadb.core.settings_manager.shutil.copyfileobj",
                side_effect=OSError("simulated copy failure"),
            ),
            self.assertRaisesRegex(OSError, "copy failure"),
        ):
            settings._preserve_corrupt_file(self.primary)

        self.assertEqual(self.primary.read_bytes(), b"forensic source must survive")
        self.assertEqual(len(list(self.root.glob("settings.corrupt-*.json"))), 1)

    def test_corrupt_primary_keeps_exact_forensic_copy(self) -> None:
        damaged = b"\xff\xfeprivate malformed settings"
        self.primary.write_bytes(damaged)

        settings = IsolatedSettings(self.root)
        notice = settings.consume_recovery_notice()

        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertEqual(len(notice.preserved_paths), 1)
        self.assertRegex(notice.preserved_paths[0].name, r"^settings\.corrupt-\d{8}-\d{6}\.json$")
        self.assertEqual(notice.preserved_paths[0].read_bytes(), damaged)

    def test_save_after_recovery_rotates_a_valid_backup(self) -> None:
        self.primary.write_text("not JSON", encoding="utf-8")
        write_json(self.backup, {"theme": "Light"})
        settings = IsolatedSettings(self.root)

        settings.set("theme", "Dark")

        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8"))["theme"], "Dark")
        recovered_backup = json.loads(self.backup.read_text(encoding="utf-8"))
        self.assertEqual(recovered_backup["theme"], "Light")

    def test_profile_and_global_settings_recover_independently(self) -> None:
        self.primary.write_text("broken global", encoding="utf-8")
        write_json(self.backup, {"theme": "Light", "global_marker": "global"})
        profile_dir = self.root / "Phones" / "device-a"
        profile_primary = profile_dir / "settings.json"
        profile_backup = profile_dir / "settings.json.bak"
        profile_primary.parent.mkdir(parents=True)
        profile_primary.write_text("broken profile", encoding="utf-8")
        write_json(
            profile_backup,
            {
                "theme": "Dark",
                "apps_filter_type": "system",
                "profile_marker": "profile",
            },
        )

        settings = IsolatedSettings(self.root)
        global_notice = settings.consume_recovery_notice()
        changed = settings.activate_device_profile("device-a", "Test device", "Phone")
        profile_notice = settings.consume_recovery_notice()

        self.assertTrue(changed)
        self.assertEqual(settings.get("theme"), "Dark")
        self.assertEqual(settings.get("apps_filter_type"), "system")
        self.assertEqual(settings.get("profile_marker"), "profile")
        self.assertEqual(settings.get_global("theme"), "Light")
        self.assertEqual(settings.get_global("global_marker"), "global")
        self.assertIsNotNone(global_notice)
        self.assertIsNotNone(profile_notice)
        assert global_notice is not None and profile_notice is not None
        self.assertEqual(global_notice.settings_path, self.primary)
        self.assertEqual(profile_notice.settings_path, profile_primary)
        self.assertTrue(list(self.root.glob("settings.corrupt-*.json")))
        self.assertTrue(list(profile_dir.glob("settings.corrupt-*.json")))

    def test_successful_save_flushes_and_best_effort_fsyncs(self) -> None:
        settings = IsolatedSettings(self.root)

        with patch("openadb.core.settings_manager.os.fsync", wraps=os.fsync) as fsync:
            settings.set("theme", "Dark")

        self.assertGreaterEqual(fsync.call_count, 2)
        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8"))["theme"], "Dark")

    def test_failed_primary_replace_keeps_valid_primary_and_backup(self) -> None:
        settings = IsolatedSettings(self.root)
        settings.set("theme", "Light")
        original_replace = os.replace

        def fail_primary_replace(source: str | os.PathLike, destination: str | os.PathLike) -> None:
            if Path(destination) == self.primary:
                raise OSError("simulated interrupted replace")
            original_replace(source, destination)

        with (
            patch("openadb.core.settings_manager.os.replace", side_effect=fail_primary_replace),
            self.assertRaisesRegex(OSError, "interrupted replace"),
        ):
            settings.set("theme", "Dark")

        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8"))["theme"], "Light")
        self.assertEqual(json.loads(self.backup.read_text(encoding="utf-8"))["theme"], "Light")
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_backup_tracks_the_previous_valid_primary(self) -> None:
        settings = IsolatedSettings(self.root)
        settings.set("theme", "Light")
        settings.set("theme", "Dark")

        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8"))["theme"], "Dark")
        self.assertEqual(json.loads(self.backup.read_text(encoding="utf-8"))["theme"], "Light")

    def test_primary_io_error_never_triggers_corruption_recovery_or_rollback(
        self,
    ) -> None:
        current = {"theme": "Dark", "marker": "current"}
        previous = {"theme": "Light", "marker": "previous"}
        write_json(self.primary, current)
        write_json(self.backup, previous)
        original_read_bytes = Path.read_bytes

        def fail_current_once(path: Path) -> bytes:
            if path == self.primary:
                raise OSError("simulated transient read fault")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", autospec=True, side_effect=fail_current_once):
            with self.assertRaisesRegex(OSError, "transient read fault"):
                IsolatedSettings(self.root)

        self.assertEqual(json.loads(self.primary.read_text(encoding="utf-8")), current)
        self.assertEqual(json.loads(self.backup.read_text(encoding="utf-8")), previous)
        self.assertEqual(list(self.root.glob("settings*.corrupt-*.json")), [])

    def test_backup_io_error_is_not_overwritten_as_an_invalid_backup(self) -> None:
        previous = {"theme": "Light", "marker": "previous"}
        settings = IsolatedSettings(self.root)
        self.primary.unlink()
        write_json(self.backup, previous)
        original_read_bytes = Path.read_bytes

        def fail_backup(path: Path) -> bytes:
            if path == self.backup:
                raise OSError("simulated backup read fault")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", autospec=True, side_effect=fail_backup):
            with self.assertRaisesRegex(OSError, "backup read fault"):
                settings._write_json_atomic(self.primary, {"theme": "System"})

        self.assertFalse(self.primary.exists())
        self.assertEqual(json.loads(self.backup.read_text(encoding="utf-8")), previous)

    def test_failed_profile_migration_discards_candidate_recovery_notice(self) -> None:
        settings = IsolatedSettings(self.root)
        source_dir = settings.device_profile_dir("same-device", "TV")
        source_primary = source_dir / "settings.json"
        source_backup = source_dir / "settings.json.bak"
        source_dir.mkdir(parents=True)
        source_primary.write_text("broken migrated profile", encoding="utf-8")
        write_json(source_backup, {"theme": "Dark"})
        target_dir = settings.device_profile_dir("same-device", "Phone")

        with (
            patch.object(
                settings,
                "_write_global_active_device",
                side_effect=OSError("global commit failed"),
            ),
            self.assertRaisesRegex(OSError, "global commit failed"),
        ):
            settings.activate_device_profile(
                "same-device",
                "Test device",
                "Phone",
            )

        self.assertFalse(target_dir.exists())
        self.assertEqual(
            source_primary.read_text(encoding="utf-8"),
            "broken migrated profile",
        )
        self.assertIsNone(settings.consume_recovery_notice())
        recovery_log = self.root / "logs" / "openadb.log"
        if recovery_log.exists():
            self.assertNotIn(str(target_dir), recovery_log.read_text(encoding="utf-8"))

    def test_successful_profile_migration_publishes_candidate_recovery_notice(
        self,
    ) -> None:
        settings = IsolatedSettings(self.root)
        source_dir = settings.device_profile_dir("same-device", "TV")
        source_primary = source_dir / "settings.json"
        source_backup = source_dir / "settings.json.bak"
        source_dir.mkdir(parents=True)
        source_primary.write_text("broken migrated profile", encoding="utf-8")
        write_json(source_backup, {"theme": "Dark"})

        changed = settings.activate_device_profile(
            "same-device",
            "Test device",
            "Phone",
        )

        notice = settings.consume_recovery_notice()
        self.assertTrue(changed)
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertEqual(notice.settings_path, settings.path)
        self.assertTrue(all(path.exists() for path in notice.preserved_paths))
        self.assertFalse(source_dir.exists())
        self.assertEqual(settings.get("theme"), "Dark")

    def test_global_recovery_precedes_profile_commit_rollback(self) -> None:
        settings = IsolatedSettings(self.root)
        settings.activate_device_profile("device-a", "Device A", "Phone")
        self.primary.write_text("broken global settings", encoding="utf-8")
        original_commit = settings._write_global_active_device

        def commit_then_fail(serial: str, display_name: str, profile_kind: str) -> None:
            original_commit(serial, display_name, profile_kind)
            raise OSError("failure after global commit")

        with (
            patch.object(
                settings,
                "_write_global_active_device",
                side_effect=commit_then_fail,
            ),
            self.assertRaisesRegex(OSError, "failure after global commit"),
        ):
            settings.activate_device_profile("device-b", "Device B", "Phone")

        recovered_global = json.loads(self.primary.read_text(encoding="utf-8"))
        self.assertIsInstance(recovered_global, dict)
        first_notice = settings.consume_recovery_notice()
        self.assertIsNotNone(first_notice)
        assert first_notice is not None
        self.assertEqual(first_notice.settings_path, self.primary)
        self.assertTrue(all(path.exists() for path in first_notice.preserved_paths))

        settings.get_global("theme")

        self.assertIsNone(settings.consume_recovery_notice())
        self.assertEqual(len(list(self.root.glob("settings.corrupt-*.json"))), 1)

    def test_non_object_legacy_json_is_recovered_without_losing_unknown_keys_in_backup(self) -> None:
        write_json(self.primary, ["not", "an", "object"])
        write_json(self.backup, {"theme": "Dark", "future_setting": {"enabled": True}})

        settings = IsolatedSettings(self.root)

        self.assertEqual(settings.get("theme"), "Dark")
        self.assertEqual(settings.get("future_setting"), {"enabled": True})

    def test_recovery_writes_technical_details_to_the_normal_log_folder(self) -> None:
        self.primary.write_text("not JSON", encoding="utf-8")
        settings = IsolatedSettings(self.root)
        notice = settings.consume_recovery_notice()

        self.assertIsNotNone(notice)
        assert notice is not None
        technical_log = notice.technical_log_path.read_text(encoding="utf-8")
        self.assertIn("Settings recovery", technical_log)
        self.assertIn(str(self.primary), technical_log)
        self.assertIn("safe defaults", technical_log)


if __name__ == "__main__":
    unittest.main()
