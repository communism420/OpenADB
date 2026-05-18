from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(value: str) -> str:
    value = value.strip() or "unknown"
    return re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", value)


def shell_quote(value: str) -> str:
    """Quote a value for Android /system/bin/sh."""
    return "'" + value.replace("'", "'\\''") + "'"


def join_android_path(parent: str, child: str) -> str:
    if parent == "/":
        return "/" + child.strip("/")
    return parent.rstrip("/") + "/" + child.strip("/")


def parent_android_path(path: str) -> str:
    clean = path.rstrip("/")
    if not clean or clean == "/":
        return "/"
    parent = clean.rsplit("/", 1)[0]
    return parent or "/"


def format_bytes(size: int | None) -> str:
    if size is None:
        return "Unknown"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def windows_drives() -> list[Path]:
    drives: list[Path] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        path = Path(f"{letter}:\\")
        if path.exists():
            drives.append(path)
    return drives


def user_home() -> Path:
    return Path.home()


def is_probably_writable_android_path(path: str) -> bool:
    clean = path.replace("\\", "/").rstrip("/") + "/"
    return clean.startswith("/sdcard/") or clean.startswith("/storage/emulated/0/")


def normalized_env_paths() -> list[Path]:
    paths: list[Path] = []
    for raw in os.environ.get("PATH", "").split(os.pathsep):
        if raw:
            try:
                paths.append(Path(raw).expanduser())
            except OSError:
                continue
    return paths
