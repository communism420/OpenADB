from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    """A label that keeps its full value while painting a compact version."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        elide_mode: Qt.TextElideMode = Qt.ElideMiddle,
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._elide_mode = elide_mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API name
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._update_elided_text()

    def full_text(self) -> str:
        return self._full_text

    def set_elide_mode(self, mode: Qt.TextElideMode) -> None:
        self._elide_mode = mode
        self._update_elided_text()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API name
        hint = super().minimumSizeHint()
        return QSize(0, hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API name
        super().resizeEvent(event)
        self._update_elided_text()

    def _update_elided_text(self) -> None:
        width = max(0, self.contentsRect().width())
        rendered = self._full_text
        if width > 0:
            rendered = self.fontMetrics().elidedText(self._full_text, self._elide_mode, width)
        super().setText(rendered)
