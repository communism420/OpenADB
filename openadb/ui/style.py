from __future__ import annotations

from PySide6.QtWidgets import QApplication, QStyleFactory


LIGHT = """
QMainWindow, QWidget { background: #f7f7f7; color: #1f1f1f; font-family: "Segoe UI"; font-size: 10pt; }
QLabel { background: transparent; }
QListWidget#nav { background: #ffffff; border: 0; padding: 8px; }
QListWidget#nav::item { padding: 10px 12px; border-radius: 6px; }
QListWidget#nav::item:hover { background: #f0f6fb; }
QListWidget#nav::item:selected { background: #e5f1fb; color: #003e73; font-weight: 600; }
QWidget#navPanel { background: #ffffff; border: 0; }
QWidget#brandHeader { background: #ffffff; border: 0; }
QLabel#brandTitle { font-size: 15pt; font-weight: 650; color: #111827; }
QLabel#brandVersion { font-size: 8pt; color: #6b7280; padding-top: 0; }
QFrame#deviceStatusBar, QFrame#card, QFrame#toolbarCard, QFrame#wirelessGroup, QFrame#commandGroup, QGroupBox { background: #ffffff; border: 1px solid #e1e1e1; border-radius: 8px; }
QFrame#card { padding: 10px; }
QLabel#pageSubtitle { color: #606060; font-size: 10pt; padding-top: 8px; }
QFrame#connectionHero { background: #ffffff; border: 2px solid #9fc5e8; border-radius: 10px; }
QLabel#connectionStatusTitle { color: #111827; font-size: 20pt; font-weight: 650; }
QLabel#connectionDeviceName { color: #334155; font-size: 14pt; font-weight: 600; }
QLabel#connectionModeValue { color: #475569; font-weight: 600; }
QLabel#connectionMetaCaption { color: #64748b; font-size: 9pt; }
QLabel#connectionMetaValue { color: #1f2937; font-size: 11pt; font-weight: 600; }
QLabel#connectionStateBadge { background: #eef2f7; border: 1px solid #cbd5e1; border-radius: 9px; color: #334155; font-size: 9pt; font-weight: 700; padding: 4px 9px; }
QLabel#connectionStateBadge[connectionState="connected"] { background: #e8f5e9; border-color: #7fba83; color: #155724; }
QLabel#connectionStateBadge[connectionState="warning"] { background: #fff4ce; border-color: #d6a500; color: #5c4500; }
QLabel#connectionStateBadge[connectionState="error"] { background: #fde7e9; border-color: #d13438; color: #8a0a0e; }
QFrame#nextActionPanel { background: #f3f8fc; border: 1px solid #c9dceb; border-radius: 8px; }
QLabel#nextActionTitle { color: #334155; font-weight: 650; }
QLabel#nextActionText { color: #475569; }
QFrame#collapsibleCard { background: #ffffff; border: 1px solid #d9e0e7; border-radius: 8px; }
QWidget#collapsibleHeaderRow, QWidget#collapsibleContent, QWidget#wirelessActionPage { background: transparent; }
QToolButton#collapsibleHeader { background: transparent; border: 0; color: #1f2937; font-size: 11pt; font-weight: 650; padding: 3px; }
QToolButton#collapsibleHeader:hover { background: #edf5fb; border-radius: 5px; }
QLabel#collapsibleSummary { color: #64748b; font-size: 9pt; }
QLabel#detailCaption { color: #64748b; font-size: 9pt; }
QLabel#detailValue { color: #1f2937; }
QLabel#sectionDescription { color: #475569; }
QFrame#wirelessScenarioPanel { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 7px; }
QLabel#wirelessStatus { background: #f3f8fc; border: 1px solid #c9dceb; border-radius: 6px; color: #334155; padding: 7px 9px; }
QLabel#wirelessGroupTitle { color: #202020; font-weight: 600; padding: 0 0 2px 0; border: 0; background: transparent; }
QLabel#commandGroupTitle { color: #202020; font-weight: 600; padding: 0 0 2px 0; border: 0; background: transparent; }
QLabel#pageTitle { font-size: 22pt; font-weight: 600; padding: 8px 0; }
QLabel#appCountLabel { color: #606060; font-size: 10pt; padding-left: 10px; }
QLabel#cardCaption { color: #606060; font-size: 9pt; }
QLabel#cardValue { font-size: 13pt; font-weight: 600; }
QLabel#hintLabel { background: #fff8df; border: 1px solid #e6bc47; border-radius: 6px; padding: 8px 10px; color: #332800; }
QFrame#appsTopBar, QFrame#appsActionPanel { background: #ffffff; border: 1px solid #e1e1e1; border-radius: 6px; }
QFrame#fileManagerCenterPanel { background: #eeeeee; border: 1px solid #b8b8b8; border-radius: 2px; }
QFrame#fileManagerCenterSeparator { background: #b8b8b8; border: 0; }
QLabel#fileManagerSideTitle { color: #606060; font-weight: 600; padding-right: 4px; }
QLabel#fileManagerStatusLabel { background: #fff8df; border: 1px solid #d7a900; border-radius: 2px; padding: 7px 10px; color: #332800; }
QLabel#fileManagerAndroidSpaceLabel { background: #f2f2f2; border: 1px solid #b8b8b8; border-radius: 2px; padding: 6px 10px; color: #202020; }
QLineEdit#fileManagerPathEdit { font-family: "Consolas"; border-radius: 2px; min-height: 22px; padding: 3px 7px; }
QPushButton { background: #ffffff; border: 1px solid #c8c8c8; border-radius: 6px; padding: 6px 11px; color: #202020; }
QPushButton:hover { background: #f5f5f5; border-color: #9f9f9f; }
QPushButton:pressed { background: #e5e5e5; }
QPushButton:disabled { color: #8a8a8a; background: #f3f3f3; border-color: #dddddd; }
QPushButton[danger="true"] { border-color: #d13438; color: #a80000; }
QPushButton#primaryAction { background: #0f6cbd; border-color: #0f6cbd; color: #ffffff; font-weight: 600; }
QPushButton#primaryAction:hover { background: #115ea3; border-color: #115ea3; }
QPushButton#primaryAction:pressed { background: #0c3b5e; }
QPushButton#primaryAction:disabled { background: #d7e2eb; border-color: #c5d1db; color: #758391; }
QMenu { background: #ffffff; border: 1px solid #c8c8c8; color: #202020; padding: 4px; }
QMenu::item { border-radius: 4px; padding: 6px 24px 6px 10px; }
QMenu::item:selected { background: #e5f1fb; color: #003e73; }
QMenu::item:disabled { color: #8a8a8a; }
QPushButton#fileManagerArrowButton { min-height: 44px; font-size: 18pt; font-weight: 700; background: #f2f2f2; border: 1px solid #9f9f9f; border-radius: 2px; color: #111111; padding: 0; }
QPushButton#fileManagerArrowButton:hover { background: #ffffff; border-color: #777777; }
QPushButton#fileManagerCompactButton { min-height: 28px; background: #f2f2f2; border: 1px solid #9f9f9f; border-radius: 2px; padding: 3px 4px; color: #111111; }
QPushButton#fileManagerCompactButton:hover { background: #ffffff; border-color: #777777; }
QToolButton#fileManagerNavButton { min-width: 34px; border-radius: 2px; padding: 4px 8px; }
QToolButton { background: #ffffff; border: 1px solid #c8c8c8; border-radius: 6px; padding: 5px 8px; color: #202020; }
QToolButton:hover { background: #f5f5f5; border-color: #9f9f9f; }
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit { background: #ffffff; border: 1px solid #cfcfcf; border-radius: 6px; padding: 5px; color: #202020; selection-background-color: #cce8ff; }
QTableWidget, QTreeView { background: #ffffff; alternate-background-color: #f6f6f6; color: #202020; border: 1px solid #d7d7d7; border-radius: 6px; gridline-color: transparent; selection-background-color: #dbeafe; selection-color: #111827; padding: 0; }
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #eeeeee; }
QTableWidget::item:selected { background: #dbeafe; color: #111827; }
QTreeView::item { padding: 4px 6px; border-bottom: 1px solid #eeeeee; }
QTreeView::item:selected { background: #dbeafe; color: #111827; }
QTableWidget#appsTable { background: #ffffff; alternate-background-color: #ffffff; border-radius: 6px; font-size: 10pt; }
QTableWidget#appsTable::viewport { background: #ffffff; }
QTableWidget#appsTable::item { border-bottom: 1px solid #e8e8e8; padding: 0 8px; }
QTableWidget#appsTable::item:selected { background: #e5f1fb; color: #111827; }
QTableWidget#appsTable QHeaderView { background: #f3f3f3; border: 0; }
QTableWidget#appsTable QHeaderView::section { background: #f3f3f3; color: #202020; border-bottom: 1px solid #d8d8d8; padding: 8px; }
QTableWidget#fileManagerAndroidTable { background: #ffffff; alternate-background-color: #f5f5f5; border: 1px solid #b8b8b8; border-radius: 0; gridline-color: #d8d8d8; }
QTableWidget#fileManagerAndroidTable::item { padding: 3px 8px; border-bottom: 1px solid #d8d8d8; }
QTableWidget#fileManagerAndroidTable::item:selected { background: #d8d8d8; color: #111111; }
QTableWidget#fileManagerAndroidTable QHeaderView::section { background: #eeeeee; color: #111111; border: 0; border-bottom: 1px solid #b8b8b8; padding: 6px 8px; }
QCheckBox, QRadioButton { background: transparent; border: 0; padding: 2px 6px; spacing: 6px; color: #202020; }
QCheckBox::indicator, QRadioButton::indicator { width: 13px; height: 13px; border: 1px solid #8a8a8a; background: #ffffff; }
QCheckBox::indicator { border-radius: 2px; }
QRadioButton::indicator { border-radius: 7px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #0f6cbd; border-color: #0f6cbd; }
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
QWidget#navPanel { background: #1f1f1f; border: 0; }
QWidget#brandHeader { background: #1f1f1f; border: 0; }
QLabel#brandTitle { font-size: 15pt; font-weight: 650; color: #ffffff; }
QLabel#brandVersion { font-size: 8pt; color: #9aa4af; padding-top: 0; }
QFrame#deviceStatusBar, QFrame#card, QFrame#toolbarCard, QFrame#wirelessGroup, QFrame#commandGroup, QGroupBox { background: #282828; border: 1px solid #3a3a3a; border-radius: 8px; }
QFrame#card { padding: 10px; }
QLabel#pageSubtitle { color: #aab3bd; font-size: 10pt; padding-top: 8px; }
QFrame#connectionHero { background: #282828; border: 2px solid #2f6f9f; border-radius: 10px; }
QLabel#connectionStatusTitle { color: #ffffff; font-size: 20pt; font-weight: 650; }
QLabel#connectionDeviceName { color: #dbe7f3; font-size: 14pt; font-weight: 600; }
QLabel#connectionModeValue { color: #c2ccd6; font-weight: 600; }
QLabel#connectionMetaCaption { color: #9aa8b6; font-size: 9pt; }
QLabel#connectionMetaValue { color: #f2f2f2; font-size: 11pt; font-weight: 600; }
QLabel#connectionStateBadge { background: #30363d; border: 1px solid #59636e; border-radius: 9px; color: #d8e0e8; font-size: 9pt; font-weight: 700; padding: 4px 9px; }
QLabel#connectionStateBadge[connectionState="connected"] { background: #173d24; border-color: #3c8c52; color: #b8f2c5; }
QLabel#connectionStateBadge[connectionState="warning"] { background: #4a390c; border-color: #a98212; color: #ffe7a0; }
QLabel#connectionStateBadge[connectionState="error"] { background: #4a1e21; border-color: #c94d52; color: #ffc4c7; }
QFrame#nextActionPanel { background: #202d38; border: 1px solid #35546d; border-radius: 8px; }
QLabel#nextActionTitle { color: #dce9f5; font-weight: 650; }
QLabel#nextActionText { color: #bac8d5; }
QFrame#collapsibleCard { background: #282828; border: 1px solid #3f4851; border-radius: 8px; }
QWidget#collapsibleHeaderRow, QWidget#collapsibleContent, QWidget#wirelessActionPage { background: transparent; }
QToolButton#collapsibleHeader { background: transparent; border: 0; color: #f2f2f2; font-size: 11pt; font-weight: 650; padding: 3px; }
QToolButton#collapsibleHeader:hover { background: #343e47; border-radius: 5px; }
QLabel#collapsibleSummary { color: #9aa8b6; font-size: 9pt; }
QLabel#detailCaption { color: #9aa8b6; font-size: 9pt; }
QLabel#detailValue { color: #f2f2f2; }
QLabel#sectionDescription { color: #bac8d5; }
QFrame#wirelessScenarioPanel { background: #23292f; border: 1px solid #3f4851; border-radius: 7px; }
QLabel#wirelessStatus { background: #202d38; border: 1px solid #35546d; border-radius: 6px; color: #dce9f5; padding: 7px 9px; }
QLabel#wirelessGroupTitle { color: #f2f2f2; font-weight: 600; padding: 0 0 2px 0; border: 0; background: transparent; }
QLabel#commandGroupTitle { color: #f2f2f2; font-weight: 600; padding: 0 0 2px 0; border: 0; background: transparent; }
QLabel#pageTitle { font-size: 22pt; font-weight: 600; padding: 8px 0; }
QLabel#appCountLabel { color: #b8b8b8; font-size: 10pt; padding-left: 10px; }
QLabel#cardCaption { color: #b8b8b8; font-size: 9pt; }
QLabel#cardValue { font-size: 13pt; font-weight: 600; }
QLabel#hintLabel { background: #342b12; border: 1px solid #7f6416; border-radius: 6px; padding: 8px 10px; color: #ffe8a3; }
QFrame#appsTopBar, QFrame#appsActionPanel { background: #303437; border: 1px solid #4a4f55; border-radius: 6px; }
QFrame#fileManagerCenterPanel { background: #151515; border: 1px solid #404040; border-radius: 2px; }
QFrame#fileManagerCenterSeparator { background: #404040; border: 0; }
QLabel#fileManagerSideTitle { color: #b8c0cc; font-weight: 600; padding-right: 4px; }
QLabel#fileManagerStatusLabel { background: #342b12; border: 1px solid #7f6416; border-radius: 2px; padding: 7px 10px; color: #ffe8a3; }
QLabel#fileManagerAndroidSpaceLabel { background: #252525; border: 1px solid #404040; border-radius: 2px; padding: 6px 10px; color: #d8d8d8; }
QLineEdit#fileManagerPathEdit { font-family: "Consolas"; background: #202020; border: 1px solid #404040; border-radius: 2px; min-height: 22px; padding: 3px 7px; color: #ffffff; }
QPushButton { background: #2d2d2d; border: 1px solid #4a4a4a; border-radius: 6px; padding: 6px 11px; color: #f2f2f2; }
QPushButton:hover { background: #383838; border-color: #666666; }
QPushButton:pressed { background: #424242; }
QPushButton:disabled { color: #777777; background: #282828; border-color: #363636; }
QPushButton[danger="true"] { border-color: #ff8a80; color: #ffb4ab; }
QPushButton#primaryAction { background: #0f6cbd; border-color: #2b88d8; color: #ffffff; font-weight: 600; }
QPushButton#primaryAction:hover { background: #1679c4; border-color: #4aa3e8; }
QPushButton#primaryAction:pressed { background: #0c4f7c; }
QPushButton#primaryAction:disabled { background: #34414c; border-color: #46535e; color: #7f8b96; }
QMenu { background: #2a2a2a; border: 1px solid #4a4a4a; color: #f2f2f2; padding: 4px; }
QMenu::item { border-radius: 4px; padding: 6px 24px 6px 10px; }
QMenu::item:selected { background: #164b76; color: #ffffff; }
QMenu::item:disabled { color: #777777; }
QPushButton#fileManagerArrowButton { min-height: 44px; font-size: 18pt; font-weight: 700; background: #404040; border: 1px solid #050505; border-radius: 2px; color: #ffffff; padding: 0; }
QPushButton#fileManagerArrowButton:hover { background: #505050; border-color: #6f6f6f; }
QPushButton#fileManagerCompactButton { min-height: 28px; background: #303030; border: 1px solid #050505; border-radius: 2px; padding: 3px 4px; color: #ffffff; }
QPushButton#fileManagerCompactButton:hover { background: #404040; border-color: #606060; }
QToolButton#fileManagerNavButton { min-width: 34px; border-radius: 2px; padding: 4px 8px; }
QToolButton { background: #2d2d2d; border: 1px solid #4a4a4a; border-radius: 6px; padding: 5px 8px; color: #f2f2f2; }
QToolButton:hover { background: #383838; border-color: #666666; }
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit { background: #2a2a2a; border: 1px solid #4a4a4a; border-radius: 6px; padding: 5px; color: #f2f2f2; selection-background-color: #164b76; }
QTableWidget, QTreeView { background: #202020; alternate-background-color: #272727; color: #f2f2f2; border: 1px solid #3a3a3a; border-radius: 6px; gridline-color: transparent; selection-background-color: #164b76; selection-color: #ffffff; padding: 0; }
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #303030; }
QTableWidget::item:alternate { background: #272727; }
QTableWidget::item:selected { background: #164b76; color: #ffffff; }
QTreeView::item { padding: 4px 6px; border-bottom: 1px solid #303030; }
QTreeView::item:selected { background: #164b76; color: #ffffff; }
QTableWidget#appsTable { background: #34383b; alternate-background-color: #34383b; border: 1px solid #4a4f55; border-radius: 6px; font-size: 10pt; }
QTableWidget#appsTable::viewport { background: #34383b; }
QTableWidget#appsTable::item { background: #34383b; border-bottom: 1px solid #151719; padding: 0 8px; }
QTableWidget#appsTable::item:selected { background: #164b76; color: #ffffff; }
QTableWidget#appsTable QHeaderView { background: #303437; border: 0; }
QTableWidget#appsTable QHeaderView::section { background: #303437; color: #ffffff; border-bottom: 1px solid #141414; padding: 8px; }
QTableWidget#fileManagerAndroidTable { background: #202020; alternate-background-color: #252525; border: 1px solid #404040; border-radius: 0; gridline-color: #101010; }
QTableWidget#fileManagerAndroidTable::item { padding: 3px 8px; border-bottom: 1px solid #101010; }
QTableWidget#fileManagerAndroidTable::item:selected { background: #6a6a6a; color: #ffffff; }
QTableWidget#fileManagerAndroidTable QHeaderView::section { background: #404040; color: #ffffff; border: 0; border-bottom: 1px solid #101010; padding: 6px 8px; }
QCheckBox, QRadioButton { background: transparent; border: 0; padding: 2px 6px; spacing: 6px; color: #f2f2f2; }
QCheckBox::indicator, QRadioButton::indicator { width: 13px; height: 13px; border: 1px solid #9aa4af; background: #24272a; }
QCheckBox::indicator { border-radius: 2px; }
QRadioButton::indicator { border-radius: 7px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: #4cc2ff; border-color: #4cc2ff; }
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
