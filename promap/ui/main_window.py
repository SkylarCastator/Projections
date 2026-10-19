from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QCloseEvent
from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QFileDialog, QMessageBox, QInputDialog, QApplication, QLabel, QProgressBar,
    QScrollArea, QToolBar, QTabBar, QStackedWidget, QWidget, QVBoxLayout,
)

from promap.ui.canvas import EditorCanvas
from promap.ui.view3d_widget import View3DWidget
from promap.ui.cue_panel import CuePanel
from promap.ui.output_window import OutputWindow
from promap.ui.state import AppState
from promap.ui.surface_panel import SurfacePanel
from promap.ui.scene_panel import ScenePanel
from promap.ui.camera_panel import CameraPanel
from promap.ui.animation_panel import AnimationPanel
from promap.ui.cue_editor import CueEditor
from promap.ui.preferences_dialog import PreferencesDialog
from promap.model.hardware import HardwareLibrary
from promap.audio.engine import AudioEngine
from promap.model.settings import Settings
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
        self.view3d_widget = View3DWidget()  # Panda3D scene, shown by the 3D tab
        # Tabs above the viewer: one per output screen, "All" (every output in one view), "3D".
        self.tabs = QTabBar()
        self.tabs.setDocumentMode(True)
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.canvas)
        self.stack.addWidget(self.view3d_widget)
        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(self.tabs)
        cl.addWidget(self._build_view_bar())
        cl.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self._refresh_tabs()
        self.tabs.currentChanged.connect(self._tab_changed)

        def scrolled(panel):
            """Panels keep their natural height; the dock scrolls instead of squashing them."""
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QScrollArea.NoFrame)
            area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            area.setWidget(panel)
            return area

        self.docks: dict[str, QDockWidget] = {}

        def dock_for(title: str, widget, area, tab_after: QDockWidget | None = None) -> QDockWidget:
            """Every panel is a dock: drag it out by its title bar (or double-click it) to get a
            separate window, close it, and bring it back from View -> Panels."""
            d = QDockWidget(title, self)
            d.setObjectName("dock_" + title.lower().replace(" ", "_"))
            d.setWidget(widget)
            d.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable | QDockWidget.DockWidgetClosable)
            self.addDockWidget(area, d)
            if tab_after is not None:
                self.tabifyDockWidget(tab_after, d)
            self.docks[title] = d
            return d

        # One side panel that follows the viewer tab: Surfaces on a screen tab, 3D scene on the 3D tab.
        self.surface_panel = SurfacePanel(state)
        self.scene_panel = ScenePanel(state)
        self.side_stack = QStackedWidget()
        self.side_stack.addWidget(scrolled(self.surface_panel))
        self.side_stack.addWidget(scrolled(self.scene_panel))
        dock = dock_for("Surfaces", self.side_stack, Qt.RightDockWidgetArea)
        dock.setMinimumWidth(360)
        self.side_dock = dock

        def tool_window(title: str, widget, size: tuple[int, int]) -> QMainWindow:
            """A top-level window of its own (taskbar entry, any screen), closed until View opens it."""
            w = QMainWindow(self, Qt.Window)
            w.setWindowTitle(f"promap — {title}")
            w.setCentralWidget(scrolled(widget))
            w.resize(*size)
            w.hide()
            return w

        self.camera_panel = CameraPanel(state)
        self.camera_window = tool_window("Camera", self.camera_panel, (600, 900))
        self.animation_panel = AnimationPanel(state)
        self.animations_window = tool_window("Animations", self.animation_panel, (520, 800))
        dock.raise_()

        # Cues and the cue editor (the playable content) live in their own window, opened from View.
        self.cue_panel = CuePanel(state)
        self.cue_editor = CueEditor(state)
        self.cues_window = QMainWindow(self, Qt.Window)
        self.cues_window.setWindowTitle("promap — Cues")
        self.cues_window.setObjectName("cues_window")
        self.cues_window.resize(1400, 520)
        self.cues_window.setCentralWidget(self.cue_editor)
        cue_dock = QDockWidget("Cue list", self.cues_window)
        cue_dock.setObjectName("cuesdock_list")
        cue_dock.setWidget(self.cue_panel)
        cue_dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.cues_window.addDockWidget(Qt.LeftDockWidgetArea, cue_dock)
        self.cues_window.resizeDocks([cue_dock], [520], Qt.Horizontal)
        self.cues_window.hide()
        self._default_layout = self.saveState()

        self._build_tools()  # before the menus: Edit reuses the lasso action
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

    # -- tool palette ---------------------------------------------------------------
    def _build_tools(self):
        """Vertical palette on the left of the canvas: the things that add to the scene, and the lasso."""
        tb = QToolBar("Tools", self)
        tb.setObjectName("tools")
        tb.setOrientation(Qt.Vertical)
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        tb.setStyleSheet("QToolBar { spacing: 2px; padding: 4px 2px; } QToolButton { font-size: 20px; min-width: 36px; min-height: 36px; }")
        self.addToolBar(Qt.LeftToolBarArea, tb)
        st = self.state

        def tool(glyph, tip, fn, shortcut=None, checkable=False):
            a = QAction(glyph, self)
            a.setToolTip(tip + (f"  ({shortcut})" if shortcut else ""))
            a.setStatusTip(tip)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if checkable:
                a.setCheckable(True)
                a.toggled.connect(fn)
            else:
                a.triggered.connect(lambda *_: fn())
            tb.addAction(a)
            return a

        tool("▢", "Add media surface: a four-corner quad with a perspective warp", lambda: st.add_surface("media"))
        tool("△", "Add triangle: A / double-click an edge grows more, drag vertices together to join them",
             lambda: st.add_surface("media", mesh="triangle"))
        tool("▣", "Add mask: a black quad to blank spill", lambda: st.add_surface("mask"))
        tool("▦", "Add reference image: a photo of the space, editor-only", self.surface_panel.add_reference)
        tb.addSeparator()
        self.lasso_act = tool("⌇", "Lasso tool: draw a shape round a prop, get a mesh", self.canvas.set_lasso_mode, "L", checkable=True)

    # -- panels ---------------------------------------------------------------------
    def open_camera_window(self):
        self._open_tool_window(self.camera_window)

    def open_animations_window(self):
        self._open_tool_window(self.animations_window)

    def open_cues_window(self):
        self._open_tool_window(self.cues_window)

    def _open_tool_window(self, w: QMainWindow):
        """Show a tool window; first time, place it beside the editor at the editor's height."""
        if not w.isVisible():
            if not w.property("placed"):
                if w is self.cues_window:
                    w.move(self.frameGeometry().left(), self.frameGeometry().bottom() + 8)
                else:
                    w.resize(w.width(), max(700, self.height() - 40))
                    w.move(self.frameGeometry().right() + 8, self.frameGeometry().top())
                w.setProperty("placed", True)
            w.show()
        w.raise_()
        w.activateWindow()

    def reset_layout(self):
        self.restoreState(self._default_layout)
        for d in self.docks.values():
            d.show()

    @staticmethod
    def _qsettings():
        from PySide6.QtCore import QSettings

        return QSettings("promap", "promap")

    def restore_layout(self):
        qs = self._qsettings()
        geo, st = qs.value("window/geometry"), qs.value("window/state")
        if geo is not None:
            self.restoreGeometry(geo)
        if st is not None:
            self.restoreState(st)
        for key, w in self._tool_windows():
            g = qs.value(f"{key}/geometry")
            if g is not None:
                w.restoreGeometry(g)
                w.setProperty("placed", True)
            if qs.value(f"{key}/open", False, type=bool):
                w.show()

    def _tool_windows(self):
        return (("camera", self.camera_window), ("animations", self.animations_window), ("cues", self.cues_window))

    def save_layout(self):
        qs = self._qsettings()
        qs.setValue("window/geometry", self.saveGeometry())
        qs.setValue("window/state", self.saveState())
        for key, w in self._tool_windows():
            qs.setValue(f"{key}/geometry", w.saveGeometry())
            qs.setValue(f"{key}/open", w.isVisible())

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
        self._act(e, "Preferences…", self._preferences, "Ctrl+,")

        vw = mb.addMenu("&View")
        panels = vw.addMenu("Panels")
        for title, d in self.docks.items():
            panels.addAction(d.toggleViewAction())
        panels.addSeparator()
        hint2 = panels.addAction("Drag a panel's title bar out of the window (or double-click it) to detach it")
        hint2.setEnabled(False)
        self._act(vw, "Camera window", self.open_camera_window, "Ctrl+Shift+C")
        self._act(vw, "Animations window", self.open_animations_window, "Ctrl+Shift+A")
        self._act(vw, "Cues window", self.open_cues_window, "Ctrl+Shift+U")
        self._act(vw, "Reset panel layout", self.reset_layout)
        # Keyboard-only actions (the buttons live in the strip at the top of the viewer)
        for text, fn, key, checkable in [
            ("Zoom in", lambda: self.canvas.zoom_by(1.25), "Ctrl++", False),
            ("Zoom in (alt)", lambda: self.canvas.zoom_by(1.25), "Ctrl+=", False),
            ("Zoom out", lambda: self.canvas.zoom_by(0.8), "Ctrl+-", False),
            ("Zoom to fit", self.canvas.zoom_fit, "Ctrl+0", False),
            ("3D scene tab", self.toggle_view3d, "F3", True),
        ]:
            a = QAction(text, self)
            a.setShortcut(QKeySequence(key))
            a.setShortcutContext(Qt.WindowShortcut)
            if checkable:
                a.setCheckable(True)
                a.toggled.connect(fn)
                self.view3d_act = a
            else:
                a.triggered.connect(lambda *_, f=fn: f())
            self.addAction(a)
        self.state.view3dRequested.connect(self._view3d_request)
        self.state.windowRequested.connect(self._window_request)

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

        g = mb.addMenu("&Generate")
        self._act(g, "Generate media with ComfyUI…", self._generate, "Ctrl+G")


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

    def _preferences(self, tab: str = "Hardware"):
        PreferencesDialog(self.state, self.hardware, self.settings, self, tab=tab).exec()

    def _settings(self):
        self._preferences("Generation")

    def _hardware(self):
        self._preferences("Hardware")

    # -- 3D view ------------------------------------------------------------------------
    def open_view3d(self, embedded: bool = True):
        """Start the Panda3D scene: in the 3D tab, or (embedded=False) in its own window."""
        if getattr(self, "_opening3d", False):
            return  # re-entered via the tab-change signal; the outer call finishes the job
        self._opening3d = True
        try:
            self._open_view3d(embedded)
        finally:
            self._opening3d = False

    def _open_view3d(self, embedded: bool):
        if self.view3d is not None and self.view3d.alive:
            if self.view3d.embedded == embedded:
                if embedded:
                    self._select_tab("3d")
                return
            self.close_view3d()
        try:
            from promap.scene.view3d import View3D

            if embedded:
                self._select_tab("3d")
                QApplication.processEvents()  # native child window exists and has its final size
                vw = self.view3d_widget
                self.view3d = View3D(self.state, parent_handle=int(vw.winId()), size=(max(vw.width(), 320), max(vw.height(), 240)))
                vw.attach(self.view3d)
                vw.setFocus()
            else:
                if self._tab_kind() == "3d":
                    self.tabs.setCurrentIndex(0)
                self.view3d = View3D(self.state)
            self.state.view3d_open = True
        except Exception as exc:
            QMessageBox.critical(self, "3D view", f"Could not start Panda3D:\n{exc}")
            self.view3d = None
            if self._tab_kind() == "3d":
                self.tabs.setCurrentIndex(0)
            self.state.view3d_open = False
        self._sync_view3d_act()

    def close_view3d(self):
        if self.view3d is not None:
            self.view3d.close()
            self.view3d = None
        self.view3d_widget.attach(None)
        if self._tab_kind() == "3d":
            self.tabs.setCurrentIndex(0)
        self.state.view3d_open = False
        self._sync_view3d_act()

    def toggle_view3d(self, on: bool):
        if on:
            self.open_view3d(embedded=True)
        else:
            self.close_view3d()

    # -- viewer strip -------------------------------------------------------------------
    def _build_view_bar(self) -> QToolBar:
        """Controls that belong to the view itself, Unity style: a strip along the top of the
        viewer whose buttons change with the tab (zoom for the 2D canvas, viewpoints for 3D)."""
        bar = QToolBar("View tools", self)
        bar.setObjectName("view_bar")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        bar.setStyleSheet("QToolBar { border: 0; padding: 1px 4px; spacing: 2px; } QToolButton { padding: 2px 8px; }")
        self.view_bar = bar
        self._bar_2d: list[QAction] = []
        self._bar_3d: list[QAction] = []

        def btn(group, text, tip, fn, checkable=False):
            a = QAction(text, self)
            a.setToolTip(tip)
            a.setStatusTip(tip)
            if checkable:
                a.setCheckable(True)
                a.toggled.connect(fn)
            else:
                a.triggered.connect(lambda *_: fn())
            bar.addAction(a)
            group.append(a)
            return a

        def label(group, text, tip=""):
            lab = QLabel(text)
            lab.setStyleSheet("color: #888; padding: 0 6px;")
            lab.setToolTip(tip)
            a = bar.addWidget(lab)
            group.append(a)
            return lab

        # 2D canvas
        btn(self._bar_2d, "−", "Zoom out  (Ctrl+-, or wheel)", lambda: self.canvas.zoom_by(0.8))
        self.zoom_label = label(self._bar_2d, "100%", "Canvas zoom relative to fit")
        btn(self._bar_2d, "+", "Zoom in  (Ctrl++, or wheel)", lambda: self.canvas.zoom_by(1.25))
        btn(self._bar_2d, "Fit", "Zoom to fit the output in the canvas  (Ctrl+0)", self.canvas.zoom_fit)
        self._bar_2d.append(bar.addSeparator())
        label(self._bar_2d, "wheel zooms · middle-drag or Alt-drag pans · L lasso",
              "Ctrl+wheel zooms the camera preview and the timeline")
        # 3D scene
        btn(self._bar_3d, "Orbit", "Free orbit  (1)", lambda: self._set_view3d("orbit"))
        btn(self._bar_3d, "Projector", "Look through the projector's lens  (2)", lambda: self._set_view3d("projector"))
        btn(self._bar_3d, "Reset", "Reset the orbit camera  (f)", lambda: self._view3d_call("_reset_orbit"))
        btn(self._bar_3d, "Grid", "Show / hide the floor grid  (g)", lambda: self._view3d_call("_toggle_grid"))
        self._bar_3d.append(bar.addSeparator())
        btn(self._bar_3d, "Pop out ↗", "Move the 3D scene to its own window (for a second screen)",
            lambda: self.open_view3d(embedded=False))
        label(self._bar_3d, "drag rotates · right-drag pans · wheel zooms · 3..9 cameras")
        self._refresh_view_bar("output")
        return bar

    def _refresh_view_bar(self, kind: str | None):
        three = kind == "3d"
        for a in self._bar_2d:
            a.setVisible(not three)
        for a in self._bar_3d:
            a.setVisible(three)

    def _view3d_call(self, method: str):
        if self.view3d is not None and self.view3d.alive:
            getattr(self.view3d, method)()

    # -- viewer tabs ------------------------------------------------------------------
    def _output_labels(self) -> list[str]:
        """One label per output screen. The project has one output today; this is the seam
        for several projectors."""
        p = self.state.project
        w, h = p.output_size
        return [p.output_screen or f"Output {w}×{h}"]

    def _refresh_tabs(self):
        labels = self._output_labels()
        kinds = [("output", i) for i in range(len(labels))] + [("all", 0), ("3d", 0)]
        texts = labels + ["All", "3D"]
        if [self.tabs.tabText(i) for i in range(self.tabs.count())] == texts:
            return
        cur = self._tab_kind()
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        for text, kind in zip(texts, kinds):
            i = self.tabs.addTab(text)
            self.tabs.setTabData(i, kind)
        self.tabs.setTabToolTip(len(labels), "Every output in one view")
        self.tabs.setTabToolTip(len(labels) + 1, "The Panda3D scene: projector, cameras, props  (F3)")
        self.tabs.blockSignals(False)
        self._select_tab(cur or "output")

    def _tab_kind(self, index: int | None = None) -> str | None:
        i = self.tabs.currentIndex() if index is None else index
        d = self.tabs.tabData(i) if i >= 0 else None
        return d[0] if d else None

    def _select_tab(self, kind: str):
        for i in range(self.tabs.count()):
            if self._tab_kind(i) == kind:
                self.tabs.setCurrentIndex(i)
                return

    def _show_side(self, which: str):
        scene = which == "scene"
        self.side_stack.setCurrentIndex(1 if scene else 0)
        self.side_dock.setWindowTitle("3D scene" if scene else "Surfaces")
        self.side_dock.show()

    def _tab_changed(self, index: int):
        kind = self._tab_kind(index)
        self._refresh_view_bar(kind)
        if kind == "3d":
            self.stack.setCurrentWidget(self.view3d_widget)
            self._show_side("scene")
            if self.view3d is None or not self.view3d.alive or not self.view3d.embedded:
                self.open_view3d(embedded=True)
        else:
            self.stack.setCurrentWidget(self.canvas)
            self._show_side("surfaces")
        self._sync_view3d_act()

    def _window_request(self, which: str):
        if which == "animations":
            self.open_animations_window()
            self.animation_panel.show_selected()
        elif which == "camera":
            self.open_camera_window()

    def _sync_view3d_act(self):
        if hasattr(self, "view3d_act"):
            self.view3d_act.blockSignals(True)
            self.view3d_act.setChecked(self.view3d is not None and self.view3d.alive and self.view3d.embedded and self._tab_kind() == "3d")
            self.view3d_act.blockSignals(False)

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
            if not self.view3d.alive:
                self.close_view3d()  # it closed itself (window X, or an error)
            elif not self.view3d.embedded or self.stack.currentWidget() is self.view3d_widget:
                self.view3d.step()
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
            self._refresh_tabs()
        if self.zoom_label.isVisible():
            pct = f"{self.canvas.zoom * 100:.0f}%"
            if self.zoom_label.text() != pct:
                self.zoom_label.setText(pct)

    def closeEvent(self, ev: QCloseEvent):
        if not self._confirm_discard():
            ev.ignore()
            return
        self.save_layout()
        self.close_output()
        self.close_view3d()
        self.camera_panel.shutdown()
        for _, w in self._tool_windows():
            w.close()
        self.audio.close()
        super().closeEvent(ev)
