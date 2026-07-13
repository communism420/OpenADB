from __future__ import annotations

import json
import shutil
import tempfile
import threading
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
        use_root: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, BackupInfo | None, str]:
        if cancel_event is not None and cancel_event.is_set():
            return False, None, "Backup cancelled before it started."
        self.refresh_root()
        if cancel_event is not None and cancel_event.is_set():
            return False, None, "Backup cancelled before it started."
        fresh_paths = adb.get_package_path(app.package_name, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return False, None, "Backup cancelled while reading APK paths."
        apk_paths = fresh_paths or app.apk_paths
        if not apk_paths:
            return False, None, "No APK path returned by pm path."

        if cancel_event is not None and cancel_event.is_set():
            return False, None, "Backup cancelled before creating local backup files."
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        package_dir = ensure_dir(self.root / safe_filename(app.package_name))
        backup_dir = Path(tempfile.mkdtemp(prefix=f".partial-{timestamp}-", dir=package_dir))
        published = False
        try:
            if cancel_event is not None and cancel_event.is_set():
                return False, None, "Backup cancelled before copying APK files."
            command_logs: list[str] = []
            apk_files: list[str] = []
            for index, apk_path in enumerate(apk_paths, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    return False, None, "Backup cancelled before the next split APK."
                filename = Path(apk_path).name or f"part_{index}.apk"
                target = backup_dir / safe_filename(filename)
                if target.exists():
                    target = backup_dir / f"{index}_{safe_filename(filename)}"
                if use_root:
                    result = adb.pull_file_streaming_to_file(
                        apk_path,
                        target,
                        timeout=300,
                        cancel_event=cancel_event,
                        use_root=True,
                    )
                else:
                    result = adb.pull(
                        apk_path,
                        target,
                        timeout=300,
                        cancel_event=cancel_event,
                    )
                if cancel_event is not None and cancel_event.is_set():
                    return False, None, f"Backup cancelled while copying {filename}."
                command_logs.append(_command_log(result))
                if not result.success or not target.exists():
                    if cancel_event is not None and cancel_event.is_set():
                        return False, None, f"Backup cancelled while copying {filename}."
                    (backup_dir / "command_log.txt").write_text("\n".join(command_logs), encoding="utf-8")
                    if cancel_event is not None and cancel_event.is_set():
                        return False, None, "Backup cancelled before writing failure metadata."
                    self._write_metadata(
                        backup_dir,
                        app,
                        device,
                        uninstall_method,
                        apk_files,
                        icon_path,
                        "failed",
                    )
                    if cancel_event is not None and cancel_event.is_set():
                        return False, None, "Backup cancelled before publishing failure metadata."
                    self._publish_backup_directory(backup_dir, timestamp)
                    published = True
                    return False, None, result.status or result.stderr or "Failed to pull APK from device."
                apk_files.append(target.name)

            icon_filename = ""
            if cancel_event is not None and cancel_event.is_set():
                return False, None, "Backup cancelled before writing local metadata."
            if icon_path:
                try:
                    source = Path(icon_path)
                    if source.exists():
                        icon_target = backup_dir / "icon.png"
                        shutil.copy2(source, icon_target)
                        icon_filename = icon_target.name
                except OSError:
                    icon_filename = ""

            if cancel_event is not None and cancel_event.is_set():
                return False, None, "Backup cancelled before writing local metadata."
            (backup_dir / "command_log.txt").write_text("\n".join(command_logs), encoding="utf-8")
            if cancel_event is not None and cancel_event.is_set():
                return False, None, "Backup cancelled before writing backup metadata."
            self._write_metadata(
                backup_dir,
                app,
                device,
                uninstall_method,
                apk_files,
                icon_filename,
                "success",
            )
            if cancel_event is not None and cancel_event.is_set():
                return False, None, "Backup cancelled before publishing backup metadata."
            final_dir = self._publish_backup_directory(backup_dir, timestamp)
            published = True
            return True, self._read_backup(final_dir), "Backup created"
        finally:
            if not published:
                self._discard_partial_backup(backup_dir)

    def scan_backups(self, cancel_event: threading.Event | None = None) -> list[BackupInfo]:
        self.refresh_root()
        backups: list[BackupInfo] = []
        if not self.root.exists() or (cancel_event is not None and cancel_event.is_set()):
            return backups
        for package_dir in self.root.iterdir():
            if cancel_event is not None and cancel_event.is_set():
                return []
            if not package_dir.is_dir():
                continue
            for backup_dir in package_dir.iterdir():
                if cancel_event is not None and cancel_event.is_set():
                    return []
                if not backup_dir.is_dir() or backup_dir.name.startswith(".partial-"):
                    continue
                backups.append(self._read_backup(backup_dir))
                if cancel_event is not None and cancel_event.is_set():
                    return []
        if cancel_event is not None and cancel_event.is_set():
            return []
        backups.sort(key=lambda item: item.backup_date or item.path.name, reverse=True)
        return backups

    @staticmethod
    def _publish_backup_directory(backup_dir: Path, timestamp: str) -> Path:
        suffix = backup_dir.name.removeprefix(f".partial-{timestamp}-")
        final_dir = backup_dir.parent / f"{timestamp}-{suffix}"
        return backup_dir.rename(final_dir)

    @staticmethod
    def _discard_partial_backup(backup_dir: Path) -> None:
        try:
            shutil.rmtree(backup_dir)
        except OSError:
            pass

    def restore_backup(
        self,
        backup: BackupInfo,
        adb: ADBClient,
        prefer_install_existing: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> CommandResult:
        if cancel_event is not None and cancel_event.is_set():
            return _restore_error(
                backup,
                "Restore cancelled before it started",
                "cancelled",
                "The restore operation was cancelled before Android package installation started.",
            )
        if prefer_install_existing and backup.package_name:
            return adb.restore_existing_package(
                backup.package_name,
                cancel_event=cancel_event,
            )
        declared_files = [str(filename or "").strip() for filename in backup.apk_files]
        declared_files = [filename for filename in declared_files if filename]
        if not declared_files:
            return _restore_error(
                backup,
                "No APK files in backup",
                "missing_backup_apk",
                "No APK files in backup.",
            )

        try:
            backup_root = backup.path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            return _restore_error(
                backup,
                "Backup path could not be validated",
                "unsafe_backup_apk_path",
                f"Backup path could not be resolved safely: {exc}",
            )

        apk_paths: list[Path] = []
        for filename in declared_files:
            if cancel_event is not None and cancel_event.is_set():
                return _restore_error(
                    backup,
                    "Restore cancelled",
                    "cancelled",
                    "The restore operation was cancelled while validating local APK files.",
                )
            candidate = backup.path / filename
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(backup_root)
            except (OSError, RuntimeError, ValueError):
                return _restore_error(
                    backup,
                    "Backup metadata contains an unsafe APK path",
                    "unsafe_backup_apk_path",
                    f"Unsafe APK entry in backup metadata: {filename}",
                )
            if candidate.suffix.casefold() != ".apk" or resolved.suffix.casefold() != ".apk":
                return _restore_error(
                    backup,
                    "Backup metadata contains an unsafe APK path",
                    "unsafe_backup_apk_path",
                    f"Backup entry is not an APK file: {filename}",
                )
            if not resolved.is_file():
                return _restore_error(
                    backup,
                    "Backup APK file is missing",
                    "missing_backup_apk",
                    f"Declared backup APK does not exist: {filename}",
                )
            apk_paths.append(resolved)

        if cancel_event is not None and cancel_event.is_set():
            return _restore_error(
                backup,
                "Restore cancelled before installation",
                "cancelled",
                "The restore operation was cancelled before Android package installation started.",
            )
        if len(apk_paths) == 1:
            return adb.install_apk(apk_paths[0], cancel_event=cancel_event)
        return adb.install_multiple(apk_paths, cancel_event=cancel_event)

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


def _restore_error(
    backup: BackupInfo,
    status: str,
    error_type: str,
    details: str,
) -> CommandResult:
    now = datetime.now()
    return CommandResult(
        command=["restore-backup", str(backup.path)],
        exit_code=1,
        stdout="",
        stderr=details,
        duration=0,
        started_at=now,
        finished_at=now,
        success=False,
        status=status,
        error_type=error_type,
    )


def _command_log(result: CommandResult) -> str:
    return (
        f"$ {result.command_text}\n"
        f"exit={result.exit_code} duration={result.duration:.2f}s status={result.status}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}\n"
    )
