from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class FileItem:
    name: str
    path: str
    is_dir: bool
    size: int | None = None
    modified: str = ""
    item_type: str = ""
    permissions: str = ""

    @property
    def size_text(self) -> str:
        if self.is_dir:
            return ""
        if self.size is None:
            return "Unknown"
        value = float(self.size)
        units = ["B", "KB", "MB", "GB", "TB"]
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return str(self.size)
