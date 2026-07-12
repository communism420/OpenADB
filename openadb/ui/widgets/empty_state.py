from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QStyle, QVBoxLayout, QWidget

from openadb.ui.design_system import set_button_role


class EmptyState(QFrame):
    """Consistent short empty-state message with exactly one next action."""

    action_requested = Signal()

    def __init__(
        self,
        title: str,
        description: str,
        action_text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        self.setProperty("stateKind", "neutral")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignCenter)

        self.icon = QLabel()
        self.icon.setObjectName("emptyStateIcon")
        self.icon.setAlignment(Qt.AlignCenter)
        self.icon.setPixmap(self.style().standardIcon(QStyle.SP_FileDialogInfoView).pixmap(28, 28))
        self.title_label = QLabel(title)
        self.title_label.setObjectName("emptyStateTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        self.title_label.setWordWrap(True)
        self.description_label = QLabel(description)
        self.description_label.setObjectName("emptyStateDescription")
        self.description_label.setAlignment(Qt.AlignCenter)
        self.description_label.setWordWrap(True)
        self.action_button = QPushButton(action_text)
        self.action_button.setObjectName("primaryAction")
        self.action_button.setVisible(bool(action_text))
        self.action_button.setDefault(bool(action_text))
        set_button_role(self.action_button, "primary")
        self.action_button.clicked.connect(self.action_requested.emit)

        layout.addStretch()
        layout.addWidget(self.icon)
        layout.addWidget(self.title_label)
        layout.addWidget(self.description_label)
        layout.addWidget(self.action_button, 0, Qt.AlignCenter)
        layout.addStretch()
        self.setAccessibleName(title)

    def set_content(
        self,
        title: str,
        description: str,
        action_text: str = "",
        kind: str = "neutral",
    ) -> None:
        self.title_label.setText(title)
        self.description_label.setText(description)
        self.action_button.setText(action_text)
        self.action_button.setVisible(bool(action_text))
        self.action_button.setDefault(bool(action_text))
        self.setProperty("stateKind", kind)
        self.setAccessibleName(title)
        self.style().unpolish(self)
        self.style().polish(self)
