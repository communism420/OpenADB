from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if os.name != "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.fastboot import FastbootClient
from openadb.core.icon_extractor import IconExtractor
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.settings_manager import SettingsManager
from openadb.models.app_info import AppInfo
from openadb.models.device_info import DeviceInfo
from openadb.models.file_item import FileItem
from openadb.models.platform_tools_info import PlatformToolsInfo
from openadb.ui.main_window import MainWindow
from openadb.ui.style import apply_theme


OUTPUT_DIR = ROOT / "docs" / "screenshots"
WINDOW_SIZE = (1280, 820)


class ScreenshotSettings(SettingsManager):
    def __init__(self, config_dir: Path) -> None:
        self._screenshot_config_dir = config_dir
        super().__init__()

    def _config_dir(self) -> Path:
        return self._screenshot_config_dir

    def _legacy_config_dirs(self) -> list[Path]:
        return []


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
    file_manager.transfer_transport_combo.setCurrentIndex(
        file_manager.transfer_transport_combo.findData("acbridge_p2p")
    )
    file_manager.p2p_parallelism_combo.setCurrentIndex(file_manager.p2p_parallelism_combo.findData(3))
    file_manager.status_label.setText(
        "Demo device ready. P2P will send different files through 3 authenticated ACBridge streams."
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
    settings_page.platform_path.setText(r"C:\Android\platform-tools")
    settings_page.adb_path.setText(r"C:\Android\platform-tools\adb.exe")
    settings_page.fastboot_path.setText(r"C:\Android\platform-tools\fastboot.exe")
    settings_page.backups_folder.setText(r"C:\OpenADB\Demo Phone\backups")
    settings_page.temp_folder.setText(r"C:\OpenADB\Demo Phone\temp")
    settings_page.logs_folder.setText(r"C:\OpenADB\Demo Phone\logs")
    settings_page.set_verification_result("adb and fastboot completed their version checks successfully.")

    window.statusBar().showMessage("Platform Tools: Found | Demo data")


def _capture(window: MainWindow, app: QApplication, page_name: str, theme: str, filename: str) -> None:
    apply_theme(app, theme)
    row = list(window.pages).index(page_name)
    window.nav.blockSignals(True)
    window.nav.setCurrentRow(row)
    window.nav.blockSignals(False)
    window.stack.setCurrentIndex(row)
    window.resize(*WINDOW_SIZE)
    window.show()
    app.processEvents()
    pixmap = window.grab()
    target = OUTPUT_DIR / filename
    if pixmap.isNull() or not pixmap.save(str(target), "PNG"):
        raise RuntimeError(f"Could not save screenshot: {target}")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("OpenADB")
    with tempfile.TemporaryDirectory(prefix="openadb-readme-") as temporary:
        root = Path(temporary)
        settings = ScreenshotSettings(root / "settings")
        settings.set_global_values({"window_width": WINDOW_SIZE[0], "window_height": WINDOW_SIZE[1]})
        settings.set("auto_refresh_device", False)
        settings.set("theme", "Dark")
        tools = _demo_tools(root / "platform-tools")
        platform_tools = PlatformToolsManager(settings)
        platform_tools.active = tools
        runner = CommandRunner(settings.logs_folder)
        adb = ADBClient(platform_tools, runner)
        fastboot = FastbootClient(platform_tools, runner)
        device_manager = DeviceManager(adb, fastboot, settings)
        device_manager.active = _demo_device()
        device_manager.devices = [device_manager.active]

        demo_windows_dir = root / "demo-files"
        (demo_windows_dir / "Documents").mkdir(parents=True)
        (demo_windows_dir / "Photos").mkdir()
        (demo_windows_dir / "openadb-notes.txt").write_text("Demonstration file", encoding="utf-8")

        with (
            patch("openadb.ui.main_window.QTimer.singleShot"),
            patch(
                "openadb.ui.file_manager_page.NativeExplorerPanel",
                side_effect=RuntimeError("Use deterministic Qt file panel for README screenshots"),
            ),
        ):
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
                captures = [
                    ("Dashboard", "Dark", "dashboard-dark.png"),
                    ("Dashboard", "Light", "dashboard-light.png"),
                    ("Apps", "Dark", "applications-dark.png"),
                    ("File Manager", "Dark", "file-manager-dark.png"),
                    ("Commands", "Dark", "commands-dark.png"),
                    ("Settings", "Dark", "settings-dark.png"),
                ]
                for page_name, theme, filename in captures:
                    _capture(window, app, page_name, theme, filename)
                    print(f"Captured {filename}")
            finally:
                window.close()
                runner.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
