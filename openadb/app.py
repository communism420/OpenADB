from __future__ import annotations

import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from openadb import __version__
from openadb.core.adb import ADBClient
from openadb.core.backup_manager import BackupManager
from openadb.core.command_runner import CommandRunner
from openadb.core.device import DeviceManager
from openadb.core.fastboot import FastbootClient
from openadb.core.file_transfer import FileTransferManager
from openadb.core.icon_extractor import IconExtractor
from openadb.core.platform_tools import PlatformToolsManager
from openadb.core.settings_manager import SettingsManager
from openadb.ui.main_window import MainWindow
from openadb.ui.branding import logo_icon
from openadb.ui.performance import configure_graphics_acceleration
from openadb.ui.style import apply_theme


_RUNTIME_REFS: dict[str, object] = {}


def _install_exception_hook(settings: SettingsManager) -> None:
    default_hook = sys.excepthook

    def hook(exc_type, exc_value, exc_traceback) -> None:
        try:
            crash_log = settings.logs_folder / "openadb-crash.log"
            text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            crash_log.write_text(text, encoding="utf-8")
        except Exception:
            pass
        default_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = hook


def run() -> int:
    configure_graphics_acceleration()
    app = QApplication(sys.argv)
    app.setApplicationName("OpenADB")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("OpenADB")
    icon = logo_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    settings = SettingsManager()
    _install_exception_hook(settings)
    apply_theme(app, str(settings.get("theme", "System")))

    platform_tools = PlatformToolsManager(settings)
    runner = CommandRunner(settings.logs_folder)
    adb = ADBClient(platform_tools, runner)
    fastboot = FastbootClient(platform_tools, runner)
    device_manager = DeviceManager(adb, fastboot, settings)
    backup_manager = BackupManager(settings)
    icon_extractor = IconExtractor(settings)
    file_transfer = FileTransferManager(adb)

    window = MainWindow(
        settings=settings,
        platform_tools=platform_tools,
        runner=runner,
        adb=adb,
        fastboot=fastboot,
        device_manager=device_manager,
        backup_manager=backup_manager,
        icon_extractor=icon_extractor,
    )
    _RUNTIME_REFS.update(
        {
            "app": app,
            "settings": settings,
            "platform_tools": platform_tools,
            "runner": runner,
            "adb": adb,
            "fastboot": fastboot,
            "device_manager": device_manager,
            "backup_manager": backup_manager,
            "icon_extractor": icon_extractor,
            "file_transfer": file_transfer,
            "window": window,
        }
    )
    window.show()
    return app.exec()


def main() -> None:
    try:
        exit_code = run()
        if exit_code:
            sys.exit(exit_code)
    except Exception as exc:
        app = QApplication.instance() or QApplication(sys.argv)
        try:
            settings = SettingsManager()
            crash_log = settings.logs_folder / "openadb-crash.log"
            crash_log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        QMessageBox.critical(None, "OpenADB", f"OpenADB failed to start:\n{exc}")
        raise
