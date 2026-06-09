from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap

from openadb.core.path_utils import app_root, package_root


def logo_path() -> Path:
    candidates = [
        app_root() / "logo.png",
        package_root() / "resources" / "icons" / "logo.png",
        app_root() / "openadb" / "resources" / "icons" / "logo.png",
    ]
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


@lru_cache(maxsize=1)
def logo_icon() -> QIcon:
    path = logo_path()
    return QIcon(str(path)) if path.exists() else QIcon()


def logo_pixmap(size: int) -> QPixmap:
    path = logo_path()
    if not path.exists():
        return QPixmap()
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return QPixmap()
    return pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
