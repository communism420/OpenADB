from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from openadb.models.app_info import AppInfo

from .path_utils import ensure_dir, safe_filename
from .settings_manager import SettingsManager


class AppInfoCache:
    VERSION = 2

    def __init__(self, settings: SettingsManager) -> None:
        self.settings = settings
        self.cache_dir = ensure_dir(settings.config_dir / "app-cache")
        self._lock = threading.RLock()

    def refresh_root(self) -> None:
        with self._lock:
            self.cache_dir = ensure_dir(self.settings.config_dir / "app-cache")

    def load(self, device_serial: str, include_system: bool = True) -> tuple[list[AppInfo], str]:
        path = self._cache_path(device_serial, include_system)
        apps, saved_at = self._load_path(path)
        if apps:
            return apps, saved_at
        if not include_system:
            apps, saved_at = self._load_path(self._cache_path(device_serial, True))
            if apps:
                return [app for app in apps if not app.is_system], saved_at
        elif include_system:
            apps, saved_at = self._load_path(self._cache_path(device_serial, False))
            if apps:
                return apps, saved_at
        return [], ""

    def _load_path(self, path: Path) -> tuple[list[AppInfo], str]:
        if not path.exists():
            return [], ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return [], ""
        if not isinstance(data, dict):
            return [], ""
        apps_data = data.get("apps", [])
        if not isinstance(apps_data, list):
            return [], ""
        apps: list[AppInfo] = []
        for item in apps_data:
            app = self._app_from_dict(item)
            if app:
                apps.append(app)
        apps.sort(key=lambda app: app.display_name.lower())
        return apps, str(data.get("saved_at", "") or "")

    def save(self, device_serial: str, include_system: bool, apps: list[AppInfo]) -> None:
        if not device_serial or not apps:
            return
        path = self._cache_path(device_serial, include_system)
        ensure_dir(path.parent)
        payload = {
            "version": self.VERSION,
            "device_serial": device_serial,
            "include_system": bool(include_system),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "apps": [self._app_to_dict(app) for app in apps if app.package_name],
        }
        temp = path.with_name(f"{path.stem}.{threading.get_ident()}.tmp")
        with self._lock:
            try:
                temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
                temp.replace(path)
            except OSError:
                try:
                    temp.unlink()
                except OSError:
                    pass

    def clear_cache(self) -> None:
        with self._lock:
            for item in self.cache_dir.glob("*"):
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                except OSError:
                    continue
            ensure_dir(self.cache_dir)

    def merge(self, fresh_apps: list[AppInfo], cached_apps: list[AppInfo]) -> list[AppInfo]:
        cached_by_package = {app.package_name: app for app in cached_apps}
        merged: list[AppInfo] = []
        for app in fresh_apps:
            cached = cached_by_package.get(app.package_name)
            if not cached:
                merged.append(app)
                continue
            same_version = self._same_app_version(app, cached)
            merged.append(
                AppInfo(
                    package_name=app.package_name,
                    app_label=app.app_label or (cached.app_label if same_version else ""),
                    app_type=app.app_type or cached.app_type,
                    state=app.state or cached.state,
                    version_name=app.version_name or (cached.version_name if same_version else ""),
                    version_code=app.version_code or cached.version_code,
                    apk_paths=app.apk_paths or cached.apk_paths,
                    size=app.size if app.size and app.size != "Unknown" else cached.size,
                    icon_path=app.icon_path or (self._valid_icon_path(cached.icon_path) if same_version else ""),
                    bloatware_removal=app.bloatware_removal or cached.bloatware_removal,
                    bloatware_list=app.bloatware_list or cached.bloatware_list,
                    bloatware_description=app.bloatware_description or cached.bloatware_description,
                    bloatware_labels=app.bloatware_labels or cached.bloatware_labels,
                    metadata_checked=bool(cached.metadata_checked and same_version),
                    assets_checked=bool(cached.assets_checked and same_version),
                )
            )
        return merged

    def _cache_path(self, device_serial: str, include_system: bool) -> Path:
        serial = safe_filename(device_serial or "unknown-device")
        suffix = "all" if include_system else "user"
        return self.cache_dir / f"{serial}_{suffix}.json"

    def _app_to_dict(self, app: AppInfo) -> dict[str, Any]:
        return {
            "package_name": app.package_name,
            "app_label": app.app_label,
            "app_type": app.app_type,
            "state": app.state,
            "version_name": app.version_name,
            "version_code": app.version_code,
            "apk_paths": list(app.apk_paths),
            "size": app.size,
            "icon_path": self._valid_icon_path(app.icon_path),
            "bloatware_removal": app.bloatware_removal,
            "bloatware_list": app.bloatware_list,
            "bloatware_description": app.bloatware_description,
            "bloatware_labels": list(app.bloatware_labels),
            "metadata_checked": bool(app.metadata_checked),
            "assets_checked": bool(app.assets_checked),
        }

    def _app_from_dict(self, item: Any) -> AppInfo | None:
        if not isinstance(item, dict):
            return None
        package_name = str(item.get("package_name", "") or "").strip()
        if not package_name:
            return None
        apk_paths = item.get("apk_paths", [])
        if not isinstance(apk_paths, list):
            apk_paths = []
        app_label = str(item.get("app_label", "") or "")
        version_name = str(item.get("version_name", "") or "")
        version_code = str(item.get("version_code", "") or "")
        icon_path = self._valid_icon_path(str(item.get("icon_path", "") or ""))
        labels = item.get("bloatware_labels", [])
        if not isinstance(labels, list):
            labels = []
        metadata_checked = item.get("metadata_checked")
        assets_checked = item.get("assets_checked")
        return AppInfo(
            package_name=package_name,
            app_label=app_label,
            app_type=str(item.get("app_type", "user") or "user"),
            state=str(item.get("state", "enabled") or "enabled"),
            version_name=version_name,
            version_code=version_code,
            apk_paths=[str(path) for path in apk_paths if path],
            size=str(item.get("size", "Unknown") or "Unknown"),
            icon_path=icon_path,
            bloatware_removal=str(item.get("bloatware_removal", "") or ""),
            bloatware_list=str(item.get("bloatware_list", "") or ""),
            bloatware_description=str(item.get("bloatware_description", "") or ""),
            bloatware_labels=[str(label) for label in labels if label],
            metadata_checked=bool(metadata_checked) if metadata_checked is not None else bool(version_name),
            assets_checked=bool(assets_checked) if assets_checked is not None else bool(app_label or icon_path),
        )

    def _valid_icon_path(self, value: str) -> str:
        if not value:
            return ""
        try:
            path = Path(value)
            return str(path) if path.is_file() and path.stat().st_size > 0 else ""
        except OSError:
            return ""

    def _same_app_version(self, fresh: AppInfo, cached: AppInfo) -> bool:
        fresh_code = (fresh.version_code or "").strip()
        cached_code = (cached.version_code or "").strip()
        if fresh_code and cached_code and fresh_code != cached_code:
            return False
        fresh_name = (fresh.version_name or "").strip()
        cached_name = (cached.version_name or "").strip()
        if fresh_name and cached_name and fresh_name != cached_name:
            return False
        return True
