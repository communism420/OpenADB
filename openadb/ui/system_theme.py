"""Live Windows System-theme tracking with a bounded Qt timer lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from openadb.ui.style import apply_theme, system_theme_name


EXPLICIT_THEMES = frozenset({"Light", "Dark"})
SYSTEM_THEME = "System"


class SystemThemeProvider(Protocol):
    """Mockable source for the currently resolved operating-system theme."""

    def current_theme(self) -> str: ...


class WindowsSystemThemeProvider:
    """Read the Windows app-theme preference using the shared style helper."""

    def current_theme(self) -> str:
        return system_theme_name()


class SystemThemeController(QObject):
    """Apply live System-theme changes without rebuilding the main window.

    Polling is active only while the selected OpenADB theme is ``System``.
    The last resolved value is compared with the application property before
    applying QSS, which prevents repeated icon and semantic-color refreshes.
    """

    DEFAULT_POLL_INTERVAL_MS = 1500

    def __init__(
        self,
        app: QApplication,
        *,
        provider: SystemThemeProvider | None = None,
        theme_applier: Callable[[QApplication, str], None] = apply_theme,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._app = app
        self._provider = provider or WindowsSystemThemeProvider()
        self._theme_applier = theme_applier
        self._theme_mode = SYSTEM_THEME
        self._running = False
        self._timer = QTimer(self)
        self._timer.setInterval(max(250, int(poll_interval_ms)))
        self._timer.timeout.connect(self.poll_now)

    @property
    def theme_mode(self) -> str:
        return self._theme_mode

    @property
    def is_listening(self) -> bool:
        return self._running and self._timer.isActive()

    def start(self, theme: str) -> None:
        """Start lifecycle management and apply/listen for ``theme``."""

        self._running = True
        self.set_theme(theme)

    def set_theme(self, theme: str) -> None:
        """Switch explicit/System mode without leaving an unnecessary timer."""

        normalized = str(theme or SYSTEM_THEME).strip().title()
        if normalized not in EXPLICIT_THEMES | {SYSTEM_THEME}:
            normalized = SYSTEM_THEME
        self._theme_mode = normalized
        if not self._running:
            return
        if normalized == SYSTEM_THEME:
            self._apply_if_changed(self._normalized_system_theme())
            if not self._timer.isActive():
                self._timer.start()
            return
        self._timer.stop()
        self._apply_if_changed(normalized)

    def poll_now(self) -> bool:
        """Poll once in System mode and report whether the UI was refreshed."""

        if not self._running or self._theme_mode != SYSTEM_THEME:
            return False
        return self._apply_if_changed(self._normalized_system_theme())

    def stop(self) -> None:
        """Stop polling; safe to call repeatedly during shutdown."""

        self._running = False
        self._timer.stop()

    def _normalized_system_theme(self) -> str:
        resolved = str(self._provider.current_theme() or "").strip().title()
        return resolved if resolved in EXPLICIT_THEMES else "Light"

    def _apply_if_changed(self, resolved_theme: str) -> bool:
        current = str(self._app.property("openadbResolvedTheme") or "")
        if current == resolved_theme:
            return False
        self._theme_applier(self._app, resolved_theme)
        return True
