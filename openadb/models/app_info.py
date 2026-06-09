from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class AppInfo:
    package_name: str
    app_label: str = ""
    app_type: str = "user"
    state: str = "enabled"
    version_name: str = ""
    version_code: str = ""
    apk_paths: list[str] = field(default_factory=list)
    size: str = "Unknown"
    icon_path: str = ""
    bloatware_removal: str = ""
    bloatware_list: str = ""
    bloatware_description: str = ""
    bloatware_labels: list[str] = field(default_factory=list)
    metadata_checked: bool = False
    assets_checked: bool = False

    @property
    def display_name(self) -> str:
        return self.app_label or self.package_name

    @property
    def apk_path_text(self) -> str:
        return "; ".join(self.apk_paths)

    @property
    def is_system(self) -> bool:
        return self.app_type == "system"

    @property
    def is_disabled(self) -> bool:
        return self.state == "disabled"
