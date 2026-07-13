"""Widget composition and signal wiring for :class:`FileManagerPage`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openadb.core.acbridge_p2p import (
    ADB_TRANSPORT,
    P2P_MAX_PARALLELISM,
    P2P_TRANSPORT,
)
from openadb.core.p2p_parallelism import AUTO_PARALLELISM_MODE
from openadb.ui.design_system import configure_page_layout, set_button_role
from openadb.ui.material_icons import material_icon
from openadb.ui.widgets.file_panel import FilePanel
from openadb.ui.widgets.no_wheel_widgets import NoWheelComboBox as QComboBox


FILE_MANAGER_ACTION_PANEL_WIDTH = 196
FILE_MANAGER_ACTION_PANEL_MIN_WIDTH = 156


def build_file_manager_view(page: Any) -> None:
    """Create widgets and retain all callbacks on the composing page."""
    layout = QVBoxLayout(page)
    layout.setSizeConstraint(QLayout.SetNoConstraint)
    configure_page_layout(layout)

    title = QLabel("File Manager")
    title.setObjectName("pageTitle")
    subtitle = QLabel("Browse and transfer files between Windows and the active Android device.")
    subtitle.setObjectName("pageSubtitle")
    subtitle.setWordWrap(True)
    layout.addWidget(title)
    layout.addWidget(subtitle)

    android_top = QHBoxLayout()
    android_top.setContentsMargins(0, 0, 0, 0)
    android_top.setSpacing(5)
    page.android_storage_combo = QComboBox()
    page.android_storage_combo.setObjectName("fileManagerStorageCombo")
    page.android_storage_combo.setMinimumWidth(150)
    page.android_storage_combo.setMaximumWidth(260)
    page.android_storage_combo.setToolTip("Android TV / Android storage volume: internal memory, MicroSD, or USB storage")
    page.android_storage_combo.currentIndexChanged.connect(page._android_storage_selected)
    page.android_storage_refresh_button = QToolButton()
    page.android_storage_refresh_button.setText("Storage")
    page.android_storage_refresh_button.setObjectName("fileManagerNavButton")
    page.android_storage_refresh_button.setToolTip("Refresh Android storage volumes")
    page.android_storage_refresh_button.setAccessibleName("Refresh Android storage volumes")
    page.android_storage_refresh_button.clicked.connect(page.refresh_android_storage_roots)
    page.android_path_edit = QLineEdit()
    page.android_path_edit.setObjectName("fileManagerPathEdit")
    page.android_path_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    page.android_path_edit.returnPressed.connect(lambda: page.navigate_android(page.android_path_edit.text()))
    page.android_up_button = QToolButton()
    page.android_up_button.setText("Up")
    page.android_up_button.setObjectName("fileManagerNavButton")
    page.android_up_button.setToolTip("Go up one Android folder")
    page.android_up_button.clicked.connect(lambda: page.navigate_android(page._android_parent_path(page.android_path)))
    android_top.addWidget(page.android_storage_combo)
    android_top.addWidget(page.android_storage_refresh_button)
    android_top.addWidget(page.android_path_edit, 1)
    android_top.addWidget(page.android_up_button)

    windows_top = QHBoxLayout()
    windows_top.setContentsMargins(0, 0, 0, 0)
    windows_top.setSpacing(5)
    page.windows_back_button = QToolButton()
    page.windows_back_button.setIcon(material_icon("chevron_left"))
    page.windows_back_button.setObjectName("fileManagerNavButton")
    page.windows_back_button.setToolTip("Back")
    page.windows_back_button.setAccessibleName("Back")
    page.windows_back_button.clicked.connect(page.windows_back)
    page.windows_forward_button = QToolButton()
    page.windows_forward_button.setIcon(material_icon("chevron_right"))
    page.windows_forward_button.setObjectName("fileManagerNavButton")
    page.windows_forward_button.setToolTip("Forward")
    page.windows_forward_button.setAccessibleName("Forward")
    page.windows_forward_button.clicked.connect(page.windows_forward)
    page.windows_path_edit = QLineEdit()
    page.windows_path_edit.setObjectName("fileManagerPathEdit")
    page.windows_path_edit.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    page.windows_path_edit.returnPressed.connect(lambda: page.navigate_windows(page.windows_path_edit.text()))
    windows_top.addWidget(page.windows_back_button)
    windows_top.addWidget(page.windows_forward_button)
    windows_top.addWidget(page.windows_path_edit, 1)

    page.file_splitter = QSplitter(Qt.Horizontal)
    page.file_splitter.setObjectName("fileManagerSplitter")
    page.file_splitter.setChildrenCollapsible(False)
    page.file_splitter.setHandleWidth(6)
    page.android_panel = FilePanel("Android", "android", show_path_bar=False, show_button_row=False)
    page.android_panel.table.setObjectName("fileManagerAndroidTable")
    page.windows_panel = page._create_windows_panel()

    android_side = QWidget()
    android_side_layout = QVBoxLayout(android_side)
    android_side_layout.setContentsMargins(0, 0, 0, 0)
    android_side_layout.setSpacing(4)
    android_side_layout.addLayout(android_top)
    android_side_layout.addWidget(page.android_panel, 1)
    page.android_space_label = QLabel("Free space: -")
    page.android_space_label.setObjectName("fileManagerAndroidSpaceLabel")
    android_side_layout.addWidget(page.android_space_label)

    windows_side = QWidget()
    windows_side_layout = QVBoxLayout(windows_side)
    windows_side_layout.setContentsMargins(0, 0, 0, 0)
    windows_side_layout.setSpacing(4)
    windows_side_layout.addLayout(windows_top)
    windows_side_layout.addWidget(page.windows_panel, 1)

    center = QFrame()
    center.setObjectName("fileManagerCenterPanel")
    center.setMinimumWidth(FILE_MANAGER_ACTION_PANEL_MIN_WIDTH)
    center.setMaximumWidth(FILE_MANAGER_ACTION_PANEL_WIDTH)
    center.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
    center_layout = QVBoxLayout(center)
    center_layout.setContentsMargins(5, 5, 5, 5)
    center_layout.setSpacing(5)

    page.pull_button = QPushButton("Android → PC")
    page.pull_button.setObjectName("fileManagerTransferButton")
    page.pull_button.setToolTip("Copy selected Android files to the current Windows folder")
    page.push_button = QPushButton("PC → Android")
    page.push_button.setObjectName("fileManagerTransferButton")
    page.push_button.setToolTip("Copy selected Windows files to the current Android folder")
    page.refresh_button = QPushButton("Refresh")
    page.refresh_button.setObjectName("fileManagerCompactButton")
    page.refresh_button.setToolTip("Refresh both panels")
    page.mkdir_button = QPushButton("New folder")
    page.mkdir_button.setObjectName("fileManagerCompactButton")
    page.mkdir_button.setToolTip("Create a folder on the active side")
    page.delete_button = QPushButton("Delete")
    page.delete_button.setObjectName("fileManagerCompactButton")
    page.delete_button.setProperty("danger", True)
    set_button_role(page.delete_button, "danger", compact=True)
    page.rename_button = QPushButton("Rename")
    page.rename_button.setObjectName("fileManagerCompactButton")
    page.copy_path_button = QPushButton("Copy path")
    page.copy_path_button.setObjectName("fileManagerCompactButton")
    page.copy_path_button.setToolTip("Copy selected path")
    page.properties_button = QPushButton("Properties")
    page.properties_button.setObjectName("fileManagerCompactButton")
    page.open_explorer_button = QPushButton("Open in Explorer")
    page.open_explorer_button.setObjectName("fileManagerCompactButton")
    page.open_explorer_button.setToolTip("Open current Windows folder in Explorer")
    page.root_boost_button = QCheckBox("Use root for transfers")
    page.root_boost_button.setObjectName("fileManagerRootToggle")
    page.root_boost_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    page.root_boost_button.setChecked(bool(page.settings.get("file_manager_root_transfer", False)))
    page.root_boost_button.setToolTip(
        "Request su/root only for File Manager transfers. Root must be granted by the connected device; "
        "when it is unavailable OpenADB falls back to normal ADB transfer."
    )
    page.root_status_label = QLabel("Root: not checked")
    page.root_status_label.setObjectName("fileManagerRootStatus")
    page.root_status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    page.transfer_transport_combo = QComboBox()
    page.transfer_transport_combo.setObjectName("fileManagerTransferTransport")
    page.transfer_transport_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    page.transfer_transport_combo.addItem("Platform Tools (ADB)", ADB_TRANSPORT)
    page.transfer_transport_combo.addItem("P2P via ACBridge", P2P_TRANSPORT)
    page.transfer_transport_combo.setAccessibleName("PC to Android transfer method")
    page.transfer_transport_combo.setToolTip(
        "Choose how PC → Android file data is sent. P2P uses an authenticated, integrity-checked ACBridge "
        "session, but file data is not encrypted. Use it only on a trusted private network. Android → PC "
        "continues through Platform Tools."
    )
    page.p2p_security_status_label = QLabel("Authenticated, not encrypted")
    page.p2p_security_status_label.setObjectName("fileManagerP2PSecurityStatus")
    page.p2p_security_status_label.setProperty("uiRole", "warning")
    page.p2p_security_status_label.setWordWrap(True)
    page.p2p_security_status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    page.p2p_security_status_label.setAccessibleName(
        "P2P security status: authenticated, not encrypted"
    )
    page.p2p_security_status_label.setAccessibleDescription(
        "ACBridge verifies the sender and file integrity, but P2P file data is visible to the local network."
    )
    page.p2p_security_status_label.setToolTip(
        "ACBridge P2P authenticates each one-time session and verifies file integrity, but file data is not "
        "encrypted. Use P2P only on a trusted private network. Do not use public, shared, guest, or untrusted "
        "Wi-Fi. Firewall rules or client isolation can block the transfer. Platform Tools (ADB) remains the "
        "safe default transfer method."
    )
    page.p2p_parallelism_row = QWidget()
    page.p2p_parallelism_row.setObjectName("fileManagerP2PParallelismRow")
    p2p_parallelism_layout = QHBoxLayout(page.p2p_parallelism_row)
    p2p_parallelism_layout.setContentsMargins(0, 0, 0, 0)
    p2p_parallelism_layout.setSpacing(6)
    page.p2p_parallelism_label = QLabel("P2P streams")
    page.p2p_parallelism_combo = QComboBox()
    page.p2p_parallelism_combo.setObjectName("fileManagerP2PParallelism")
    page.p2p_parallelism_combo.setAccessibleName("Number of parallel P2P streams")
    page.p2p_parallelism_combo.addItem("Auto (recommended)", AUTO_PARALLELISM_MODE)
    for count in range(1, P2P_MAX_PARALLELISM + 1):
        page.p2p_parallelism_combo.addItem(str(count), count)
    page.p2p_parallelism_combo.setToolTip(
        "Auto chooses a conservative number of authenticated ACBridge sessions from the captured transfer "
        "plan. Manual values 5–8 are advanced overrides. A single file always uses one stream and the selected "
        "count never exceeds the number of files."
    )
    p2p_parallelism_layout.addWidget(page.p2p_parallelism_label)
    p2p_parallelism_layout.addWidget(page.p2p_parallelism_combo, 1)
    page._restore_p2p_parallelism()
    page._restore_transfer_transport()

    center_layout.addWidget(page._action_group_title("Transfer"))
    center_layout.addWidget(page.transfer_transport_combo)
    center_layout.addWidget(page.p2p_security_status_label)
    center_layout.addWidget(page.p2p_parallelism_row)
    for button in [page.pull_button, page.push_button]:
        button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        button.setMinimumHeight(38)
        center_layout.addWidget(button)
    center_layout.addWidget(page._center_separator())
    center_layout.addWidget(page._action_group_title("File operations"))
    file_operations = [
        page.refresh_button,
        page.mkdir_button,
        page.rename_button,
        page.delete_button,
        page.copy_path_button,
        page.properties_button,
    ]
    file_operations_grid = QGridLayout()
    file_operations_grid.setContentsMargins(0, 0, 0, 0)
    file_operations_grid.setSpacing(4)
    for index, button in enumerate(file_operations):
        button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        file_operations_grid.addWidget(button, index // 2, index % 2)
    center_layout.addLayout(file_operations_grid)
    center_layout.addWidget(page._center_separator())
    center_layout.addWidget(page._action_group_title("Advanced"))
    page.open_explorer_button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    center_layout.addWidget(page.open_explorer_button)
    center_layout.addWidget(page.root_boost_button)
    center_layout.addWidget(page.root_status_label)
    center_layout.addStretch()

    page.file_splitter.addWidget(android_side)
    page.file_splitter.addWidget(center)
    page.file_splitter.addWidget(windows_side)
    page.file_splitter.setStretchFactor(0, 1)
    page.file_splitter.setStretchFactor(1, 0)
    page.file_splitter.setStretchFactor(2, 1)
    layout.addWidget(page.file_splitter, 1)

    page.status_label = QLabel("Select files on one side and use the middle buttons with the selected transfer method.")
    page.status_label.setObjectName("fileManagerStatusLabel")
    page.status_label.setWordWrap(True)
    layout.addWidget(page.status_label)

    page.android_panel.navigate_requested.connect(page.navigate_android)
    page.android_panel.up_requested.connect(lambda: page.navigate_android(page._android_parent_path(page.android_path)))
    page.android_panel.refresh_requested.connect(page.refresh_android)
    page.android_panel.new_folder_requested.connect(lambda: page.new_folder("android"))
    page.android_panel.delete_requested.connect(lambda: page.delete_selected("android"))
    page.android_panel.rename_requested.connect(lambda: page.rename_selected("android"))
    page.android_panel.transfer_requested.connect(page.pull_selected)
    page.android_panel.copy_path_requested.connect(lambda: page.copy_path("android"))
    page.android_panel.properties_requested.connect(lambda: page.properties("android"))
    page.android_panel.dropped.connect(page.push_paths)
    page.android_panel.table.focused.connect(lambda: page._set_active_side("android"))

    page.windows_panel.navigate_requested.connect(page.navigate_windows)
    page.windows_panel.up_requested.connect(lambda: page.navigate_windows(str(Path(page.windows_path).parent)))
    page.windows_panel.refresh_requested.connect(page.refresh_windows)
    page.windows_panel.new_folder_requested.connect(lambda: page.new_folder("windows"))
    page.windows_panel.delete_requested.connect(lambda: page.delete_selected("windows"))
    page.windows_panel.rename_requested.connect(lambda: page.rename_selected("windows"))
    page.windows_panel.transfer_requested.connect(page.push_selected)
    page.windows_panel.copy_path_requested.connect(lambda: page.copy_path("windows"))
    page.windows_panel.properties_requested.connect(lambda: page.properties("windows"))
    page.windows_panel.open_external_requested.connect(page.open_explorer)
    page.windows_panel.dropped.connect(page.pull_paths)
    if hasattr(page.windows_panel, "path_changed"):
        page.windows_panel.path_changed.connect(page._windows_path_changed)
    if hasattr(page.windows_panel, "tree"):
        page.windows_panel.tree.focused.connect(lambda: page._set_active_side("windows"))
    if hasattr(page.windows_panel, "focused"):
        page.windows_panel.focused.connect(lambda: page._set_active_side("windows"))

    page.refresh_button.clicked.connect(page.refresh_all)
    page.mkdir_button.clicked.connect(lambda: page.new_folder(page._active_side))
    page.pull_button.clicked.connect(page.pull_selected)
    page.push_button.clicked.connect(page.push_selected)
    page.delete_button.clicked.connect(lambda: page.delete_selected(page._active_side))
    page.rename_button.clicked.connect(lambda: page.rename_selected(page._active_side))
    page.copy_path_button.clicked.connect(lambda: page.copy_path(page._active_side))
    page.properties_button.clicked.connect(lambda: page.properties(page._active_side))
    page.open_explorer_button.clicked.connect(page.open_explorer)
    page.root_boost_button.toggled.connect(page._root_transfer_toggled)
    page.transfer_transport_combo.currentIndexChanged.connect(page._transfer_transport_changed)
    page.p2p_parallelism_combo.currentIndexChanged.connect(page._p2p_parallelism_changed)

    page._splitter_save_timer = QTimer(page)
    page._splitter_save_timer.setSingleShot(True)
    page._splitter_save_timer.setInterval(250)
    page._splitter_save_timer.timeout.connect(page._save_splitter_state)
    page.file_splitter.splitterMoved.connect(lambda _position, _index: page._splitter_save_timer.start())
    page._restore_splitter_state()

    page.refresh_shortcut = QShortcut(QKeySequence("F5"), page)
    page.refresh_shortcut.activated.connect(page.refresh_all)

    page.android_panel.set_path(page.android_path)
    page.android_path_edit.setText(page.android_path)
    page._set_android_storage_combo([])
    initial_root_state = (
        "not checked" if page.device_manager.active.mode in {"ADB", "Recovery"} else "unavailable"
    )
    page._set_root_status(initial_root_state)
    page.navigate_windows(page.windows_path)
