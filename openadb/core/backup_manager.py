from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from openadb.models.app_info import AppInfo
from openadb.models.backup_info import BackupInfo
from openadb.models.command_result import CommandResult
from openadb.models.device_info import DeviceInfo

from .adb import ADBClient
from .path_utils import ensure_dir, safe_filename
from .settings_manager import SettingsManager


class BackupManager:
    def __init__(self, settings: SettingsManager) -> None:
        self.settings = settings
        self.root = ensure_dir(settings.backups_folder)

    def refresh_root(self) -> None:
        self.root = ensure_dir(self.settings.backups_folder)

    def create_backup(
        self,
        app: AppInfo,
        adb: ADBClient,
        device: DeviceInfo,
        uninstall_method: str,
        icon_path: str = "",
    ) -> tuple[bool, BackupInfo | None, str]:
        self.refresh_root()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_dir = ensure_dir(self.root / safe_filename(app.package_name) / timestamp)
        command_logs: list[str] = []

        fresh_paths = adb.get_package_path(app.package_name)
        apk_paths = fresh_paths or app.apk_paths
        if not apk_paths:
            return False, None, "No APK path returned by pm path."

        apk_files: list[str] = []
        for index, apk_path in enumerate(apk_paths, start=1):
            filename = Path(apk_path).name or f"part_{index}.apk"
            target = backup_dir / safe_filename(filename)
            if target.exists():
                target = backup_dir / f"{index}_{safe_filename(filename)}"
            result = adb.pull(apk_path, target, timeout=300)
            command_logs.append(_command_log(result))
            if not result.success or not target.exists():
                (backup_dir / "command_log.txt").write_text("\n".join(command_logs), encoding="utf-8")
                self._write_metadata(
                    backup_dir,
                    app,
                    device,
                    uninstall_method,
                    apk_files,
                    icon_path,
                    "failed",
                )
                return False, None, result.status or result.stderr or "Failed to pull APK from device."
            apk_files.append(target.name)

        icon_filename = ""
        if icon_path:
            try:
                source = Path(icon_path)
                if source.exists():
                    icon_target = backup_dir / "icon.png"
                    shutil.copy2(source, icon_target)
                    icon_filename = icon_target.name
            except OSError:
                icon_filename = ""

        (backup_dir / "command_log.txt").write_text("\n".join(command_logs), encoding="utf-8")
        self._write_metadata(
            backup_dir,
            app,
            device,
            uninstall_method,
            apk_files,
            icon_filename,
            "success",
        )
        return True, self._read_backup(backup_dir), "Backup created"

    def scan_backups(self) -> list[BackupInfo]:
        self.refresh_root()
        backups: list[BackupInfo] = []
        if not self.root.exists():
            return backups
        for package_dir in self.root.iterdir():
            if not package_dir.is_dir():
                continue
            for backup_dir in package_dir.iterdir():
                if backup_dir.is_dir():
                    backups.append(self._read_backup(backup_dir))
        backups.sort(key=lambda item: item.backup_date or item.path.name, reverse=True)
        return backups

    def restore_backup(self, backup: BackupInfo, adb: ADBClient, prefer_install_existing: bool = False) -> CommandResult:
        if prefer_install_existing and backup.package_name:
            return adb.restore_existing_package(backup.package_name)
        apk_paths = [backup.path / filename for filename in backup.apk_files]
        apk_paths = [path for path in apk_paths if path.exists()]
        if not apk_paths:
            now = datetime.now()
            return CommandResult(
                command=["restore-backup", str(backup.path)],
                exit_code=1,
                stdout="",
                stderr="No APK files in backup.",
                duration=0,
                started_at=now,
                finished_at=now,
                success=False,
                status="No APK files in backup",
                error_type="missing_backup_apk",
            )
        if len(apk_paths) == 1:
            return adb.install_apk(apk_paths[0])
        return adb.install_multiple(apk_paths)

    def delete_backup(self, backup: BackupInfo) -> None:
        root = self.root.resolve()
        target = backup.path.resolve()
        target.relative_to(root)
        shutil.rmtree(target)

    def _write_metadata(
        self,
        backup_dir: Path,
        app: AppInfo,
        device: DeviceInfo,
        uninstall_method: str,
        apk_files: list[str],
        icon_filename: str,
        status: str,
    ) -> None:
        metadata = {
            "package_name": app.package_name,
            "app_label": app.display_name,
            "version_name": app.version_name,
            "version_code": app.version_code,
            "apk_path_on_device": app.apk_paths[0] if app.apk_paths else "",
            "apk_paths_on_device": app.apk_paths,
            "backup_date": datetime.now().isoformat(timespec="seconds"),
            "device_model": device.model,
            "device_serial": device.serial,
            "android_version": device.android_version,
            "uninstall_method": uninstall_method,
            "apk_filename": apk_files[0] if apk_files else "",
            "apk_files": apk_files,
            "icon_filename": icon_filename,
            "backup_status": status,
        }
        (backup_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    def _read_backup(self, backup_dir: Path) -> BackupInfo:
        metadata_path = backup_dir / "metadata.json"
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                apk_files = metadata.get("apk_files") or ([metadata.get("apk_filename")] if metadata.get("apk_filename") else [])
                return BackupInfo(
                    path=backup_dir,
                    package_name=str(metadata.get("package_name", "")),
                    app_label=str(metadata.get("app_label", "")),
                    backup_date=str(metadata.get("backup_date", "")),
                    device_model=str(metadata.get("device_model", "")),
                    device_serial=str(metadata.get("device_serial", "")),
                    android_version=str(metadata.get("android_version", "")),
                    apk_files=[str(item) for item in apk_files if item],
                    restore_method="adb install-multiple" if len(apk_files) > 1 else "adb install",
                    metadata_exists=True,
                    uninstall_method=str(metadata.get("uninstall_method", "")),
                )
            except (OSError, json.JSONDecodeError):
                pass
        apk_files = [path.name for path in backup_dir.glob("*.apk")]
        return BackupInfo(
            path=backup_dir,
            package_name=backup_dir.parent.name,
            backup_date=backup_dir.name,
            apk_files=apk_files,
            restore_method="adb install-multiple" if len(apk_files) > 1 else "adb install",
            metadata_exists=False,
        )


def _command_log(result: CommandResult) -> str:
    return (
        f"$ {result.command_text}\n"
        f"exit={result.exit_code} duration={result.duration:.2f}s status={result.status}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}\n"
    )
