from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openadb.models.app_info import AppInfo

from .path_utils import package_root


@dataclass(frozen=True, slots=True)
class BloatwareInfo:
    package_name: str
    removal: str = ""
    source_list: str = ""
    description: str = ""
    labels: list[str] = field(default_factory=list)

    @property
    def is_debloat_candidate(self) -> bool:
        return self.removal in {"Recommended", "Advanced", "Expert"}

    @property
    def display_status(self) -> str:
        if self.removal == "Unsafe":
            return "Unsafe"
        if self.is_debloat_candidate:
            return self.removal
        return "Not listed"


class BloatwareDatabase:
    SOURCE_NAME = "Universal Android Debloater Next Generation / Universal Debloat List"
    SOURCE_URL = "https://github.com/Universal-Debloater-Alliance/universal-android-debloater-next-generation/blob/main/resources/assets/uad_lists.json"
    LICENSE = "GPL-3.0"

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or package_root() / "resources" / "uad_lists.json"
        self._entries: dict[str, BloatwareInfo] | None = None

    def lookup(self, package_name: str) -> BloatwareInfo | None:
        return self.entries.get((package_name or "").strip())

    def annotate(self, apps: list[AppInfo]) -> None:
        entries = self.entries
        for app in apps:
            info = entries.get(app.package_name)
            if not info:
                app.bloatware_removal = ""
                app.bloatware_list = ""
                app.bloatware_description = ""
                app.bloatware_labels = []
                continue
            app.bloatware_removal = info.removal
            app.bloatware_list = info.source_list
            app.bloatware_description = info.description
            app.bloatware_labels = list(info.labels)

    @property
    def entries(self) -> dict[str, BloatwareInfo]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def _load(self) -> dict[str, BloatwareInfo]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        entries: dict[str, BloatwareInfo] = {}
        for package_name, item in raw.items():
            if not isinstance(package_name, str) or not isinstance(item, dict):
                continue
            entries[package_name] = self._entry_from_dict(package_name, item)
        return entries

    def _entry_from_dict(self, package_name: str, item: dict[str, Any]) -> BloatwareInfo:
        labels = item.get("labels", [])
        if not isinstance(labels, list):
            labels = []
        return BloatwareInfo(
            package_name=package_name,
            removal=str(item.get("removal", "") or ""),
            source_list=str(item.get("list", "") or ""),
            description=str(item.get("description", "") or ""),
            labels=[str(label) for label in labels if label],
        )
