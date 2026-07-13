from __future__ import annotations

from dataclasses import replace
from typing import Callable, Protocol

from openadb.models.app_info import AppInfo

from .path_utils import format_bytes


class CancellationEvent(Protocol):
    def is_set(self) -> bool: ...


class PackageMetadataClient(Protocol):
    def get_package_details_many(
        self,
        package_names: list[str],
        *,
        max_workers: int,
        progress_callback=None,
        cancel_event=None,
    ) -> dict[str, dict[str, str]]: ...


def metadata_worker_count(target_count: int, configured: object = 6) -> int:
    """Return a conservative worker count for device package metadata reads."""
    if target_count <= 1:
        return 1
    try:
        value = int(configured)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 6
    return max(2, min(value, target_count, 8))


def size_text_from_metadata(metadata: dict[str, str]) -> str:
    raw = str(metadata.get("sizeBytes", "") or "").strip()
    if not raw:
        return ""
    try:
        return format_bytes(max(0, int(raw)))
    except ValueError:
        return ""


def metadata_has_size(metadata: dict[str, str]) -> bool:
    return bool(size_text_from_metadata(metadata))


def has_known_size(app: AppInfo) -> bool:
    value = (app.size or "").strip()
    return bool(value) and value.lower() != "unknown"


def metadata_is_complete(details: dict[str, str]) -> bool:
    """Return whether a package response contains the stable version identity.

    ``dumpsys package`` may transiently return an empty or truncated payload while
    a package/device is changing state.  A label or just one version field is still
    useful to merge, but must remain eligible for a later metadata retry.
    """
    return all(str(details.get(key, "") or "").strip() for key in ("versionName", "versionCode"))


def app_with_metadata(app: AppInfo, details: dict[str, str]) -> AppInfo:
    """Build an isolated AppInfo snapshot from one metadata response."""
    return replace(
        app,
        app_label=details.get("appLabel", "") or app.app_label,
        version_name=details.get("versionName", "") or app.version_name,
        version_code=details.get("versionCode", "") or app.version_code,
        apk_paths=list(app.apk_paths),
        size=size_text_from_metadata(details) or app.size,
        bloatware_labels=list(app.bloatware_labels),
        metadata_checked=bool(app.metadata_checked or metadata_is_complete(details)),
    )


class AppMetadataLoader:
    """Load package metadata without knowing about Qt widgets or active UI state."""

    def __init__(self, adb: PackageMetadataClient, configured_parallelism: object = 6) -> None:
        self._adb = adb
        self._configured_parallelism = configured_parallelism

    def load(
        self,
        apps: list[AppInfo],
        *,
        cancel_event: CancellationEvent | None = None,
        progress_callback: Callable[[str], None] | None = None,
        item_callback: Callable[[AppInfo], None] | None = None,
    ) -> list[AppInfo]:
        app_snapshots = list(apps)
        if not app_snapshots or self._cancelled(cancel_event):
            return []

        package_names = [app.package_name for app in app_snapshots]
        app_by_package = {app.package_name: app for app in app_snapshots}
        updated_by_package: dict[str, AppInfo] = {}
        max_workers = metadata_worker_count(len(app_snapshots), self._configured_parallelism)

        def on_progress(
            done: int,
            total: int,
            package_name: str,
            details: dict[str, str],
        ) -> None:
            if self._cancelled(cancel_event):
                return
            app = app_by_package.get(package_name)
            if app is None:
                return
            updated = app_with_metadata(app, details)
            updated_by_package[package_name] = updated
            if item_callback is not None:
                item_callback(updated)
            if progress_callback is not None:
                progress_callback(
                    f"App metadata: {done}/{total} packages loaded in parallel "
                    f"({max_workers} workers). Current: {package_name}"
                )

        self._adb.get_package_details_many(
            package_names,
            max_workers=max_workers,
            progress_callback=on_progress,
            cancel_event=cancel_event,
        )
        if self._cancelled(cancel_event):
            return []
        return [
            updated_by_package.get(app.package_name) or app_with_metadata(app, {})
            for app in app_snapshots
        ]

    @staticmethod
    def _cancelled(cancel_event: CancellationEvent | None) -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())
