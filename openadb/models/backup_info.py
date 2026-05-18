from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class BackupInfo:
    path: Path
    package_name: str = ""
    app_label: str = ""
    backup_date: str = ""
    device_model: str = ""
    device_serial: str = ""
    android_version: str = ""
    apk_files: list[str] = field(default_factory=list)
    restore_method: str = "adb install"
    metadata_exists: bool = False
    uninstall_method: str = ""

    @property
    def display_name(self) -> str:
        return self.app_label or self.package_name or self.path.name

    @property
    def apk_count(self) -> int:
        return len(self.apk_files)
