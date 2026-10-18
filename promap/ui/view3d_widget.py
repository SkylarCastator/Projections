"""Qt host for the embedded Panda3D scene: a native child window Panda renders into."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QKeyEvent, QWheelEvent
from PySide6.QtWidgets import QWidget, QSizePolicy


class View3DWidget(QWidget):
    """Owns no Panda state; the main window creates a View3D with this widget's winId() as parent
    and hands it over with attach(). Keys and wheel are forwarded because the Panda child window
    does not reliably receive X keyboard focus."""

    KEYS = {Qt.Key_F: "f", Qt.Key_G: "g"} | {getattr(Qt, f"Key_{i}"): str(i) for i in range(1, 10)}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view3d = None
        self.setAttribute(Qt.WA_NativeWindow)
        self.setAttribute(Qt.WA_PaintOnScreen)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 240)
        self.setToolTip("1 orbit, 2 through projector, 3..9 cameras, f reset, g grid; drag rotates, right-drag pans, wheel zooms")

    def paintEngine(self):  # Panda paints; Qt must not
        return None

    def sizeHint(self) -> QSize:
        return QSize(640, 480)

    def attach(self, view3d):
        self.view3d = view3d
        if view3d is not None:
            view3d.resize(self.width(), self.height())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.view3d is not None and self.view3d.alive:
            self.view3d.resize(ev.size().width(), ev.size().height())

    def keyPressEvent(self, ev: QKeyEvent):
        ch = self.KEYS.get(ev.key())
        if ch and self.view3d is not None:
            self.view3d.key(ch)
            return
        super().keyPressEvent(ev)

    def wheelEvent(self, ev: QWheelEvent):
        if self.view3d is not None and self.view3d.alive:
            self.view3d.key("wheel_up" if ev.angleDelta().y() > 0 else "wheel_down")
            return
        super().wheelEvent(ev)

    def mousePressEvent(self, ev):
        self.setFocus()
        super().mousePressEvent(ev)
