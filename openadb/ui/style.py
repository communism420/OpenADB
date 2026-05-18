from __future__ import annotations

from PySide6.QtWidgets import QApplication, QStyleFactory


LIGHT = """
QMainWindow, QWidget { background: #f7f7f7; color: #1f1f1f; font-family: "Segoe UI"; font-size: 10pt; }
QLabel { background: transparent; }
QListWidget#nav { background: #ffffff; border: 0; padding: 8px; }
QListWidget#nav::item { padding: 10px 12px; border-radius: 6px; }
QListWidget#nav::item:hover { background: #f0f6fb; }
QListWidget#nav::item:selected { background: #e5f1fb; color: #003e73; font-weight: 600; }
QFrame#deviceStatusBar, QFrame#card, QFrame#toolbarCard, QGroupBox { background: #ffffff; border: 1px solid #e1e1e1; border-radius: 8px; }
QFrame#card { padding: 10px; }
QLabel#pageTitle { font-size: 22pt; font-weight: 600; padding: 8px 0; }
QLabel#cardCaption { color: #606060; font-size: 9pt; }
QLabel#cardValue { font-size: 13pt; font-weight: 600; }
QLabel#hintLabel { background: #fff8df; border: 1px solid #e6bc47; border-radius: 6px; padding: 8px 10px; color: #332800; }
QPushButton { background: #ffffff; border: 1px solid #c8c8c8; border-radius: 6px; padding: 6px 11px; color: #202020; }
QPushButton:hover { background: #f5f5f5; border-color: #9f9f9f; }
QPushButton:pressed { background: #e5e5e5; }
QPushButton:disabled { color: #8a8a8a; background: #f3f3f3; border-color: #dddddd; }
QPushButton[danger="true"] { border-color: #d13438; color: #a80000; }
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit { background: #ffffff; border: 1px solid #cfcfcf; border-radius: 6px; padding: 5px; color: #202020; selection-background-color: #cce8ff; }
QTableWidget, QTreeView { background: #ffffff; alternate-background-color: #f6f6f6; color: #202020; border: 1px solid #d7d7d7; border-radius: 6px; gridline-color: transparent; selection-background-color: #dbeafe; selection-color: #111827; padding: 0; }
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #eeeeee; }
QTableWidget::item:selected { background: #dbeafe; color: #111827; }
QTreeView::item { padding: 4px 6px; border-bottom: 1px solid #eeeeee; }
QTreeView::item:selected { background: #dbeafe; color: #111827; }
QTableWidget#appsTable { font-size: 9.5pt; }
QHeaderView::section { background: #f3f3f3; border: 0; border-bottom: 1px solid #d0d0d0; padding: 7px 8px; color: #202020; font-weight: 600; }
QProgressBar { background: #eeeeee; border: 1px solid #cccccc; border-radius: 6px; text-align: center; color: #202020; }
QProgressBar::chunk { background: #0f6cbd; border-radius: 5px; }
QScrollBar:vertical, QScrollBar:horizontal { background: transparent; width: 12px; height: 12px; margin: 0; }
QScrollBar::handle { background: #c8c8c8; border-radius: 6px; min-height: 26px; min-width: 26px; }
QScrollBar::handle:hover { background: #a8a8a8; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QStatusBar { background: #ffffff; border-top: 1px solid #e5e5e5; }
"""


DARK = """
QMainWindow, QWidget { background: #1f1f1f; color: #f2f2f2; font-family: "Segoe UI"; font-size: 10pt; }
QLabel { background: transparent; }
QListWidget#nav { background: #242424; border: 1px solid #303030; border-radius: 6px; padding: 6px; }
QListWidget#nav::item { padding: 10px 12px; border-radius: 6px; }
QListWidget#nav::item:hover { background: #303030; }
QListWidget#nav::item:selected { background: #164b76; color: #ffffff; font-weight: 600; }
QFrame#deviceStatusBar, QFrame#card, QFrame#toolbarCard, QGroupBox { background: #282828; border: 1px solid #3a3a3a; border-radius: 8px; }
QFrame#card { padding: 10px; }
QLabel#pageTitle { font-size: 22pt; font-weight: 600; padding: 8px 0; }
QLabel#cardCaption { color: #b8b8b8; font-size: 9pt; }
QLabel#cardValue { font-size: 13pt; font-weight: 600; }
QLabel#hintLabel { background: #342b12; border: 1px solid #7f6416; border-radius: 6px; padding: 8px 10px; color: #ffe8a3; }
QPushButton { background: #2d2d2d; border: 1px solid #4a4a4a; border-radius: 6px; padding: 6px 11px; color: #f2f2f2; }
QPushButton:hover { background: #383838; border-color: #666666; }
QPushButton:pressed { background: #424242; }
QPushButton:disabled { color: #777777; background: #282828; border-color: #363636; }
QPushButton[danger="true"] { border-color: #ff8a80; color: #ffb4ab; }
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit { background: #2a2a2a; border: 1px solid #4a4a4a; border-radius: 6px; padding: 5px; color: #f2f2f2; selection-background-color: #164b76; }
QTableWidget, QTreeView { background: #202020; alternate-background-color: #272727; color: #f2f2f2; border: 1px solid #3a3a3a; border-radius: 6px; gridline-color: transparent; selection-background-color: #164b76; selection-color: #ffffff; padding: 0; }
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #303030; }
QTableWidget::item:alternate { background: #272727; }
QTableWidget::item:selected { background: #164b76; color: #ffffff; }
QTreeView::item { padding: 4px 6px; border-bottom: 1px solid #303030; }
QTreeView::item:selected { background: #164b76; color: #ffffff; }
QTableWidget#appsTable { font-size: 9.5pt; }
QHeaderView::section { background: #303030; border: 0; border-bottom: 1px solid #4a4a4a; padding: 7px 8px; color: #f2f2f2; font-weight: 600; }
QProgressBar { background: #2a2a2a; border: 1px solid #4a4a4a; border-radius: 6px; text-align: center; color: #f2f2f2; }
QProgressBar::chunk { background: #4cc2ff; border-radius: 5px; }
QScrollBar:vertical, QScrollBar:horizontal { background: transparent; width: 12px; height: 12px; margin: 0; }
QScrollBar::handle { background: #555555; border-radius: 6px; min-height: 26px; min-width: 26px; }
QScrollBar::handle:hover { background: #6a6a6a; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QStatusBar { background: #181818; border-top: 1px solid #333333; color: #f2f2f2; }
"""


def apply_theme(app: QApplication, theme: str) -> None:
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    if theme == "Dark":
        app.setStyleSheet(DARK)
    elif theme == "Light":
        app.setStyleSheet(LIGHT)
    else:
        app.setStyleSheet(DARK if _system_prefers_dark() else LIGHT)


def _system_prefers_dark() -> bool:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        palette = QApplication.palette()
        return palette.window().color().lightness() < 128
