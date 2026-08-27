from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QListWidget,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
)


class NoWheelComboBox(QComboBox):
    """A wheel-safe combo whose current value remains readable when narrow."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._explicit_tooltip = ""
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.currentTextChanged.connect(self._update_current_tooltip)

    def wheelEvent(self, event) -> None:
        event.ignore()

    def setToolTip(self, text: str) -> None:  # noqa: N802 - Qt API spelling
        """Preserve caller help while also exposing the complete selected value."""

        self._explicit_tooltip = str(text or "").strip()
        self._update_current_tooltip()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API spelling
        if self.isEditable():
            super().paintEvent(_event)
            return
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        edit_rect = self.style().subControlRect(
            QStyle.CC_ComboBox,
            option,
            QStyle.SC_ComboBoxEditField,
            self,
        )
        available = max(0, edit_rect.width())
        if not option.currentIcon.isNull():
            icon_width = option.iconSize.width() or self.style().pixelMetric(
                QStyle.PM_SmallIconSize,
                option,
                self,
            )
            available = max(0, available - icon_width - 4)
        option.currentText = self.fontMetrics().elidedText(
            option.currentText,
            Qt.ElideRight,
            available,
        )
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        painter.drawControl(QStyle.CE_ComboBoxLabel, option)

    def _update_current_tooltip(self, *_args) -> None:
        current_text = str(self.currentText() or "").strip()
        item_tooltip = ""
        if self.currentIndex() >= 0:
            item_tooltip = str(
                self.itemData(self.currentIndex(), Qt.ToolTipRole) or ""
            ).strip()
        parts: list[str] = []
        for value in (current_text, item_tooltip, self._explicit_tooltip):
            if value and value not in parts:
                parts.append(value)
        super().setToolTip("\n\n".join(parts))


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelListWidget(QListWidget):
    def wheelEvent(self, event) -> None:
        event.ignore()
