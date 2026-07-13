"""Profile-aware File Manager UI state without device or transfer logic."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .path_utils import safe_filename


DEFAULT_ANDROID_PATH = "/sdcard/"
DEFAULT_SPLITTER_SIZES = (420, 176, 420)


class StaleFileManagerProfile(RuntimeError):
    """Raised when UI state is about to be saved into another profile."""


def normalize_android_path(path: str) -> str:
    """Return one stable spelling for an absolute Android path."""

    normalized = str(path or "").strip().replace("\\", "/") or DEFAULT_ANDROID_PATH
    parts = [part for part in normalized.split("/") if part]
    normalized = "/" + "/".join(parts)
    if normalized in {"/sdcard", "/storage/emulated/0"}:
        return normalized + "/"
    return normalized or "/"


def normalize_splitter_sizes(value: object) -> tuple[int, int, int]:
    """Validate persisted three-panel splitter sizes."""

    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return DEFAULT_SPLITTER_SIZES
    try:
        sizes = tuple(max(1, int(item)) for item in value)
    except (TypeError, ValueError):
        return DEFAULT_SPLITTER_SIZES
    return sizes[0], sizes[1], sizes[2]


@dataclass(frozen=True, slots=True)
class FileManagerUIState:
    """Serializable snapshot of File Manager presentation state."""

    profile_key: str
    profile_kind: str
    profile_path: str
    android_path: str
    windows_path: str
    splitter_sizes: tuple[int, int, int]

    def to_mapping(self) -> dict[str, object]:
        return {
            "profile_key": self.profile_key,
            "profile_kind": self.profile_kind,
            "profile_path": self.profile_path,
            "android_path": self.android_path,
            "windows_path": self.windows_path,
            "splitter_sizes": list(self.splitter_sizes),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FileManagerUIState:
        return cls(
            profile_key=str(value.get("profile_key", "") or ""),
            profile_kind=str(value.get("profile_kind", "") or ""),
            profile_path=str(value.get("profile_path", "") or ""),
            android_path=normalize_android_path(
                str(value.get("android_path", DEFAULT_ANDROID_PATH) or DEFAULT_ANDROID_PATH)
            ),
            windows_path=str(value.get("windows_path", "") or ""),
            splitter_sizes=normalize_splitter_sizes(value.get("splitter_sizes")),
        )


class FileManagerState:
    """Load and persist path/splitter state in the correct settings scope.

    Android path is profile-local. Windows path and splitter sizes are global.
    Captured profile key, kind, and settings path prevent a late signal from
    saving an old page's Android path into a newly activated profile.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._snapshot = FileManagerUIState(
            profile_key="",
            profile_kind="",
            profile_path="",
            android_path=DEFAULT_ANDROID_PATH,
            windows_path="",
            splitter_sizes=DEFAULT_SPLITTER_SIZES,
        )
        self.reload()

    @property
    def snapshot(self) -> FileManagerUIState:
        return self._snapshot

    @property
    def profile_key(self) -> str:
        return self._snapshot.profile_key

    @property
    def android_path(self) -> str:
        return self._snapshot.android_path

    @property
    def windows_path(self) -> str:
        return self._snapshot.windows_path

    @property
    def splitter_sizes(self) -> tuple[int, int, int]:
        return self._snapshot.splitter_sizes

    def reload(
        self,
        profile_key: str | None = None,
        *,
        profile_kind: str | None = None,
        profile_path: str | Path | None = None,
    ) -> FileManagerUIState:
        """Reload active profile and global state as one validated snapshot."""

        with self._settings_transaction():
            before = self._active_profile_identity()
            expected = self._expected_identity(
                before,
                profile_key=profile_key,
                profile_kind=profile_kind,
                profile_path=profile_path,
            )
            self._require_profile(expected, actual=before)

            android_path = normalize_android_path(
                str(
                    self.settings.get(
                        "file_manager_android_path",
                        DEFAULT_ANDROID_PATH,
                    )
                    or DEFAULT_ANDROID_PATH
                )
            )
            windows_path = str(
                self.settings.get_global("file_manager_windows_path", "") or ""
            )
            splitter_sizes = normalize_splitter_sizes(
                self.settings.get_global(
                    "file_manager_splitter_sizes",
                    list(DEFAULT_SPLITTER_SIZES),
                )
            )

            after = self._active_profile_identity()
            if before != after:
                raise StaleFileManagerProfile(
                    "The active File Manager profile changed while UI state was loading"
                )
            self._require_profile(expected, actual=after)
        self._snapshot = FileManagerUIState(
            profile_key=expected[0],
            profile_kind=expected[1],
            profile_path=expected[2],
            android_path=android_path,
            windows_path=windows_path,
            splitter_sizes=splitter_sizes,
        )
        return self._snapshot

    def save_android_path(
        self,
        path: str,
        *,
        profile_key: str | None = None,
    ) -> str:
        normalized = normalize_android_path(path)
        with self._settings_transaction():
            expected = self._expected_profile(profile_key)
            self.settings.set("file_manager_android_path", normalized)
            self._require_profile(expected)
        self._snapshot = replace(self._snapshot, android_path=normalized)
        return normalized

    def save_windows_path(self, path: str | Path) -> str:
        value = str(Path(path).expanduser()) if str(path or "").strip() else ""
        self.settings.set_global_values({"file_manager_windows_path": value})
        self._snapshot = replace(self._snapshot, windows_path=value)
        return value

    def save_splitter_sizes(self, sizes: object) -> tuple[int, int, int]:
        normalized = normalize_splitter_sizes(sizes)
        self.settings.set_global_values(
            {"file_manager_splitter_sizes": list(normalized)}
        )
        self._snapshot = replace(self._snapshot, splitter_sizes=normalized)
        return normalized

    def _expected_profile(
        self,
        profile_key: str | None,
    ) -> tuple[str, str, str]:
        expected = (
            str(profile_key if profile_key is not None else self.profile_key),
            self._snapshot.profile_kind,
            self._snapshot.profile_path,
        )
        self._require_profile(expected)
        return expected

    def _require_profile(
        self,
        expected: tuple[str, str, str],
        *,
        actual: tuple[str, str, str] | None = None,
    ) -> None:
        actual_identity = self._active_profile_identity() if actual is None else actual
        # Settings doubles and legacy implementations may not expose profile
        # identity. Compare every component that both sides can identify.
        labels = ("key", "kind", "path")
        for label, expected_value, actual_value in zip(labels, expected, actual_identity):
            if expected_value and actual_value and expected_value != actual_value:
                raise StaleFileManagerProfile(
                    "File Manager state belongs to profile "
                    f"{expected!r}, not {actual_identity!r} ({label} changed)"
                )

    def _active_profile_identity(self) -> tuple[str, str, str]:
        explicit = str(getattr(self.settings, "active_profile_key", "") or "").strip()
        serial = str(getattr(self.settings, "active_profile_serial", "") or "").strip()
        key = explicit or (safe_filename(serial) if serial else "")
        kind = str(getattr(self.settings, "active_profile_kind", "") or "").strip()
        raw_path = getattr(self.settings, "config_dir", "") or ""
        if not raw_path:
            settings_path = getattr(self.settings, "path", None)
            raw_path = Path(settings_path).parent if settings_path else ""
        try:
            path = str(Path(raw_path).expanduser().resolve(strict=False)) if raw_path else ""
        except (OSError, RuntimeError):
            path = str(raw_path or "")
        return key, kind, path

    @staticmethod
    def _expected_identity(
        active: tuple[str, str, str],
        *,
        profile_key: str | None,
        profile_kind: str | None,
        profile_path: str | Path | None,
    ) -> tuple[str, str, str]:
        path = active[2]
        if profile_path is not None:
            try:
                path = str(Path(profile_path).expanduser().resolve(strict=False))
            except (OSError, RuntimeError):
                path = str(profile_path)
        return (
            active[0] if profile_key is None else str(profile_key or ""),
            active[1] if profile_kind is None else str(profile_kind or ""),
            path,
        )

    def _settings_transaction(self):
        lock = getattr(self.settings, "_save_lock", None)
        return lock if hasattr(lock, "__enter__") else nullcontext()


def state_from_settings(
    settings,
    profile_key: str | None = None,
    *,
    profile_kind: str | None = None,
    profile_path: str | Path | None = None,
) -> FileManagerUIState:
    """Convenience helper for page/controller composition."""

    state = FileManagerState(settings)
    if profile_key is None and profile_kind is None and profile_path is None:
        return state.snapshot
    return state.reload(
        profile_key,
        profile_kind=profile_kind,
        profile_path=profile_path,
    )
