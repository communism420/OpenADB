from __future__ import annotations

import json
import html
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from openadb.models.app_info import AppInfo

from .path_utils import ensure_dir
from .quiet_output import quiet_third_party_output
from .settings_manager import SettingsManager


class APKMetadataExtractor:
    def __init__(self, settings: SettingsManager) -> None:
        self.settings = settings
        self.cache_dir = ensure_dir(settings.temp_folder / "apk-metadata")
        self.label_cache_path = self.cache_dir / "app-label-cache.json"
        self._label_cache: dict[str, str] | None = None
        self._lock = threading.RLock()

    def refresh_root(self) -> None:
        with self._lock:
            self.cache_dir = ensure_dir(self.settings.temp_folder / "apk-metadata")
            self.label_cache_path = self.cache_dir / "app-label-cache.json"
            self._label_cache = None

    def cache_key(self, app: AppInfo) -> str:
        apk_path = app.apk_paths[0] if app.apk_paths else ""
        return "|".join([app.package_name, app.version_code, apk_path])

    def cached_label(self, app: AppInfo) -> str:
        with self._lock:
            return self._clean_label(self._labels_unlocked().get(self.cache_key(app), ""))

    def set_cached_label(self, app: AppInfo, label: str) -> None:
        label = self._clean_label(label)
        if not label:
            return
        with self._lock:
            labels = self._labels_unlocked()
            labels[self.cache_key(app)] = label
            try:
                self.label_cache_path.write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass

    def clear_cache(self) -> None:
        with self._lock:
            self._label_cache = {}
            for item in self.cache_dir.glob("*"):
                try:
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
                except OSError:
                    continue
            ensure_dir(self.cache_dir)

    def extract_label(self, apk_path: str | Path) -> str:
        aapt_label = self._extract_label_with_aapt(Path(apk_path))
        if aapt_label:
            return aapt_label

        try:
            import apkutils2
        except ImportError:
            return ""

        try:
            with quiet_third_party_output():
                apk = apkutils2.APK(str(apk_path))
                manifest = apk.get_manifest() or {}
                org_manifest = apk.get_org_manifest() or ""
                label_ref = self._manifest_label_ref(manifest) or self._label_ref_from_xml(org_manifest)
                label = self._resolve_label(apk, label_ref)
            return self._clean_label(label)
        except Exception:
            return ""

    def _labels(self) -> dict[str, str]:
        with self._lock:
            return self._labels_unlocked()

    def _labels_unlocked(self) -> dict[str, str]:
        if self._label_cache is not None:
            return self._label_cache
        if not self.label_cache_path.exists():
            self._label_cache = {}
            return self._label_cache
        try:
            loaded = json.loads(self.label_cache_path.read_text(encoding="utf-8"))
            self._label_cache = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._label_cache = {}
        return self._label_cache

    def _manifest_label_ref(self, manifest: dict[str, Any]) -> str:
        if isinstance(manifest, dict) and isinstance(manifest.get("manifest"), dict):
            manifest = manifest["manifest"]
        application = manifest.get("application", {})
        if isinstance(application, list):
            application = application[0] if application else {}
        label = self._attr(application, "label")
        if label:
            return label

        # Some apps put the user-visible label on the launcher activity.
        activities: list[dict[str, Any]] = []
        for key in ("activity", "activity-alias"):
            value = application.get(key) if isinstance(application, dict) else None
            if isinstance(value, list):
                activities.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                activities.append(value)
        for activity in activities:
            if self._is_launcher_activity(activity):
                label = self._attr(activity, "label")
                if label:
                    return label
        return ""

    def _is_launcher_activity(self, activity: dict[str, Any]) -> bool:
        filters = activity.get("intent-filter")
        if isinstance(filters, dict):
            filters = [filters]
        if not isinstance(filters, list):
            return False
        for intent_filter in filters:
            if not isinstance(intent_filter, dict):
                continue
            actions = intent_filter.get("action")
            categories = intent_filter.get("category")
            action_names = self._names(actions)
            category_names = self._names(categories)
            if "android.intent.action.MAIN" in action_names and "android.intent.category.LAUNCHER" in category_names:
                return True
        return False

    def _names(self, value: Any) -> set[str]:
        if isinstance(value, dict):
            return {self._attr(value, "name")}
        if isinstance(value, list):
            return {self._attr(item, "name") for item in value if isinstance(item, dict)}
        return set()

    def _attr(self, data: Any, name: str) -> str:
        if not isinstance(data, dict):
            return ""
        for key in (f"@android:{name}", f"android:{name}", f"@{name}", name):
            value = data.get(key)
            if value is not None:
                return str(value).strip()
        return ""

    def _resolve_label(self, apk: Any, label_ref: str) -> str:
        label_ref = (label_ref or "").strip()
        if not label_ref:
            return ""
        if not label_ref.startswith("@"):
            return label_ref

        try:
            with quiet_third_party_output():
                arsc = apk.get_arsc()
        except Exception:
            return ""
        if arsc is None:
            return ""

        string_match = re.search(r"(?:@[^:]+:|@)?string/([^/]+)$", label_ref)
        if string_match:
            key = string_match.group(1)
            return self._resolve_string_key(arsc, key)

        resource_id = self._parse_resource_id(label_ref)
        if resource_id is None:
            return ""
        return self._resolve_string_id(arsc, resource_id)

    def _resolve_string_key(self, arsc: Any, key: str) -> str:
        try:
            packages = arsc.get_packages_names()
        except Exception:
            return ""
        locales = ["\x00\x00", "DEFAULT", "ru", "ru-RU", "en", "en-US"]
        try:
            arsc._analyse()
            for package_name in packages:
                for locale in getattr(arsc, "values", {}).get(package_name, {}):
                    if locale not in locales:
                        locales.append(locale)
        except Exception:
            pass
        for package_name in packages:
            for locale in locales:
                try:
                    item = arsc.get_string(package_name, key, locale)
                    if item and len(item) > 1 and item[1]:
                        return str(item[1])
                except Exception:
                    continue
        return ""

    def _resolve_string_id(self, arsc: Any, resource_id: int) -> str:
        try:
            resolved = arsc.get_resolved_strings()
        except Exception:
            return ""
        preferred_locales = ["DEFAULT", "\x00\x00", "en", "ru"]
        for package_values in resolved.values():
            for locale in preferred_locales:
                locale_values = package_values.get(locale)
                if isinstance(locale_values, dict) and locale_values.get(resource_id):
                    return str(locale_values[resource_id])
            for locale_values in package_values.values():
                if isinstance(locale_values, dict) and locale_values.get(resource_id):
                    return str(locale_values[resource_id])
        return ""

    def _parse_resource_id(self, value: str) -> int | None:
        raw = value.strip().lstrip("@")
        if ":" in raw:
            raw = raw.split(":", 1)[1]
        if raw.startswith("+"):
            raw = raw[1:]
        if raw.startswith("0x"):
            raw = raw[2:]
        if not raw:
            return None
        try:
            if raw.isdigit() and len(raw) > 8:
                return int(raw, 10)
            return int(raw, 16)
        except ValueError:
            return None

    def _clean_label(self, label: str) -> str:
        label = html.unescape(label or "").strip()
        if not label:
            return ""
        lowered = label.lower()
        if label.startswith("@"):
            return ""
        if lowered.startswith("0x"):
            return ""
        if any(token in lowered for token in ("<", ">", "type 0x", "0x", "resource id", "xml", "null")):
            return ""
        if any(phrase in lowered for phrase in ("please try again", "can't connect", "couldn't connect", "left and right")):
            return ""
        if len(label) > 72:
            return ""
        return " ".join(label.split())

    def _label_ref_from_xml(self, xml_text: str) -> str:
        if not xml_text:
            return ""
        application_match = re.search(r"<application\b[^>]*>", xml_text, re.IGNORECASE)
        if not application_match:
            return ""
        application_tag = application_match.group(0)
        match = re.search(r'(?:android:)?label="([^"]+)"', application_tag)
        return match.group(1).strip() if match else ""

    def _extract_label_with_aapt(self, apk_path: Path) -> str:
        aapt = self._find_aapt()
        if not aapt or not apk_path.exists():
            return ""
        try:
            completed = subprocess.run(
                [str(aapt), "dump", "badging", str(apk_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if completed.returncode != 0:
            return ""
        return self._label_from_badging(completed.stdout or "")

    def _label_from_badging(self, output: str) -> str:
        default_label = ""
        localized: dict[str, str] = {}
        for line in output.splitlines():
            match = re.match(r"application-label(?:-([A-Za-z0-9_-]+))?:'([^']*)'", line.strip())
            if not match:
                continue
            locale = match.group(1) or "default"
            label = self._clean_label(match.group(2))
            if not label:
                continue
            if locale == "default":
                default_label = label
            else:
                localized[locale.lower()] = label
        for locale in ("ru", "ru-ru", "en", "en-us"):
            if localized.get(locale):
                return localized[locale]
        return default_label or next(iter(localized.values()), "")

    def _find_aapt(self) -> Path | None:
        found = shutil.which("aapt") or shutil.which("aapt.exe")
        if found:
            return Path(found)
        roots: list[Path] = []
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            roots.append(Path(local_app_data) / "Android" / "Sdk" / "build-tools")
        for root in roots:
            if not root.exists():
                continue
            candidates = sorted(root.glob("*/aapt.exe"), reverse=True)
            if candidates:
                return candidates[0]
        return None
