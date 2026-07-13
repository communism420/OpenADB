from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from openadb.models.app_info import AppInfo
from openadb.ui.widgets.app_list_widget import APP_SORT_MODES, AppFilterState


FILTER_SETTING_KEYS = {
    "app_type": "apps_filter_type",
    "app_state": "apps_filter_state",
    "uad_category": "apps_filter_uad",
    "search_text": "apps_filter_search",
    "sort_mode": "apps_sort_mode",
}

FILTER_LABELS = {
    "app_type": {"all": "All", "user": "User", "system": "System"},
    "app_state": {"any": "Any", "enabled": "Enabled", "disabled": "Disabled"},
    "uad_category": {
        "any": "Any",
        "recommended": "Recommended",
        "advanced": "Advanced",
        "expert": "Expert",
        "unsafe": "Unsafe",
        "not listed": "Not listed",
    },
}


class AppsFilterSettings(Protocol):
    active_profile_serial: str
    active_profile_kind: str
    path: Path

    def get(self, key: str, default=None): ...

    def set(self, key: str, value, save: bool = True) -> None: ...

    def save(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AppsViewState:
    """Normalized profile-local Applications view state."""

    filters: AppFilterState = field(default_factory=AppFilterState)
    sort_mode: str = "name"

    @classmethod
    def from_values(
        cls,
        *,
        search_text: str = "",
        app_type: str = "all",
        app_state: str = "any",
        uad_category: str = "any",
        sort_mode: str = "name",
    ) -> AppsViewState:
        normalized_sort = str(sort_mode or "").strip().casefold()
        return cls(
            filters=AppFilterState.from_values(
                search_text=search_text,
                app_type=app_type,
                app_state=app_state,
                uad_category=uad_category,
            ),
            sort_mode=normalized_sort if normalized_sort in APP_SORT_MODES else "name",
        )


@dataclass(frozen=True, slots=True)
class AppsFilterSummary:
    active_parts: tuple[str, ...]
    active_text: str
    menu_filter_count: int
    filter_button_text: str
    tooltip: str

    @property
    def has_active_filters(self) -> bool:
        return bool(self.active_parts)


class AppsFilterController:
    """Owns filter/search/sort state without depending on page widgets.

    ``SettingsManager`` already redirects reads and writes to the active device
    profile. The controller tracks that identity and automatically reloads when
    it changes, preventing values from the previous profile being persisted into
    the new one.
    """

    def __init__(self, settings: AppsFilterSettings) -> None:
        self._settings = settings
        self._profile_identity = self._current_profile_identity()
        self._state = AppsViewState()
        self.reload()

    @property
    def state(self) -> AppsViewState:
        self._sync_profile()
        return self._state

    @property
    def profile_identity(self) -> tuple[str, str, str]:
        self._sync_profile()
        return self._profile_identity

    def reload(self) -> AppsViewState:
        self._profile_identity = self._current_profile_identity()
        self._state = AppsViewState.from_values(
            search_text=str(self._settings.get(FILTER_SETTING_KEYS["search_text"], "") or ""),
            app_type=str(self._settings.get(FILTER_SETTING_KEYS["app_type"], "all") or "all"),
            app_state=str(self._settings.get(FILTER_SETTING_KEYS["app_state"], "any") or "any"),
            uad_category=str(self._settings.get(FILTER_SETTING_KEYS["uad_category"], "any") or "any"),
            sort_mode=str(self._settings.get(FILTER_SETTING_KEYS["sort_mode"], "name") or "name"),
        )
        return self._state

    def set_filters(self, filters: AppFilterState, *, persist: bool = True) -> AppsViewState:
        current = self._sync_profile()
        normalized = AppFilterState.from_values(
            search_text=filters.search_text,
            app_type=filters.app_type,
            app_state=filters.app_state,
            uad_category=filters.uad_category,
        )
        self._state = AppsViewState(filters=normalized, sort_mode=current.sort_mode)
        if persist:
            self.persist()
        return self._state

    def update_filters(
        self,
        *,
        search_text: str | None = None,
        app_type: str | None = None,
        app_state: str | None = None,
        uad_category: str | None = None,
        persist: bool = True,
    ) -> AppsViewState:
        current = self._sync_profile()
        filters = current.filters
        return self.set_filters(
            AppFilterState.from_values(
                search_text=filters.search_text if search_text is None else search_text,
                app_type=filters.app_type if app_type is None else app_type,
                app_state=filters.app_state if app_state is None else app_state,
                uad_category=filters.uad_category if uad_category is None else uad_category,
            ),
            persist=persist,
        )

    def set_sort_mode(self, sort_mode: str, *, persist: bool = True) -> AppsViewState:
        current = self._sync_profile()
        self._state = AppsViewState.from_values(
            search_text=current.filters.search_text,
            app_type=current.filters.app_type,
            app_state=current.filters.app_state,
            uad_category=current.filters.uad_category,
            sort_mode=sort_mode,
        )
        if persist:
            self.persist()
        return self._state

    def reset_filters(self, *, persist: bool = True) -> AppsViewState:
        """Reset filters and search while preserving the selected sort mode."""

        current = self._sync_profile()
        self._state = AppsViewState(filters=AppFilterState(), sort_mode=current.sort_mode)
        if persist:
            self.persist()
        return self._state

    def reset_view(self, *, persist: bool = True) -> AppsViewState:
        """Reset filters, search, and sorting to safe defaults."""

        self._sync_profile()
        self._state = AppsViewState()
        if persist:
            self.persist()
        return self._state

    def persist(self) -> None:
        self._sync_profile()
        filters = self._state.filters
        values = {
            FILTER_SETTING_KEYS["app_type"]: filters.app_type,
            FILTER_SETTING_KEYS["app_state"]: filters.app_state,
            FILTER_SETTING_KEYS["uad_category"]: filters.uad_category,
            FILTER_SETTING_KEYS["search_text"]: filters.search_text,
            FILTER_SETTING_KEYS["sort_mode"]: self._state.sort_mode,
        }
        for key, value in values.items():
            self._settings.set(key, value, save=False)
        self._settings.save()

    def matches(self, app: AppInfo, uad_category: str) -> bool:
        """Delegate matching to the canonical ``AppFilterState`` implementation."""

        return self.state.filters.matches(app, uad_category)

    def summary(self) -> AppsFilterSummary:
        filters = self.state.filters
        active: list[str] = []
        if filters.app_type != "all":
            active.append(FILTER_LABELS["app_type"][filters.app_type])
        if filters.app_state != "any":
            active.append(FILTER_LABELS["app_state"][filters.app_state])
        if filters.uad_category != "any":
            active.append(FILTER_LABELS["uad_category"][filters.uad_category])
        if filters.search_text:
            active.append(f'Search: "{filters.search_text}"')
        menu_count = sum(
            value != default
            for value, default in zip(
                (filters.app_type, filters.app_state, filters.uad_category),
                ("all", "any", "any"),
                strict=True,
            )
        )
        return AppsFilterSummary(
            active_parts=tuple(active),
            active_text=" · ".join(active) if active else "No active filters",
            menu_filter_count=menu_count,
            filter_button_text=f"Filters ({menu_count})" if menu_count else "Filters",
            tooltip="\n".join(
                [
                    f"Type: {FILTER_LABELS['app_type'][filters.app_type]}",
                    f"State: {FILTER_LABELS['app_state'][filters.app_state]}",
                    f"UAD category: {FILTER_LABELS['uad_category'][filters.uad_category]}",
                ]
            ),
        )

    def _sync_profile(self) -> AppsViewState:
        if self._current_profile_identity() != self._profile_identity:
            return self.reload()
        return self._state

    def _current_profile_identity(self) -> tuple[str, str, str]:
        return (
            str(getattr(self._settings, "active_profile_kind", "") or ""),
            str(getattr(self._settings, "active_profile_serial", "") or ""),
            str(getattr(self._settings, "path", "") or ""),
        )


__all__ = [
    "AppsFilterController",
    "AppsFilterSummary",
    "AppsViewState",
    "FILTER_SETTING_KEYS",
]
