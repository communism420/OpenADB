from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
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
DEVICE_PROFILE_ROOTS = {
    "Phone": "Phones",
    "TV": "TVs",
}

LOGGER = logging.getLogger(__name__)


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
        for devices_dir in [self.base_config_dir / "Phones", self.base_config_dir / "TVs", devices_dir]:
            if not devices_dir.exists():
                continue
            try:
                for child in devices_dir.iterdir():
                    if child.is_dir():
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
            self.data = merged
            self._normalize_wireless_mode_settings()

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

    def activate_device_profile(self, serial: str, display_name: str = "", form_factor: str = "") -> bool:
        serial = str(serial or "").strip()
        if not serial:
            return False
        with self._save_lock:
            profile_kind = self._profile_kind_for_device(serial, form_factor)
            target_dir = self.device_profile_dir(serial, profile_kind)
            if (
                serial == self.active_profile_serial
                and profile_kind == self.active_profile_kind
                and self.config_dir == target_dir
            ):
                return False

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
                self._ensure_default_folders()
                self.save()

                # Commit the global pointer only after the candidate profile is
                # complete. A failed commit must not make startup select it.
                global_commit_started = True
                self._write_global_active_device(serial, display_name, profile_kind)
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
                if global_commit_started:
                    self._restore_global_settings(global_snapshot)
                raise
            if recovery_transaction_started:
                self._finish_recovery_transaction(commit=True)
            if migration_source is not None:
                self._retire_migrated_profile(migration_source)
            return True

    def _write_global_active_device(self, serial: str, display_name: str = "", profile_kind: str = "Phone") -> None:
        with self._save_lock, self._disk_lock:
            global_data = self._load_settings_path(self.global_path) or {}
            merged = dict(DEFAULT_SETTINGS)
            merged.update(global_data)
            merged["active_device_serial"] = serial
            merged["last_connected_device_serial"] = serial
            merged["device_profile_kind"] = self._normalize_profile_kind(profile_kind)
            if display_name:
                merged["device_profile_name"] = display_name
            self._write_json_atomic(self.global_path, merged)

    def _snapshot_global_settings(self) -> tuple[bool, bytes, bool, bytes]:
        with self._disk_lock:
            backup_path = self._backup_path(self.global_path)
            primary_exists = self._path_exists(self.global_path)
            backup_exists = self._path_exists(backup_path)
            return (
                primary_exists,
                self._read_bytes_with_retry(self.global_path) if primary_exists else b"",
                backup_exists,
                self._read_bytes_with_retry(backup_path) if backup_exists else b"",
            )

    def _restore_global_settings(self, snapshot: tuple[bool, bytes, bool, bytes]) -> None:
        with self._disk_lock:
            existed, content, backup_existed, backup_content = snapshot
            backup_path = self._backup_path(self.global_path)
            if existed:
                self._write_bytes_atomic(self.global_path, content)
            else:
                self.global_path.unlink(missing_ok=True)
            if backup_existed:
                self._write_bytes_atomic(backup_path, backup_content)
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

    def set_global_values(self, values: dict[str, Any]) -> None:
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
