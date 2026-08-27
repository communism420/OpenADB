from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractButton, QLabel, QWidget


@dataclass(frozen=True, slots=True)
class TextOverflowIssue:
    kind: str
    object_name: str
    widget_type: str
    text: str
    available: tuple[int, int]
    required: tuple[int, int]

    def __str__(self) -> str:
        name = self.object_name or "<unnamed>"
        return (
            f"{self.kind}: {self.widget_type}#{name} needs {self.required}, "
            f"has {self.available}: {self.text!r}"
        )


def find_text_overflow(root: QWidget, *, tolerance: int = 2) -> list[TextOverflowIssue]:
    """Audit visible Qt labels/buttons after a real layout pass.

    Callers should show the root (``WA_DontShowOnScreen`` is fine), process Qt
    events, and run this at each target width/theme. Qt reports geometry in
    device-independent pixels, so the same checks remain valid under Windows
    DPI scaling.
    """

    issues: list[TextOverflowIssue] = []
    widgets = [root, *root.findChildren(QWidget)]
    for widget in widgets:
        if not widget.isVisibleTo(root):
            continue
        if isinstance(widget, QLabel):
            issues.extend(_label_issues(widget, tolerance=tolerance))
        elif isinstance(widget, QAbstractButton):
            issue = _button_issue(widget, tolerance=tolerance)
            if issue is not None:
                issues.append(issue)
    return issues


def _label_issues(label: QLabel, *, tolerance: int) -> list[TextOverflowIssue]:
    text = str(label.text() or "")
    if not text or label.pixmap() is not None or label.textFormat() == Qt.RichText:
        return []
    width = max(0, label.contentsRect().width())
    height = max(0, label.contentsRect().height())
    if width <= 0 or height <= 0:
        return [
            _issue(label, "zero-text-geometry", text, (width, height), (1, 1))
        ]

    if label.wordWrap():
        required_height = label.heightForWidth(label.width())
        if required_height > label.height() + tolerance:
            kind = (
                "fixed-height-wrapped-label"
                if label.minimumHeight() == label.maximumHeight()
                else "wrapped-label-height"
            )
            return [
                _issue(
                    label,
                    kind,
                    text,
                    (width, height),
                    (width, required_height),
                )
            ]
        return []

    required_width = max(
        (label.fontMetrics().horizontalAdvance(line) for line in text.splitlines()),
        default=0,
    )
    if required_width > width + tolerance:
        return [
            _issue(
                label,
                "unwrapped-label-width",
                text,
                (width, height),
                (required_width, height),
            )
        ]
    return []


def _button_issue(button: QAbstractButton, *, tolerance: int) -> TextOverflowIssue | None:
    text = str(button.text() or "")
    if not text:
        return None
    width = max(0, button.contentsRect().width())
    height = max(0, button.contentsRect().height())
    required_width = button.fontMetrics().horizontalAdvance(text)
    if required_width <= width + tolerance:
        return None
    return _issue(
        button,
        "button-text-width",
        text,
        (width, height),
        (required_width, height),
    )


def _issue(
    widget: QWidget,
    kind: str,
    text: str,
    available: tuple[int, int],
    required: tuple[int, int],
) -> TextOverflowIssue:
    return TextOverflowIssue(
        kind=kind,
        object_name=widget.objectName(),
        widget_type=type(widget).__name__,
        text=text,
        available=available,
        required=required,
    )
