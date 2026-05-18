from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class PlatformToolsInfo:
    folder: Path | None = None
    adb_path: Path | None = None
    fastboot_path: Path | None = None
    adb_version: str = "Unknown"
    fastboot_version: str = "Unknown"
    adb_works: bool = False
    fastboot_works: bool = False
    source: str = ""

    @property
    def has_adb(self) -> bool:
        return self.adb_path is not None and self.adb_path.exists()

    @property
    def has_fastboot(self) -> bool:
        return self.fastboot_path is not None and self.fastboot_path.exists()

    @property
    def is_found(self) -> bool:
        return self.has_adb and self.has_fastboot

    @property
    def is_partial(self) -> bool:
        return (self.has_adb or self.has_fastboot) and not self.is_found

    @property
    def status(self) -> str:
        if self.is_found:
            return "Found"
        if self.is_partial:
            return "Partially found"
        return "Not found"

    @property
    def folder_text(self) -> str:
        return str(self.folder) if self.folder else ""
