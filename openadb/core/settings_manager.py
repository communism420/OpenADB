from __future__ import annotations

import json
import os
import shutil
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
    "file_manager_root_transfer": False,
    "active_device_serial": "",
    "last_apps_device_serial": "",
    "last_connected_device_serial": "",
    "command_history": [],
    "device_profile_name": "",
}

PROFILE_FOLDER_KEYS = {"backups_folder", "temp_folder", "logs_folder"}
RUNTIME_DEVICE_KEYS = {"active_device_serial", "last_apps_device_serial", "last_connected_device_serial"}
CACHE_FOLDER_NAMES = {"app-cache", "icon-cache", "temp"}


class SettingsManager:
    def __init__(self) -> None:
        self.root = app_root()
        self.base_config_dir = self._config_dir()
        self.config_dir = self.base_config_dir
        self._migrate_legacy_config_dir()
        ensure_dir(self.config_dir)
        self.global_path = self.base_config_dir / "settings.json"
        self.path = self.global_path
        self.active_profile_serial = ""
        self.data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.load()
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
            if self._remove_file(settings_file):
                removed.append(str(settings_file))
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
        self.data = dict(DEFAULT_SETTINGS)
        self._ensure_default_folders()
        self.save()
        return removed

    def _known_config_dirs(self) -> list[Path]:
        result: list[Path] = []
        for path in [self.base_config_dir, self.config_dir]:
            self._append_unique_path(result, path)
        devices_dir = self.base_config_dir / "devices"
        if devices_dir.exists():
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
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                merged = dict(DEFAULT_SETTINGS)
                merged.update(loaded)
                self.data = merged
        except (OSError, json.JSONDecodeError):
            self.data = dict(DEFAULT_SETTINGS)

    def activate_device_profile(self, serial: str, display_name: str = "") -> bool:
        serial = str(serial or "").strip()
        if not serial or serial == self.active_profile_serial:
            return False

        self.save()
        previous_data = dict(self.data)
        self._write_global_active_device(serial, display_name)
        profile_dir = ensure_dir(self.device_profile_dir(serial))
        self.config_dir = profile_dir
        self.path = profile_dir / "settings.json"
        self.active_profile_serial = serial

        if self.path.exists():
            self.data = dict(DEFAULT_SETTINGS)
            self.load()
        else:
            self.data = self._initial_profile_data(previous_data, serial, display_name)

        self.data["active_device_serial"] = serial
        self.data["last_connected_device_serial"] = serial
        if display_name:
            self.data["device_profile_name"] = display_name
        self._ensure_default_folders()
        self.save()
        return True

    def _write_global_active_device(self, serial: str, display_name: str = "") -> None:
        try:
            if self.global_path.exists():
                loaded = json.loads(self.global_path.read_text(encoding="utf-8"))
                global_data = loaded if isinstance(loaded, dict) else {}
            else:
                global_data = {}
            merged = dict(DEFAULT_SETTINGS)
            merged.update(global_data)
            merged["active_device_serial"] = serial
            merged["last_connected_device_serial"] = serial
            if display_name:
                merged["device_profile_name"] = display_name
            ensure_dir(self.global_path.parent)
            self.global_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    def device_profile_dir(self, serial: str) -> Path:
        key = safe_filename(serial or "unknown-device")
        return self.base_config_dir / "devices" / key

    def _initial_profile_data(self, previous_data: dict[str, Any], serial: str, display_name: str) -> dict[str, Any]:
        data = dict(DEFAULT_SETTINGS)
        for key, value in previous_data.items():
            if key in PROFILE_FOLDER_KEYS or key in RUNTIME_DEVICE_KEYS:
                continue
            data[key] = value
        for key in PROFILE_FOLDER_KEYS:
            data[key] = ""
        data["active_device_serial"] = serial
        data["last_connected_device_serial"] = serial
        data["last_apps_device_serial"] = ""
        data["device_profile_name"] = display_name
        return data

    def save(self) -> None:
        ensure_dir(self.path.parent)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self.data[key] = value
        if save:
            self.save()

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
