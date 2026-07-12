from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from openadb.ui.material_icons import material_icon
from openadb.ui.widgets.elided_label import ElidedLabel


class CollapsibleCard(QFrame):
    """Reusable card with a compact header and an optional content area."""

    expanded_changed = Signal(bool)

    def __init__(
        self,
        title: str,
        summary: str = "",
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._expanded = False
        self.setObjectName("collapsibleCard")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("collapsibleHeaderRow")
        header.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 8, 10, 8)
        header_layout.setSpacing(8)

        self.toggle_button = QToolButton(header)
        self.toggle_button.setObjectName("collapsibleHeader")
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setAccessibleName(title)
        self.toggle_button.clicked.connect(self._toggle_clicked)
        header_layout.addWidget(self.toggle_button)

        self.summary_label = ElidedLabel(summary, header, Qt.ElideRight)
        self.summary_label.setObjectName("collapsibleSummary")
        header_layout.addWidget(self.summary_label, 1)
        outer.addWidget(header)

        self.content_widget = QWidget(self)
        self.content_widget.setObjectName("collapsibleContent")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(12, 4, 12, 12)
        self.content_layout.setSpacing(10)
        outer.addWidget(self.content_widget)

        self.set_expanded(expanded, notify=False)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool, notify: bool = True) -> None:
        expanded = bool(expanded)
        changed = expanded != self._expanded
        self._expanded = expanded
        self.toggle_button.blockSignals(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.blockSignals(False)
        self.toggle_button.setArrowType(Qt.NoArrow)
        self.toggle_button.setIcon(material_icon("expand_more" if expanded else "chevron_right"))
        self.content_widget.setVisible(expanded)
        self.setSizePolicy(
            QSizePolicy.Preferred,
            QSizePolicy.Preferred if expanded else QSizePolicy.Maximum,
        )
        self.updateGeometry()
        if changed and notify:
            self.expanded_changed.emit(expanded)

    def set_summary(self, summary: str) -> None:
        self.summary_label.setText(summary)

    def refresh_material_icons(self) -> None:
        self.toggle_button.setIcon(material_icon("expand_more" if self._expanded else "chevron_right"))

    def _toggle_clicked(self, checked: bool) -> None:
        self.set_expanded(checked)
