from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QListWidget, QSpinBox


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelListWidget(QListWidget):
    def wheelEvent(self, event) -> None:
        event.ignore()
