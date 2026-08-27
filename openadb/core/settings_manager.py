from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .path_utils import app_root, ensure_dir, safe_filename

DEFAULT_SETTINGS: dict[str, Any] = {
    "platform_tools_path": "",
    "backups_folder": "",
    "temp_folder": "",
    "logs_folder": "",
    "theme": "System",
    "auto_refresh_device": True,
    "refresh_interval_seconds": 8,
    "show_system_apps": True,
    "show_warnings": True,
    "require_backup_before_uninstall": True,
    "root_mode_enabled": False,
    "privilege_backend": "standard",
    # A non-empty value is a one-shot offline selection for the next profile
    # that is successfully activated.
    "pending_privilege_backend": "",
    "apps_metadata_parallelism": 6,
    "apps_filter_type": "all",
    "apps_filter_state": "any",
    "apps_filter_uad": "any",
    "apps_filter_search": "",
    "apps_sort_mode": "name",
    "file_manager_root_transfer": False,
    "file_manager_transfer_transport": "adb",
    "file_manager_p2p_parallelism": "auto",
    "file_manager_p2p_security_acknowledged": False,
    "file_manager_android_path": "/sdcard/",
    "file_manager_windows_path": "",
    "file_manager_splitter_sizes": [420, 176, 420],
    "dashboard_details_expanded": False,
    "dashboard_wireless_expanded": False,
    "window_x": None,
    "window_y": None,
    "window_width": 1280,
    "window_height": 820,
    "window_maximized": False,
    "navigation_collapsed": False,
    "wireless_dashboard_scenario": "",
    "wireless_connection_mode": "modern",
    "wireless_adb_mode": "modern",
    "wireless_adb_host": "",
    "wireless_adb_port": 5555,
    "wireless_adb_pair_port": "",
    "wireless_modern_host": "",
    "wireless_modern_port": 5555,
    "wireless_modern_pair_port": "",
    "wireless_legacy_host": "",
    "wireless_tv_host": "",
    "wireless_tv_port": 5555,
    "wireless_tv_pair_port": "",
    "active_device_serial": "",
    "last_apps_device_serial": "",
    "last_connected_device_serial": "",
    "command_history": [],
    "commands_view_mode": "Basic",
    "device_profile_name": "",
    "device_profile_kind": "Phone",
}

PROFILE_FOLDER_KEYS = {"backups_folder", "temp_folder", "logs_folder"}
RUNTIME_DEVICE_KEYS = {"active_device_serial", "last_apps_device_serial", "last_connected_device_serial"}
PROFILE_LOCAL_UI_KEYS = {
    "apps_filter_type",
    "apps_filter_state",
    "apps_filter_uad",
    "apps_filter_search",
    "apps_sort_mode",
    "file_manager_android_path",
    "file_manager_root_transfer",
    "file_manager_transfer_transport",
    "file_manager_p2p_parallelism",
    "file_manager_p2p_security_acknowledged",
    "privilege_backend",
    # Legacy compatibility mirror of privilege_backend. Keeping it profile-local
    # prevents a root-enabled device from leaking that state into a new profile.
    "root_mode_enabled",
}
UI_RESET_KEYS = {
    "theme",
    "apps_filter_type",
    "apps_filter_state",
    "apps_filter_uad",
    "apps_filter_search",
    "apps_sort_mode",
    "file_manager_root_transfer",
    "file_manager_transfer_transport",
    "file_manager_p2p_parallelism",
    "file_manager_android_path",
    "file_manager_windows_path",
    "file_manager_splitter_sizes",
    "dashboard_details_expanded",
    "dashboard_wireless_expanded",
    "window_x",
    "window_y",
    "window_width",
    "window_height",
    "window_maximized",
    "navigation_collapsed",
    "wireless_dashboard_scenario",
    "wireless_connection_mode",
    "wireless_adb_mode",
    "commands_view_mode",
}
CACHE_FOLDER_NAMES = {"app-cache", "icon-cache", "temp"}
BACKUP_PARTIAL_NAME_PATTERN = re.compile(
    r"^\.partial-\d{4}-\d{2}-\d{2}_(?:\d{2}-\d{2}-\d{2}|\d{6})-.+$"
)
DEVICE_PROFILE_ROOTS = {
    "Phone": "Phones",
    "TV": "TVs",
}


def read_privilege_backend_setting(
    settings: object,
    *,
    profile_available: bool = True,
) -> object:
    """Read the configured mode while retaining compatibility with settings doubles."""

    reader = getattr(settings, "privilege_backend_value", None)
    if callable(reader):
        return reader(profile_available=profile_available)
    if not profile_available:
        pending_reader = getattr(settings, "pending_privilege_backend", None)
        if callable(pending_reader):
            return pending_reader()
        global_reader = getattr(settings, "get_global", None)
        if callable(global_reader):
            return global_reader(
                "pending_privilege_backend",
                "",
            )
        getter = getattr(settings, "get", None)
        if callable(getter):
            return getter("pending_privilege_backend", "")
        return ""
    getter = getattr(settings, "get", None)
    if callable(getter):
        return getter(
            "privilege_backend",
            DEFAULT_SETTINGS["privilege_backend"],
        )
    return DEFAULT_SETTINGS["privilege_backend"]

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApkBackupCleanupResult:
    """Result of deleting only filesystem entries owned by APK backups."""

    backup_roots: tuple[Path, ...]
    removed_snapshots: tuple[Path, ...]
    failures: tuple[str, ...]

    @property
    def success(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class SettingsRecoveryNotice:
    """One-shot, user-facing description of a recovered settings file."""

    settings_path: Path
    preserved_paths: tuple[Path, ...]
    restored_from_backup: bool
    primary_was_missing: bool
    technical_log_path: Path

    @property
    def title(self) -> str:
        return "Settings recovery"

    @property
    def message(self) -> str:
        if self.primary_was_missing and self.restored_from_backup:
            summary = "OpenADB restored a missing settings file from its last-known-good backup."
        elif self.primary_was_missing:
            summary = "OpenADB found an unusable settings backup and loaded safe defaults."
        elif self.restored_from_backup:
            summary = "OpenADB recovered damaged settings from the last-known-good backup."
        else:
            summary = "OpenADB could not recover damaged settings and loaded safe defaults."

        details = [summary]
        if self.preserved_paths:
            label = "The damaged file was preserved at:" if len(self.preserved_paths) == 1 else "The damaged files were preserved at:"
            details.extend(("", label, *(str(path) for path in self.preserved_paths)))
        details.extend(
            (
                "",
                "Device profiles, backups, and logs were not removed.",
                f"Technical details: {self.technical_log_path}",
            )
        )
        return "\n".join(details)


@dataclass(frozen=True)
class _SettingsRecoveryRecord:
    path: Path
    preserved_paths: tuple[Path, ...]
    restored_from_backup: bool
    primary_was_missing: bool
    reason: str


class SettingsManager:
    _disk_lock = threading.RLock()

    def __init__(self) -> None:
        self._save_lock = threading.RLock()
        self._notice_lock = threading.Lock()
        self._recovery_notices: list[SettingsRecoveryNotice] = []
        self._recovery_listeners: list[Callable[[], None]] = []
        self._deferred_recovery_path: Path | None = None
        self._deferred_recovery_records: list[_SettingsRecoveryRecord] = []
        self.root = app_root()
        self.base_config_dir = self._config_dir()
        self.config_dir = self.base_config_dir
        self._migrate_legacy_config_dir()
        ensure_dir(self.config_dir)
        self.global_path = self.base_config_dir / "settings.json"
        self.path = self.global_path
        self.active_profile_serial = ""
        self.active_profile_kind = ""
        self.data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.load()
        self._normalize_wireless_mode_settings()
        self._normalize_privilege_settings()
        self._ensure_default_folders()

    def _config_dir(self) -> Path:
        return Path.home() / "OpenADB"

    def _legacy_config_dirs(self) -> list[Path]:
        candidates = [
            self.root / "OpenADB-data",
            Path.home() / "AppData" / "Roaming" / "OpenADB",
        ]
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "OpenADB")

        result: list[Path] = []
        seen: set[str] = set()
        try:
            base_key = str(self.base_config_dir.expanduser().resolve()).lower()
        except OSError:
            base_key = str(self.base_config_dir.expanduser()).lower()
        for path in candidates:
            try:
                resolved = path.expanduser().resolve()
            except OSError:
                resolved = path.expanduser()
            key = str(resolved).lower()
            if key == base_key or key in seen:
                continue
            seen.add(key)
            result.append(resolved)
        return result

    def _migrate_legacy_config_dir(self) -> None:
        if self.base_config_dir.exists():
            try:
                if any(self.base_config_dir.iterdir()):
                    return
            except OSError:
                return
        for legacy in self._legacy_config_dirs():
            if not legacy.exists() or not legacy.is_dir():
                continue
            try:
                ensure_dir(self.base_config_dir)
                shutil.copytree(legacy, self.base_config_dir, dirs_exist_ok=True)
                break
            except OSError:
                continue

    def _ensure_default_folders(self) -> None:
        defaults = {
            "backups_folder": self.config_dir / "backups",
            "temp_folder": self.config_dir / "temp",
            "logs_folder": self.config_dir / "logs",
        }
        changed = False
        for key, value in defaults.items():
            current = str(self.data.get(key, "") or "").strip()
            if not current or self._is_legacy_profile_folder(current):
                self.data[key] = str(value)
                changed = True
            ensure_dir(Path(self.data[key]))
        if changed:
            self.save()

    def _is_legacy_profile_folder(self, value: str) -> bool:
        try:
            path = Path(value).expanduser().resolve()
        except OSError:
            return False
        for legacy in self._legacy_config_dirs():
            try:
                path.relative_to(legacy)
                return True
            except ValueError:
                continue
        return False

    def reset_settings_and_caches(self) -> list[str]:
        """Reset all OpenADB settings and clear cache/temp folders.

        Backups are intentionally preserved. The Settings UI warning explains
        the reset; this method still avoids deleting anything named backups.
        """
        config_dirs = self._known_config_dirs()
        protected_dirs = self._protected_backup_dirs(config_dirs)
        temp_dirs = self._configured_folder_paths(config_dirs, "temp_folder")
        removed: list[str] = []

        for config_dir in config_dirs:
            settings_file = config_dir / "settings.json"
            for recovery_file in (settings_file, self._backup_path(settings_file)):
                if self._remove_file(recovery_file):
                    removed.append(str(recovery_file))
            for folder_name in CACHE_FOLDER_NAMES:
                cache_path = config_dir / folder_name
                if self._remove_cache_path(cache_path, protected_dirs):
                    removed.append(str(cache_path))

        for temp_dir in temp_dirs:
            if self._remove_cache_path(temp_dir, protected_dirs):
                removed.append(str(temp_dir))

        self.config_dir = self.base_config_dir
        self.path = self.global_path
        self.active_profile_serial = ""
        self.active_profile_kind = ""
        self.data = dict(DEFAULT_SETTINGS)
        self._ensure_default_folders()
        self.save()
        return removed

    def apk_backup_folders(self) -> tuple[Path, ...]:
        """Snapshot every configured APK-backup root without following links.

        Both live and last-known-good settings files are inspected because a
        full reset removes them and would otherwise lose external/profile
        backup locations before the optional cleanup can run.
        """

        with self._save_lock:
            paths, _, _ = self._discover_apk_backup_folders()
            return paths

    def clear_apk_backups(
        self,
        *,
        expected_folders: Iterable[str | Path] | None = None,
    ) -> ApkBackupCleanupResult:
        """Permanently remove recognized OpenADB APK-backup snapshots.

        Arbitrary configured roots are treated as shared folders: their root
        and unrelated siblings are preserved.  Only the two-level layout
        produced by :class:`BackupManager` is removed, and links/reparse
        points are never traversed.
        """

        with self._save_lock:
            current_roots, config_dirs, discovery_failures = (
                self._discover_apk_backup_folders()
            )
            if expected_folders is not None:
                expected_roots: list[Path] = []
                for path in expected_folders:
                    self._append_unique_lexical_path(
                        expected_roots,
                        Path(path).expanduser(),
                    )
                current_keys = {self._lexical_path_key(path) for path in current_roots}
                expected_keys = {self._lexical_path_key(path) for path in expected_roots}
                if current_keys != expected_keys:
                    return ApkBackupCleanupResult(
                        backup_roots=current_roots,
                        removed_snapshots=(),
                        failures=(
                            "APK backup locations changed after confirmation; nothing was deleted.",
                        ),
                    )

            if discovery_failures:
                return ApkBackupCleanupResult(
                    backup_roots=current_roots,
                    removed_snapshots=(),
                    failures=discovery_failures,
                )

            protected_paths = self._backup_cleanup_protected_paths(config_dirs)
            removed: list[Path] = []
            failures = [
                f"{root}: {safety_error}"
                for root in current_roots
                if (
                    safety_error := self._backup_root_safety_error(
                        root,
                        protected_paths=protected_paths,
                    )
                )
            ]
            if failures:
                return ApkBackupCleanupResult(
                    backup_roots=current_roots,
                    removed_snapshots=(),
                    failures=tuple(failures),
                )
            content_failures = [
                failure
                for root in current_roots
                if root.exists()
                for failure in self._backup_root_content_safety_failures(root)
            ]
            if content_failures:
                return ApkBackupCleanupResult(
                    backup_roots=current_roots,
                    removed_snapshots=(),
                    failures=tuple(content_failures),
                )
            for root in current_roots:
                if not root.exists():
                    continue
                root_removed, root_failures = self._remove_backup_snapshots(root)
                removed.extend(root_removed)
                failures.extend(root_failures)
            return ApkBackupCleanupResult(
                backup_roots=current_roots,
                removed_snapshots=tuple(removed),
                failures=tuple(failures),
            )

    def _discover_apk_backup_folders(
        self,
    ) -> tuple[tuple[Path, ...], list[Path], tuple[str, ...]]:
        """Discover roots lexically and report unsafe/unreadable profile trees."""

        config_dirs: list[Path] = []
        paths: list[Path] = []
        failures: list[str] = []
        for path in (self.base_config_dir, self.config_dir):
            lexical = self._lexical_absolute_path(path)
            self._append_unique_lexical_path(config_dirs, lexical)
            if self._is_link_or_reparse_point(lexical):
                failures.append(
                    f"{lexical}: an OpenADB configuration root is a symlink, junction, or reparse point"
                )

        for profile_root in (
            self.base_config_dir / "Phones",
            self.base_config_dir / "TVs",
            self.base_config_dir / "devices",
        ):
            lexical_root = self._lexical_absolute_path(profile_root)
            if self._is_link_or_reparse_point(lexical_root):
                self._append_unique_lexical_path(paths, lexical_root / "backups")
                failures.append(
                    f"{lexical_root}: a profile container is a symlink, junction, or reparse point"
                )
                continue
            if not lexical_root.exists():
                continue
            if not lexical_root.is_dir():
                failures.append(f"{lexical_root}: a profile container is not a directory")
                continue
            try:
                profile_entries = list(lexical_root.iterdir())
            except OSError as exc:
                failures.append(
                    f"{lexical_root}: could not inspect device profiles: {exc}"
                )
                continue
            for profile_dir in profile_entries:
                if self._is_link_or_reparse_point(profile_dir):
                    self._append_unique_lexical_path(paths, profile_dir / "backups")
                    failures.append(
                        f"{profile_dir}: a device profile is a symlink, junction, or reparse point"
                    )
                    continue
                if profile_dir.is_dir():
                    self._append_unique_lexical_path(config_dirs, profile_dir)

        for config_dir in config_dirs:
            self._append_unique_lexical_path(paths, config_dir / "backups")
            settings_file = config_dir / "settings.json"
            for candidate in (settings_file, self._backup_path(settings_file)):
                try:
                    loaded = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(loaded, dict):
                    continue
                value = str(loaded.get("backups_folder", "") or "").strip()
                if value:
                    self._append_unique_lexical_path(
                        paths,
                        Path(value).expanduser(),
                    )
        current = str(self.data.get("backups_folder", "") or "").strip()
        if current:
            self._append_unique_lexical_path(paths, Path(current).expanduser())
        return tuple(paths), config_dirs, tuple(failures)

    def reset_ui_settings(self) -> list[str]:
        """Reset presentation state without removing profiles, caches, or user files."""
        defaults = {
            key: self._copy_default_value(DEFAULT_SETTINGS[key])
            for key in UI_RESET_KEYS
        }
        self.set_global_values(defaults)
        # set_global_values writes the global file while a profile is active;
        # persist the same UI defaults in the active profile as well.
        if self.path != self.global_path:
            self.save()
        self._normalize_wireless_mode_settings()
        return sorted(defaults)

    @staticmethod
    def _copy_default_value(value: Any) -> Any:
        if isinstance(value, list):
            return list(value)
        if isinstance(value, dict):
            return dict(value)
        return value

    def clear_temporary_files(
        self,
        expected_path: str | Path | None = None,
    ) -> list[str] | None:
        """Clear the active temporary folder when it is recognisably OpenADB-owned.

        When ``expected_path`` is provided, cleanup is rejected if the active
        profile changed its temporary folder after the caller obtained consent.
        ``None`` means the configured path failed the safety check; an empty
        list means that a safe folder was already empty.
        """
        configured_path = Path(str(self.get("temp_folder", ""))).expanduser()
        temp_path = (
            Path(expected_path).expanduser()
            if expected_path is not None
            else configured_path
        )
        try:
            resolved = temp_path.resolve()
            configured_resolved = configured_path.resolve()
        except OSError:
            return None
        if expected_path is not None and resolved != configured_resolved:
            return None
        protected = self._protected_backup_dirs(self._known_config_dirs())
        if self._is_protected_path(resolved, protected) or not self._is_safe_cache_path(resolved):
            return None
        try:
            ensure_dir(resolved)
        except OSError:
            return None
        removed: list[str] = []
        try:
            children = list(resolved.iterdir())
        except OSError:
            return None
        for child in children:
            if self._remove_cache_path(child, protected):
                removed.append(str(child))
        return removed

    def _known_config_dirs(self) -> list[Path]:
        result: list[Path] = []
        for path in [self.base_config_dir, self.config_dir]:
            self._append_unique_path(result, path)
        devices_dir = self.base_config_dir / "devices"
        for profile_root in [
            self.base_config_dir / "Phones",
            self.base_config_dir / "TVs",
            devices_dir,
        ]:
            if (
                not profile_root.exists()
                or self._is_link_or_reparse_point(profile_root)
            ):
                continue
            try:
                for child in profile_root.iterdir():
                    if (
                        not self._is_link_or_reparse_point(child)
                        and child.is_dir()
                    ):
                        self._append_unique_path(result, child)
            except OSError:
                pass
        return result

    def _configured_folder_paths(self, config_dirs: list[Path], key: str) -> list[Path]:
        paths: list[Path] = []
        for config_dir in config_dirs:
            settings_file = config_dir / "settings.json"
            if not settings_file.exists():
                continue
            try:
                loaded = json.loads(settings_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(loaded, dict):
                continue
            value = str(loaded.get(key, "") or "").strip()
            if value:
                self._append_unique_path(paths, Path(value).expanduser())
        return paths

    def _protected_backup_dirs(self, config_dirs: list[Path]) -> list[Path]:
        protected: list[Path] = []
        for config_dir in config_dirs:
            self._append_unique_path(protected, config_dir / "backups")
        for backups_dir in self._configured_folder_paths(config_dirs, "backups_folder"):
            self._append_unique_path(protected, backups_dir)
        return protected

    def _backup_cleanup_protected_paths(
        self,
        config_dirs: list[Path],
    ) -> tuple[Path, ...]:
        protected: list[Path] = [Path.home(), self.base_config_dir, *config_dirs]
        for key in ("logs_folder", "temp_folder"):
            protected.extend(self._configured_folder_paths(config_dirs, key))
            current = str(self.data.get(key, "") or "").strip()
            if current:
                self._append_unique_path(protected, Path(current).expanduser())
        deduplicated: list[Path] = []
        for path in protected:
            self._append_unique_path(deduplicated, path)
        return tuple(deduplicated)

    def _backup_root_safety_error(
        self,
        root: Path,
        *,
        protected_paths: tuple[Path, ...],
    ) -> str:
        lexical = self._lexical_absolute_path(root)
        anchor = Path(lexical.anchor) if lexical.anchor else None
        if anchor is not None and self._lexical_path_key(lexical) == self._lexical_path_key(anchor):
            return "a drive/filesystem root cannot be used for destructive cleanup"
        if self._is_link_or_reparse_point(lexical):
            return "the configured backup root is a symlink, junction, or reparse point"
        try:
            resolved = lexical.resolve(strict=False)
        except (OSError, RuntimeError):
            return "the configured backup root could not be resolved safely"
        if self._lexical_path_key(lexical) != self._lexical_path_key(resolved):
            return "the configured backup path crosses a symlink, junction, or reparse point"
        if lexical.exists() and not lexical.is_dir():
            return "the configured backup root is not a directory"
        for protected in protected_paths:
            try:
                protected_resolved = protected.expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                continue
            if self._same_path(resolved, protected_resolved):
                return "the configured backup root overlaps protected OpenADB or user data"
            try:
                protected_resolved.relative_to(resolved)
            except ValueError:
                continue
            return "the configured backup root is an ancestor of protected OpenADB or user data"
        return ""

    def _remove_backup_snapshots(
        self,
        root: Path,
    ) -> tuple[list[Path], list[str]]:
        removed: list[Path] = []
        failures: list[str] = []
        try:
            package_entries = list(root.iterdir())
        except OSError as exc:
            return removed, [f"{root}: could not list backup folder: {exc}"]
        for package_dir in package_entries:
            if self._is_link_or_reparse_point(package_dir):
                failures.append(
                    f"{package_dir}: linked/reparse backup package folder was preserved"
                )
                continue
            if not package_dir.is_dir():
                continue
            try:
                candidates = list(package_dir.iterdir())
            except OSError as exc:
                failures.append(f"{package_dir}: could not list backup package folder: {exc}")
                continue
            owned_candidates = [
                candidate
                for candidate in candidates
                if self._looks_like_openadb_backup_snapshot(candidate)
            ]
            if not owned_candidates:
                continue
            for snapshot in owned_candidates:
                try:
                    self._remove_backup_tree(snapshot, root=root)
                    removed.append(snapshot)
                except OSError as exc:
                    failures.append(f"{snapshot}: {exc}")
            try:
                if not any(package_dir.iterdir()):
                    package_dir.rmdir()
            except OSError as exc:
                failures.append(f"{package_dir}: could not remove empty package folder: {exc}")
        return removed, failures

    def _looks_like_openadb_backup_snapshot(self, path: Path) -> bool:
        name = path.name
        if self._is_link_or_reparse_point(path):
            return False
        if not path.is_dir():
            return False
        if BACKUP_PARTIAL_NAME_PATTERN.fullmatch(name):
            return True
        metadata_path = path / "metadata.json"
        if self._is_link_or_reparse_point(metadata_path):
            return False
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(metadata, dict):
            return False
        package_name = str(metadata.get("package_name", "") or "").strip()
        if not package_name or safe_filename(package_name).casefold() != path.parent.name.casefold():
            return False
        apk_files = metadata.get("apk_files")
        if not isinstance(apk_files, list):
            return False
        schema_markers = {
            "app_label",
            "backup_date",
            "backup_status",
            "device_model",
            "device_serial",
            "uninstall_method",
            "apk_filename",
            "apk_path_on_device",
        }
        return len(schema_markers.intersection(metadata)) >= 2

    def _backup_root_content_safety_failures(self, root: Path) -> list[str]:
        """Reject top-level links before any root is modified."""

        failures: list[str] = []
        try:
            package_entries = list(root.iterdir())
        except OSError as exc:
            return [f"{root}: could not inspect backup folder safely: {exc}"]
        for package_dir in package_entries:
            if self._is_link_or_reparse_point(package_dir):
                failures.append(
                    f"{package_dir}: linked/reparse backup package folder cannot be cleaned safely"
                )
                continue
            if not package_dir.is_dir():
                continue
            try:
                candidates = list(package_dir.iterdir())
            except OSError as exc:
                failures.append(
                    f"{package_dir}: could not inspect backup package folder safely: {exc}"
                )
                continue
            for candidate in candidates:
                if self._is_link_or_reparse_point(candidate):
                    failures.append(
                        f"{candidate}: linked/reparse backup snapshot cannot be cleaned safely"
                    )
        return failures

    def _remove_backup_tree(self, path: Path, *, root: Path) -> None:
        if not self._lexically_within(path, root):
            raise OSError("refusing to delete a backup path outside its configured root")
        metadata = path.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag):
            if stat.S_ISDIR(metadata.st_mode):
                path.rmdir()
            else:
                path.unlink()
            return
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    self._remove_backup_tree(Path(entry.path), root=root)
            path.rmdir()
            return
        path.unlink()

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        try:
            metadata = path.lstat()
        except OSError:
            return False
        attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)

    @classmethod
    def _lexically_within(cls, path: Path, root: Path) -> bool:
        path_key = cls._lexical_path_key(path)
        root_key = cls._lexical_path_key(root)
        try:
            return os.path.commonpath((path_key, root_key)) == root_key
        except ValueError:
            return False

    @staticmethod
    def _lexical_absolute_path(path: Path) -> Path:
        return Path(os.path.abspath(os.path.normpath(os.fspath(path.expanduser()))))

    @classmethod
    def _lexical_path_key(cls, path: Path) -> str:
        return os.path.normcase(str(cls._lexical_absolute_path(path))).casefold()

    @classmethod
    def _same_path(cls, first: Path, second: Path) -> bool:
        return cls._lexical_path_key(first) == cls._lexical_path_key(second)

    def _append_unique_lexical_path(self, paths: list[Path], path: Path) -> None:
        lexical = self._lexical_absolute_path(path)
        key = self._lexical_path_key(lexical)
        if not any(self._lexical_path_key(existing) == key for existing in paths):
            paths.append(lexical)

    def _append_unique_path(self, paths: list[Path], path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path.expanduser()
        key = str(resolved).lower()
        if not any(str(existing).lower() == key for existing in paths):
            paths.append(resolved)

    def _remove_file(self, path: Path) -> bool:
        try:
            if path.exists() and path.is_file():
                path.unlink()
                return True
        except OSError:
            return False
        return False

    def _remove_cache_path(self, path: Path, protected_dirs: list[Path]) -> bool:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return False
        if self._is_protected_path(resolved, protected_dirs):
            return False
        if resolved.name.lower() == "backups":
            return False
        if not self._is_safe_cache_path(resolved):
            return False
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved)
                return True
            if resolved.exists():
                resolved.unlink()
                return True
        except OSError:
            return False
        return False

    def _is_protected_path(self, path: Path, protected_dirs: list[Path]) -> bool:
        for protected in protected_dirs:
            try:
                if path == protected or protected.relative_to(path):
                    return True
            except ValueError:
                pass
            try:
                path.relative_to(protected)
                return True
            except ValueError:
                continue
        return False

    def _is_safe_cache_path(self, path: Path) -> bool:
        try:
            path.relative_to(self.base_config_dir.expanduser().resolve())
            return True
        except (OSError, ValueError):
            pass
        dangerous_roots = {Path.home().expanduser().resolve()}
        try:
            dangerous_roots.add(path.anchor and Path(path.anchor).resolve())
        except OSError:
            pass
        if any(root and path == root for root in dangerous_roots):
            return False
        external_names = {path.name.lower(), path.parent.name.lower()}
        if any("openadb" in part for part in external_names):
            return True
        safe_names = {
            "acbridge",
            "apk-assets",
            "apk-metadata",
            "app-cache",
            "icon-cache",
            "openadb-cache",
            "openadb-temp",
            "openadb_cache",
            "openadb_temp",
        }
        return path.name.lower() in safe_names

    def load(self) -> None:
        with self._save_lock:
            loaded = self._load_settings_path(self.path)
            if loaded is None:
                return
            merged = dict(DEFAULT_SETTINGS)
            merged.update(loaded)
            if "privilege_backend" not in loaded:
                merged["privilege_backend"] = (
                    "root" if bool(loaded.get("root_mode_enabled", False)) else "standard"
                )
            self.data = merged
            self._normalize_wireless_mode_settings()
            self._normalize_privilege_settings()

    def consume_recovery_notice(self) -> SettingsRecoveryNotice | None:
        """Return each recovery notice once for presentation by the UI."""

        with self._notice_lock:
            if not self._recovery_notices:
                return None
            return self._recovery_notices.pop(0)

    def add_recovery_listener(self, listener: Callable[[], None]) -> None:
        """Notify UI adapters when a new one-shot recovery notice is queued."""

        with self._notice_lock:
            if listener not in self._recovery_listeners:
                self._recovery_listeners.append(listener)

    def remove_recovery_listener(self, listener: Callable[[], None]) -> None:
        with self._notice_lock:
            if listener in self._recovery_listeners:
                self._recovery_listeners.remove(listener)

    @classmethod
    def _backup_path(cls, path: Path) -> Path:
        return path.with_name(f"{path.name}.bak")

    @staticmethod
    def _decode_settings(content: bytes) -> dict[str, Any]:
        loaded = json.loads(content.decode("utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("settings JSON root must be an object")
        return loaded

    def _load_settings_path(self, path: Path) -> dict[str, Any] | None:
        """Load one settings scope and repair it without touching sibling data."""

        with self._disk_lock:
            backup_path = self._backup_path(path)
            if not self._path_exists(path):
                if not self._path_exists(backup_path):
                    return None
                try:
                    recovered = self._decode_settings(
                        self._read_bytes_with_retry(backup_path)
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    preserved = self._preserve_corrupt_file(backup_path, backup=True)
                    defaults = dict(DEFAULT_SETTINGS)
                    self._write_json_atomic(path, defaults)
                    self._record_recovery(
                        path,
                        preserved_paths=(preserved,),
                        restored_from_backup=False,
                        primary_was_missing=True,
                        reason=f"missing primary and unusable backup: {type(exc).__name__}: {exc}",
                    )
                    return defaults
                self._write_json_atomic(path, recovered)
                self._record_recovery(
                    path,
                    preserved_paths=(),
                    restored_from_backup=True,
                    primary_was_missing=True,
                    reason="primary settings file was missing",
                )
                return recovered

            try:
                return self._decode_settings(self._read_bytes_with_retry(path))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as primary_exc:
                preserved_paths = [self._preserve_corrupt_file(path)]
                recovered: dict[str, Any] | None = None
                backup_error = "backup does not exist"
                if self._path_exists(backup_path):
                    try:
                        recovered = self._decode_settings(
                            self._read_bytes_with_retry(backup_path)
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                        backup_error = f"{type(exc).__name__}: {exc}"
                        preserved_paths.append(self._preserve_corrupt_file(backup_path, backup=True))

                restored_from_backup = recovered is not None
                replacement = recovered if recovered is not None else dict(DEFAULT_SETTINGS)
                self._write_json_atomic(path, replacement)
                self._record_recovery(
                    path,
                    preserved_paths=tuple(preserved_paths),
                    restored_from_backup=restored_from_backup,
                    primary_was_missing=False,
                    reason=(
                        f"unusable primary: {type(primary_exc).__name__}: {primary_exc}; "
                        f"backup: {'valid' if restored_from_backup else backup_error}"
                    ),
                )
                return replacement

    @staticmethod
    def _corrupt_path(path: Path, *, backup: bool, timestamp: str, suffix: int = 0) -> Path:
        marker = ".bak" if backup else ""
        collision = f"-{suffix}" if suffix else ""
        return path.parent / f"settings{marker}.corrupt-{timestamp}{collision}.json"

    def _preserve_corrupt_file(self, path: Path, *, backup: bool = False) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = 0
        while True:
            candidate = self._corrupt_path(path, backup=backup, timestamp=timestamp, suffix=suffix)
            try:
                if os.name == "nt":
                    # Windows rename is an atomic no-replace move: unlike
                    # os.replace it fails when another forensic file already
                    # owns the destination, and it has no copy/unlink gap.
                    os.rename(path, candidate)
                else:
                    # A conservative fallback publishes an exclusive exact
                    # copy but leaves the source for the subsequent atomic
                    # settings replacement. Never unlink on uncertainty.
                    self._copy_file_exclusive(path, candidate)
            except FileExistsError:
                suffix += 1
                continue
            return candidate

    @classmethod
    def _copy_file_exclusive(cls, source: Path, destination: Path) -> None:
        """Publish an exact copy without ever replacing another forensic file.

        Settings are small, so an exclusive copy is preferable to a
        check-then-rename sequence. If the process stops before unlinking the
        source, both copies remain and the next recovery can safely retry. The
        caller deliberately does not unlink the source on this fallback path.
        """

        # On failure keep any partial exclusive destination as well as the
        # untouched source. Deleting it here would introduce another race with
        # a process that replaced that path after our exclusive open.
        with destination.open("xb") as destination_stream:
            with source.open("rb") as source_stream:
                shutil.copyfileobj(source_stream, destination_stream)
            destination_stream.flush()
            cls._best_effort_fsync(destination_stream.fileno())

    @property
    def _recovery_log_path(self) -> Path:
        return self.base_config_dir / "logs" / "openadb.log"

    def _record_recovery(
        self,
        path: Path,
        *,
        preserved_paths: tuple[Path, ...],
        restored_from_backup: bool,
        primary_was_missing: bool,
        reason: str,
    ) -> None:
        record = _SettingsRecoveryRecord(
            path=path,
            preserved_paths=preserved_paths,
            restored_from_backup=restored_from_backup,
            primary_was_missing=primary_was_missing,
            reason=reason,
        )
        if self._deferred_recovery_path == path:
            self._deferred_recovery_records.append(record)
            return
        self._publish_recovery(record)

    def _publish_recovery(self, record: _SettingsRecoveryRecord) -> None:
        log_path = self._recovery_log_path
        notice = SettingsRecoveryNotice(
            settings_path=record.path,
            preserved_paths=record.preserved_paths,
            restored_from_backup=record.restored_from_backup,
            primary_was_missing=record.primary_was_missing,
            technical_log_path=log_path,
        )
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        preserved = ", ".join(str(item) for item in record.preserved_paths) or "none"
        line = (
            f"[{timestamp}] Settings recovery: path={record.path}; "
            f"source={'backup' if record.restored_from_backup else 'safe defaults'}; "
            f"preserved={preserved}; reason={record.reason}\n"
        )
        try:
            ensure_dir(log_path.parent)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(line)
                stream.flush()
                self._best_effort_fsync(stream.fileno())
        except OSError:
            LOGGER.warning(
                "Could not write settings recovery log for %s",
                record.path,
                exc_info=True,
            )
        with self._notice_lock:
            self._recovery_notices.append(notice)
            listeners = tuple(self._recovery_listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:
                LOGGER.warning(
                    "Settings recovery listener failed for %s",
                    record.path,
                    exc_info=True,
                )

    def _begin_recovery_transaction(self, path: Path) -> None:
        """Defer notices whose forensic copies live in a profile candidate."""

        if self._deferred_recovery_path is not None:
            raise RuntimeError("settings recovery transaction is already active")
        self._deferred_recovery_path = path
        self._deferred_recovery_records = []

    def _finish_recovery_transaction(self, *, commit: bool) -> None:
        records = self._deferred_recovery_records if commit else []
        self._deferred_recovery_path = None
        self._deferred_recovery_records = []
        for record in records:
            self._publish_recovery(record)

    def _normalize_wireless_mode_settings(self) -> None:
        mode = str(
            self.data.get("wireless_connection_mode", "")
            or self.data.get("wireless_adb_mode", "")
            or DEFAULT_SETTINGS["wireless_connection_mode"]
        ).strip().lower()
        normalized = "legacy" if mode in {"legacy", "tcpip", "tcp/ip", "old", "ip"} else "modern"
        self.data["wireless_connection_mode"] = normalized
        self.data["wireless_adb_mode"] = normalized

    def _normalize_privilege_settings(self) -> None:
        backend = self._normalize_privilege_backend_value(
            self.data.get("privilege_backend", DEFAULT_SETTINGS["privilege_backend"])
        )
        pending_raw = (
            str(self.data.get("pending_privilege_backend", "") or "").strip()
            if self.path == self.global_path
            else ""
        )
        pending_backend = self._normalize_pending_privilege_backend_value(
            pending_raw
        )
        self.data["pending_privilege_backend"] = pending_backend
        self.data["privilege_backend"] = backend
        # Retain the legacy key for older OpenADB builds and existing root-only
        # workflows. Shizuku is intentionally not treated as root unless its
        # live service reports UID 0 for the captured device operation.
        self.data["root_mode_enabled"] = backend == "root"

    @staticmethod
    def _normalize_privilege_backend_value(value: object) -> str:
        backend = str(
            value or DEFAULT_SETTINGS["privilege_backend"]
        ).strip().casefold()
        aliases = {
            "adb": "standard",
            "none": "standard",
            "disabled": "standard",
            "su": "root",
            "sui": "shizuku",
        }
        backend = aliases.get(backend, backend)
        if backend not in {"standard", "root", "shizuku"}:
            backend = "standard"
        return backend

    @staticmethod
    def _normalize_pending_privilege_backend_value(value: object) -> str:
        raw = str(value or "").strip().casefold()
        if not raw:
            return ""
        aliases = {
            "standard": "standard",
            "adb": "standard",
            "none": "standard",
            "disabled": "standard",
            "root": "root",
            "su": "root",
            "shizuku": "shizuku",
            "sui": "shizuku",
        }
        return aliases.get(raw, "")

    def privilege_backend_value(self, *, profile_available: bool = True) -> str:
        """Return the active-profile mode or the persisted offline selection."""

        if not profile_available:
            return self.pending_privilege_backend()
        raw = self.get("privilege_backend", DEFAULT_SETTINGS["privilege_backend"])
        return self._normalize_privilege_backend_value(raw)

    def select_privilege_backend(
        self,
        value: object,
        *,
        profile_available: bool,
    ) -> str:
        """Persist a profile choice or queue one offline choice for the next profile."""

        backend = self._normalize_privilege_backend_value(value)
        values = {
            "privilege_backend": backend,
            "root_mode_enabled": backend == "root",
        }
        if profile_available:
            with self._save_lock:
                self.data.update(values)
                self.save()
        else:
            self.set_global_values(
                {
                    **values,
                    "pending_privilege_backend": backend,
                },
                update_active=False,
            )
        return backend

    def pending_privilege_backend(self) -> str:
        """Return a valid explicit pending mode, or an empty string when absent."""

        raw = str(self.get_global("pending_privilege_backend", "") or "").strip()
        if not raw:
            return ""
        return self._normalize_pending_privilege_backend_value(raw)

    def activate_device_profile(self, serial: str, display_name: str = "", form_factor: str = "") -> bool:
        serial = str(serial or "").strip()
        if not serial:
            return False
        with self._save_lock:
            pending_backend = self.pending_privilege_backend()
            profile_kind = self._profile_kind_for_device(serial, form_factor)
            target_dir = self.device_profile_dir(serial, profile_kind)
            if (
                serial == self.active_profile_serial
                and profile_kind == self.active_profile_kind
                and self.config_dir == target_dir
            ):
                if not pending_backend:
                    return False
                previous_data = dict(self.data)
                profile_snapshot = self._snapshot_settings_files(self.path)
                global_snapshot = self._snapshot_global_settings()
                try:
                    self.data["privilege_backend"] = pending_backend
                    self.data["root_mode_enabled"] = pending_backend == "root"
                    self.save()
                    self._clear_pending_privilege_backend()
                except Exception:
                    self.data = previous_data
                    self._restore_settings_files(self.path, profile_snapshot)
                    self._restore_global_settings(global_snapshot)
                    raise
                return True

            previous_config_dir = self.config_dir
            previous_path = self.path
            previous_profile_serial = self.active_profile_serial
            previous_profile_kind = self.active_profile_kind
            previous_data = dict(self.data)
            self.save()
            # Repair the global scope before it becomes the rollback snapshot.
            # Otherwise a failed profile commit could restore known-corrupt
            # bytes and trigger the same recovery warning on every retry.
            self._load_settings_path(self.global_path)
            global_snapshot = self._snapshot_global_settings()
            global_commit_started = False
            migration_source: Path | None = None
            candidate_created = False
            recovery_transaction_started = False
            target_snapshot = (
                self._snapshot_settings_files(target_dir / "settings.json")
                if pending_backend and target_dir.exists()
                else None
            )
            try:
                profile_dir, migration_source, candidate_created = self._migrate_device_profile(
                    serial,
                    profile_kind,
                    target_dir,
                )
                self.config_dir = profile_dir
                self.path = profile_dir / "settings.json"
                self.active_profile_serial = serial
                self.active_profile_kind = profile_kind
                if candidate_created:
                    self._begin_recovery_transaction(self.path)
                    recovery_transaction_started = True

                if self.path.exists() or self._backup_path(self.path).exists():
                    self.data = dict(DEFAULT_SETTINGS)
                    self.load()
                else:
                    self.data = self._initial_profile_data(previous_data, serial, display_name, profile_kind)
                    self._normalize_wireless_mode_settings()

                if migration_source is not None:
                    self._rebase_migrated_profile_paths(migration_source, profile_dir)

                self.data["active_device_serial"] = serial
                self.data["last_connected_device_serial"] = serial
                self.data["device_profile_kind"] = profile_kind
                if display_name:
                    self.data["device_profile_name"] = display_name
                if pending_backend:
                    self.data["privilege_backend"] = pending_backend
                    self.data["root_mode_enabled"] = pending_backend == "root"
                self._ensure_default_folders()
                self.save()

                # Commit the global pointer only after the candidate profile is
                # complete. A failed commit must not make startup select it.
                global_commit_started = True
                if pending_backend:
                    self._write_global_active_device(
                        serial,
                        display_name,
                        profile_kind,
                        clear_pending_privilege=True,
                    )
                else:
                    self._write_global_active_device(
                        serial,
                        display_name,
                        profile_kind,
                    )
            except Exception:
                # Keep the last usable in-memory profile active so a transient
                # disk, migration, profile-save, or global-commit failure can be
                # retried on the next device refresh.
                if recovery_transaction_started:
                    self._finish_recovery_transaction(commit=False)
                self.config_dir = previous_config_dir
                self.path = previous_path
                self.active_profile_serial = previous_profile_serial
                self.active_profile_kind = previous_profile_kind
                self.data = previous_data
                if candidate_created:
                    self._discard_profile_candidate(target_dir)
                elif target_snapshot is not None:
                    self._restore_settings_files(
                        target_dir / "settings.json",
                        target_snapshot,
                    )
                if global_commit_started:
                    self._restore_global_settings(global_snapshot)
                raise
            if recovery_transaction_started:
                self._finish_recovery_transaction(commit=True)
            if migration_source is not None:
                self._retire_migrated_profile(migration_source)
            return True

    def _write_global_active_device(
        self,
        serial: str,
        display_name: str = "",
        profile_kind: str = "Phone",
        *,
        clear_pending_privilege: bool = False,
    ) -> None:
        with self._save_lock, self._disk_lock:
            global_data = self._load_settings_path(self.global_path) or {}
            merged = dict(DEFAULT_SETTINGS)
            merged.update(global_data)
            merged["active_device_serial"] = serial
            merged["last_connected_device_serial"] = serial
            merged["device_profile_kind"] = self._normalize_profile_kind(profile_kind)
            if clear_pending_privilege:
                merged["pending_privilege_backend"] = ""
                merged["privilege_backend"] = DEFAULT_SETTINGS["privilege_backend"]
                merged["root_mode_enabled"] = DEFAULT_SETTINGS["root_mode_enabled"]
            if display_name:
                merged["device_profile_name"] = display_name
            self._write_json_atomic(self.global_path, merged)

    def _clear_pending_privilege_backend(self) -> None:
        self.set_global_values(
            {
                "pending_privilege_backend": "",
                "privilege_backend": DEFAULT_SETTINGS["privilege_backend"],
                "root_mode_enabled": DEFAULT_SETTINGS["root_mode_enabled"],
            },
            update_active=False,
        )

    def _snapshot_global_settings(self) -> tuple[bool, bytes, bool, bytes]:
        return self._snapshot_settings_files(self.global_path)

    def _restore_global_settings(self, snapshot: tuple[bool, bytes, bool, bytes]) -> None:
        self._restore_settings_files(self.global_path, snapshot)

    @classmethod
    def _snapshot_settings_files(
        cls,
        path: Path,
    ) -> tuple[bool, bytes, bool, bytes]:
        with cls._disk_lock:
            backup_path = cls._backup_path(path)
            primary_exists = cls._path_exists(path)
            backup_exists = cls._path_exists(backup_path)
            return (
                primary_exists,
                cls._read_bytes_with_retry(path) if primary_exists else b"",
                backup_exists,
                cls._read_bytes_with_retry(backup_path) if backup_exists else b"",
            )

    @classmethod
    def _restore_settings_files(
        cls,
        path: Path,
        snapshot: tuple[bool, bytes, bool, bytes],
    ) -> None:
        with cls._disk_lock:
            existed, content, backup_existed, backup_content = snapshot
            backup_path = cls._backup_path(path)
            if existed:
                cls._write_bytes_atomic(path, content)
            else:
                path.unlink(missing_ok=True)
            if backup_existed:
                cls._write_bytes_atomic(backup_path, backup_content)
            else:
                backup_path.unlink(missing_ok=True)

    def device_profile_dir(self, serial: str, profile_kind: str = "Phone") -> Path:
        key = safe_filename(serial or "unknown-device")
        return self.base_config_dir / DEVICE_PROFILE_ROOTS[self._normalize_profile_kind(profile_kind)] / key

    def _legacy_device_profile_dir(self, serial: str) -> Path:
        key = safe_filename(serial or "unknown-device")
        return self.base_config_dir / "devices" / key

    def _migrate_device_profile(
        self,
        serial: str,
        profile_kind: str,
        target_dir: Path,
    ) -> tuple[Path, Path | None, bool]:
        """Atomically publish a complete candidate without retiring its source."""
        if target_dir.exists():
            return target_dir, None, False
        ensure_dir(target_dir.parent)
        sources = [
            self._legacy_device_profile_dir(serial),
            self.device_profile_dir(serial, self._opposite_profile_kind(profile_kind)),
        ]
        copy_error: OSError | None = None
        for source in sources:
            if not source.exists() or not source.is_dir():
                continue
            try:
                self._publish_profile_candidate(target_dir, source=source)
                return target_dir, source, True
            except OSError as exc:
                copy_error = exc
        if copy_error is not None:
            raise copy_error
        self._publish_profile_candidate(target_dir)
        return target_dir, None, True

    @classmethod
    def _publish_profile_candidate(
        cls,
        target_dir: Path,
        *,
        source: Path | None = None,
    ) -> None:
        """Copy into a sibling work directory, then expose it with one rename."""

        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{target_dir.name}.migration-",
                dir=target_dir.parent,
            )
        )
        try:
            if source is not None:
                shutil.copytree(source, staging_dir, dirs_exist_ok=True)
            staging_dir.rename(target_dir)
        finally:
            cls._discard_profile_candidate(staging_dir)

    def _rebase_migrated_profile_paths(self, source: Path, target: Path) -> None:
        """Keep profile-owned folders inside the copied profile after migration."""

        try:
            source_root = source.expanduser().resolve(strict=False)
            target_root = target.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return
        for key in PROFILE_FOLDER_KEYS:
            configured = str(self.data.get(key, "") or "").strip()
            if not configured:
                continue
            try:
                relative = Path(configured).expanduser().resolve(strict=False).relative_to(source_root)
            except (OSError, RuntimeError, ValueError):
                continue
            self.data[key] = str(target_root / relative)

    @staticmethod
    def _discard_profile_candidate(target_dir: Path) -> None:
        try:
            shutil.rmtree(target_dir)
        except OSError:
            pass

    @staticmethod
    def _retire_migrated_profile(source: Path) -> None:
        try:
            shutil.rmtree(source)
        except OSError:
            # The committed target remains usable; an old duplicate is safer
            # than rolling back after the global pointer has been published.
            pass

    def _profile_kind_from_form_factor(self, form_factor: str) -> str:
        text = str(form_factor or "").strip().lower()
        if "tv" in text or "television" in text:
            return "TV"
        return "Phone"

    def _profile_kind_for_device(self, serial: str, form_factor: str) -> str:
        if str(form_factor or "").strip():
            return self._profile_kind_from_form_factor(form_factor)
        if serial and serial == str(self.data.get("active_device_serial", "") or ""):
            return self._normalize_profile_kind(str(self.data.get("device_profile_kind", "") or "Phone"))
        if serial and serial == str(self.data.get("last_connected_device_serial", "") or ""):
            return self._normalize_profile_kind(str(self.data.get("device_profile_kind", "") or "Phone"))
        return "Phone"

    def _normalize_profile_kind(self, profile_kind: str) -> str:
        text = str(profile_kind or "").strip().lower()
        if text in {"tv", "tvs", "android tv", "television"}:
            return "TV"
        return "Phone"

    def _opposite_profile_kind(self, profile_kind: str) -> str:
        return "Phone" if self._normalize_profile_kind(profile_kind) == "TV" else "TV"

    def _initial_profile_data(
        self,
        previous_data: dict[str, Any],
        serial: str,
        display_name: str,
        profile_kind: str = "Phone",
    ) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        for key, value in previous_data.items():
            if key in PROFILE_FOLDER_KEYS or key in RUNTIME_DEVICE_KEYS or key in PROFILE_LOCAL_UI_KEYS:
                continue
            data[key] = value
        for key in PROFILE_FOLDER_KEYS:
            data[key] = ""
        # This one-shot marker belongs only to the application-wide settings.
        # A device profile must never retain or re-queue an offline choice.
        data["pending_privilege_backend"] = ""
        data["active_device_serial"] = serial
        data["last_connected_device_serial"] = serial
        data["last_apps_device_serial"] = ""
        data["device_profile_name"] = display_name
        data["device_profile_kind"] = self._normalize_profile_kind(profile_kind)
        return data

    def save(self) -> None:
        with self._save_lock, self._disk_lock:
            # Detect and preserve damage that appeared after the last load
            # before publishing the current in-memory snapshot.
            if self.path.exists() or self._backup_path(self.path).exists():
                self._load_settings_path(self.path)
            self._write_json_atomic(self.path, self.data)

    @classmethod
    def _write_json_atomic(cls, path: Path, data: dict[str, Any]) -> None:
        with cls._disk_lock:
            ensure_dir(path.parent)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    json.dump(data, stream, indent=2, ensure_ascii=False)
                    stream.flush()
                    cls._best_effort_fsync(stream.fileno())
                    temporary = Path(stream.name)

                backup_path = cls._backup_path(path)
                backup_content: bytes | None = None
                if cls._path_exists(path):
                    candidate = cls._read_bytes_with_retry(path)
                    try:
                        cls._decode_settings(candidate)
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                        pass
                    else:
                        backup_content = candidate
                if backup_content is None and not cls._valid_settings_file(backup_path):
                    backup_content = cls._read_bytes_with_retry(temporary)
                if backup_content is not None:
                    cls._write_bytes_atomic(backup_path, backup_content)
                cls._replace_with_retry(temporary, path)
            finally:
                try:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @classmethod
    def _valid_settings_file(cls, path: Path) -> bool:
        if not cls._path_exists(path):
            return False
        try:
            cls._decode_settings(cls._read_bytes_with_retry(path))
            return True
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False

    @staticmethod
    def _path_exists(path: Path) -> bool:
        """Check existence without converting access failures into absence."""

        try:
            path.stat()
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _read_bytes_with_retry(path: Path) -> bytes:
        """Retry a short Windows sharing violation without hiding I/O errors."""

        for attempt in range(10):
            try:
                return path.read_bytes()
            except PermissionError:
                if attempt >= 9:
                    raise
                time.sleep(0.01 * (attempt + 1))
        raise RuntimeError("unreachable settings read retry state")

    @classmethod
    def _write_bytes_atomic(cls, path: Path, content: bytes) -> None:
        with cls._disk_lock:
            ensure_dir(path.parent)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "wb",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    stream.write(content)
                    stream.flush()
                    cls._best_effort_fsync(stream.fileno())
                    temporary = Path(stream.name)
                cls._replace_with_retry(temporary, path)
            finally:
                try:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _best_effort_fsync(file_descriptor: int) -> None:
        try:
            os.fsync(file_descriptor)
        except OSError:
            pass

    @staticmethod
    def _replace_with_retry(source: Path, destination: Path) -> None:
        for attempt in range(10):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt >= 9:
                    raise
                time.sleep(0.01 * (attempt + 1))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with self._save_lock:
            self.data[key] = value
            if save:
                self.save()

    def get_global(self, key: str, default: Any = None) -> Any:
        """Read application-wide state even while a device profile is active."""
        if self.path == self.global_path:
            return self.data.get(key, default)
        loaded = self._load_settings_path(self.global_path)
        if loaded is not None:
            return loaded.get(key, DEFAULT_SETTINGS.get(key, default))
        return DEFAULT_SETTINGS.get(key, default)

    def set_global_values(
        self,
        values: dict[str, Any],
        *,
        update_active: bool = True,
    ) -> None:
        """Persist application-wide UI state without changing profile-local settings."""
        with self._save_lock, self._disk_lock:
            if self.path == self.global_path:
                self.data.update(values)
                self.save()
                return
            global_data = self._load_settings_path(self.global_path) or {}
            merged = dict(DEFAULT_SETTINGS)
            merged.update(global_data)
            merged.update(values)
            self._write_json_atomic(self.global_path, merged)
            if update_active:
                for key, value in values.items():
                    self.data[key] = value

    def folder(self, key: str) -> Path:
        path = Path(str(self.get(key, ""))).expanduser()
        ensure_dir(path)
        return path

    @property
    def backups_folder(self) -> Path:
        return self.folder("backups_folder")

    @property
    def temp_folder(self) -> Path:
        return self.folder("temp_folder")

    @property
    def logs_folder(self) -> Path:
        return self.folder("logs_folder")

    def append_command_history(self, command: str) -> None:
        command = command.strip()
        if not command:
            return
        history = [item for item in self.get("command_history", []) if item != command]
        history.insert(0, command)
        self.set("command_history", history[:50])
