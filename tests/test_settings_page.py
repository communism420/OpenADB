from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from openadb.core.settings_manager import DEFAULT_SETTINGS, SettingsManager
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.settings_page import SettingsPage
from openadb.ui.style import apply_theme


class IsolatedSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._test_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._test_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


class SettingsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name)
        self.settings = IsolatedSettings(self.config_dir)
        self.pages: list[SettingsPage] = []

    def tearDown(self) -> None:
        for page in self.pages:
            page.close()
            page.deleteLater()
        self.app.processEvents()
        self.temp_dir.cleanup()

    def _page(self) -> SettingsPage:
        page = SettingsPage(self.settings)
        self.pages.append(page)
        return page

    def test_seven_sections_and_legacy_json_defaults(self) -> None:
        legacy = {"theme": "Dark", "auto_refresh_device": False, "platform_tools_path": "C:/old/tools"}
        self.settings.global_path.write_text(json.dumps(legacy), encoding="utf-8")
        self.settings = IsolatedSettings(self.config_dir)
        page = self._page()

        headings = [
            label.text()
            for label in page.findChildren(QLabel)
            if label.objectName() == "settingsSectionTitle"
        ]
        self.assertEqual(
            headings,
            [
                "Platform Tools",
                "Appearance",
                "Device monitoring",
                "Applications and backups",
                "Root and advanced features",
                "Storage paths",
                "Maintenance",
            ],
        )
        self.assertEqual(page.theme.currentText(), "Dark")
        self.assertFalse(page.auto_refresh.isChecked())
        self.assertFalse(page.refresh_interval.isEnabled())
        self.assertEqual(self.settings.get("refresh_interval_seconds"), DEFAULT_SETTINGS["refresh_interval_seconds"])
        self.assertEqual(self.settings.get("apps_filter_type"), DEFAULT_SETTINGS["apps_filter_type"])

    def test_monitoring_interval_follows_auto_refresh_and_saves(self) -> None:
        self.settings.set("auto_refresh_device", False)
        page = self._page()
        change_count = 0

        def changed() -> None:
            nonlocal change_count
            change_count += 1

        page.settings_changed.connect(changed)
        self.assertFalse(page.refresh_interval.isEnabled())
        page.auto_refresh.setChecked(True)
        page.refresh_interval.setValue(17)
        self.assertTrue(page.refresh_interval.isEnabled())
        self.assertTrue(self.settings.get("auto_refresh_device"))
        self.assertEqual(self.settings.get("refresh_interval_seconds"), 17)
        self.assertGreaterEqual(change_count, 2)
        page.auto_refresh.setChecked(False)
        self.assertFalse(page.refresh_interval.isEnabled())
        self.assertIn("Enable automatic refresh", page.refresh_interval.toolTip())

    def test_platform_tools_actions_are_independent_and_paths_have_tooltips(self) -> None:
        page = self._page()
        counts = {"find": 0, "choose": 0, "verify": 0}
        page.detect_tools_requested.connect(lambda: counts.__setitem__("find", counts["find"] + 1))
        page.choose_tools_requested.connect(lambda: counts.__setitem__("choose", counts["choose"] + 1))
        page.verify_tools_requested.connect(lambda: counts.__setitem__("verify", counts["verify"] + 1))
        self.assertFalse(page.check_button.isEnabled())

        long_folder = self.config_dir / ("long-platform-tools-folder-" * 8)
        long_folder.mkdir()
        adb_path = long_folder / "adb.exe"
        fastboot_path = long_folder / "fastboot.exe"
        adb_path.touch()
        fastboot_path.touch()
        info = PlatformToolsInfo(
            folder=long_folder,
            adb_path=adb_path,
            fastboot_path=fastboot_path,
            adb_version="Android Debug Bridge version test",
            fastboot_version="fastboot version test",
            adb_works=True,
            fastboot_works=True,
            source="Saved settings",
        )
        page.update_tools(info)
        page.detect_button.click()
        page.change_button.click()
        page.check_button.click()

        self.assertEqual(counts, {"find": 1, "choose": 1, "verify": 1})
        self.assertEqual(page.platform_path.toolTip(), str(long_folder))
        self.assertEqual(page.adb_path.toolTip(), str(adb_path))
        self.assertEqual(page.platform_source.text(), "Saved settings")
        self.assertEqual(page.platform_status.text(), "Found")

    def test_reset_ui_preserves_tools_safety_paths_and_profile(self) -> None:
        self.settings.set("platform_tools_path", "C:/Android/platform-tools")
        self.settings.set("root_mode_enabled", True)
        backups = str(self.config_dir / "my-backups")
        self.settings.set("backups_folder", backups)
        self.settings.set("theme", "Dark")
        self.settings.set("navigation_collapsed", True)
        self.settings.activate_device_profile("serial-one", "Test phone", "Phone")
        self.settings.set("apps_filter_type", "system")
        profile_path = self.settings.path

        reset_keys = self.settings.reset_ui_settings()

        self.assertIn("theme", reset_keys)
        self.assertEqual(self.settings.get("theme"), "System")
        self.assertEqual(self.settings.get("apps_filter_type"), "all")
        self.assertFalse(self.settings.get_global("navigation_collapsed"))
        self.assertEqual(self.settings.get("platform_tools_path"), "C:/Android/platform-tools")
        self.assertTrue(self.settings.get("root_mode_enabled"))
        self.assertTrue(profile_path.exists())
        profile_json = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(profile_json["apps_filter_type"], "all")
        self.assertEqual(profile_json["theme"], "System")

    def test_temporary_cleanup_preserves_backups_and_rejects_unowned_folder(self) -> None:
        temporary_file = self.settings.temp_folder / "payload.apk"
        temporary_file.write_text("temporary", encoding="utf-8")
        backup_file = self.settings.backups_folder / "saved.apk"
        backup_file.write_text("backup", encoding="utf-8")

        removed = self.settings.clear_temporary_files()
        self.assertEqual(
            [Path(path).resolve(strict=False) for path in removed or []],
            [temporary_file.resolve(strict=False)],
        )
        self.assertFalse(temporary_file.exists())
        self.assertTrue(backup_file.exists())

        with tempfile.TemporaryDirectory() as external:
            unsafe = Path(external) / "ordinary-folder"
            unsafe.mkdir()
            protected = unsafe / "keep.txt"
            protected.write_text("keep", encoding="utf-8")
            self.settings.set("temp_folder", str(unsafe))
            self.assertIsNone(self.settings.clear_temporary_files())
            self.assertTrue(protected.exists())

    def test_temporary_cleanup_rejects_a_path_changed_after_confirmation(self) -> None:
        confirmed_folder = self.settings.temp_folder
        confirmed_file = confirmed_folder / "confirmed-profile.tmp"
        confirmed_file.write_text("keep", encoding="utf-8")
        replacement_folder = self.config_dir / "OpenADB-temp-replacement"
        replacement_folder.mkdir()
        replacement_file = replacement_folder / "replacement-profile.tmp"
        replacement_file.write_text("keep", encoding="utf-8")
        self.settings.set("temp_folder", str(replacement_folder))

        removed = self.settings.clear_temporary_files(expected_path=confirmed_folder)

        self.assertIsNone(removed)
        self.assertTrue(confirmed_file.exists())
        self.assertTrue(replacement_file.exists())

    def test_temporary_cleanup_reports_unavailable_folder_without_raising(self) -> None:
        expected_path = self.settings.temp_folder
        marker = expected_path / "keep.tmp"
        marker.write_text("keep", encoding="utf-8")

        with patch(
            "openadb.core.settings_manager.ensure_dir",
            side_effect=OSError("temporary drive unavailable"),
        ):
            removed = self.settings.clear_temporary_files(
                expected_path=expected_path
            )

        self.assertIsNone(removed)
        self.assertTrue(marker.exists())

    def test_failed_profile_activation_restores_the_last_usable_in_memory_profile(self) -> None:
        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "Phone"))
        previous_config_dir = self.settings.config_dir
        previous_path = self.settings.path
        previous_data = dict(self.settings.data)

        with (
            patch.object(
                self.settings,
                "_ensure_default_folders",
                side_effect=OSError("profile storage unavailable"),
            ),
            self.assertRaises(OSError),
        ):
            self.settings.activate_device_profile("device-b", "Device B", "Phone")

        self.assertEqual(self.settings.config_dir, previous_config_dir)
        self.assertEqual(self.settings.path, previous_path)
        self.assertEqual(self.settings.active_profile_serial, "device-a")
        self.assertEqual(self.settings.active_profile_kind, "Phone")
        self.assertEqual(self.settings.data, previous_data)

    def test_profile_is_saved_before_global_active_device_commit(self) -> None:
        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "Phone"))
        previous_global = self.settings.global_path.read_bytes()
        candidate_path = self.settings.device_profile_dir("device-b", "Phone") / "settings.json"

        def fail_after_candidate_save(serial: str, _display_name: str, _profile_kind: str) -> None:
            self.assertEqual(serial, "device-b")
            candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
            self.assertEqual(candidate["active_device_serial"], "device-b")
            raise OSError("global settings unavailable")

        with (
            patch.object(
                self.settings,
                "_write_global_active_device",
                side_effect=fail_after_candidate_save,
            ),
            self.assertRaises(OSError),
        ):
            self.settings.activate_device_profile("device-b", "Device B", "Phone")

        self.assertEqual(self.settings.active_profile_serial, "device-a")
        self.assertEqual(self.settings.global_path.read_bytes(), previous_global)

    def test_global_commit_failure_restores_exact_previous_snapshot(self) -> None:
        self.assertTrue(self.settings.activate_device_profile("device-a", "Device A", "Phone"))
        previous_global = self.settings.global_path.read_bytes()
        original_commit = self.settings._write_global_active_device

        def write_then_fail(serial: str, display_name: str, profile_kind: str) -> None:
            original_commit(serial, display_name, profile_kind)
            committed = json.loads(self.settings.global_path.read_text(encoding="utf-8"))
            self.assertEqual(committed["active_device_serial"], "device-b")
            raise OSError("failure after atomic global replace")

        with (
            patch.object(
                self.settings,
                "_write_global_active_device",
                side_effect=write_then_fail,
            ),
            self.assertRaises(OSError),
        ):
            self.settings.activate_device_profile("device-b", "Device B", "Phone")

        self.assertEqual(self.settings.active_profile_serial, "device-a")
        self.assertEqual(self.settings.global_path.read_bytes(), previous_global)

    def test_kind_change_commit_failure_preserves_original_profile_exactly(self) -> None:
        serial = "same-device"
        self.assertTrue(self.settings.activate_device_profile(serial, "Living room TV", "TV"))
        self.settings.set("theme", "Dark")
        original_dir = self.settings.config_dir
        original_path = self.settings.path
        original_content = original_path.read_bytes()
        previous_global = self.settings.global_path.read_bytes()
        candidate_dir = self.settings.device_profile_dir(serial, "Phone")
        original_commit = self.settings._write_global_active_device

        def write_then_fail(new_serial: str, display_name: str, profile_kind: str) -> None:
            original_commit(new_serial, display_name, profile_kind)
            raise OSError("failure after global profile-kind commit")

        with (
            patch.object(
                self.settings,
                "_write_global_active_device",
                side_effect=write_then_fail,
            ),
            self.assertRaises(OSError),
        ):
            self.settings.activate_device_profile(serial, "Living room phone", "Phone")

        self.assertEqual(self.settings.config_dir, original_dir)
        self.assertEqual(self.settings.path, original_path)
        self.assertEqual(self.settings.active_profile_serial, serial)
        self.assertEqual(self.settings.active_profile_kind, "TV")
        self.assertEqual(self.settings.get("theme"), "Dark")
        self.assertTrue(original_path.exists())
        self.assertEqual(original_path.read_bytes(), original_content)
        self.assertFalse(candidate_dir.exists())
        self.assertEqual(self.settings.global_path.read_bytes(), previous_global)

    def test_interrupted_kind_copy_never_exposes_a_partial_candidate(self) -> None:
        serial = "same-device"
        self.assertTrue(self.settings.activate_device_profile(serial, "Living room TV", "TV"))
        original_dir = self.settings.config_dir
        original_path = self.settings.path
        original_content = original_path.read_bytes()
        candidate_dir = self.settings.device_profile_dir(serial, "Phone")
        staging_dirs: list[Path] = []

        def fail_mid_copy(_source: Path, destination: Path, **_kwargs) -> None:
            staging = Path(destination)
            staging_dirs.append(staging)
            (staging / "partial.txt").write_text("incomplete", encoding="utf-8")
            raise OSError("profile copy interrupted")

        with (
            patch(
                "openadb.core.settings_manager.shutil.copytree",
                side_effect=fail_mid_copy,
            ),
            self.assertRaises(OSError),
        ):
            self.settings.activate_device_profile(serial, "Living room phone", "Phone")

        self.assertEqual(self.settings.config_dir, original_dir)
        self.assertTrue(original_path.exists())
        self.assertEqual(original_path.read_bytes(), original_content)
        self.assertFalse(candidate_dir.exists())
        self.assertTrue(staging_dirs)
        self.assertTrue(all(not path.exists() for path in staging_dirs))

    def test_successful_kind_change_retires_source_after_commit(self) -> None:
        serial = "same-device"
        self.assertTrue(self.settings.activate_device_profile(serial, "Living room TV", "TV"))
        self.settings.set("theme", "Dark")
        original_dir = self.settings.config_dir
        backup_marker = self.settings.backups_folder / "preserved.apk"
        backup_marker.write_bytes(b"profile backup")

        self.assertTrue(self.settings.activate_device_profile(serial, "Pocket phone", "Phone"))

        expected_dir = self.settings.device_profile_dir(serial, "Phone")
        self.assertEqual(self.settings.config_dir, expected_dir)
        self.assertEqual(self.settings.active_profile_serial, serial)
        self.assertEqual(self.settings.active_profile_kind, "Phone")
        self.assertEqual(self.settings.get("theme"), "Dark")
        self.assertTrue(self.settings.path.exists())
        for folder in (
            self.settings.backups_folder,
            self.settings.temp_folder,
            self.settings.logs_folder,
        ):
            folder.resolve(strict=False).relative_to(expected_dir.resolve(strict=False))
        self.assertEqual(
            (self.settings.backups_folder / backup_marker.name).read_bytes(),
            b"profile backup",
        )
        self.assertFalse(original_dir.exists())
        global_data = json.loads(self.settings.global_path.read_text(encoding="utf-8"))
        self.assertEqual(global_data["active_device_serial"], serial)
        self.assertEqual(global_data["device_profile_kind"], "Phone")

    def test_full_reset_removes_profiles_and_caches_but_preserves_apk_backups(self) -> None:
        backup_file = self.settings.backups_folder / "preserved.apk"
        backup_file.write_text("backup", encoding="utf-8")
        cache_file = self.config_dir / "icon-cache" / "cached.png"
        cache_file.parent.mkdir()
        cache_file.write_text("cache", encoding="utf-8")
        self.settings.activate_device_profile("reset-device", "Reset phone", "Phone")
        profile_path = self.settings.path
        temp_file = self.settings.temp_folder / "temporary.apk"
        temp_file.write_text("temporary", encoding="utf-8")

        removed = self.settings.reset_settings_and_caches()

        self.assertTrue(removed)
        self.assertFalse(profile_path.exists())
        self.assertFalse(cache_file.exists())
        self.assertFalse(temp_file.exists())
        self.assertTrue(backup_file.exists())
        self.assertEqual(self.settings.get("theme"), DEFAULT_SETTINGS["theme"])
        self.assertEqual(self.settings.active_profile_serial, "")

    def test_all_themes_render_at_narrow_width(self) -> None:
        page = self._page()
        page.resize(630, 520)
        page.show()
        for theme in ("System", "Light", "Dark"):
            apply_theme(self.app, theme)
            self.app.processEvents()
            self.assertFalse(page.grab().isNull())
            self.assertEqual(page.horizontalScrollBar().maximum(), 0)


if __name__ == "__main__":
    unittest.main()
