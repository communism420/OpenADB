from __future__ import annotations

from PySide6.QtWidgets import QApplication, QStyleFactory

from openadb.ui.design_system import LAYOUT, TYPOGRAPHY, DARK_COLORS, LIGHT_COLORS, ColorTokens


_LIGHT_BASE = """
QMainWindow, QWidget { background: #f7f7f7; color: #1f1f1f; font-family: "Segoe UI"; font-size: 10pt; }
QLabel { background: transparent; }
QListWidget#nav { background: #ffffff; border: 0; padding: 8px; }
QListWidget#nav::item { padding: 10px 12px; border-radius: 6px; }
QListWidget#nav::item:hover { background: #f0f6fb; }
QListWidget#nav::item:selected { background: #e5f1fb; color: #003e73; font-weight: 600; }
QListWidget#nav[collapsed="true"]::item { padding: 10px 4px; }
QToolButton#navToggle { border: 0; min-width: 32px; min-height: 28px; }
QWidget#navPanel { background: #ffffff; border: 0; }
QWidget#brandHeader { background: #ffffff; border: 0; }
QLabel#brandTitle { font-size: 15pt; font-weight: 650; color: #111827; }
QLabel#brandVersion { font-size: 8pt; color: #6b7280; padding-top: 0; }
QFrame#deviceStatusBar, QFrame#card, QFrame#toolbarCard, QFrame#wirelessGroup, QFrame#commandGroup, QGroupBox { background: #ffffff; border: 1px solid #e1e1e1; border-radius: 8px; }
QLabel#statusSummary { color: #111827; font-weight: 700; }
QLabel#statusDeviceName { color: #334155; font-weight: 600; }
QLabel#statusMode { background: #eef2f7; border: 1px solid #cbd5e1; border-radius: 8px; color: #334155; font-size: 9pt; font-weight: 700; padding: 3px 7px; }
QLabel#statusState { color: #64748b; font-size: 9pt; }
QToolButton#deviceDetailsButton { padding-left: 7px; padding-right: 7px; }
QFrame#card { padding: 10px; }
QFrame#settingsSection { background: #ffffff; border: 1px solid #d9e0e7; border-radius: 8px; }
QLabel#settingsSectionTitle { color: #1f2937; font-size: 12pt; font-weight: 650; }
QLabel#settingsStatusValue { color: #0f5f9f; font-weight: 700; }
QLabel#settingsVerificationResult { background: #f3f8fc; border: 1px solid #c9dceb; border-radius: 5px; color: #334155; padding: 6px 8px; }
QFrame#commandToolbar, QFrame#commandDetailsPanel, QFrame#commandOutputPanel { background: #ffffff; border: 1px solid #d9e0e7; border-radius: 8px; }
QLabel#commandCount { color: #64748b; }
QLabel#commandDetailsTitle { color: #1f2937; font-size: 12pt; font-weight: 650; }
QLabel#commandActualText { background: #f3f4f6; border: 1px solid #d1d5db; border-radius: 5px; color: #111827; font-family: "Consolas"; padding: 7px; }
QLabel#commandMetadata { color: #475569; font-size: 9pt; }
QLabel#commandRiskBadge { background: #edf7ed; border: 1px solid #9fc79f; border-radius: 7px; color: #155724; font-weight: 650; padding: 4px 7px; }
QLabel#commandRiskBadge[riskLevel="Changes device state"] { background: #fff8df; border-color: #d7a900; color: #5c4700; }
QLabel#commandRiskBadge[riskLevel="May erase data"], QLabel#commandRiskBadge[riskLevel="Critical"] { background: #fde7e9; border-color: #d13438; color: #8a0a0e; }
QLabel#commandAvailability { color: #475569; }
QLabel#commandOutputStatus { background: #f3f4f6; border: 1px solid #d1d5db; border-radius: 7px; color: #334155; font-weight: 650; padding: 3px 7px; }
QLabel#commandOutputStatus[resultState="running"] { background: #e5f1fb; border-color: #7aa7cc; color: #003e73; }
QLabel#commandOutputStatus[resultState="success"] { background: #edf7ed; border-color: #9fc79f; color: #155724; }
QLabel#commandOutputStatus[resultState="error"], QLabel#commandOutputStatus[resultState="cancelled"] { background: #fde7e9; border-color: #d13438; color: #8a0a0e; }
QTreeWidget#commandTree { border-radius: 8px; }
QTreeWidget#commandTree::item { min-height: 24px; }
QSplitter#commandsMainSplitter::handle, QSplitter#commandsBrowserSplitter::handle { background: #d5dbe1; border-radius: 2px; }
QSplitter#commandsMainSplitter::handle:hover, QSplitter#commandsBrowserSplitter::handle:hover { background: #9fc5e8; }
QFrame#commandOutputPanel QPlainTextEdit { font-family: "Consolas"; }
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
QLabel#appFilterSummary { color: #0f5f9f; font-size: 9pt; padding-left: 8px; }
QLabel#appsSelectionSummary { color: #475569; font-size: 9pt; padding-left: 4px; }
QToolButton#appsFiltersButton { background: #ffffff; border: 1px solid #c8c8c8; border-radius: 6px; color: #202020; padding: 6px 11px; }
QToolButton#appsFiltersButton:hover { background: #f5f5f5; border-color: #9f9f9f; }
QPushButton#appsResetFilters { padding-left: 9px; padding-right: 9px; }
QLabel#cardCaption { color: #606060; font-size: 9pt; }
QLabel#cardValue { font-size: 13pt; font-weight: 600; }
QLabel#hintLabel { background: #fff8df; border: 1px solid #e6bc47; border-radius: 6px; padding: 8px 10px; color: #332800; }
QFrame#appsTopBar, QFrame#appsBulkActionBar { background: #ffffff; border: 1px solid #e1e1e1; border-radius: 6px; }
QFrame#fileManagerCenterPanel { background: #eeeeee; border: 1px solid #b8b8b8; border-radius: 2px; }
QFrame#fileManagerCenterSeparator { background: #b8b8b8; border: 0; }
QSplitter#fileManagerSplitter::handle { background: #d5d5d5; border-radius: 2px; }
QSplitter#fileManagerSplitter::handle:hover { background: #9fc5e8; }
QLabel#fileManagerActionGroupTitle { color: #4b5563; font-size: 9pt; font-weight: 700; padding: 2px 1px; }
QLabel#fileManagerRootStatus { color: #606060; font-size: 8pt; padding: 2px; }
QLabel#fileManagerRootStatus[rootState="granted"] { color: #155724; }
QLabel#fileManagerRootStatus[rootState="denied"], QLabel#fileManagerRootStatus[rootState="unavailable"] { color: #8a0a0e; }
QLabel#fileManagerSideTitle { color: #606060; font-weight: 600; padding-right: 4px; }
QLabel#fileManagerStatusLabel { background: #fff8df; border: 1px solid #d7a900; border-radius: 2px; padding: 7px 10px; color: #332800; }
QLabel#fileManagerAndroidSpaceLabel { background: #f2f2f2; border: 1px solid #b8b8b8; border-radius: 2px; padding: 6px 10px; color: #202020; }
QLineEdit#fileManagerPathEdit { font-family: "Consolas"; border-radius: 2px; min-height: 22px; padding: 3px 7px; }
QPushButton { background: #ffffff; border: 1px solid #c8c8c8; border-radius: 6px; padding: 6px 11px; color: #202020; }
QPushButton:hover { background: #f5f5f5; border-color: #9f9f9f; }
QPushButton:pressed { background: #e5e5e5; }
QPushButton:disabled { color: #8a8a8a; background: #f3f3f3; border-color: #dddddd; }
QPushButton[danger="true"] { border-color: #d13438; color: #a80000; }
QPushButton[danger="true"]:disabled { color: #9b7777; background: #f3f3f3; border-color: #d8c4c4; }
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
QPushButton#fileManagerTransferButton { min-height: 34px; font-weight: 700; background: #e5f1fb; border: 1px solid #7aa7cc; border-radius: 4px; color: #003e73; padding: 4px 6px; }
QPushButton#fileManagerTransferButton:hover { background: #d2e9f9; border-color: #4f8fbe; }
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


_DARK_BASE = """
QMainWindow, QWidget { background: #1f1f1f; color: #f2f2f2; font-family: "Segoe UI"; font-size: 10pt; }
QLabel { background: transparent; }
QListWidget#nav { background: #242424; border: 1px solid #303030; border-radius: 6px; padding: 6px; }
QListWidget#nav::item { padding: 10px 12px; border-radius: 6px; }
QListWidget#nav::item:hover { background: #303030; }
QListWidget#nav::item:selected { background: #164b76; color: #ffffff; font-weight: 600; }
QListWidget#nav[collapsed="true"]::item { padding: 10px 4px; }
QToolButton#navToggle { border: 0; min-width: 32px; min-height: 28px; }
QWidget#navPanel { background: #1f1f1f; border: 0; }
QWidget#brandHeader { background: #1f1f1f; border: 0; }
QLabel#brandTitle { font-size: 15pt; font-weight: 650; color: #ffffff; }
QLabel#brandVersion { font-size: 8pt; color: #9aa4af; padding-top: 0; }
QFrame#deviceStatusBar, QFrame#card, QFrame#toolbarCard, QFrame#wirelessGroup, QFrame#commandGroup, QGroupBox { background: #282828; border: 1px solid #3a3a3a; border-radius: 8px; }
QLabel#statusSummary { color: #ffffff; font-weight: 700; }
QLabel#statusDeviceName { color: #dbe7f3; font-weight: 600; }
QLabel#statusMode { background: #30363d; border: 1px solid #59636e; border-radius: 8px; color: #d8e0e8; font-size: 9pt; font-weight: 700; padding: 3px 7px; }
QLabel#statusState { color: #9aa8b6; font-size: 9pt; }
QToolButton#deviceDetailsButton { padding-left: 7px; padding-right: 7px; }
QFrame#card { padding: 10px; }
QFrame#settingsSection { background: #282828; border: 1px solid #3f4851; border-radius: 8px; }
QLabel#settingsSectionTitle { color: #f2f2f2; font-size: 12pt; font-weight: 650; }
QLabel#settingsStatusValue { color: #8ac7f5; font-weight: 700; }
QLabel#settingsVerificationResult { background: #202d38; border: 1px solid #35546d; border-radius: 5px; color: #dce9f5; padding: 6px 8px; }
QFrame#commandToolbar, QFrame#commandDetailsPanel, QFrame#commandOutputPanel { background: #282828; border: 1px solid #3f4851; border-radius: 8px; }
QLabel#commandCount { color: #9aa8b6; }
QLabel#commandDetailsTitle { color: #f2f2f2; font-size: 12pt; font-weight: 650; }
QLabel#commandActualText { background: #202225; border: 1px solid #4a4f55; border-radius: 5px; color: #f2f2f2; font-family: "Consolas"; padding: 7px; }
QLabel#commandMetadata { color: #aab3bd; font-size: 9pt; }
QLabel#commandRiskBadge { background: #173d24; border: 1px solid #3c8c52; border-radius: 7px; color: #b8f2c5; font-weight: 650; padding: 4px 7px; }
QLabel#commandRiskBadge[riskLevel="Changes device state"] { background: #4a390c; border-color: #a98212; color: #ffe7a0; }
QLabel#commandRiskBadge[riskLevel="May erase data"], QLabel#commandRiskBadge[riskLevel="Critical"] { background: #4a1e21; border-color: #c94d52; color: #ffc4c7; }
QLabel#commandAvailability { color: #bac8d5; }
QLabel#commandOutputStatus { background: #30363d; border: 1px solid #59636e; border-radius: 7px; color: #d8e0e8; font-weight: 650; padding: 3px 7px; }
QLabel#commandOutputStatus[resultState="running"] { background: #164b76; border-color: #2f6f9f; color: #ffffff; }
QLabel#commandOutputStatus[resultState="success"] { background: #173d24; border-color: #3c8c52; color: #b8f2c5; }
QLabel#commandOutputStatus[resultState="error"], QLabel#commandOutputStatus[resultState="cancelled"] { background: #4a1e21; border-color: #c94d52; color: #ffc4c7; }
QTreeWidget#commandTree { border-radius: 8px; }
QTreeWidget#commandTree::item { min-height: 24px; }
QSplitter#commandsMainSplitter::handle, QSplitter#commandsBrowserSplitter::handle { background: #3a3f44; border-radius: 2px; }
QSplitter#commandsMainSplitter::handle:hover, QSplitter#commandsBrowserSplitter::handle:hover { background: #2f6f9f; }
QFrame#commandOutputPanel QPlainTextEdit { font-family: "Consolas"; }
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
QLabel#appFilterSummary { color: #8ac7f5; font-size: 9pt; padding-left: 8px; }
QLabel#appsSelectionSummary { color: #bac8d5; font-size: 9pt; padding-left: 4px; }
QToolButton#appsFiltersButton { background: #2d2d2d; border: 1px solid #4a4a4a; border-radius: 6px; color: #f2f2f2; padding: 6px 11px; }
QToolButton#appsFiltersButton:hover { background: #383838; border-color: #666666; }
QPushButton#appsResetFilters { padding-left: 9px; padding-right: 9px; }
QLabel#cardCaption { color: #b8b8b8; font-size: 9pt; }
QLabel#cardValue { font-size: 13pt; font-weight: 600; }
QLabel#hintLabel { background: #342b12; border: 1px solid #7f6416; border-radius: 6px; padding: 8px 10px; color: #ffe8a3; }
QFrame#appsTopBar, QFrame#appsBulkActionBar { background: #303437; border: 1px solid #4a4f55; border-radius: 6px; }
QFrame#fileManagerCenterPanel { background: #151515; border: 1px solid #404040; border-radius: 2px; }
QFrame#fileManagerCenterSeparator { background: #404040; border: 0; }
QSplitter#fileManagerSplitter::handle { background: #3a3a3a; border-radius: 2px; }
QSplitter#fileManagerSplitter::handle:hover { background: #2f6f9f; }
QLabel#fileManagerActionGroupTitle { color: #b8c0cc; font-size: 9pt; font-weight: 700; padding: 2px 1px; }
QLabel#fileManagerRootStatus { color: #9aa4af; font-size: 8pt; padding: 2px; }
QLabel#fileManagerRootStatus[rootState="granted"] { color: #b8f2c5; }
QLabel#fileManagerRootStatus[rootState="denied"], QLabel#fileManagerRootStatus[rootState="unavailable"] { color: #ffc4c7; }
QLabel#fileManagerSideTitle { color: #b8c0cc; font-weight: 600; padding-right: 4px; }
QLabel#fileManagerStatusLabel { background: #342b12; border: 1px solid #7f6416; border-radius: 2px; padding: 7px 10px; color: #ffe8a3; }
QLabel#fileManagerAndroidSpaceLabel { background: #252525; border: 1px solid #404040; border-radius: 2px; padding: 6px 10px; color: #d8d8d8; }
QLineEdit#fileManagerPathEdit { font-family: "Consolas"; background: #202020; border: 1px solid #404040; border-radius: 2px; min-height: 22px; padding: 3px 7px; color: #ffffff; }
QPushButton { background: #2d2d2d; border: 1px solid #4a4a4a; border-radius: 6px; padding: 6px 11px; color: #f2f2f2; }
QPushButton:hover { background: #383838; border-color: #666666; }
QPushButton:pressed { background: #424242; }
QPushButton:disabled { color: #777777; background: #282828; border-color: #363636; }
QPushButton[danger="true"] { border-color: #ff8a80; color: #ffb4ab; }
QPushButton[danger="true"]:disabled { color: #8f6f70; background: #282828; border-color: #4d3a3a; }
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
QPushButton#fileManagerTransferButton { min-height: 34px; font-weight: 700; background: #164b76; border: 1px solid #2f6f9f; border-radius: 4px; color: #ffffff; padding: 4px 6px; }
QPushButton#fileManagerTransferButton:hover { background: #1d5f91; border-color: #4a8fbe; }
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


def _semantic_styles(colors: ColorTokens) -> str:
    """Theme-independent component rules rendered from semantic color tokens."""
    return f"""
QWidget[uiSurface="card"], QFrame#emptyState {{ background: {colors.surface}; border: 1px solid {colors.border}; border-radius: 8px; }}
QFrame#toolbarCard {{ background: {colors.surface_alt}; border: 1px solid {colors.border}; border-radius: {LAYOUT.control_radius}px; }}
QLabel#pageTitle {{ color: {colors.text}; font-size: {TYPOGRAPHY.page_title_pt}pt; font-weight: 600; padding: 4px 0; }}
QLabel#dialogTitle {{ color: {colors.text}; font-size: 16pt; font-weight: 600; padding: 2px 0 5px 0; }}
QLabel#cardTitle, QLabel#settingsSectionTitle, QLabel#commandGroupTitle, QLabel#wirelessGroupTitle {{ color: {colors.text}; font-size: {TYPOGRAPHY.card_title_pt}pt; font-weight: 650; }}
QLabel#emptyStateIcon {{ color: {colors.text_secondary}; border: 0; }}
QLabel#emptyStateTitle {{ color: {colors.text}; font-size: 13pt; font-weight: 650; border: 0; }}
QLabel#emptyStateDescription {{ color: {colors.text_secondary}; border: 0; }}
QFrame#emptyState[stateKind="warning"] {{ background: {colors.warning_surface}; border-color: {colors.warning}; }}
QLabel[uiRole="secondary"], QLabel#pageSubtitle, QLabel#sectionDescription {{ color: {colors.text_secondary}; font-size: {TYPOGRAPHY.secondary_text_pt}pt; }}
QLabel[uiRole="success"] {{ color: {colors.success}; }}
QLabel[uiRole="warning"] {{ color: {colors.warning}; }}
QLabel[uiRole="danger"] {{ color: {colors.danger}; }}
QLabel[link="true"] {{ color: {colors.link}; text-decoration: underline; }}
QPushButton, QToolButton {{ min-height: 20px; }}
QPushButton[compact="true"], QToolButton[compact="true"] {{ min-height: 18px; padding: 4px 8px; }}
QPushButton[uiRole="primary"], QPushButton#primaryAction {{ background: {colors.primary}; border-color: {colors.primary}; color: {colors.primary_text}; font-weight: 650; }}
QPushButton[uiRole="primary"]:hover, QPushButton#primaryAction:hover {{ background: {colors.primary_hover}; border-color: {colors.primary_hover}; }}
QPushButton[uiRole="success"] {{ background: {colors.success_surface}; border-color: {colors.success}; color: {colors.success}; }}
QPushButton[uiRole="warning"] {{ background: {colors.warning_surface}; border-color: {colors.warning}; color: {colors.warning}; }}
QPushButton[uiRole="danger"], QPushButton[danger="true"] {{ background: {colors.danger_surface}; border-color: {colors.danger}; color: {colors.danger}; font-weight: 600; }}
QPushButton:disabled, QToolButton:disabled, QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ background: {colors.disabled_surface}; border-color: {colors.border}; color: {colors.disabled_text}; }}
QPushButton:focus, QToolButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus, QTableWidget:focus, QTreeView:focus, QListWidget:focus {{ border: 2px solid {colors.focus}; }}
QCheckBox:focus, QRadioButton:focus {{ background: {colors.focus_surface}; border: 1px solid {colors.focus}; border-radius: 4px; }}
QTabBar::tab:focus {{ border: 2px solid {colors.focus}; }}
QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit {{ placeholder-text-color: {colors.disabled_text}; }}
QLineEdit, QComboBox, QSpinBox {{ min-height: {LAYOUT.input_height - 10}px; }}
QTableWidget::item, QTreeView::item {{ min-height: {LAYOUT.table_row_height}px; }}
QTableWidget::item:selected, QTreeView::item:selected, QListWidget::item:selected {{ background: {colors.selection}; color: {colors.selection_text}; }}
QToolTip {{ background: {colors.tooltip_surface}; color: {colors.tooltip_text}; border: 1px solid {colors.border}; padding: 5px 7px; }}
QDialog#appDialog, QMessageBox {{ background: {colors.canvas}; color: {colors.text}; }}
QDialogButtonBox QPushButton:default {{ background: {colors.primary}; border-color: {colors.primary}; color: {colors.primary_text}; font-weight: 650; }}
QStatusBar {{ color: {colors.text_secondary}; }}
"""


LIGHT = _LIGHT_BASE + _semantic_styles(LIGHT_COLORS)
DARK = _DARK_BASE + _semantic_styles(DARK_COLORS)


def apply_theme(app: QApplication, theme: str) -> None:
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    resolved_theme = theme if theme in {"Light", "Dark"} else ("Dark" if _system_prefers_dark() else "Light")
    app.setProperty("openadbResolvedTheme", resolved_theme)
    if resolved_theme == "Dark":
        app.setStyleSheet(DARK)
    else:
        app.setStyleSheet(LIGHT)
    for widget in app.allWidgets():
        refresh = getattr(widget, "refresh_semantic_colors", None)
        if callable(refresh):
            refresh()


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
