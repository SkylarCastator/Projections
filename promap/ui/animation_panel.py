"""Animations dock: browse the scripted effects, edit their parameters live, assign them.

The list on top is every effect in promap.media.generative.EFFECTS. Below it a live
preview and a parameter editor generated from the effect's spec (sliders, colour buttons,
a text box). *Assign* puts the animation on the selected surface (in the editor, or in the
running cue), *+ Surface* adds a new quad with it, *+ Cue* adds a cue from the editor.
When the selected surface already shows an animation, the editor loads its parameters and
every change applies to the running layer without restarting it.
"""
from __future__ import annotations

import time

import moderngl
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QFormLayout, QLabel, QSlider,
    QLineEdit, QColorDialog, QGroupBox, QCheckBox,
)

from promap.media import generative as gen
from promap.model.project import Surface
from promap.render.renderer import Renderer, letterbox
from promap.show.engine import RenderItem
from promap.ui.state import AppState


class GenPreview(QOpenGLWidget):
    """A small live render of one effect with the panel's current parameters."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ctx = None
        self.renderer = None
        self.effect: str | None = None
        self.params: dict = {}
        self.t0 = time.monotonic()
        self.setMinimumSize(240, 135)
        self.setMaximumHeight(220)

    def initializeGL(self):
        self.ctx = moderngl.create_context()
        self.renderer = Renderer(self.ctx)

    def paintGL(self):
        if self.effect is None or self.renderer is None:
            self.ctx.clear(0.1, 0.1, 0.1)
            return
        fbo = self.ctx.detect_framebuffer()
        size = gen.EFFECTS[self.effect].size
        w, h = 16, 9
        x, y, vw, vh = letterbox(w, h, fbo.width, fbo.height)
        surf = Surface(id="__preview__", name="preview", corners=[[0, 0], [w, 0], [w, h], [0, h]])
        frame = gen.GenFrame(self.effect, self.params, time.monotonic() - self.t0, (size[0], int(size[0] * 9 / 16)))
        self.renderer.render(fbo, (w, h), [RenderItem("__preview__", surf, frame, 0, 1.0)], (x, fbo.height - y - vh, vw, vh),
                             clear_color=(0.08, 0.08, 0.09))


class AnimationPanel(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._loading = False
        self.effect: gen.Effect | None = None
        self.params: dict = {}
        self.widgets: dict[str, QWidget] = {}
        lay = QVBoxLayout(self)

        self.list = QListWidget()
        self.list.setMinimumHeight(160)
        for name, eff in gen.EFFECTS.items():
            it = QListWidgetItem(f"{eff.title}   ·  {eff.group}")
            it.setData(Qt.UserRole, name)
            it.setToolTip(eff.blurb)
            self.list.addItem(it)
        self.list.currentRowChanged.connect(self._on_row)
        lay.addWidget(self.list, 1)

        self.preview = GenPreview()
        lay.addWidget(self.preview)
        self.blurb = QLabel("")
        self.blurb.setWordWrap(True)
        self.blurb.setStyleSheet("color: #aaa")
        lay.addWidget(self.blurb)

        row = QHBoxLayout()
        self.assign_btn = QPushButton("Assign to surface")
        self.assign_btn.setToolTip("Put this animation on the selected surface (in the editor, or in the running cue)")
        self.assign_btn.clicked.connect(self._assign)
        self.add_surface_btn = QPushButton("+ Surface")
        self.add_surface_btn.setToolTip("Add a new quad showing this animation")
        self.add_surface_btn.clicked.connect(self._add_surface)
        self.add_cue_btn = QPushButton("+ Cue")
        self.add_cue_btn.setToolTip("Add a cue from the editor's current assignments")
        self.add_cue_btn.clicked.connect(self._add_cue)
        self.follow = QCheckBox("Edit selected surface's animation")
        self.follow.setChecked(True)
        self.follow.setToolTip("When the selected surface shows an animation, load it here and apply edits to it live")
        for b in (self.assign_btn, self.add_surface_btn, self.add_cue_btn):
            row.addWidget(b)
        lay.addLayout(row)
        lay.addWidget(self.follow)

        self.params_box = QGroupBox("Parameters")
        self.form = QFormLayout(self.params_box)
        lay.addWidget(self.params_box)

        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self.preview.update)
        self.timer.start()
        state.selectionChanged.connect(self._follow_selection)
        state.showChanged.connect(lambda: self._follow_selection(self.state.selected))
        self.list.setCurrentRow(0)

    # -- selection ---------------------------------------------------------------------
    def _on_row(self, row: int):
        it = self.list.item(row)
        if it is None:
            return
        name = it.data(Qt.UserRole)
        if self.effect is not None and self.effect.name == name:
            return
        self._load(name, {})

    def _load(self, name: str, params: dict):
        self.effect = gen.EFFECTS[name]
        self.params = self.effect.normalize(params)
        self.blurb.setText(self.effect.blurb)
        self.preview.effect = name
        self.preview.params = self.params
        self._build_form()
        self._loading = True
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == name:
                self.list.setCurrentRow(i)
        self._loading = False

    def _target_media(self) -> str | None:
        """The selected surface's media as it is right now (editor or running cue)."""
        s = self.state.selected_surface
        if s is None:
            return None
        cue = self.state.engine.current_cue
        return cue.media.get(s.id) if cue is not None else s.media

    def _follow_selection(self, sid):
        if not self.follow.isChecked():
            return
        media = self._target_media()
        if gen.is_generative(media):
            eff, params = gen.parse_media(media)
            if self.effect is None or self.effect.name != eff.name or params != self.params:
                self._load(eff.name, params)

    # -- parameter editor ------------------------------------------------------------------
    def _build_form(self):
        while self.form.rowCount():
            self.form.removeRow(0)
        self.widgets.clear()
        for p in self.effect.params:
            if p.kind == "color":
                b = QPushButton()
                b.setFixedHeight(22)
                b.clicked.connect(lambda _=False, key=p.key: self._pick_color(key))
                self._paint_color(b, self.params[p.key])
                self.widgets[p.key] = b
                self.form.addRow(p.label, b)
            elif p.kind == "text":
                e = QLineEdit(self.params[p.key])
                e.setMaxLength(16)
                e.textChanged.connect(lambda txt, key=p.key: self._set(key, txt))
                self.widgets[p.key] = e
                self.form.addRow(p.label, e)
            else:
                sl = QSlider(Qt.Horizontal)
                steps = max(1, int(round((p.hi - p.lo) / p.step)))
                sl.setRange(0, steps)
                sl.setValue(int(round((float(self.params[p.key]) - p.lo) / (p.hi - p.lo) * steps)))
                sl.setToolTip(p.help or p.label)
                val = QLabel(f"{float(self.params[p.key]):.2f}")
                val.setFixedWidth(42)
                sl.valueChanged.connect(lambda v, key=p.key, pp=p, lab=val: self._slide(key, pp, v, lab))
                row = QHBoxLayout()
                row.addWidget(sl, 1)
                row.addWidget(val)
                w = QWidget()
                w.setLayout(row)
                self.widgets[p.key] = sl
                self.form.addRow(p.label, w)

    @staticmethod
    def _paint_color(btn: QPushButton, hexcol: str):
        c = QColor("#" + hexcol)
        btn.setStyleSheet(f"background-color: {c.name()}; color: {'black' if c.lightness() > 110 else 'white'}")
        btn.setText(c.name())

    def _pick_color(self, key: str):
        c = QColorDialog.getColor(QColor("#" + self.params[key]), self, "Colour")
        if c.isValid():
            self._set(key, c.name()[1:])
            self._paint_color(self.widgets[key], self.params[key])

    def _slide(self, key: str, p: gen.Param, v: int, label: QLabel):
        steps = max(1, int(round((p.hi - p.lo) / p.step)))
        value = p.lo + (p.hi - p.lo) * v / steps
        label.setText(f"{value:.2f}")
        self._set(key, value)

    def _set(self, key: str, value):
        if self._loading or self.effect is None:
            return
        self.params = self.effect.normalize({**self.params, key: value})
        self.preview.params = self.params
        if self.follow.isChecked():
            s = self.state.selected_surface
            media = self._target_media()
            if s is not None and gen.is_generative(media) and gen.parse_media(media)[0].name == self.effect.name:
                self.state.update_gen_params(s.id, {key: self.params[key]})

    # -- actions ---------------------------------------------------------------------------
    def media(self) -> str:
        return gen.make_media(self.effect.name, self.params)

    def _assign(self):
        s = self.state.selected_surface
        if s is None or self.effect is None:
            return
        if s.kind == "reference":
            return
        self.state.assign_media(s.id, self.media())

    def _add_surface(self):
        if self.effect is None:
            return
        s = self.state.add_surface("media")
        s.name = self.effect.title
        self.state.assign_media(s.id, self.media())

    def _add_cue(self):
        from promap.model.project import Cue

        p = self.state.project
        cue = Cue(name=f"Cue {len(p.cues) + 1}", media=self.state.editor_assignments())
        p.cues.append(cue)
        self.state.cues_edited()
