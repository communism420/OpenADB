from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from openadb.models.platform_tools_info import PlatformToolsInfo

from .path_utils import app_root, normalized_env_paths, package_root, user_home
from .settings_manager import SettingsManager


class PlatformToolsManager:
    def __init__(self, settings: SettingsManager) -> None:
        self.settings = settings
        self.active = PlatformToolsInfo()

    def detect(self, select: bool = True) -> list[PlatformToolsInfo]:
        candidates: list[tuple[Path, str]] = []
        saved = str(self.settings.get("platform_tools_path", "")).strip()
        if saved:
            candidates.append((Path(saved).expanduser(), "Saved settings"))

        root = app_root()
        candidates.extend(
            [
                (root / "platform-tools", "Near program"),
                (root, "Program folder"),
            ]
        )
        packaged_root = package_root().parent
        if packaged_root != root:
            candidates.append((packaged_root / "platform-tools", "Bundled with OpenADB"))

        for exe_name in ("adb.exe", "fastboot.exe"):
            found = shutil.which(exe_name)
            if found:
                candidates.append((Path(found).resolve().parent, "PATH"))

        for path in normalized_env_paths():
            if path.name.lower() == "platform-tools" or (path / "adb.exe").exists() or (path / "fastboot.exe").exists():
                candidates.append((path, "PATH"))

        try:
            import winreg

            registry_paths = [
                (winreg.HKEY_CURRENT_USER, r"Environment", "User PATH"),
                (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", "System PATH"),
            ]
            for hive, key_path, label in registry_paths:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        raw, _ = winreg.QueryValueEx(key, "Path")
                    for item in str(raw).split(os.pathsep):
                        if item:
                            candidates.append((Path(os.path.expandvars(item)), label))
                except OSError:
                    continue
        except Exception:
            pass

        home = user_home()
        candidates.extend(
            [
                (Path("C:/platform-tools"), "Typical folder"),
                (Path("C:/Android/platform-tools"), "Typical folder"),
                (Path("C:/Program Files/Android/platform-tools"), "Typical folder"),
                (home / "AppData/Local/Android/Sdk/platform-tools", "Android SDK"),
                (home / "platform-tools", "User folder"),
            ]
        )

        seen: set[str] = set()
        infos: list[PlatformToolsInfo] = []
        for folder, source in candidates:
            try:
                resolved = folder.expanduser().resolve()
            except OSError:
                continue
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            info = self.inspect_folder(resolved, source)
            if info.has_adb or info.has_fastboot:
                infos.append(info)

        infos.sort(
            key=lambda item: (
                0 if item.is_found else 1,
                0 if item.source == "Bundled with OpenADB" else 1,
                str(item.folder).lower() if item.folder else "",
            )
        )
        if infos and select:
            selected = self._select_saved_or_best(infos)
            self.set_active(selected, save=selected.is_found or selected.has_adb)
        elif not infos and select:
            self.active = PlatformToolsInfo()
        return infos

    def inspect_folder(self, folder: Path, source: str = "Manual") -> PlatformToolsInfo:
        adb = folder / "adb.exe"
        fastboot = folder / "fastboot.exe"
        info = PlatformToolsInfo(
            folder=folder,
            adb_path=adb if adb.exists() else None,
            fastboot_path=fastboot if fastboot.exists() else None,
            source=source,
        )
        if info.adb_path:
            info.adb_version, info.adb_works = self._version(info.adb_path, ["version"])
        if info.fastboot_path:
            info.fastboot_version, info.fastboot_works = self._version(info.fastboot_path, ["--version"], unknown_ok=True)
        return info

    def set_active(self, info: PlatformToolsInfo, save: bool = True) -> None:
        self.active = info
        if save and info.folder:
            self.settings.set("platform_tools_path", str(info.folder))

    def choose_folder(self, folder: str | Path) -> PlatformToolsInfo:
        info = self.inspect_folder(Path(folder).expanduser())
        self.set_active(info, save=info.has_adb or info.has_fastboot)
        return info

    def _select_saved_or_best(self, infos: list[PlatformToolsInfo]) -> PlatformToolsInfo:
        saved = str(self.settings.get("platform_tools_path", "")).strip()
        if saved:
            try:
                saved_path = str(Path(saved).resolve()).lower()
                for info in infos:
                    if info.folder and str(info.folder.resolve()).lower() == saved_path:
                        return info
            except OSError:
                pass
        return infos[0]

    def _version(self, exe: Path, args: list[str], unknown_ok: bool = False) -> tuple[str, bool]:
        try:
            completed = subprocess.run(
                [str(exe), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return ("Unknown" if unknown_ok else "Unavailable", False)
        output = (completed.stdout or completed.stderr or "").strip()
        if not output:
            return ("Unknown", completed.returncode == 0 or unknown_ok)
        first_line = output.splitlines()[0].strip()
        return first_line or "Unknown", completed.returncode == 0 or unknown_ok

    @property
    def adb_path(self) -> Path | None:
        return self.active.adb_path

    @property
    def fastboot_path(self) -> Path | None:
        return self.active.fastboot_path
