from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class VisibleSelectionState(str, Enum):
    UNCHECKED = "unchecked"
    PARTIALLY_CHECKED = "partially_checked"
    CHECKED = "checked"


@dataclass(frozen=True, slots=True)
class AppSelectionSummary:
    total_selected: int
    visible_selected: int
    hidden_selected: int
    visible_total: int
    visible_state: VisibleSelectionState
    text: str


class AppSelectionModel:
    """Package-keyed selection that survives filtering and sorting."""

    def __init__(self, selected_packages: Iterable[str] = ()) -> None:
        self._selected = self._normalized_packages(selected_packages)

    @property
    def selected_packages(self) -> frozenset[str]:
        return frozenset(self._selected)

    def __len__(self) -> int:
        return len(self._selected)

    def __contains__(self, package_name: object) -> bool:
        return self._normalize_package(package_name) in self._selected

    def is_selected(self, package_name: str) -> bool:
        return self._normalize_package(package_name) in self._selected

    def set_selected(self, package_name: str, selected: bool = True) -> bool:
        package = self._normalize_package(package_name)
        if not package:
            return False
        before = package in self._selected
        if selected:
            self._selected.add(package)
        else:
            self._selected.discard(package)
        return before != selected

    def toggle(self, package_name: str) -> bool:
        package = self._normalize_package(package_name)
        if not package:
            return False
        return self.set_selected(package, package not in self._selected)

    def replace(self, package_names: Iterable[str]) -> bool:
        replacement = self._normalized_packages(package_names)
        if replacement == self._selected:
            return False
        self._selected = replacement
        return True

    def retain(self, available_packages: Iterable[str]) -> bool:
        """Drop selections no longer present after a full data reload."""

        retained = self._selected & self._normalized_packages(available_packages)
        if retained == self._selected:
            return False
        self._selected = retained
        return True

    def select_visible(self, visible_packages: Iterable[str]) -> bool:
        visible = self._normalized_packages(visible_packages)
        before = len(self._selected)
        self._selected.update(visible)
        return len(self._selected) != before

    def unselect_visible(self, visible_packages: Iterable[str]) -> bool:
        visible = self._normalized_packages(visible_packages)
        before = len(self._selected)
        self._selected.difference_update(visible)
        return len(self._selected) != before

    def clear(self) -> bool:
        if not self._selected:
            return False
        self._selected.clear()
        return True

    def visible_state(self, visible_packages: Iterable[str]) -> VisibleSelectionState:
        visible = self._normalized_packages(visible_packages)
        if not visible:
            return VisibleSelectionState.UNCHECKED
        visible_selected = len(visible & self._selected)
        if visible_selected <= 0:
            return VisibleSelectionState.UNCHECKED
        if visible_selected >= len(visible):
            return VisibleSelectionState.CHECKED
        return VisibleSelectionState.PARTIALLY_CHECKED

    def summary(self, visible_packages: Iterable[str]) -> AppSelectionSummary:
        visible = self._normalized_packages(visible_packages)
        visible_selected = len(visible & self._selected)
        total_selected = len(self._selected)
        hidden_selected = max(0, total_selected - visible_selected)
        text = f"{total_selected} selected"
        if hidden_selected:
            text += f" · {hidden_selected} hidden by filters"
        return AppSelectionSummary(
            total_selected=total_selected,
            visible_selected=visible_selected,
            hidden_selected=hidden_selected,
            visible_total=len(visible),
            visible_state=self.visible_state(visible),
            text=text,
        )

    @classmethod
    def _normalized_packages(cls, package_names: Iterable[str]) -> set[str]:
        return {package for value in package_names if (package := cls._normalize_package(value))}

    @staticmethod
    def _normalize_package(package_name: object) -> str:
        return str(package_name or "").strip()


__all__ = [
    "AppSelectionModel",
    "AppSelectionSummary",
    "VisibleSelectionState",
]
