from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication, QAbstractItemView, QTableWidget


def configure_graphics_acceleration() -> None:
    """Prefer desktop OpenGL for Qt compositing where it is safe.

    QTableWidget/QTreeWidget OpenGL viewports are intentionally disabled by
    default below. On Windows they can corrupt text painting in item views.
    """
    if os.environ.get("OPENADB_DISABLE_OPENGL") == "1":
        return
    try:
        QApplication.setAttribute(Qt.AA_UseDesktopOpenGL, True)
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
        fmt = QSurfaceFormat()
        fmt.setRenderableType(QSurfaceFormat.OpenGL)
        fmt.setSwapInterval(0)
        QSurfaceFormat.setDefaultFormat(fmt)
    except Exception:
        pass


def accelerate_item_view(view: QAbstractItemView) -> None:
    if os.environ.get("OPENADB_ENABLE_OPENGL_TABLES") != "1":
        return
    if os.environ.get("OPENADB_DISABLE_OPENGL") == "1":
        return
    try:
        from PySide6.QtOpenGLWidgets import QOpenGLWidget

        view.setViewport(QOpenGLWidget(view))
    except Exception:
        pass


def optimize_table(table: QTableWidget) -> None:
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.ElideRight)
    table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.verticalHeader().setDefaultSectionSize(32)
    accelerate_item_view(table)
