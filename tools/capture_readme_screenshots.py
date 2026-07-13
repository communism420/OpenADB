from __future__ import annotations

# ruff: noqa: E402 -- the script supports direct execution outside the repository root.

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from time import sleep
from types import SimpleNamespace
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint
from PySide6.QtGui import QFontDatabase, QImage, QPixmap, QRegion
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from PIL import Image

from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.fastboot import FastbootClient
from openadb.core.icon_extractor import IconExtractor
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.p2p_parallelism import AUTO_PARALLELISM_MODE
from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.models.device_info import DeviceInfo
from openadb.models.file_item import FileItem
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.main_window import MainWindow
from openadb.ui.style import apply_theme
from openadb.ui.widgets.file_panel import FilePanel


OUTPUT_DIR = ROOT / "docs" / "screenshots"
WINDOW_SIZE = (1280, 820)
WINDOWS_SCREENSHOT_FONTS = (
    "segoeui.ttf",
    "segoeuib.ttf",
    "segoeuii.ttf",
    "segoeuil.ttf",
    "segoeuisl.ttf",
    "segoeuiz.ttf",
)


class ScreenshotSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._screenshot_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._screenshot_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


def _load_offscreen_fonts() -> None:
    """Make the Windows UI font available to Qt's isolated offscreen plugin."""

    if os.name != "nt":
        return
    fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    loaded = [
        QFontDatabase.addApplicationFont(str(fonts_dir / filename))
        for filename in WINDOWS_SCREENSHOT_FONTS
        if (fonts_dir / filename).is_file()
    ]
    if not loaded or any(font_id < 0 for font_id in loaded):
        raise RuntimeError("Could not load Segoe UI for offscreen screenshot capture")


def _demo_device() -> DeviceInfo:
    return DeviceInfo(
        serial="DEMO-ANDROID-001",
        model="Demo Pixel",
        manufacturer="OpenADB Demo",
        android_version="16",
        sdk_version="36",
        mode="ADB",
        state="device",
        transport_id="1",
        product="demo_phone",
        form_factor="Phone",
    )


def _demo_tools(folder: Path) -> PlatformToolsInfo:
    folder.mkdir(parents=True, exist_ok=True)
    adb_path = folder / "adb.exe"
    fastboot_path = folder / "fastboot.exe"
    adb_path.touch()
    fastboot_path.touch()
    return PlatformToolsInfo(
        folder=folder,
        adb_path=adb_path,
        fastboot_path=fastboot_path,
        adb_version="Android Debug Bridge 1.0.41 (36.0.0)",
        fastboot_version="fastboot 36.0.0",
        adb_works=True,
        fastboot_works=True,
        source="Bundled with OpenADB",
    )


def _demo_apps() -> list[AppInfo]:
    return [
        AppInfo(
            "com.android.settings",
            "Android Settings",
            "system",
            "enabled",
            size="84.2 MB",
            bloatware_removal="Unsafe",
            bloatware_list="Demo list",
        ),
        AppInfo(
            "com.example.camera",
            "Demo Camera",
            "user",
            "enabled",
            size="36.8 MB",
            bloatware_removal="Recommended",
            bloatware_list="Demo list",
        ),
        AppInfo(
            "com.example.music",
            "Demo Music",
            "user",
            "disabled",
            size="24.1 MB",
            bloatware_removal="Advanced",
            bloatware_list="Demo list",
        ),
        AppInfo("com.example.notes", "Demo Notes", "user", "enabled", size="18.4 MB"),
        AppInfo(
            "com.example.weather",
            "Demo Weather",
            "user",
            "enabled",
            size="12.7 MB",
            bloatware_removal="Recommended",
            bloatware_list="Demo list",
        ),
        AppInfo("com.example.tv", "Demo TV Remote", "system", "enabled", size="9.6 MB"),
    ]


def _configure_demo(window: MainWindow, demo_windows_dir: Path, tools: PlatformToolsInfo) -> None:
    device = _demo_device()
    window.device_manager.active = device
    window.device_manager.devices = [device]
    window.device_bar.set_device(device)
    window.dashboard.update_device(device)
    window.dashboard.update_tools(tools)
    window.apps_page.update_device_state(device)
    window.commands_page.update_device_state(device)

    apps = _demo_apps()
    window.apps_page.apps = apps
    window.apps_page.table.set_apps_sorted(apps)
    window.apps_page.apply_filter(save_state=False)
    window.apps_page.status_label.setText(
        "Demonstration application data loaded. Filters and selections stay local until an action is run."
    )

    android_items = [
        FileItem("DCIM", "/sdcard/DCIM", True, modified="2026-07-10 14:32", item_type="Folder"),
        FileItem("Download", "/sdcard/Download", True, modified="2026-07-12 09:15", item_type="Folder"),
        FileItem("Movies", "/sdcard/Movies", True, modified="2026-07-08 18:40", item_type="Folder"),
        FileItem("Music", "/sdcard/Music", True, modified="2026-07-09 11:25", item_type="Folder"),
        FileItem(
            "demo-photo.jpg",
            "/sdcard/demo-photo.jpg",
            False,
            size=2_831_155,
            modified="2026-07-11 16:05",
            item_type="JPEG image",
        ),
    ]
    file_manager = window.file_manager_page
    file_manager.android_path = "/sdcard/"
    file_manager.android_panel.set_path("/sdcard/")
    file_manager.android_path_edit.setText("/sdcard/")
    file_manager.android_panel.set_items(android_items)
    file_manager._set_android_storage_combo(
        [
            SimpleNamespace(
                path="/sdcard/",
                label="Internal shared storage",
                free_bytes=88_476_811_264,
                total_bytes=137_438_953_472,
                state="mounted",
            )
        ]
    )
    file_manager.android_space_label.setText("Free space: 82.4 GB | Total: 128.0 GB | 36% used")
    file_manager.windows_panel.set_path(str(demo_windows_dir))
    file_manager.windows_path = str(demo_windows_dir)
    file_manager.windows_path_edit.setText(r"C:\Demo\OpenADB")
    file_manager.windows_panel.set_items(
        [
            FileItem(
                "Documents",
                r"C:\Demo\OpenADB\Documents",
                True,
                modified="2026-07-13 12:00",
                item_type="Folder",
            ),
            FileItem(
                "Photos",
                r"C:\Demo\OpenADB\Photos",
                True,
                modified="2026-07-13 12:00",
                item_type="Folder",
            ),
            FileItem(
                "openadb-notes.txt",
                r"C:\Demo\OpenADB\openadb-notes.txt",
                False,
                size=18,
                modified="2026-07-13 12:00",
                item_type="Text file",
            ),
        ]
    )
    file_manager.transfer_transport_combo.blockSignals(True)
    file_manager.transfer_transport_combo.setCurrentIndex(
        file_manager.transfer_transport_combo.findData("acbridge_p2p")
    )
    file_manager.transfer_transport_combo.blockSignals(False)
    file_manager._accepted_transfer_transport = "acbridge_p2p"
    file_manager._update_transfer_transport_ui()
    file_manager.p2p_parallelism_combo.setCurrentIndex(
        file_manager.p2p_parallelism_combo.findData(AUTO_PARALLELISM_MODE)
    )
    file_manager.status_label.setText(
        "Demo device ready. Auto will choose a conservative number of authenticated ACBridge streams."
    )

    commands = window.commands_page
    for category_index in range(commands.tree.topLevelItemCount()):
        category = commands.tree.topLevelItem(category_index)
        if category.childCount():
            commands.tree.setCurrentItem(category.child(0))
            category.setExpanded(True)
            break
    commands.output_content.setCurrentWidget(commands.output_tabs)
    commands.output_status.setText("Completed")
    commands.output_exit.setText("Exit code: 0")
    commands.output_duration.setText("Duration: 0.18 s")
    commands.output_command.setText("adb devices -l")
    commands.stdout_output.setPlainText(
        "List of devices attached\nDEMO-ANDROID-001    device product:demo_phone model:Demo_Pixel"
    )

    settings_page = window.settings_page
    settings_page.update_tools(tools)
    settings_page.platform_path.setText(r"C:\Demo\platform-tools")
    settings_page.adb_path.setText(r"C:\Demo\platform-tools\adb.exe")
    settings_page.fastboot_path.setText(r"C:\Demo\platform-tools\fastboot.exe")
    settings_page.backups_folder.setText(r"C:\Demo\OpenADB\backups")
    settings_page.temp_folder.setText(r"C:\Demo\OpenADB\temp")
    settings_page.logs_folder.setText(r"C:\Demo\OpenADB\logs")
    settings_page.set_verification_result("adb and fastboot completed their version checks successfully.")

    window.statusBar().showMessage("Platform Tools: Found | Demo data")


def _widget_depth(widget: QWidget, root: QWidget) -> int:
    depth = 0
    parent = widget.parentWidget()
    while parent is not None and parent is not root:
        depth += 1
        parent = parent.parentWidget()
    return depth


def _render_widget_tree(
    root: QWidget,
    window: MainWindow,
    pixmap: QPixmap,
) -> None:
    render_flags = (
        QWidget.RenderFlag.DrawWindowBackground | QWidget.RenderFlag.IgnoreMask
    )
    root.render(
        pixmap,
        root.mapTo(window, QPoint()),
        QRegion(root.rect()),
        render_flags,
    )
    children = [
        widget
        for widget in root.findChildren(QWidget)
        if widget.window() is window and widget.isVisibleTo(root)
    ]
    children.sort(key=lambda widget: _widget_depth(widget, root))
    for widget in children:
        widget.render(
            pixmap,
            widget.mapTo(window, QPoint()),
            QRegion(widget.rect()),
            render_flags,
        )


def _capture(window: MainWindow, app: QApplication, page_name: str, filename: str) -> None:
    row = list(window.pages).index(page_name)
    window.nav.blockSignals(True)
    window.nav.setCurrentRow(row)
    window.nav.blockSignals(False)
    window.stack.setCurrentIndex(row)
    window.resize(*WINDOW_SIZE)
    window.show()
    app.processEvents()
    # An actual post-show resize invalidates the complete Windows offscreen
    # backing store; update()/repaint() alone can retain only a partial region.
    window.resize(WINDOW_SIZE[0] - 1, WINDOW_SIZE[1] - 1)
    app.processEvents()
    QTest.qWait(50)
    window.resize(*WINDOW_SIZE)
    app.processEvents()
    QTest.qWait(50)
    if (window.width(), window.height()) != WINDOW_SIZE:
        raise RuntimeError(
            f"Unexpected screenshot size: {window.width()}x{window.height()}"
        )
    if window.centralWidget().layout() is not None:
        window.centralWidget().layout().activate()
    # Give layouts and item views enough time to settle after configuring the
    # stacked page and any contextual action bar.
    contextual_apps = (
        filename == "applications-contextual-actions-dark-v3.0.0.png"
        and app.platformName().casefold() == "offscreen"
    )
    for _ in range(6 if contextual_apps else 3):
        window.update()
        window.repaint()
        app.processEvents()
        QTest.qWait(100)

    native_windows_capture = os.name == "nt" and app.platformName().casefold() == "windows"
    if native_windows_capture:
        # QWidget.render()/grab() can preserve only a native child's latest
        # dirty region on the Windows QPA backend. Ask the desktop compositor
        # for the already-visible client area instead.
        screen = window.screen()
        pixmap = screen.grabWindow(int(window.winId()), 0, 0, *WINDOW_SIZE)
    else:
        pixmap = QPixmap(window.size())
        pixmap.fill(window.palette().window().color())
    if contextual_apps:
        render_flags = (
            QWidget.RenderFlag.DrawWindowBackground | QWidget.RenderFlag.IgnoreMask
        )
        window.render(pixmap, QPoint(), QRegion(window.rect()), render_flags)
        # The dynamic Applications action bar exposes a Qt offscreen
        # backing-store edge case where only the latest dirty region is kept.
        # Paint every visible widget explicitly for this deterministic frame.
        widgets = [
            widget
            for widget in window.findChildren(QWidget)
            if widget.window() is window and widget.isVisibleTo(window)
        ]
        widgets.sort(key=lambda widget: _widget_depth(widget, window))
        for widget in widgets:
            widget.render(
                pixmap,
                widget.mapTo(window, QPoint()),
                QRegion(widget.rect()),
                render_flags,
            )
    elif not native_windows_capture:
        window.ensurePolished()
        for widget in window.findChildren(QWidget):
            widget.ensurePolished()
            if widget.layout() is not None:
                widget.layout().activate()
            widget.update()
        app.sendPostedEvents()
        app.processEvents()
        render_flags = (
            QWidget.RenderFlag.DrawWindowBackground
            | QWidget.RenderFlag.DrawChildren
            | QWidget.RenderFlag.IgnoreMask
        )
        window.render(pixmap, QPoint(), QRegion(window.rect()), render_flags)
        # These persistent chrome widgets may have independent backing stores
        # under the Windows offscreen plugin, so paint their visible widget
        # trees explicitly after the central page.
        for widget in (window.device_bar, window.side_panel):
            _render_widget_tree(widget, window, pixmap)
    if pixmap.size().toTuple() != WINDOW_SIZE:
        raise RuntimeError(
            f"Unexpected captured frame size: {pixmap.width()}x{pixmap.height()}"
        )
    target = OUTPUT_DIR / filename
    output_image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB32)
    if output_image.isNull() or not output_image.save(str(target), "PNG"):
        raise RuntimeError(f"Could not save screenshot: {target}")


def _set_demo_app_selection(window: MainWindow, packages: set[str]) -> None:
    """Set a deterministic checkbox selection without invoking an app action."""

    page = window.apps_page
    page.table.set_apps_sorted(page.apps, page._sort_mode, checked_packages=packages)
    page._selection_changed()


def _capture_fresh_page(
    app: QApplication,
    root: Path,
    folder_name: str,
    page_name: str,
    theme: str,
    filename: str,
    selected_packages: set[str] | None = None,
) -> None:
    """Capture a stacked page from its own first-show offscreen window."""

    capture_root = root / folder_name
    settings = ScreenshotSettings(capture_root / "settings")
    settings.set_global_values({"window_width": WINDOW_SIZE[0], "window_height": WINDOW_SIZE[1]})
    settings.set("auto_refresh_device", False)
    settings.set("theme", theme)
    tools = _demo_tools(capture_root / "platform-tools")
    platform_tools = PlatformToolsManager(settings)
    platform_tools.active = tools
    runner = CommandRunner(settings.logs_folder)
    adb = ADBClient(platform_tools, runner)
    fastboot = FastbootClient(platform_tools, runner)
    device_manager = DeviceManager(adb, fastboot, settings)
    device_manager.active = _demo_device()
    device_manager.devices = [device_manager.active]
    demo_windows_dir = capture_root / "demo-files"
    (demo_windows_dir / "Documents").mkdir(parents=True)
    (demo_windows_dir / "Photos").mkdir()
    (demo_windows_dir / "openadb-notes.txt").write_text("Demonstration file", encoding="utf-8")

    window = MainWindow(
        settings=settings,
        platform_tools=platform_tools,
        runner=runner,
        adb=adb,
        fastboot=fastboot,
        device_manager=device_manager,
        backup_manager=BackupManager(settings),
        icon_extractor=IconExtractor(settings),
    )
    try:
        _configure_demo(window, demo_windows_dir, tools)
        if selected_packages is not None:
            _set_demo_app_selection(window, selected_packages)
        if window.windowTitle() != "OpenADB 3.0.0":
            raise RuntimeError(f"Unexpected screenshot title: {window.windowTitle()!r}")
        _capture(window, app, page_name, filename)
        print(f"Captured {filename}")
    finally:
        window.hide()
        window.close()
        app.processEvents()
        QTest.qWait(100)
        window.deleteLater()
        app.processEvents()
        runner.shutdown()


CAPTURE_TARGETS: dict[str, tuple[str, str, str, set[str] | None]] = {
    "dashboard-dark": ("Dashboard", "Dark", "dashboard-dark-v3.0.0.png", None),
    "dashboard-light": ("Dashboard", "Light", "dashboard-light-v3.0.0.png", None),
    "applications": ("Apps", "Dark", "applications-dark-v3.0.0.png", set()),
    "applications-contextual": (
        "Apps",
        "Dark",
        "applications-contextual-actions-dark-v3.0.0.png",
        {"com.example.camera", "com.example.notes"},
    ),
    "file-manager": ("File Manager", "Dark", "file-manager-dark-v3.0.0.png", None),
    "commands": ("Commands", "Dark", "commands-dark-v3.0.0.png", None),
    "settings": ("Settings", "Dark", "settings-dark-v3.0.0.png", None),
}


def _capture_target(target_name: str) -> None:
    page_name, theme, filename, selected_packages = CAPTURE_TARGETS[target_name]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("OpenADB")
    app.setQuitOnLastWindowClosed(False)
    _load_offscreen_fonts()
    # Construct every widget under its final palette/QSS. Applying a dark
    # application stylesheet only after an offscreen top-level window exists
    # can leave native child backing stores with partially repainted regions.
    apply_theme(app, theme)
    with tempfile.TemporaryDirectory(prefix="openadb-readme-") as temporary:
        with (
            patch("openadb.ui.main_window.QTimer.singleShot"),
            patch(
                "openadb.ui.file_manager_page.NativeExplorerPanel",
                side_effect=RuntimeError(
                    "Use deterministic Qt file panel for README screenshots"
                ),
            ),
            patch(
                "openadb.ui.file_manager_page.WindowsFilePanel",
                side_effect=lambda *_args, **_kwargs: FilePanel(
                    "Windows",
                    "windows",
                    show_path_bar=False,
                    show_button_row=False,
                ),
            ),
        ):
            _capture_fresh_page(
                app,
                Path(temporary),
                target_name,
                page_name,
                theme,
                filename,
                selected_packages,
            )


def _validate_captured_frame(filename: str) -> None:
    path = OUTPUT_DIR / filename
    with Image.open(path) as image:
        if image.size != WINDOW_SIZE or image.mode != "RGB":
            raise RuntimeError(f"Unexpected screenshot format: {filename}")
        grayscale = image.convert("L")
        anchors = (
            grayscale.crop((20, 65, 220, 115)),
            grayscale.crop((10, 10, 480, 55)),
        )
        for anchor in anchors:
            darkest, lightest = anchor.getextrema()
            if darkest > 80 or lightest < 150:
                raise RuntimeError(f"Incomplete Windows frame: {filename}")
        # A partially preserved native backing store can still satisfy the
        # extrema check with one surviving icon. Require enough foreground
        # pixels for the brand, status bar, and complete navigation list.
        foreground_regions = (
            (grayscale.crop((10, 62, 230, 120)), 500),
            (grayscale.crop((10, 8, 1270, 58)), 500),
            (grayscale.crop((10, 120, 230, 445)), 1_500),
        )
        for region, minimum in foreground_regions:
            bright_pixels = sum(region.histogram()[131:])
            if bright_pixels < minimum:
                raise RuntimeError(f"Incomplete Windows frame: {filename}")


def main() -> int:
    arguments = sys.argv[1:]
    if not arguments:
        script = str(Path(__file__).resolve())
        for target_name in CAPTURE_TARGETS:
            filename = CAPTURE_TARGETS[target_name][2]
            for attempt in range(1, 4):
                subprocess.run(
                    [sys.executable, script, "--capture", target_name],
                    cwd=ROOT,
                    check=True,
                    timeout=60,
                )
                try:
                    _validate_captured_frame(filename)
                except RuntimeError:
                    if attempt == 3:
                        raise
                    sleep(0.75)
                    continue
                break
            sleep(0.5)
        return 0

    if (
        len(arguments) != 2
        or arguments[0] != "--capture"
        or arguments[1] not in CAPTURE_TARGETS
    ):
        choices = ", ".join(CAPTURE_TARGETS)
        raise SystemExit(
            f"Usage: capture_readme_screenshots.py [--capture {{{choices}}}]"
        )
    _capture_target(arguments[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
