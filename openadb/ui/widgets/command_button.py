from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QPushButton


class CommandButton(QPushButton):
    triggered = Signal(object)

    def __init__(self, title: str, spec: dict, dangerous: bool = False, parent=None) -> None:
        super().__init__(title, parent)
        self.spec = spec
        self.dangerous = dangerous
        self.setMinimumHeight(34)
        if dangerous:
            self.setProperty("danger", True)
            self.setToolTip(spec.get("risk", "This command requires confirmation."))
        else:
            self.setToolTip(spec.get("description", title))
        self.clicked.connect(lambda: self.triggered.emit(self.spec))
