from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class StorageVolume:
    label: str
    path: str
    kind: str = ""
    state: str = ""
    filesystem: str = ""
    total_bytes: int | None = None
    used_bytes: int | None = None
    free_bytes: int | None = None
    used_percent: int | None = None
