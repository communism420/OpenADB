from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Callable, Protocol

from openadb.models.app_info import AppInfo

from .acbridge import ACBridgeClient
from .apk_metadata import APKMetadataExtractor
from .app_metadata_loader import (
    CancellationEvent,
    has_known_size,
    metadata_has_size,
    metadata_is_complete,
    metadata_worker_count,
    size_text_from_metadata,
)
from .icon_extractor import IconExtractor
from .device_context import StaleDeviceContext
from .path_utils import ensure_dir, safe_filename


class AssetADBClient(Protocol):
    def root_available(self, cancel_event=None) -> bool: ...

    def get_package_details_many(
        self,
        package_names: list[str],
        *,
        max_workers: int,
        cancel_event=None,
    ) -> dict[str, dict[str, str]]: ...

    def get_package_sizes_bulk(
        self,
        package_names: list[str],
        *,
        use_root: bool,
        cancel_event=None,
    ) -> dict[str, int]: ...

    def get_package_paths_bulk(
        self,
        package_names: list[str],
        *,
        cancel_event=None,
    ) -> dict[str, list[str]]: ...

    def pull_files_via_temp(
        self,
        pull_plan: list[tuple[str, Path]],
        **kwargs,
    ): ...


class ProfileSettings(Protocol):
    temp_folder: Path


def asset_progress_percent(total: int, labels: int, icons: int) -> int:
    total = max(0, total)
    if total <= 0:
        return 100
    labels = min(max(0, labels), total)
    icons = min(max(0, icons), total)
    return int(round(((labels + icons) / (total * 2)) * 100))


def asset_progress_text(
    total: int,
    labels: int,
    icons: int,
    processed: int,
    phase: str,
    percent_override: int | None = None,
) -> str:
    total = max(0, total)
    if total <= 0:
        return f"App labels/icons: no apps need loading. {phase}"
    labels = min(max(0, labels), total)
    icons = min(max(0, icons), total)
    processed = min(max(0, processed), total)
    percent = (
        asset_progress_percent(total, labels, icons)
        if percent_override is None
        else percent_override
    )
    percent = min(max(0, percent), 100)
    return (
        f"App labels/icons: {percent}% | "
        f"labels {labels}/{total}, icons {icons}/{total}, processed {processed}/{total}. "
        f"{phase}"
    )


class AppLabelFormatter:
    """Normalize package-derived labels without depending on the Apps widget."""

    def is_placeholder(self, label: str, package_name: str) -> bool:
        value = (label or "").strip()
        if not value or value == (package_name or "").strip():
            return True
        return value.lower() in {"unknown", "not extracted", "package", "application"}

    def cached_display_label(
        self,
        app: AppInfo,
        apk_metadata: APKMetadataExtractor,
    ) -> str:
        for label in (app.app_label, apk_metadata.cached_label(app)):
            value = " ".join((label or "").replace("\n", " ").replace("\r", " ").split())
            if self.is_placeholder(value, app.package_name) or self.looks_like_internal(
                value,
                app.package_name,
            ):
                continue
            normalized = self.compact(value, app.package_name)
            if normalized and not self.looks_like_generated(normalized, app.package_name):
                return normalized
        return ""

    def normalize(
        self,
        label: str,
        package_name: str,
        apk_paths: list[str] | None = None,
    ) -> str:
        value = " ".join((label or "").replace("\n", " ").replace("\r", " ").split())
        if self.is_placeholder(value, package_name) or self.looks_like_internal(value, package_name):
            value = self.fallback(package_name, apk_paths)
        if not value:
            value = self.fallback(package_name, apk_paths)
        return self.compact(value, package_name)

    def looks_like_internal(self, label: str, package_name: str) -> bool:
        value = (label or "").strip()
        if not value:
            return True
        if " " in value:
            return self.looks_like_generated(value, package_name)
        lowered = value.lower()
        if lowered.startswith(("com.", "org.", "net.", "android.")) and value.count(".") >= 2:
            return True
        package_prefix = f"{(package_name or '').strip()}."
        if package_prefix != "." and value.startswith(package_prefix):
            return True
        return value.endswith(("Application", ".Application")) and value.count(".") >= 1

    def looks_like_generated(self, label: str, package_name: str) -> bool:
        value = " ".join((label or "").split()).strip().lower()
        package_name = (package_name or "").strip()
        if not value or not package_name:
            return False
        generated = self.label_from_package_tokens(package_name).strip().lower()
        if generated and value == generated:
            return True
        # A spaced, user-facing label may collapse to the same characters as a
        # package tail (for example "Open Camera" and "opencamera"). Treating
        # that as generated discards a perfectly good Android label.
        if " " in value:
            return False
        compact_value = re.sub(r"[^a-z0-9]+", "", value)
        package_compact = re.sub(r"[^a-z0-9]+", "", package_name.lower())
        if package_compact and compact_value == package_compact:
            return True
        package_tail = re.sub(r"[^a-z0-9]+", "", package_name.split(".")[-1].lower())
        return bool(package_tail and compact_value == package_tail)

    def compact(self, label: str, package_name: str) -> str:
        value = " ".join((label or "").split()).strip(" -_")
        if not value:
            return ""
        if value == package_name:
            value = self.label_from_package_tokens(package_name)
        if len(value) <= 64:
            return value
        words = value.split()
        compact: list[str] = []
        for word in words:
            candidate = " ".join(compact + [word])
            if len(candidate) > 64:
                break
            compact.append(word)
        return " ".join(compact) if compact else value[:64].rstrip()

    def fallback(self, package_name: str, apk_paths: list[str] | None = None) -> str:
        package_name = (package_name or "").strip()
        if not package_name:
            return ""
        apk_label = self.label_from_apk_paths(apk_paths or [])
        lowered = package_name.lower()
        if "auto_generated" in lowered or lowered.endswith("_rro") or ".overlay" in lowered:
            base = self.overlay_label_source(package_name, apk_paths or [])
            label = f"{base} overlay" if base else apk_label or "Generated overlay"
            return self.compact(label, package_name)
        if apk_label and len(apk_label) <= 48:
            return self.compact(apk_label, package_name)
        return self.compact(self.label_from_package_tokens(package_name), package_name)

    def label_from_package_tokens(self, package_name: str) -> str:
        tokens = [part for part in re.split(r"[._-]+", package_name) if part]
        ignored_prefixes = {"com", "org", "net", "android", "apps", "app", "io", "dev", "co"}
        while tokens and tokens[0].lower() in ignored_prefixes:
            tokens.pop(0)
        while tokens and tokens[0].lower() in {"google", "android"} and len(tokens) > 1:
            tokens.pop(0)
        while tokens and tokens[0].lower() in {"apps", "app"} and len(tokens) > 1:
            tokens.pop(0)
        while tokens and tokens[0].lower() in {"ai", "x"} and len(tokens) > 1:
            tokens.pop(0)
        if len(tokens) == 2 and self.looks_like_publisher_token(tokens[0], tokens[1]):
            tokens = tokens[1:]
        useful = tokens[-3:] if len(tokens) > 3 else tokens
        label = " ".join(self.label_token(token) for token in useful)
        return " ".join(label.split()) or package_name

    @staticmethod
    def looks_like_publisher_token(publisher: str, product: str) -> bool:
        publisher = (publisher or "").lower()
        product = (product or "").lower()
        if not publisher or not product:
            return False
        if product in {"manager", "service", "provider", "settings", "launcher", "shell", "systemui"}:
            return False
        if publisher in {"google", "android", "microsoft", "samsung", "xiaomi", "huawei", "sony", "meta"}:
            return False
        return bool(
            re.search(
                r"(app|pro|plus|manager|player|viewer|editor|analyzer|vpn|camera|browser|store|tool)$",
                product,
            )
        )

    def label_from_apk_paths(self, apk_paths: list[str]) -> str:
        for path in apk_paths:
            stem = Path(path).stem
            if not stem or stem.lower() in {"base", "split_config"}:
                continue
            stem = re.sub(r"__.*$", "", stem)
            stem = re.sub(r"(?i)(prebuilt|release|signed)$", "", stem)
            stem = re.sub(r"(?i)(google)?overlay$", "", stem)
            stem = stem.strip("._- ")
            if stem:
                label = self.split_identifier(stem)
                if label:
                    return label
        return ""

    def overlay_label_source(self, package_name: str, apk_paths: list[str]) -> str:
        path_text = " ".join(apk_paths)
        candidates: list[str] = []
        for path in apk_paths:
            stem = Path(path).stem
            stem = re.sub(r"__.*$", "", stem)
            stem = re.sub(r"(?i)auto_generated.*$", "", stem)
            stem = re.sub(r"(?i)overlay$", "", stem)
            if stem:
                candidates.append(stem)
        candidates.extend(part for part in re.split(r"[._-]+", package_name) if part)
        ignored = {
            "com",
            "android",
            "google",
            "auto",
            "generated",
            "rro",
            "product",
            "vendor",
            "characteristics",
            "overlay",
            "pixel",
            "husky",
            "nosdcard",
        }
        words: list[str] = []
        for candidate in candidates:
            for token in re.findall(r"[A-Za-z0-9]+", candidate):
                if token.lower() in ignored:
                    continue
                for word in self.split_identifier(self.label_token(token)).split():
                    if word.lower() in {existing.lower() for existing in words}:
                        continue
                    words.append(word)
                if len(words) >= 3:
                    return " ".join(words)
        return "Framework resources" if "framework-res" in path_text else ""

    def label_token(self, token: str) -> str:
        if not token:
            return ""
        known = {
            "aicore": "AI Core",
            "androidauto": "Android Auto",
            "backupconfirm": "Backup Confirm",
            "cellbroadcastreceiver": "Cell Broadcast Receiver",
            "cellbroadcastservice": "Cell Broadcast Service",
            "companiondevicemanager": "Companion Device Manager",
            "ctsshim": "CTS Shim",
            "devicediagnostics": "Device Diagnostics",
            "filemanager": "File Manager",
            "gms": "Google Mobile Services",
            "gsf": "Google Services Framework",
            "imsserviceentitlement": "IMS Service Entitlement",
            "inputdevices": "Input Devices",
            "localtransport": "Local Transport",
            "managedprovisioning": "Managed Provisioning",
            "mmsservice": "MMS Service",
            "partnerbookmarks": "Partner Bookmarks",
            "permissioncontroller": "Permission Controller",
            "pixeldisplayservice": "Pixel Display Service",
            "packageinstaller": "Package Installer",
            "sandbox": "Sandbox",
            "settingsintelligence": "Settings Intelligence",
            "systemui": "System UI",
            "wifianalyzer": "WiFi Analyzer",
            "wifianalyzerpro": "WiFi Analyzer Pro",
        }
        acronyms = {
            "apk": "APK",
            "cts": "CTS",
            "ims": "IMS",
            "ons": "ONS",
            "qns": "QNS",
            "uwb": "UWB",
            "nfc": "NFC",
            "sdk": "SDK",
            "rro": "RRO",
        }
        lowered = token.lower()
        if lowered in known:
            return known[lowered]
        if lowered in acronyms:
            return acronyms[lowered]
        spaced = self.split_identifier(token)
        return spaced[:1].upper() + spaced[1:]

    @staticmethod
    def split_identifier(value: str) -> str:
        value = re.sub(r"[_\-.]+", " ", value or "")
        value = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value)
        value = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", value)
        value = re.sub(r"(?<=\D)(?=\d)|(?<=\d)(?=\D)", " ", value)
        return " ".join(part for part in value.split() if part)


LABEL_FORMATTER = AppLabelFormatter()


def cached_icon_path(
    app: AppInfo,
    device_serial: str,
    icon_extractor: IconExtractor,
) -> Path | None:
    serial_key = safe_filename(device_serial or "device")
    return icon_extractor.cached_icon_path(
        app.package_name,
        app.version_name,
        app.version_code,
        source_keys=[f"acbridge_{serial_key}", ""],
    )


def cached_acbridge_icon_path(
    app: AppInfo,
    device_serial: str,
    icon_extractor: IconExtractor,
) -> Path | None:
    serial_key = safe_filename(device_serial or "device")
    path = icon_extractor.cache_path(
        app.package_name,
        app.version_name,
        app.version_code,
        source_key=f"acbridge_{serial_key}",
    )
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


class _CallbackEmitter:
    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def emit(self, message: str) -> None:
        self._callback(message)


class AppAssetLoader:
    """Resolve labels/icons for captured apps using cache, ACBridge, then ADB fallback."""

    def __init__(
        self,
        adb: AssetADBClient,
        settings: ProfileSettings,
        apk_metadata: APKMetadataExtractor,
        icon_extractor: IconExtractor,
        *,
        device_serial: str,
        temp_path: Path,
        metadata_parallelism: object = 6,
        root_available: Callable[[CancellationEvent | None], bool] | None = None,
    ) -> None:
        self._adb = adb
        self._settings = settings
        self._apk_metadata = apk_metadata
        self._icon_extractor = icon_extractor
        self._device_serial = device_serial
        self._temp_path = Path(temp_path)
        self._metadata_parallelism = metadata_parallelism
        self._root_available = root_available or (
            lambda cancel_event: adb.root_available(cancel_event=cancel_event)
        )

    def load(
        self,
        apps: list[AppInfo],
        targets: list[AppInfo],
        metadata_targets: list[AppInfo] | None = None,
        *,
        cancel_event: CancellationEvent | None = None,
        progress_callback: Callable[[str], None] | None = None,
        item_callback: Callable[[AppInfo], None] | None = None,
    ) -> list[AppInfo]:
        target_apps = list(targets)
        if not target_apps or self._cancelled(cancel_event):
            return []
        all_apps = list(apps)
        metadata_target_packages = {app.package_name for app in (metadata_targets or [])}
        updated_apps: list[AppInfo] = []
        pull_dir = ensure_dir(self._temp_path / "apk-assets")
        total = len(target_apps)
        pull_plan: list[tuple[str, Path]] = []
        local_apks: dict[str, list[Path]] = {}
        apps_by_package = {
            app.package_name: (app.version_name, app.version_code) for app in target_apps
        }
        cached_labels: dict[str, str] = {}
        cached_icons: dict[str, Path] = {}
        bridge_metadata: dict[str, dict[str, str]] = {}
        last_percent = 0

        def progress_text(
            labels: int,
            icons: int,
            processed: int,
            phase: str,
            stage_done: int = 0,
            stage_total: int = 0,
            stage_weight: int = 30,
        ) -> str:
            nonlocal last_percent
            percent = asset_progress_percent(total, labels, icons)
            if stage_total > 0:
                stage_done = min(max(0, stage_done), stage_total)
                stage_percent = int(round((stage_done / stage_total) * max(0, stage_weight)))
                percent = max(percent, stage_percent)
            percent = max(percent, last_percent)
            last_percent = percent
            return asset_progress_text(
                total,
                labels,
                icons,
                processed,
                phase,
                percent_override=percent,
            )

        for app in target_apps:
            if self._cancelled(cancel_event):
                return []
            label = LABEL_FORMATTER.cached_display_label(app, self._apk_metadata)
            if label:
                cached_labels[app.package_name] = label
            icon = cached_acbridge_icon_path(
                app,
                self._device_serial,
                self._icon_extractor,
            ) or cached_icon_path(app, self._device_serial, self._icon_extractor)
            if icon:
                cached_icons[app.package_name] = icon

        missing_labels = {
            app.package_name for app in target_apps if app.package_name not in cached_labels
        }
        missing_icons = {
            app.package_name for app in target_apps if app.package_name not in cached_icons
        }
        missing_metadata = {
            app.package_name
            for app in target_apps
            if app.package_name in metadata_target_packages
            and (not app.metadata_checked or not has_known_size(app))
        }
        self._emit(
            progress_callback,
            progress_text(
                len(cached_labels),
                len(cached_icons),
                0,
                "Checked local cache before downloading missing app data.",
            ),
        )

        if not missing_labels and not missing_icons and not missing_metadata:
            self._emit(
                progress_callback,
                progress_text(total, total, total, "All app labels and icons were loaded from local cache."),
            )
            return [
                replace(
                    app,
                    app_label=cached_labels.get(app.package_name, app.app_label),
                    apk_paths=list(app.apk_paths),
                    icon_path=str(cached_icons[app.package_name]),
                    bloatware_labels=list(app.bloatware_labels),
                    assets_checked=True,
                )
                for app in target_apps
            ]

        self._emit(
            progress_callback,
            progress_text(
                len(cached_labels),
                len(cached_icons),
                0,
                "Installing or starting ACBridge helper for app labels and icons.",
            ),
        )
        if self._cancelled(cancel_event):
            return []

        bridge_package_names = missing_labels | missing_icons | missing_metadata
        bridge_root = False
        try:
            bridge = ACBridgeClient(
                self._adb,  # type: ignore[arg-type]
                self._settings,  # type: ignore[arg-type]
                self._icon_extractor,
            )
            bridge_root = self._root_available(cancel_event)
            if self._cancelled(cancel_event):
                return []
            bridge_progress = self._bridge_progress_adapter(
                progress_callback,
                progress_text,
                len(cached_labels),
                len(cached_icons),
            )
            bridge_result = bridge.load_app_data(
                {
                    package: apps_by_package[package]
                    for package in bridge_package_names
                    if package in apps_by_package
                },
                device_serial=self._device_serial,
                icon_size=96,
                need_labels=bool(missing_labels),
                need_icons=bool(missing_icons),
                need_metadata=bool(missing_metadata),
                use_root=bridge_root,
                progress_callback=bridge_progress,
                cancel_event=cancel_event,
            )
            if self._cancelled(cancel_event):
                return []
            targets_by_name = {app.package_name: app for app in target_apps}
            bridge_labels: dict[str, str] = {}
            for package_name, label in bridge_result.labels.items():
                app = targets_by_name.get(package_name)
                normalized = LABEL_FORMATTER.normalize(
                    label,
                    package_name,
                    app.apk_paths if app else [],
                )
                if normalized:
                    bridge_labels[package_name] = normalized
            cached_labels.update(bridge_labels)
            bridge_metadata.update(bridge_result.metadata)
            cached_icons.update(bridge_result.icons)
            bridge_message = bridge_result.message
        except StaleDeviceContext:
            raise
        except Exception as exc:
            if self._cancelled(cancel_event):
                return []
            bridge_message = f"ACBridge failed: {exc}. OpenADB fallback APK parser will continue."

        missing_metadata_after_bridge = [
            package for package in missing_metadata if package not in bridge_metadata
        ]
        if missing_metadata_after_bridge:
            self._emit(
                progress_callback,
                progress_text(
                    len(cached_labels),
                    len(cached_icons),
                    0,
                    "ACBridge metadata was incomplete. "
                    f"Using slower ADB fallback for {len(missing_metadata_after_bridge)} packages.",
                ),
            )
            if self._cancelled(cancel_event):
                return []
            bridge_metadata.update(
                self._adb.get_package_details_many(
                    missing_metadata_after_bridge,
                    max_workers=metadata_worker_count(
                        len(missing_metadata_after_bridge),
                        self._metadata_parallelism,
                    ),
                    cancel_event=cancel_event,
                )
            )
            if self._cancelled(cancel_event):
                return []

        missing_sizes_after_bridge = [
            app.package_name
            for app in target_apps
            if not has_known_size(app)
            and not metadata_has_size(bridge_metadata.get(app.package_name, {}))
        ]
        if missing_sizes_after_bridge:
            self._emit(
                progress_callback,
                progress_text(
                    len(cached_labels),
                    len(cached_icons),
                    0,
                    f"Resolving APK sizes for {len(missing_sizes_after_bridge)} apps through ADB.",
                ),
            )
            if self._cancelled(cancel_event):
                return []
            sizes_by_package = self._adb.get_package_sizes_bulk(
                missing_sizes_after_bridge,
                use_root=bridge_root,
                cancel_event=cancel_event,
            )
            if self._cancelled(cancel_event):
                return []
            for package_name, size_bytes in sizes_by_package.items():
                bridge_metadata.setdefault(package_name, {})["sizeBytes"] = str(size_bytes)

        all_apps_by_name = {app.package_name: app for app in all_apps}
        if self._cancelled(cancel_event):
            return []
        for package_name, label in cached_labels.items():
            app = all_apps_by_name.get(package_name)
            if app:
                self._apk_metadata.set_cached_label(app, label)
        self._emit(
            progress_callback,
            progress_text(
                len(cached_labels),
                len(cached_icons),
                0,
                bridge_message,
            ),
        )

        fallback_apps = [
            app
            for app in target_apps
            if app.package_name not in cached_labels or app.package_name not in cached_icons
        ]
        apk_paths_by_package: dict[str, list[str]] = {}
        if fallback_apps:
            self._emit(
                progress_callback,
                progress_text(
                    len(cached_labels),
                    len(cached_icons),
                    0,
                    f"Resolving APK paths for {len(fallback_apps)} apps still missing labels or icons.",
                ),
            )
            if self._cancelled(cancel_event):
                return []
            apk_paths_by_package = self._adb.get_package_paths_bulk(
                [app.package_name for app in fallback_apps],
                cancel_event=cancel_event,
            )
            if self._cancelled(cancel_event):
                return []

        for app in fallback_apps:
            if self._cancelled(cancel_event):
                return []
            apk_paths = apk_paths_by_package.get(app.package_name) or app.apk_paths
            local_targets: list[Path] = []
            for index, apk_path in enumerate(apk_paths):
                apk_name = safe_filename(Path(apk_path).name or f"part_{index}.apk")
                target = pull_dir / (
                    f"{safe_filename(app.package_name)}_"
                    f"{safe_filename(app.version_code or '0')}_{index}_{apk_name}"
                )
                local_targets.append(target)
            local_apks[app.package_name] = local_targets
            if app.package_name not in cached_labels or app.package_name not in cached_icons:
                pull_plan.extend(
                    (apk_path, target)
                    for apk_path, target in zip(apk_paths, local_targets, strict=True)
                    if not target.exists()
                )

        if pull_plan:
            self._emit(
                progress_callback,
                progress_text(
                    len(cached_labels),
                    len(cached_icons),
                    0,
                    f"Pulling {len(pull_plan)} APK parts through ADB for fallback label/icon extraction.",
                ),
            )

            def pull_progress(
                done: int,
                part_total: int,
                remote: str,
                local: str,
                success: bool,
            ) -> None:
                status = "pulled" if success else "failed"
                current_name = Path(local).name or Path(remote).name or remote
                self._emit(
                    progress_callback,
                    progress_text(
                        len(cached_labels),
                        len(cached_icons),
                        0,
                        f"Downloading APK parts for fallback extraction: "
                        f"{done}/{part_total} {status}. Current: {current_name}",
                        stage_done=done,
                        stage_total=part_total,
                        stage_weight=35,
                    ),
                )

            if self._cancelled(cancel_event):
                return []
            self._adb.pull_files_via_temp(
                pull_plan,
                chunk_size=16,
                timeout=900,
                progress_callback=pull_progress,
                parallel_chunks=2,
                use_root=bridge_root,
                cancel_event=cancel_event,
            )
            if self._cancelled(cancel_event):
                return []

        def build_updated_app(app: AppInfo) -> AppInfo:
            initial_label = (
                "" if LABEL_FORMATTER.is_placeholder(app.app_label, app.package_name) else app.app_label
            )
            updated = replace(
                app,
                app_label=initial_label,
                apk_paths=list(apk_paths_by_package.get(app.package_name) or app.apk_paths),
                bloatware_labels=list(app.bloatware_labels),
                assets_checked=True,
            )
            metadata = bridge_metadata.get(app.package_name, {})
            if metadata:
                updated.version_name = metadata.get("versionName", "") or updated.version_name
                updated.version_code = metadata.get("versionCode", "") or updated.version_code
                updated.size = size_text_from_metadata(metadata) or updated.size
                updated.metadata_checked = bool(
                    updated.metadata_checked or metadata_is_complete(metadata)
                )
            if app.package_name in cached_labels:
                updated.app_label = cached_labels[app.package_name]
            if app.package_name in cached_icons:
                updated.icon_path = str(cached_icons[app.package_name])
            if (updated.app_label and updated.icon_path) or not updated.apk_paths:
                if not updated.app_label:
                    updated.app_label = LABEL_FORMATTER.fallback(
                        updated.package_name,
                        updated.apk_paths,
                    )
                return updated

            local_targets = local_apks.get(app.package_name, [])
            if not any(target.exists() for target in local_targets):
                if not updated.app_label:
                    updated.app_label = LABEL_FORMATTER.fallback(
                        updated.package_name,
                        updated.apk_paths,
                    )
                return updated
            for target in local_targets:
                if not target.exists():
                    continue
                if not updated.app_label:
                    label = self._apk_metadata.extract_label(target)
                    label = LABEL_FORMATTER.normalize(label, app.package_name, updated.apk_paths)
                    if label:
                        updated.app_label = label
                        self._apk_metadata.set_cached_label(app, label)
                if not updated.icon_path:
                    icon = self._icon_extractor.extract_from_apk(
                        target,
                        app.package_name,
                        app.version_name,
                        app.version_code,
                    )
                    if icon:
                        updated.icon_path = str(icon)
                if updated.app_label and updated.icon_path:
                    break
            if not updated.app_label:
                updated.app_label = LABEL_FORMATTER.fallback(updated.package_name, updated.apk_paths)
            return updated

        max_workers = min(8, max(1, (len(target_apps) + 24) // 25))
        label_packages = set(cached_labels)
        icon_packages = set(cached_icons)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(build_updated_app, app) for app in target_apps]
            for index, future in enumerate(as_completed(futures), start=1):
                if self._cancelled(cancel_event):
                    for pending in futures:
                        pending.cancel()
                    break
                updated = future.result()
                updated_apps.append(updated)
                if updated.app_label:
                    label_packages.add(updated.package_name)
                if updated.icon_path:
                    icon_packages.add(updated.package_name)
                self._emit(
                    progress_callback,
                    progress_text(
                        len(label_packages),
                        len(icon_packages),
                        index,
                        "Applying downloaded app labels and icons.",
                    ),
                )
                if item_callback is not None:
                    item_callback(updated)
        return [] if self._cancelled(cancel_event) else updated_apps

    @staticmethod
    def _bridge_progress_adapter(
        progress_callback: Callable[[str], None] | None,
        progress_text: Callable[..., str],
        cached_label_count: int,
        cached_icon_count: int,
    ) -> _CallbackEmitter | None:
        if progress_callback is None:
            return None

        def emit(message: str) -> None:
            match = re.search(
                r"ACBRIDGE_PROGRESS\s+labels=(\d+)\s+icons=(\d+)\s+total=(\d+)\s+stage=([A-Za-z0-9_-]+)",
                message or "",
            )
            if match:
                labels = cached_label_count + int(match.group(1))
                icons = cached_icon_count + int(match.group(2))
                total = max(1, int(match.group(3)))
                stage = match.group(4)
                done = int(match.group(1)) + int(match.group(2))
                progress_callback(
                    progress_text(
                        labels,
                        icons,
                        max(int(match.group(1)), int(match.group(2))),
                        f"ACBridge is rendering app labels and icons on the phone "
                        f"({stage}, {done}/{total * 2} items).",
                        stage_done=done,
                        stage_total=total * 2,
                        stage_weight=70,
                    )
                )
                return
            progress_callback(
                progress_text(cached_label_count, cached_icon_count, 0, message)
            )

        return _CallbackEmitter(emit)

    @staticmethod
    def _emit(callback: Callable[[str], None] | None, message: str) -> None:
        if callback is not None:
            callback(message)

    @staticmethod
    def _cancelled(cancel_event: CancellationEvent | None) -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())
