# -*- mode: python ; coding: utf-8 -*-

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPECPATH).resolve()
sys.path.insert(0, str(ROOT))

from openadb.version import ACBRIDGE_APK_FILENAME, RELEASE_EXE_FILENAME


APP_NAME = Path(RELEASE_EXE_FILENAME).stem


def find_platform_tools() -> Path:
    candidates = []
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(variable, "").strip()
        if value:
            candidates.append(Path(value) / "platform-tools")
    candidates.extend(
        [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools",
            Path("C:/platform-tools"),
            Path("C:/Android/platform-tools"),
        ]
    )
    adb_from_path = shutil.which("adb.exe")
    if adb_from_path:
        candidates.append(Path(adb_from_path).resolve().parent)
    for folder in candidates:
        if (folder / "adb.exe").is_file() and (folder / "fastboot.exe").is_file():
            return folder.resolve()
    raise SystemExit(
        "Android Platform Tools were not found. Set ANDROID_HOME/ANDROID_SDK_ROOT "
        "or install them in the standard Android SDK location before building."
    )


datas = [
    (str(ROOT / "logo.png"), "openadb/resources/icons"),
    (str(ROOT / "openadb/resources/uad_lists.json"), "openadb/resources"),
    (str(ROOT / "openadb/resources/UAD_LIST_SOURCE.txt"), "openadb/resources"),
    (str(ROOT / "openadb/resources/acbridge" / ACBRIDGE_APK_FILENAME), "openadb/resources/acbridge"),
    (str(ROOT / "openadb/resources/material_symbols/NOTICE.md"), "openadb/resources/material_symbols"),
]
binaries = []
hiddenimports = []

for package in ("apkutils2", "PIL", "qrcode", "zeroconf"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

platform_tools = find_platform_tools()
for filename in (
    "adb.exe",
    "fastboot.exe",
    "AdbWinApi.dll",
    "AdbWinUsbApi.dll",
    "libwinpthread-1.dll",
):
    source = platform_tools / filename
    if source.is_file():
        binaries.append((str(source), "platform-tools"))
notice = platform_tools / "NOTICE.txt"
if notice.is_file():
    datas.append((str(notice), "platform-tools"))


a = Analysis(
    [str(ROOT / "openadb/main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ROOT / "logo.ico")],
    version=str(ROOT / "tools/openadb_version_info.txt"),
)
