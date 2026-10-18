from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QCloseEvent
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QFileDialog, QMessageBox, QInputDialog, QApplication, QLabel, QProgressBar,
    QScrollArea,
)

from promap.ui.canvas import EditorCanvas
from promap.ui.cue_panel import CuePanel
from promap.ui.output_window import OutputWindow
from promap.ui.state import AppState
from promap.ui.surface_panel import SurfacePanel
from promap.ui.scene_panel import ScenePanel
from promap.ui.camera_panel import CameraPanel
from promap.ui.animation_panel import AnimationPanel
from promap.ui.timeline_panel import TimelinePanel
from promap.ui.hardware_dialog import HardwareDialog
from promap.model.hardware import HardwareLibrary
from promap.audio.engine import AudioEngine
from promap.model.settings import Settings
from promap.ui.settings_dialog import SettingsDialog
from promap.ui.generate_dialog import GenerateDialog

PROJECT_FILTER = "promap project (*.promap *.json)"


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.output: OutputWindow | None = None
        self.view3d = None
        self.hardware = HardwareLibrary.load()
        state.hardware = self.hardware
        self.settings = Settings.load()
        self.audio = AudioEngine()
        state.audio = self.audio
        state.engine.audio = self.audio
        self.resize(1400, 900)

        self.canvas = EditorCanvas(state)
        self.setCentralWidget(self.canvas)

        def scrolled(panel):
            """Panels keep their natural height; the dock scrolls instead of squashing them."""
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QScrollArea.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            area.setWidget(panel)
            return area

        self.surface_panel = SurfacePanel(state)
        dock = QDockWidget("Surfaces", self)
        dock.setWidget(scrolled(self.surface_panel))
        dock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        dock.setMinimumWidth(360)

        self.scene_panel = ScenePanel(state)
        sdock = QDockWidget("3D scene", self)
        sdock.setWidget(scrolled(self.scene_panel))
        sdock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.RightDockWidgetArea, sdock)
        self.tabifyDockWidget(dock, sdock)

        self.camera_panel = CameraPanel(state)
        camdock = QDockWidget("Camera", self)
        camdock.setWidget(scrolled(self.camera_panel))
        camdock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.RightDockWidgetArea, camdock)
        self.tabifyDockWidget(sdock, camdock)

        self.animation_panel = AnimationPanel(state)
        adock = QDockWidget("Animations", self)
        adock.setWidget(scrolled(self.animation_panel))
        adock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.RightDockWidgetArea, adock)
        self.tabifyDockWidget(camdock, adock)
        dock.raise_()

        cdock = QDockWidget("Cues", self)
        cdock.setWidget(CuePanel(state))
        cdock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.BottomDockWidgetArea, cdock)
        self.timeline_panel = TimelinePanel(state)
        tdock = QDockWidget("Timeline", self)
        tdock.setWidget(self.timeline_panel)
        tdock.setFeatures(QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.BottomDockWidgetArea, tdock)
        self.tabifyDockWidget(cdock, tdock)
        cdock.raise_()
        self.resizeDocks([cdock], [220], Qt.Vertical)

        self._build_menus()
        self.level = QProgressBar()
        self.level.setRange(0, 100)
        self.level.setTextVisible(False)
        self.level.setFixedWidth(120)
        self.level.setToolTip("audio level (rms)")
        self.statusBar().addPermanentWidget(QLabel("♪"))
        self.statusBar().addPermanentWidget(self.level)
        self.fps_label = QLabel()
        self.statusBar().addPermanentWidget(self.fps_label)
        if not self.audio.available:
            self.statusBar().showMessage(f"Audio output unavailable: {self.audio.error}", 8000)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self._lasso_sync = QTimer(self)
        self._lasso_sync.setInterval(200)
        self._lasso_sync.timeout.connect(lambda: self.lasso_act.setChecked(self.canvas.lasso_mode) if hasattr(self, "lasso_act") else None)
        self._lasso_sync.start()
        self.timer.timeout.connect(self._frame)
        self.timer.start(16)
        self._fps_t = time.monotonic()
        self._fps_n = 0

        state.projectReplaced.connect(self._title)
        state.surfacesChanged.connect(self._title)
        state.cuesChanged.connect(self._title)
        self._title()

    # -- menus -------------------------------------------------------------------
    def _act(self, menu, text, fn, shortcut=None, checkable=False):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
            a.setShortcutContext(Qt.ApplicationShortcut)
        a.setCheckable(checkable)
        a.triggered.connect(fn)
        menu.addAction(a)
        return a

    def _build_menus(self):
        mb = self.menuBar()
        f = mb.addMenu("&File")
        self._act(f, "New", self._new, "Ctrl+N")
        self._act(f, "Open…", self._open, "Ctrl+O")
        self._act(f, "Save", self._save, "Ctrl+S")
        self._act(f, "Save As…", self._save_as, "Ctrl+Shift+S")
        f.addSeparator()
        self._act(f, "Quit", self.close, "Ctrl+Q")

        e = mb.addMenu("&Edit")
        self.undo_act = self._act(e, "Undo", self.state.undo, "Ctrl+Z")
        self.redo_act = self._act(e, "Redo", self.state.redo, "Ctrl+Shift+Z")
        self._act(e, "Redo (alt)", self.state.redo, "Ctrl+Y")
        self.state.undoChanged.connect(self._undo_state)
        self._undo_state()
        e.addSeparator()
        self.lasso_act = self._act(e, "Lasso tool  (draw a shape, get a mesh)", self.canvas.set_lasso_mode, "L", checkable=True)

        vw = mb.addMenu("&View")
        self._act(vw, "Zoom in", lambda: self.canvas.zoom_by(1.25), "Ctrl++")
        self._act(vw, "Zoom out", lambda: self.canvas.zoom_by(0.8), "Ctrl+-")
        self._act(vw, "Zoom to fit", self.canvas.zoom_fit, "Ctrl+0")
        vw.addSeparator()
        hint = vw.addAction("Wheel zooms the canvas; middle-drag or Alt-drag pans; Ctrl+wheel zooms the camera preview and timeline")
        hint.setEnabled(False)

        o = mb.addMenu("&Output")
        self.screen_menu = o.addMenu("Open on screen")
        self.screen_menu.aboutToShow.connect(self._fill_screens)
        self._act(o, "Close output", self.close_output, "Ctrl+W")
        self.pattern_act = self._act(o, "Test pattern", self.state.set_test_pattern, "T", checkable=True)
        o.addSeparator()
        self._act(o, "Set output size…", self._set_output_size)
        self._act(o, "Match output size to a screen…", self._match_screen)

        s = mb.addMenu("&Show")
        self._act(s, "GO", self.state.go, "Space")
        self._act(s, "Back", self.state.back, "Backspace")
        self._act(s, "First cue", lambda: self.state.goto(0), "Home")
        self._act(s, "Preview mode", self.state.preview_mode, "Ctrl+P")
        self.blackout_act = self._act(s, "Blackout", self.state.set_blackout, "B", checkable=True)
        s.addSeparator()
        self.mute_act = self._act(s, "Mute audio", self._mute, "M", checkable=True)
        self._act(s, "Audio volume…", self._volume)
        self.state.showChanged.connect(lambda: self.blackout_act.setChecked(self.state.engine.blackout))
        self.state.showChanged.connect(lambda: self.pattern_act.setChecked(self.state.test_pattern))

        su = mb.addMenu("S&urface")
        self._act(su, "Add quad", lambda: self.state.add_surface("media"))
        self._act(su, "Add triangle", lambda: self.state.add_surface("media", mesh="triangle"), "Ctrl+T")
        self._act(su, "Add square (2 triangles)", lambda: self.state.add_surface("media", mesh="square"))
        self._act(su, "Add mask", lambda: self.state.add_surface("mask"))
        su.addSeparator()
        self._act(su, "Convert quad to mesh (4x4)", lambda: self.state.convert_to_mesh(n=4), "Ctrl+M")
        self._act(su, "Convert mesh to quad", self.state.convert_to_quad)
        su.addSeparator()
        self._act(su, "Extrude triangle from edge  (A in canvas)", self.state.extrude)
        self._act(su, "Delete triangle  (Delete in canvas)", self.state.delete_triangle)
        self._act(su, "Detach triangle", self.state.detach_triangle)
        self._act(su, "Subdivide", self.state.subdivide)
        self._act(su, "Weld vertices", self.state.weld)
        self._act(su, "Merge with mesh above", self.state.merge_with_above)
        self._act(su, "Fit UVs to bounds", self.state.fit_uvs)
        self.uv_act = self._act(su, "UV view  (U in canvas)", self.state.set_uv_mode, checkable=True)
        self.state.selectionChanged.connect(lambda _: self.uv_act.setChecked(self.state.uv_mode))

        hw = mb.addMenu("&Hardware")
        self._act(hw, "Library…", self._hardware, "Ctrl+H")

        g = mb.addMenu("&Generate")
        self._act(g, "Generate media with ComfyUI…", self._generate, "Ctrl+G")
        self._act(g, "Settings…", self._settings)

        v = mb.addMenu("&3D")
        self._act(v, "Open 3D view", self.open_view3d, "F3")
        self._act(v, "Close 3D view", self.close_view3d)
        v.addSeparator()
        self._act(v, "View: orbit", lambda: self._set_view3d("orbit"))
        self._act(v, "View: through projector", lambda: self._set_view3d("projector"))
        self.state.view3dRequested.connect(self._view3d_request)

    def _fill_screens(self):
        self.screen_menu.clear()
        for scr in QGuiApplication.screens():
            g = scr.geometry()
            a = self.screen_menu.addAction(f"{scr.name()}  {g.width()}x{g.height()} @ {g.x()},{g.y()}")
            a.triggered.connect(lambda _=False, name=scr.name(): self.open_output(name))

    # -- output --------------------------------------------------------------------
    def open_output(self, screen_name: str | None = None):
        screens = QGuiApplication.screens()
        screen = next((s for s in screens if s.name() == screen_name), None)
        if screen is None:
            screen = next((s for s in screens if s is not self.screen()), screens[0])
        if screen is self.screen() and len(screens) > 1:
            if QMessageBox.question(self, "Open output", f"{screen.name()} is the screen this editor is on. The output window is "
                                    "fullscreen and stays on top, so it will cover the editor (Esc in it closes it, Ctrl+W too). "
                                    "Open it here anyway?") != QMessageBox.Yes:
                return
        self.close_output()
        self.output = OutputWindow(self.state)
        self.output.closed.connect(self._output_closed)
        self.output.open_on(screen)
        self.state.project.output_screen = screen.name()
        self.statusBar().showMessage(f"Output on {screen.name()} (Esc in that window closes it)", 5000)
        self.activateWindow()

    def close_output(self):
        if self.output is not None:
            self.output.close()
            self.output = None

    def _output_closed(self):
        self.output = None

    def _set_output_size(self):
        w, h = self.state.project.output_size
        txt, ok = QInputDialog.getText(self, "Output size", "Width x Height (projector resolution):", text=f"{w}x{h}")
        if not ok:
            return
        try:
            nw, nh = (int(v) for v in txt.lower().replace(" ", "").split("x"))
        except ValueError:
            QMessageBox.warning(self, "Output size", "Use the form 1920x1080")
            return
        self.state.project.output_width, self.state.project.output_height = nw, nh
        self.state.surface_edited(None, geometry_only=True)

    def _match_screen(self):
        names = [f"{s.name()} {s.geometry().width()}x{s.geometry().height()}" for s in QGuiApplication.screens()]
        choice, ok = QInputDialog.getItem(self, "Match screen", "Screen:", names, 0, False)
        if ok:
            scr = QGuiApplication.screens()[names.index(choice)]
            g = scr.geometry()
            self.state.project.output_width, self.state.project.output_height = g.width(), g.height()
            self.state.surface_edited(None, geometry_only=True)

    def _mute(self, on: bool):
        self.audio.muted = on

    def _volume(self):
        v, ok = QInputDialog.getInt(self, "Audio volume", "Master volume (%):", int(self.audio.master * 100), 0, 100)
        if ok:
            self.audio.master = v / 100.0

    def _generate(self):
        GenerateDialog(self.state, self.settings, self).exec()

    def _settings(self):
        SettingsDialog(self.settings, self).exec()

    def _hardware(self):
        HardwareDialog(self.state, self.hardware, self).exec()

    # -- 3D view ------------------------------------------------------------------------
    def open_view3d(self):
        if self.view3d is not None and self.view3d.alive:
            return
        try:
            from promap.scene.view3d import View3D
            self.view3d = View3D(self.state)
            self.state.view3d_open = True
        except Exception as exc:
            QMessageBox.critical(self, "3D view", f"Could not start Panda3D:\n{exc}")
            self.view3d = None
            self.state.view3d_open = False

    def close_view3d(self):
        if self.view3d is not None:
            self.view3d.close()
            self.view3d = None
        self.state.view3d_open = False

    def _set_view3d(self, view: str):
        if self.view3d is None or not self.view3d.alive:
            self.open_view3d()
        if self.view3d is not None:
            self.view3d.set_view(view)

    def _view3d_request(self, what: str):
        if what == "open":
            self.open_view3d()
        elif what == "close":
            self.close_view3d()
        else:
            self._set_view3d(what)

    # -- project file ----------------------------------------------------------------
    def _undo_state(self):
        self.undo_act.setEnabled(self.state.can_undo)
        self.redo_act.setEnabled(self.state.can_redo)

    def _confirm_discard(self) -> bool:
        if not self.state.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Discard unsaved changes?", QMessageBox.Yes | QMessageBox.No)
        return r == QMessageBox.Yes

    def _new(self):
        if self._confirm_discard():
            self.state.new_project()

    def _open(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", "", PROJECT_FILTER)
        if path:
            self.load_project(path)

    def load_project(self, path: str | Path):
        try:
            self.state.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", str(exc))

    def _save(self):
        if self.state.project.path is None:
            return self._save_as()
        self.state.save()
        self._title()

    def _save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save project", str(self.state.project.base_dir), PROJECT_FILTER)
        if not path:
            return
        if not path.endswith((".promap", ".json")):
            path += ".promap"
        self.state.save(path)
        self._title()

    def _title(self):
        p = self.state.project
        name = p.path.name if p.path else "untitled"
        self.setWindowTitle(f"promap — {name}{' *' if self.state.dirty else ''}")

    # -- frame loop -------------------------------------------------------------------
    def _frame(self):
        if self.state.view3d_open and (self.view3d is None or not self.view3d.alive):
            self.state.view3d_open = False
        self.state.tick()
        self.canvas.update()
        if self.output is not None:
            self.output.update()
        if self.view3d is not None:
            if self.view3d.alive:
                self.view3d.step()
            else:
                self.view3d = None
        self.level.setValue(int(self.audio.analysis.level("rms") * 100))
        self._fps_n += 1
        now = time.monotonic()
        if now - self._fps_t >= 1.0:
            errs = self.state.engine.load_errors
            msg = f"{self._fps_n / (now - self._fps_t):.0f} fps"
            if errs:
                msg += f"   |   {len(errs)} media file(s) missing"
            self.fps_label.setText(msg)
            self._fps_t, self._fps_n = now, 0

    def closeEvent(self, ev: QCloseEvent):
        if not self._confirm_discard():
            ev.ignore()
            return
        self.close_output()
        self.close_view3d()
        self.camera_panel.shutdown()
        self.audio.close()
        super().closeEvent(ev)
