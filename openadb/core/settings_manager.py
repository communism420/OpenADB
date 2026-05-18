from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .path_utils import app_root, ensure_dir


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
    "active_device_serial": "",
    "command_history": [],
}


class SettingsManager:
    def __init__(self) -> None:
        self.root = app_root()
        self.portable = (self.root / "portable.flag").exists()
        self.config_dir = self._config_dir()
        ensure_dir(self.config_dir)
        self.path = self.config_dir / "settings.json"
        self.data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.load()
        self._ensure_default_folders()

    def _config_dir(self) -> Path:
        if self.portable:
            return self.root / "OpenADB-data"
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "OpenADB"
        return Path.home() / "AppData" / "Roaming" / "OpenADB"

    def _ensure_default_folders(self) -> None:
        defaults = {
            "backups_folder": self.config_dir / "backups",
            "temp_folder": self.config_dir / "temp",
            "logs_folder": self.config_dir / "logs",
        }
        changed = False
        for key, value in defaults.items():
            if not self.data.get(key):
                self.data[key] = str(value)
                changed = True
            ensure_dir(Path(self.data[key]))
        if changed:
            self.save()

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
