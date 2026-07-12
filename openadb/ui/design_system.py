from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QDialog, QLayout, QPushButton, QToolButton, QWidget


@dataclass(frozen=True, slots=True)
class LayoutTokens:
    page_margins: tuple[int, int, int, int] = (16, 12, 16, 12)
    page_spacing: int = 10
    section_spacing: int = 10
    card_padding: int = 12
    control_radius: int = 6
    card_radius: int = 8
    button_height: int = 32
    compact_button_height: int = 28
    input_height: int = 30
    table_row_height: int = 30


@dataclass(frozen=True, slots=True)
class TypographyTokens:
    page_title_pt: int = 22
    card_title_pt: int = 11
    secondary_text_pt: int = 9


@dataclass(frozen=True, slots=True)
class ColorTokens:
    canvas: str
    surface: str
    surface_alt: str
    text: str
    text_secondary: str
    border: str
    primary: str
    primary_hover: str
    primary_text: str
    success: str
    success_surface: str
    warning: str
    warning_surface: str
    danger: str
    danger_surface: str
    focus: str
    focus_surface: str
    disabled_text: str
    disabled_surface: str
    selection: str
    selection_text: str
    tooltip_surface: str
    tooltip_text: str
    link: str


LAYOUT = LayoutTokens()
TYPOGRAPHY = TypographyTokens()

LIGHT_COLORS = ColorTokens(
    canvas="#f7f7f7",
    surface="#ffffff",
    surface_alt="#f3f4f6",
    text="#1f1f1f",
    text_secondary="#52606d",
    border="#c8cfd6",
    primary="#0f6cbd",
    primary_hover="#115ea3",
    primary_text="#ffffff",
    success="#155724",
    success_surface="#e8f5e9",
    warning="#5c4500",
    warning_surface="#fff4ce",
    danger="#8a0a0e",
    danger_surface="#fde7e9",
    focus="#005fb8",
    focus_surface="#e5f1fb",
    disabled_text="#737373",
    disabled_surface="#ededed",
    selection="#dbeafe",
    selection_text="#111827",
    tooltip_surface="#202020",
    tooltip_text="#ffffff",
    link="#005ea6",
)

DARK_COLORS = ColorTokens(
    canvas="#1f1f1f",
    surface="#282828",
    surface_alt="#303030",
    text="#f2f2f2",
    text_secondary="#b8c0cc",
    border="#525a63",
    primary="#1679c4",
    primary_hover="#2389d7",
    primary_text="#ffffff",
    success="#b8f2c5",
    success_surface="#173d24",
    warning="#ffe7a0",
    warning_surface="#4a390c",
    danger="#ffc4c7",
    danger_surface="#4a1e21",
    focus="#60cdff",
    focus_surface="#163b50",
    disabled_text="#909090",
    disabled_surface="#292929",
    selection="#164b76",
    selection_text="#ffffff",
    tooltip_surface="#f2f2f2",
    tooltip_text="#202020",
    link="#8ac7f5",
)


def configure_page_layout(layout: QLayout) -> None:
    layout.setContentsMargins(*LAYOUT.page_margins)
    layout.setSpacing(LAYOUT.page_spacing)


def set_button_role(button: QWidget, role: str = "secondary", compact: bool = False) -> None:
    button.setProperty("uiRole", role)
    button.setProperty("compact", bool(compact))
    if isinstance(button, (QPushButton, QToolButton)):
        button.setMinimumHeight(LAYOUT.compact_button_height if compact else LAYOUT.button_height)


def configure_icon_button(button: QToolButton, tooltip: str) -> None:
    """Apply the mandatory accessibility contract for an icon-only button."""
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setProperty("iconOnly", True)
    button.setMinimumSize(LAYOUT.button_height, LAYOUT.button_height)


def configure_dialog(dialog: QDialog, accessible_name: str = "") -> None:
    dialog.setObjectName("appDialog")
    dialog.setSizeGripEnabled(True)
    if accessible_name:
        dialog.setAccessibleName(accessible_name)
