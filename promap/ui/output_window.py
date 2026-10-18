"""Fullscreen, frameless output window placed on the projector's screen."""
from __future__ import annotations

import moderngl
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QScreen, QKeyEvent
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from promap.render.renderer import Renderer, letterbox
from promap.ui.state import AppState


class OutputWindow(QOpenGLWidget):
    closed = Signal()

    def __init__(self, state: AppState):
        super().__init__(None)
        self.state = state
        self.ctx = None
        self.renderer = None
        self.setWindowTitle("promap output")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setCursor(Qt.BlankCursor)
        self.frames = 0

    def open_on(self, screen: QScreen):
        self.winId()  # force a native window so windowHandle() exists
        self.windowHandle().setScreen(screen)
        self.setGeometry(screen.geometry())
        self.showFullScreen()

    def initializeGL(self):
        self.ctx = moderngl.create_context()
        self.renderer = Renderer(self.ctx)

    def paintGL(self):
        fbo = self.ctx.detect_framebuffer()
        out_w, out_h = self.state.project.output_size
        x, y, w, h = letterbox(out_w, out_h, fbo.width, fbo.height)
        outlines = self.state.outlines() if self.state.test_pattern else None
        self.renderer.render(
            fbo, (out_w, out_h), self.state.render_items(), (x, fbo.height - y - h, w, h), outlines=outlines
        )
        self.frames += 1

    def keyPressEvent(self, ev: QKeyEvent):
        if ev.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(ev)

    def closeEvent(self, ev):
        self.closed.emit()
        super().closeEvent(ev)
