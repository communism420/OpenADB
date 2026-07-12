from __future__ import annotations

from PySide6.QtCore import QByteArray, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle


# Material Symbols Rounded, 24 px, weight 400, fill 0. The original vector
# designs are published by Google under Apache-2.0. See the bundled notice in
# openadb/resources/material_symbols/NOTICE.md.
_PATHS = {
    "apps": "M240-160q-33 0-56.5-23.5T160-240q0-33 23.5-56.5T240-320q33 0 56.5 23.5T320-240q0 33-23.5 56.5T240-160Zm240 0q-33 0-56.5-23.5T400-240q0-33 23.5-56.5T480-320q33 0 56.5 23.5T560-240q0 33-23.5 56.5T480-160Zm240 0q-33 0-56.5-23.5T640-240q0-33 23.5-56.5T720-320q33 0 56.5 23.5T800-240q0 33-23.5 56.5T720-160ZM240-400q-33 0-56.5-23.5T160-480q0-33 23.5-56.5T240-560q33 0 56.5 23.5T320-480q0 33-23.5 56.5T240-400Zm240 0q-33 0-56.5-23.5T400-480q0-33 23.5-56.5T480-560q33 0 56.5 23.5T560-480q0 33-23.5 56.5T480-400Zm240 0q-33 0-56.5-23.5T640-480q0-33 23.5-56.5T720-560q33 0 56.5 23.5T800-480q0 33-23.5 56.5T720-400ZM240-640q-33 0-56.5-23.5T160-720q0-33 23.5-56.5T240-800q33 0 56.5 23.5T320-720q0 33-23.5 56.5T240-640Zm240 0q-33 0-56.5-23.5T400-720q0-33 23.5-56.5T480-800q33 0 56.5 23.5T560-720q0 33-23.5 56.5T480-640Zm240 0q-33 0-56.5-23.5T640-720q0-33 23.5-56.5T720-800q33 0 56.5 23.5T800-720q0 33-23.5 56.5T720-640Z",
    "arrow_downward": "M440-313v-447q0-17 11.5-28.5T480-800q17 0 28.5 11.5T520-760v447l196-196q12-12 28-11.5t28 12.5q11 12 11.5 28T772-452L508-188q-6 6-13 8.5t-15 2.5q-8 0-15-2.5t-13-8.5L188-452q-11-11-11-27.5t11-28.5q12-12 28.5-12t28.5 12l195 195Z",
    "arrow_upward": "M440-647 244-451q-12 12-28 11.5T188-452q-11-12-11.5-28t11.5-28l264-264q6-6 13-8.5t15-2.5q8 0 15 2.5t13 8.5l264 264q11 11 11 27.5T772-452q-12 12-28.5 12T715-452L520-647v447q0 17-11.5 28.5T480-160q-17 0-28.5-11.5T440-200v-447Z",
    "backup": "m440-446-36 35q-11 11-27.5 11T348-412q-11-11-11-28t11-28l104-104q12-12 28-12t28 12l104 104q11 11 11.5 27.5T612-412q-11 11-27.5 11.5T556-411l-36-35v206h220q42 0 71-29t29-71q0-42-29-71t-71-29h-60v-80q0-83-58.5-141.5T480-720q-83 0-141.5 58.5T280-520h-20q-58 0-99 41t-41 99q0 58 41 99t99 41h60q17 0 28.5 11.5T360-200q0 17-11.5 28.5T320-160h-60q-91 0-155.5-63T40-377q0-78 47-139t123-78q25-92 100-149t170-57q117 0 198.5 81.5T760-520q69 8 114.5 59.5T920-340q0 75-52.5 127.5T740-160H520q-33 0-56.5-23.5T440-240v-206Z",
    "check": "m382-354 339-339q12-12 28-12t28 12q12 12 12 28.5T777-636L410-268q-12 12-28 12t-28-12L182-440q-12-12-11.5-28.5T183-497q12-12 28.5-12t28.5 12l142 143Z",
    "chevron_left": "m432-480 156 156q11 11 11 28t-11 28q-11 11-28 11t-28-11L348-452q-6-6-8.5-13t-2.5-15q0-8 2.5-15t8.5-13l184-184q11-11 28-11t28 11q11 11 11 28t-11 28L432-480Z",
    "chevron_right": "M504-480 348-636q-11-11-11-28t11-28q11-11 28-11t28 11l184 184q6 6 8.5 13t2.5 15q0 8-2.5 15t-8.5 13L404-268q-11 11-28 11t-28-11q-11-11-11-28t11-28l156-156Z",
    "close": "M480-424 284-228q-11 11-28 11t-28-11q-11-11-11-28t11-28l196-196-196-196q-11-11-11-28t11-28q11-11 28-11t28 11l196 196 196-196q11-11 28-11t28 11q11 11 11 28t-11 28L536-480l196 196q11 11 11 28t-11 28q-11 11-28 11t-28-11L480-424Z",
    "computer": "M80-120q-17 0-28.5-11.5T40-160q0-17 11.5-28.5T80-200h800q17 0 28.5 11.5T920-160q0 17-11.5 28.5T880-120H80Zm80-120q-33 0-56.5-23.5T80-320v-440q0-33 23.5-56.5T160-840h640q33 0 56.5 23.5T880-760v440q0 33-23.5 56.5T800-240H160Zm0-80h640v-440H160v440Z",
    "create_new_folder": "M160-160q-33 0-56.5-23.5T80-240v-480q0-33 23.5-56.5T160-800h207q16 0 30.5 6t25.5 17l57 57h320q33 0 56.5 23.5T880-640v400q0 33-23.5 56.5T800-160H160Zm0-80h640v-400H447l-80-80H160v480Zm400-160v40q0 17 11.5 28.5T600-320q17 0 28.5-11.5T640-360v-40h40q17 0 28.5-11.5T720-440q0-17-11.5-28.5T680-480h-40v-40q0-17-11.5-28.5T600-560q-17 0-28.5 11.5T560-520v40h-40q-17 0-28.5 11.5T480-440q0 17 11.5 28.5T520-400h40Z",
    "dashboard": "M520-640v-160q0-17 11.5-28.5T560-840h240q17 0 28.5 11.5T840-800v160q0 17-11.5 28.5T800-600H560q-17 0-28.5-11.5T520-640ZM120-480v-320q0-17 11.5-28.5T160-840h240q17 0 28.5 11.5T440-800v320q0 17-11.5 28.5T400-440H160q-17 0-28.5-11.5T120-480Zm400 320v-320q0-17 11.5-28.5T560-520h240q17 0 28.5 11.5T840-480v320q0 17-11.5 28.5T800-120H560q-17 0-28.5-11.5T520-160Zm-400 0v-160q0-17 11.5-28.5T160-360h240q17 0 28.5 11.5T440-320v160q0 17-11.5 28.5T400-120H160q-17 0-28.5-11.5T120-160Zm80-360h160v-240H200v240Zm400 320h160v-240H600v240Zm0-480h160v-80H600v80ZM200-200h160v-80H200v80Z",
    "delete": "M280-120q-33 0-56.5-23.5T200-200v-520q-17 0-28.5-11.5T160-760q0-17 11.5-28.5T200-800h160q0-17 11.5-28.5T400-840h160q17 0 28.5 11.5T600-800h160q17 0 28.5 11.5T800-760q0 17-11.5 28.5T760-720v520q0 33-23.5 56.5T680-120H280Zm400-600H280v520h400v-520ZM400-280q17 0 28.5-11.5T440-320v-280q0-17-11.5-28.5T400-640q-17 0-28.5 11.5T360-600v280q0 17 11.5 28.5T400-280Zm160 0q17 0 28.5-11.5T600-320v-280q0-17-11.5-28.5T560-640q-17 0-28.5 11.5T520-600v280q0 17 11.5 28.5T560-280Z",
    "description": "M360-240h240q17 0 28.5-11.5T640-280q0-17-11.5-28.5T600-320H360q-17 0-28.5 11.5T320-280q0 17 11.5 28.5T360-240Zm0-160h240q17 0 28.5-11.5T640-440q0-17-11.5-28.5T600-480H360q-17 0-28.5 11.5T320-440q0 17 11.5 28.5T360-400ZM240-80q-33 0-56.5-23.5T160-160v-640q0-33 23.5-56.5T240-880h287q16 0 30.5 6t25.5 17l194 194q11 11 17 25.5t6 30.5v447q0 33-23.5 56.5T720-80H240Zm280-560v-160H240v640h480v-440H560q-17 0-28.5-11.5T520-640Z",
    "draft": "M240-80q-33 0-56.5-23.5T160-160v-640q0-33 23.5-56.5T240-880h287q16 0 30.5 6t25.5 17l194 194q11 11 17 25.5t6 30.5v447q0 33-23.5 56.5T720-80H240Zm280-560v-160H240v640h480v-440H560q-17 0-28.5-11.5T520-640Z",
    "error": "M480-280q17 0 28.5-11.5T520-320q0-17-11.5-28.5T480-360q-17 0-28.5 11.5T440-320q0 17 11.5 28.5T480-280Zm0-160q17 0 28.5-11.5T520-480v-160q0-17-11.5-28.5T480-680q-17 0-28.5 11.5T440-640v160q0 17 11.5 28.5T480-440Zm0 360q-83 0-156-31.5T197-197q-54-54-85.5-127T80-480q0-83 31.5-156T197-763q54-54 127-85.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 83-31.5 156T763-197q-54 54-127 85.5T480-80Zm0-80q134 0 227-93t93-227q0-134-93-227t-227-93q-134 0-227 93t-93 227q0 134 93 227t227 93Z",
    "expand_more": "M480-362q-8 0-15-2.5t-13-8.5L268-557q-11-11-11-28t11-28q11-11 28-11t28 11l156 156 156-156q11-11 28-11t28 11q11 11 11 28t-11 28L508-373q-6 6-13 8.5t-15 2.5Z",
    "folder": "M160-160q-33 0-56.5-23.5T80-240v-480q0-33 23.5-56.5T160-800h207q16 0 30.5 6t25.5 17l57 57h320q33 0 56.5 23.5T880-640v400q0 33-23.5 56.5T800-160H160Zm0-80h640v-400H447l-80-80H160v480Z",
    "folder_open": "M160-160q-33 0-56.5-23.5T80-240v-480q0-33 23.5-56.5T160-800h207q16 0 30.5 6t25.5 17l57 57h360q17 0 28.5 11.5T880-680q0 17-11.5 28.5T840-640H447l-80-80H160v480l79-263q8-26 29.5-41.5T316-560h516q41 0 64.5 32.5T909-457l-72 240q-8 26-29.5 41.5T760-160H160Zm84-80h516l72-240H316l-72 240Z",
    "help": "M478-240q21 0 35.5-14.5T528-290q0-21-14.5-35.5T478-340q-21 0-35.5 14.5T428-290q0 21 14.5 35.5T478-240Zm2 160q-83 0-156-31.5T197-197q-54-54-85.5-127T80-480q0-83 31.5-156T197-763q54-54 127-85.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 83-31.5 156T763-197q-54 54-127 85.5T480-80Zm0-80q134 0 227-93t93-227q0-134-93-227t-227-93q-134 0-227 93t-93 227q0 134 93 227t227 93Zm4-492q25 0 43.5 16t18.5 40q0 22-13.5 39T502-525q-23 20-40.5 44T444-427q0 14 10.5 23.5T479-394q15 0 25.5-10t13.5-25q4-21 18-37.5t30-31.5q23-22 39.5-48t16.5-58q0-51-41.5-83.5T484-720q-38 0-72.5 16T359-655q-7 12-4.5 25.5T368-609q14 8 29 5t25-17q11-15 27.5-23t34.5-8Z",
    "inbox": "M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h560q33 0 56.5 23.5T840-760v560q0 33-23.5 56.5T760-120H200Zm0-80h560v-120H640q-30 38-71.5 59T480-240q-47 0-88.5-21T320-320H200v120Zm280-120q32 0 59-16.5t44-43.5q6-9 15-14.5t20-5.5h142v-360H200v360h142q11 0 20 5.5t15 14.5q17 27 44 43.5t59 16.5Z",
    "info": "M480-280q17 0 28.5-11.5T520-320v-160q0-17-11.5-28.5T480-520q-17 0-28.5 11.5T440-480v160q0 17 11.5 28.5T480-280Zm0-320q17 0 28.5-11.5T520-640q0-17-11.5-28.5T480-680q-17 0-28.5 11.5T440-640q0 17 11.5 28.5T480-600Zm0 520q-83 0-156-31.5T197-197q-54-54-85.5-127T80-480q0-83 31.5-156T197-763q54-54 127-85.5T480-880q83 0 156 31.5T763-763q54 54 85.5 127T880-480q0 83-31.5 156T763-197q-54 54-127 85.5T480-80Zm0-80q134 0 227-93t93-227q0-134-93-227t-227-93q-134 0-227 93t-93 227q0 134 93 227t227 93Z",
    "play_arrow": "M320-273v-414q0-17 12-28.5t28-11.5q5 0 10.5 1.5T381-721l326 207q9 6 13.5 15t4.5 19q0 10-4.5 19T707-446L381-239q-5 3-10.5 4.5T360-233q-16 0-28-11.5T320-273Zm80-73 210-134-210-134v268Z",
    "refresh": "M480-160q-134 0-227-93t-93-227q0-134 93-227t227-93q69 0 132 28.5T720-690v-70q0-17 11.5-28.5T760-800q17 0 28.5 11.5T800-760v200q0 17-11.5 28.5T760-520H560q-17 0-28.5-11.5T520-560q0-17 11.5-28.5T560-600h128q-32-56-87.5-88T480-720q-100 0-170 70t-70 170q0 100 70 170t170 70q68 0 124.5-34.5T692-367q8-14 22.5-19.5t29.5-.5q16 5 23 21t-1 30q-41 80-117 128t-169 48Z",
    "save": "M200-120q-33 0-56.5-23.5T120-200v-560q0-33 23.5-56.5T200-840h447q16 0 30.5 6t25.5 17l114 114q11 11 17 25.5t6 30.5v447q0 33-23.5 56.5T760-120H200Zm560-526L646-760H200v560h560v-446ZM480-240q50 0 85-35t35-85q0-50-35-85t-85-35q-50 0-85 35t-35 85q0 50 35 85t85 35ZM280-560h280q17 0 28.5-11.5T600-600v-80q0-17-11.5-28.5T560-720H280q-17 0-28.5 11.5T240-680v80q0 17 11.5 28.5T280-560Z",
    "settings": "M433-80q-27 0-46.5-18T363-142l-9-66q-13-5-24.5-12T307-235l-62 26q-25 11-50 2t-39-32l-47-82q-14-23-8-49t27-43l53-40q-1-7-1-13.5v-27q0-6.5 1-13.5l-53-40q-21-17-27-43t8-49l47-82q14-23 39-32t50 2l62 26q11-8 23-15t24-12l9-66q4-26 23.5-44t46.5-18h94q27 0 46.5 18t23.5 44l9 66q13 5 24.5 12t22.5 15l62-26q25-11 50-2t39 32l47 82q14 23 8 49t-27 43l-53 40q1 7 1 13.5v27q0 6.5-2 13.5l53 40q21 17 27 43t-8 49l-48 82q-14 23-39 32t-50-2l-60-26q-11 8-23 15t-24 12l-9 66q-4 26-23.5 44T527-80h-94Zm7-80h79l14-106q31-8 57.5-23.5T639-327l99 41 39-68-86-65q5-14 7-29.5t2-31.5q0-16-2-31.5t-7-29.5l86-65-39-68-99 42q-22-23-48.5-38.5T533-694l-13-106h-79l-14 106q-31 8-57.5 23.5T321-633l-99-41-39 68 86 64q-5 15-7 30t-2 32q0 16 2 31t7 30l-86 65 39 68 99-42q22 23 48.5 38.5T427-266l13 106Zm42-180q58 0 99-41t41-99q0-58-41-99t-99-41q-59 0-99.5 41T342-480q0 58 40.5 99t99.5 41Z",
    "stop": "M240-320v-320q0-33 23.5-56.5T320-720h320q33 0 56.5 23.5T720-640v320q0 33-23.5 56.5T640-240H320q-33 0-56.5-23.5T240-320Zm80 0h320v-320H320v320Z",
    "terminal": "M160-160q-33 0-56.5-23.5T80-240v-480q0-33 23.5-56.5T160-800h640q33 0 56.5 23.5T880-720v480q0 33-23.5 56.5T800-160H160Zm0-80h640v-400H160v400Zm187-200-76-76q-12-12-11.5-28t12.5-28q12-11 28-11.5t28 11.5l104 104q12 12 12 28t-12 28L328-308q-11 11-27.5 11.5T272-308q-11-11-11-28t11-28l75-76Zm173 160q-17 0-28.5-11.5T480-320q0-17 11.5-28.5T520-360h160q17 0 28.5 11.5T720-320q0 17-11.5 28.5T680-280H520Z",
    "warning": "M109-120q-11 0-20-5.5T75-140q-5-9-5.5-19.5T75-180l370-640q6-10 15.5-15t19.5-5q10 0 19.5 5t15.5 15l370 640q6 10 5.5 20.5T885-140q-5 9-14 14.5t-20 5.5H109Zm69-80h604L480-720 178-200Zm302-40q17 0 28.5-11.5T520-280q0-17-11.5-28.5T480-320q-17 0-28.5 11.5T440-280q0 17 11.5 28.5T480-240Zm0-120q17 0 28.5-11.5T520-400v-120q0-17-11.5-28.5T480-560q-17 0-28.5 11.5T440-520v120q0 17 11.5 28.5T480-360Z",
}


_COLORS = {
    "Light": {
        "normal": "#334155",
        "primary": "#0f6cbd",
        "selected": "#003e73",
        "disabled": "#8a8a8a",
        "warning": "#8a6500",
        "danger": "#a80000",
    },
    "Dark": {
        "normal": "#d8e0e8",
        "primary": "#8ac7f5",
        "selected": "#ffffff",
        "disabled": "#777777",
        "warning": "#ffd166",
        "danger": "#ffb4ab",
    },
}


class MaterialIconEngine(QIconEngine):
    def __init__(self, name: str, role: str = "normal") -> None:
        super().__init__()
        if name not in _PATHS:
            raise KeyError(f"Unknown Material Symbol: {name}")
        self.name = name
        self.role = role if role in {"normal", "primary", "warning", "danger"} else "normal"

    def clone(self) -> MaterialIconEngine:
        return MaterialIconEngine(self.name, self.role)

    def key(self) -> str:
        return "OpenADB.MaterialSymbolsRounded"

    def iconName(self) -> str:
        return self.name

    def actualSize(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QSize:
        return size

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        renderer = QSvgRenderer(QByteArray(self._svg(mode, state)))
        renderer.render(painter, QRectF(rect))

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self._render_pixmap(size, mode, state)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        scale = max(1.0, float(scale))
        physical_size = QSize(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        pixmap = self._render_pixmap(physical_size, mode, state)
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def _render_pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        safe_size = QSize(max(1, size.width()), max(1, size.height()))
        pixmap = QPixmap(safe_size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.paint(painter, QRect(0, 0, safe_size.width(), safe_size.height()), mode, state)
        painter.end()
        return pixmap

    def _svg(self, mode: QIcon.Mode, state: QIcon.State) -> bytes:
        color = _material_color(self.role, mode, state)
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960">'
            f'<path fill="{color}" d="{_PATHS[self.name]}"/></svg>'
        ).encode("utf-8")


def material_icon(name: str, role: str = "normal") -> QIcon:
    return QIcon(MaterialIconEngine(name, role))


def material_icon_names() -> tuple[str, ...]:
    return tuple(sorted(_PATHS))


def _material_color(role: str, mode: QIcon.Mode, state: QIcon.State) -> str:
    app = QApplication.instance()
    theme = str(app.property("openadbResolvedTheme") if app is not None else "Light")
    colors = _COLORS["Dark" if theme == "Dark" else "Light"]
    if mode == QIcon.Disabled:
        return colors["disabled"]
    if mode == QIcon.Selected:
        return colors["selected"]
    if mode == QIcon.Active or state == QIcon.On:
        return colors["primary"]
    return colors[role]


def _standard_icon_specs() -> dict[QStyle.StandardPixmap, tuple[str, str, bool]]:
    standard = QStyle.StandardPixmap
    result: dict[QStyle.StandardPixmap, tuple[str, str, bool]] = {}
    entries = (
        ("SP_ComputerIcon", "computer", "normal", False),
        ("SP_FileDialogListView", "apps", "normal", False),
        ("SP_DriveHDIcon", "backup", "normal", False),
        ("SP_DirIcon", "folder", "normal", False),
        ("SP_DirOpenIcon", "folder_open", "normal", False),
        ("SP_FileIcon", "draft", "normal", False),
        ("SP_FileDialogDetailedView", "settings", "normal", False),
        ("SP_MediaPlay", "play_arrow", "normal", False),
        ("SP_MediaStop", "stop", "normal", False),
        ("SP_ArrowLeft", "chevron_left", "normal", False),
        ("SP_ArrowRight", "chevron_right", "normal", False),
        ("SP_ArrowUp", "arrow_upward", "normal", False),
        ("SP_ArrowDown", "arrow_downward", "normal", False),
        ("SP_BrowserReload", "refresh", "normal", False),
        ("SP_TrashIcon", "delete", "danger", False),
        ("SP_FileDialogNewFolder", "create_new_folder", "normal", False),
        ("SP_FileDialogBack", "chevron_left", "normal", False),
        ("SP_FileDialogToParent", "arrow_upward", "normal", False),
        ("SP_FileDialogInfoView", "info", "primary", False),
        ("SP_MessageBoxInformation", "info", "primary", True),
        ("SP_MessageBoxWarning", "warning", "warning", True),
        ("SP_MessageBoxCritical", "error", "danger", True),
        ("SP_MessageBoxQuestion", "help", "primary", True),
        ("SP_DialogOpenButton", "folder_open", "normal", False),
        ("SP_DialogSaveButton", "save", "normal", False),
        ("SP_DialogCloseButton", "close", "normal", False),
        ("SP_DialogCancelButton", "close", "normal", False),
        ("SP_DialogApplyButton", "check", "normal", False),
        ("SP_DialogResetButton", "refresh", "normal", False),
        ("SP_DialogDiscardButton", "delete", "danger", False),
        ("SP_DialogYesButton", "check", "normal", False),
        ("SP_DialogNoButton", "close", "normal", False),
    )
    for enum_name, icon_name, role, large in entries:
        value = getattr(standard, enum_name, None)
        if value is not None:
            result[value] = (icon_name, role, large)
    return result


_STANDARD_ICON_SPECS = _standard_icon_specs()


class MaterialProxyStyle(QProxyStyle):
    """Fusion behavior with Material Symbols replacing platform stock icons."""

    def standardIcon(self, standard_icon, option=None, widget=None) -> QIcon:
        spec = _STANDARD_ICON_SPECS.get(standard_icon)
        if spec is not None:
            return material_icon(spec[0], spec[1])
        return super().standardIcon(standard_icon, option, widget)

    def standardPixmap(self, standard_pixmap, option=None, widget=None) -> QPixmap:
        spec = _STANDARD_ICON_SPECS.get(standard_pixmap)
        if spec is None:
            return super().standardPixmap(standard_pixmap, option, widget)
        metric = QStyle.PixelMetric.PM_MessageBoxIconSize if spec[2] else QStyle.PixelMetric.PM_SmallIconSize
        size = max(16, self.pixelMetric(metric, option, widget))
        return material_icon(spec[0], spec[1]).pixmap(size, size)
