"""Cue editor: a cue laid out like a video editor's sequence.

One track per media surface, clips along a time axis, a ruler with a playhead you can
scrub, transport buttons, and an inspector for the selected clip. Everything you do here
plays on the canvas and the output straight away: picking a cue fires it (paused at 0),
dragging the playhead seeks it, Space plays it.

Mouse on the sequence:
  ruler            click / drag scrubs
  clip body        drag moves (also onto another track); double-click opens its media menu
  clip left/right  drag trims
  square handles   drag sets the fade-in / fade-out length
  empty track      double-click adds a clip there; right-click for what to add
  right-click      clip menu (media, split, duplicate, delete) or cue menu (length, loop)
Keys: Space play/pause, Home start, ←/→ nudge 0.1 s (Shift 1 s), S split at playhead, Delete.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QMouseEvent, QWheelEvent, QKeyEvent, QPolygonF, QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QWidget, QMenu, QFileDialog, QInputDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QToolButton, QLabel,
    QComboBox, QDoubleSpinBox, QCheckBox, QSlider, QPushButton, QSizePolicy,
)

from promap.media import generative as gen
from promap.model.project import Cue, Clip
from promap.ui.state import AppState

LABEL_W = 150
RULER_H = 28
TRACK_H = 44
CLIP_PAD = 5
HANDLE = 7  # px, fade handles and edge grab width
NEW_CLIP_S = 5.0

GROUP_COLORS = {
    "organic": QColor(90, 180, 120), "retro": QColor(200, 140, 70), "material": QColor(120, 150, 220), "geometric": QColor(220, 90, 90),
    "light": QColor(230, 90, 200), "3d": QColor(120, 200, 220), "print": QColor(170, 120, 200), "text": QColor(240, 200, 60),
    "particles": QColor(90, 210, 170), "digital": QColor(80, 220, 200), "lines": QColor(150, 190, 100),
}
FILE_COLOR = QColor(70, 130, 180)
DARK_COLOR = QColor(55, 55, 60)


def clip_color(media) -> QColor:
    if not media:
        return DARK_COLOR
    if gen.is_generative(media):
        name = media[len(gen.PREFIX):].split("?")[0]
        eff = gen.EFFECTS.get(name)
        return GROUP_COLORS.get(eff.group, FILE_COLOR) if eff else FILE_COLOR
    return FILE_COLOR


def clip_label(media) -> str:
    if not media:
        return "dark"
    return gen.media_label(media) if gen.is_generative(media) else Path(media).name


def fmt_time(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:05.2f}"


class SequenceView(QWidget):
    """The tracks-and-clips drawing. Edits go through the editor's state helpers."""

    clipSelected = Signal(object)  # Clip | None

    def __init__(self, editor: "CueEditor"):
        super().__init__()
        self.ed = editor
        self.state: AppState = editor.state
        self.px_per_s = 40.0
        self.selected: str | None = None  # clip id
        self._drag: dict | None = None
        self._hover = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(RULER_H + TRACK_H)

    # -- geometry -------------------------------------------------------------------------
    def cue(self) -> Cue | None:
        return self.ed.cue()

    def tracks(self):
        return [s for s in self.state.project.surfaces if s.kind == "media"]

    def x_at(self, t: float) -> float:
        return LABEL_W + t * self.px_per_s

    def t_at(self, x: float) -> float:
        return max(0.0, (x - LABEL_W) / self.px_per_s)

    def row_y(self, r: int) -> float:
        return RULER_H + r * TRACK_H

    def clip_rect(self, c: Clip) -> QRectF | None:
        ids = [s.id for s in self.tracks()]
        if c.surface_id not in ids:
            return None
        y = self.row_y(ids.index(c.surface_id))
        return QRectF(self.x_at(c.start), y + CLIP_PAD, c.duration * self.px_per_s, TRACK_H - 2 * CLIP_PAD)

    def relayout(self):
        cue = self.cue()
        total = cue.total if cue else 10.0
        w = int(self.x_at(total) + 240)
        h = int(RULER_H + TRACK_H * max(1, len(self.tracks())) + 10)
        self.setMinimumSize(w, h)
        self.resize(max(w, self.width()), h)
        self.update()

    def set_zoom(self, px: float, about_t: float | None = None):
        self.px_per_s = max(6.0, min(400.0, px))
        self.relayout()

    # -- painting ---------------------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(30, 30, 33))
        font = QFont()
        font.setPointSize(9)
        p.setFont(font)
        cue = self.cue()
        tracks = self.tracks()
        total = cue.total if cue else 10.0
        sel_sid = self.state.selected
        # tracks
        for r, s in enumerate(tracks):
            y = self.row_y(r)
            p.fillRect(QRectF(LABEL_W, y, self.width() - LABEL_W, TRACK_H), QColor(36, 36, 40) if r % 2 else QColor(40, 40, 44))
            p.fillRect(QRectF(0, y, LABEL_W, TRACK_H), QColor(46, 46, 52) if s.id == sel_sid else QColor(42, 42, 47))
            p.setPen(QPen(QColor(235, 235, 235) if s.id == sel_sid else QColor(175, 175, 175)))
            glyph = "△" if s.mesh is not None else "▢"
            p.drawText(QRectF(10, y, LABEL_W - 14, TRACK_H), Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(f"{glyph} {s.name}", Qt.ElideRight, LABEL_W - 18))
            p.setPen(QPen(QColor(22, 22, 25)))
            p.drawLine(QPointF(0, y + TRACK_H), QPointF(self.width(), y + TRACK_H))
        # beyond the cue's length
        xe = self.x_at(total)
        p.fillRect(QRectF(xe, RULER_H, self.width() - xe, self.height() - RULER_H), QColor(0, 0, 0, 70))
        # clips
        if cue is not None:
            for c in sorted(cue.clips, key=lambda c: c.start):
                rect = self.clip_rect(c)
                if rect is None:
                    continue
                self._paint_clip(p, c, rect)
        # ruler
        p.fillRect(QRectF(0, 0, self.width(), RULER_H), QColor(24, 24, 27))
        p.fillRect(QRectF(0, 0, LABEL_W, RULER_H), QColor(30, 30, 34))
        step = self._tick_step()
        p.setPen(QPen(QColor(120, 120, 130)))
        t = 0.0
        while self.x_at(t) < self.width():
            x = self.x_at(t)
            major = abs((t / step) % 5) < 1e-6
            p.drawLine(QPointF(x, RULER_H - (12 if major else 6)), QPointF(x, RULER_H))
            if major:
                p.drawText(QRectF(x + 3, 0, 80, RULER_H - 8), Qt.AlignVCenter | Qt.AlignLeft, fmt_time(t))
            if step * self.px_per_s >= 24:  # faint grid lines down the tracks on majors
                if major:
                    p.setPen(QPen(QColor(255, 255, 255, 14)))
                    p.drawLine(QPointF(x, RULER_H), QPointF(x, self.height()))
                    p.setPen(QPen(QColor(120, 120, 130)))
            t += step
        # cue end marker
        p.setPen(QPen(QColor(230, 200, 90), 2))
        p.drawLine(QPointF(xe, 0), QPointF(xe, self.height()))
        p.drawText(QRectF(xe + 4, RULER_H - 14, 120, 14), Qt.AlignVCenter | Qt.AlignLeft, f"end {fmt_time(total)}")
        # playhead
        if cue is not None and self.ed.on_this_cue():
            x = self.x_at(self.state.engine.cue_time())
            p.setPen(QPen(QColor(255, 80, 80), 2))
            p.drawLine(QPointF(x, 0), QPointF(x, self.height()))
            p.setBrush(QBrush(QColor(255, 80, 80)))
            p.setPen(Qt.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 9)]))
        if not tracks:
            p.setPen(QPen(QColor(150, 150, 150)))
            p.drawText(QRectF(LABEL_W + 10, RULER_H, 600, TRACK_H), Qt.AlignVCenter | Qt.AlignLeft,
                       "No media surfaces yet: add one with the palette in the editor")
        elif cue is None:
            p.setPen(QPen(QColor(150, 150, 150)))
            p.drawText(QRectF(LABEL_W + 10, RULER_H, 600, TRACK_H), Qt.AlignVCenter | Qt.AlignLeft,
                       "No cue: press + Cue, or 'Add from editor' in the Cues list")
        p.end()

    def _paint_clip(self, p: QPainter, c: Clip, r: QRectF):
        color = clip_color(c.media)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(color))
        p.drawRoundedRect(r, 4, 4)
        if c.media is None:  # hatched dark stretch
            p.setPen(QPen(QColor(90, 90, 96), 1))
            x = r.left() + 4
            while x < r.right():
                p.drawLine(QPointF(x, r.bottom()), QPointF(min(x + r.height(), r.right()), r.top()))
                x += 10
            p.setPen(Qt.NoPen)
        # fade ramps: the darkened wedge is where the clip is below full opacity
        shade = QColor(0, 0, 0, 110)
        p.setBrush(QBrush(shade))
        if c.fade_in > 0:
            w = min(c.fade_in, c.duration) * self.px_per_s
            p.drawPolygon(QPolygonF([QPointF(r.left(), r.bottom()), QPointF(r.left() + w, r.top()), QPointF(r.left(), r.top())]))
        if c.fade_out > 0:
            w = min(c.fade_out, c.duration) * self.px_per_s
            p.drawPolygon(QPolygonF([QPointF(r.right(), r.bottom()), QPointF(r.right() - w, r.top()), QPointF(r.right(), r.top())]))
        selected = c.id == self.selected
        if selected:
            p.setPen(QPen(QColor(255, 255, 255), 2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 4, 4)
        # fade handles (always drawn, small; brighter when selected)
        hcol = QColor(255, 255, 255, 230 if selected else 120)
        p.setPen(QPen(QColor(0, 0, 0, 160), 1))
        p.setBrush(QBrush(hcol))
        for hx in (r.left() + min(c.fade_in, c.duration) * self.px_per_s, r.right() - min(c.fade_out, c.duration) * self.px_per_s):
            p.drawRect(QRectF(hx - HANDLE / 2, r.top() + 1, HANDLE, HANDLE))
        # label
        text = clip_label(c.media)
        dark_text = color.lightness() > 120 and c.media is not None
        p.setPen(QPen(QColor(15, 15, 15) if dark_text else QColor(235, 235, 235)))
        inner = r.adjusted(6, HANDLE + 2, -6, -2)
        if inner.width() > 14:
            p.drawText(inner, Qt.AlignVCenter | Qt.AlignLeft, p.fontMetrics().elidedText(text, Qt.ElideRight, int(inner.width())))

    def _tick_step(self) -> float:
        for step in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300):
            if step * self.px_per_s >= 14:
                return step
        return 600

    # -- hit testing -------------------------------------------------------------------------
    def hit(self, pos: QPointF):
        """('ruler', t) | ('fade_in'|'fade_out'|'left'|'right'|'body', clip) | ('track', surface, t) | None"""
        if pos.y() < RULER_H and pos.x() >= LABEL_W:
            return ("ruler", self.t_at(pos.x()))
        cue = self.cue()
        if cue is not None:
            for c in sorted(cue.clips, key=lambda c: -c.start):  # topmost (latest start) first
                r = self.clip_rect(c)
                if r is None or not r.adjusted(-HANDLE / 2, 0, HANDLE / 2, 0).contains(pos):
                    continue
                if pos.y() <= r.top() + HANDLE + 2:
                    if abs(pos.x() - (r.left() + min(c.fade_in, c.duration) * self.px_per_s)) <= HANDLE:
                        return ("fade_in", c)
                    if abs(pos.x() - (r.right() - min(c.fade_out, c.duration) * self.px_per_s)) <= HANDLE:
                        return ("fade_out", c)
                if abs(pos.x() - r.left()) <= HANDLE:
                    return ("left", c)
                if abs(pos.x() - r.right()) <= HANDLE:
                    return ("right", c)
                if r.contains(pos):
                    return ("body", c)
        r = int((pos.y() - RULER_H) // TRACK_H)
        tracks = self.tracks()
        if 0 <= r < len(tracks) and pos.x() >= LABEL_W:
            return ("track", tracks[r], self.t_at(pos.x()))
        return None

    def _row_at(self, y: float):
        r = int((y - RULER_H) // TRACK_H)
        tracks = self.tracks()
        return tracks[r] if 0 <= r < len(tracks) else None

    def _snap(self, t: float, exclude: Clip | None = None) -> float:
        """Snap to 0.05 s, and to other clips' edges / the playhead / the cue end when within 6 px."""
        cue = self.cue()
        t = round(t * 20) / 20
        if cue is None:
            return max(0.0, t)
        cands = [0.0, cue.total]
        if self.ed.on_this_cue():
            cands.append(self.state.engine.cue_time())
        for c in cue.clips:
            if c is not exclude:
                cands += [c.start, c.end]
        tol = 6 / self.px_per_s
        best = min(cands, key=lambda c: abs(c - t))
        return max(0.0, best if abs(best - t) <= tol else t)

    # -- mouse ------------------------------------------------------------------------------------
    def mousePressEvent(self, ev: QMouseEvent):
        self.setFocus()
        pos = ev.position()
        h = self.hit(pos)
        if ev.button() == Qt.RightButton:
            self._menu(ev, h)
            return
        if ev.button() != Qt.LeftButton:
            return
        if h is None:
            self.select_clip(None)
            return
        kind = h[0]
        if kind == "ruler":
            self.ed.seek(h[1])
            self._drag = {"mode": "scrub"}
            return
        if kind == "track":
            self.select_clip(None)
            self.state.select(h[1].id)
            return
        c: Clip = h[1]
        self.select_clip(c.id)
        self.state.begin_gesture()
        self._drag = {"mode": kind, "clip": c, "x0": pos.x(), "y0": pos.y(), "start": c.start, "dur": c.duration,
                      "fi": c.fade_in, "fo": c.fade_out, "sid": c.surface_id, "moved": False}

    def mouseMoveEvent(self, ev: QMouseEvent):
        pos = ev.position()
        if self._drag is None:
            h = self.hit(pos)
            k = h[0] if h else None
            self.setCursor({"left": Qt.SizeHorCursor, "right": Qt.SizeHorCursor, "fade_in": Qt.SizeHorCursor,
                            "fade_out": Qt.SizeHorCursor, "body": Qt.OpenHandCursor, "ruler": Qt.PointingHandCursor}.get(k, Qt.ArrowCursor))
            return
        d = self._drag
        if d["mode"] == "scrub":
            self.ed.seek(self.t_at(pos.x()))
            return
        c: Clip = d["clip"]
        dt = (pos.x() - d["x0"]) / self.px_per_s
        d["moved"] = True
        if d["mode"] == "body":
            c.start = self._snap(d["start"] + dt, exclude=c)
            row = self._row_at(pos.y())
            if row is not None:
                c.surface_id = row.id
        elif d["mode"] == "left":
            end = d["start"] + d["dur"]
            ns = min(self._snap(d["start"] + dt, exclude=c), end - 0.1)
            c.start, c.duration = ns, end - ns
            c.fade_in = min(c.fade_in, c.duration)
        elif d["mode"] == "right":
            ne = max(self._snap(d["start"] + d["dur"] + dt, exclude=c), c.start + 0.1)
            c.duration = ne - c.start
            c.fade_out = min(c.fade_out, c.duration)
        elif d["mode"] == "fade_in":
            c.fade_in = round(max(0.0, min(c.duration, d["fi"] + dt)), 2)
        elif d["mode"] == "fade_out":
            c.fade_out = round(max(0.0, min(c.duration, d["fo"] - dt)), 2)
        self.ed.clip_changed(c, final=False)

    def mouseReleaseEvent(self, ev: QMouseEvent):
        d, self._drag = self._drag, None
        if d is None or d["mode"] == "scrub":
            return
        self.state.end_gesture()
        if d["moved"]:
            self.ed.clip_changed(d["clip"], final=True)

    def mouseDoubleClickEvent(self, ev: QMouseEvent):
        h = self.hit(ev.position())
        if h is None:
            return
        if h[0] == "track":
            self.ed.add_clip(h[1].id, h[1].media, at=h[2])
        elif h[0] in ("body", "left", "right", "fade_in", "fade_out"):
            self._menu(ev, h, media_only=True)

    def wheelEvent(self, ev: QWheelEvent):
        if ev.modifiers() & Qt.ControlModifier:
            self.ed.zoom_by(1.15 if ev.angleDelta().y() > 0 else 1 / 1.15)
        else:
            ev.ignore()

    def keyPressEvent(self, ev: QKeyEvent):
        k = ev.key()
        if k == Qt.Key_Space:
            self.ed.toggle_play()
        elif k == Qt.Key_Home:
            self.ed.seek(0.0)
        elif k in (Qt.Key_Left, Qt.Key_Right):
            step = 1.0 if ev.modifiers() & Qt.ShiftModifier else 0.1
            self.ed.seek(self.state.engine.cue_time() + (step if k == Qt.Key_Right else -step))
        elif k == Qt.Key_S:
            self.ed.split_at_playhead()
        elif k in (Qt.Key_Delete, Qt.Key_Backspace):
            self.ed.delete_selected()
        else:
            super().keyPressEvent(ev)

    def select_clip(self, cid: str | None):
        self.selected = cid
        cue = self.cue()
        c = cue.clip_by_id(cid) if (cue and cid) else None
        if c is not None:
            self.state.select(c.surface_id)
        self.clipSelected.emit(c)
        self.update()

    # -- menus ------------------------------------------------------------------------------
    def _media_menu(self, m: QMenu, apply, surface):
        anim = m.addMenu("Animation")
        for name, eff in gen.EFFECTS.items():
            anim.addAction(f"{eff.title}   ·  {eff.group}", lambda n=name: apply(gen.make_media(n)))
        m.addAction("File…", lambda: self._pick_file(apply))
        if surface is not None and surface.media:
            m.addAction(f"Editor media  ({clip_label(surface.media)})", lambda: apply(surface.media))
        m.addAction("Dark", lambda: apply(None))

    def _pick_file(self, apply):
        from promap.ui.surface_panel import MEDIA_FILTER

        path, _ = QFileDialog.getOpenFileName(self, "Choose media", str(self.state.project.base_dir), MEDIA_FILTER)
        if path:
            apply(self.state.project.relativize_media(path))

    def _menu(self, ev, h, media_only: bool = False):
        cue = self.cue()
        m = QMenu(self)
        if h and h[0] in ("body", "left", "right", "fade_in", "fade_out"):
            c: Clip = h[1]
            self.select_clip(c.id)
            s = self.state.project.surface_by_id(c.surface_id)
            m.addSection(f"Clip: {clip_label(c.media)}")
            self._media_menu(m, lambda media: self.ed.set_clip_media(c, media), s)
            if gen.is_generative(c.media):
                m.addAction("Parameters…", lambda: self.ed.edit_params(c))
            if not media_only:
                m.addSeparator()
                m.addAction("Fade in…", lambda: self.ed.ask_fade(c, "fade_in"))
                m.addAction("Fade out…", lambda: self.ed.ask_fade(c, "fade_out"))
                m.addAction("Split at playhead  (S)", self.ed.split_at_playhead)
                m.addAction("Duplicate", lambda: self.ed.duplicate_clip(c))
                m.addAction("Delete  (Del)", lambda: self.ed.delete_clip(c))
        elif h and h[0] == "track" and cue is not None:
            s, t = h[1], h[2]
            m.addSection(f"Add clip on {s.name} at {fmt_time(t)}")
            self._media_menu(m, lambda media: self.ed.add_clip(s.id, media, at=t), s)
        if cue is not None and not media_only:
            m.addSeparator()
            m.addAction("Fit cue length to last clip", self.ed.fit_length)
            m.addAction("Set cue length…", self.ed.ask_length)
            loop = m.addAction("Loop")
            loop.setCheckable(True)
            loop.setChecked(cue.loop)
            loop.toggled.connect(self.ed.set_loop)
        if not m.isEmpty():
            m.exec(ev.globalPosition().toPoint())


class CueEditor(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._cue_index = 0  # the cue being edited (follows the engine when it runs one)
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # transport / cue row
        top = QHBoxLayout()
        self.cue_combo = QComboBox()
        self.cue_combo.setMinimumWidth(180)
        self.cue_combo.setToolTip("Which cue to edit. Picking one fires it on the output, paused at 0.")
        self.cue_combo.currentIndexChanged.connect(self._combo_changed)
        top.addWidget(self.cue_combo)
        self.add_cue_btn = QPushButton("+ Cue")
        self.add_cue_btn.setToolTip("New empty cue after this one")
        self.add_cue_btn.clicked.connect(self.add_cue)
        top.addWidget(self.add_cue_btn)
        top.addSpacing(12)

        def tb(text, tip, fn):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            top.addWidget(b)
            return b

        tb("⏮", "To start  (Home)", lambda: self.seek(0.0))
        self.play_btn = tb("▶", "Play / pause  (Space)", self.toggle_play)
        tb("⏭", "Next cue  (GO)", self.state.go)
        self.time_label = QLabel("00:00.00")
        self.time_label.setStyleSheet("font-family: monospace; font-size: 14px; padding: 0 8px;")
        top.addWidget(self.time_label)
        top.addSpacing(12)
        top.addWidget(QLabel("Length"))
        self.length = QDoubleSpinBox()
        self.length.setRange(0.1, 36000)
        self.length.setDecimals(1)
        self.length.setSuffix(" s")
        self.length.setToolTip("How long the cue's sequence runs before it loops or holds")
        self.length.valueChanged.connect(self._length_changed)
        top.addWidget(self.length)
        self.loop = QCheckBox("Loop")
        self.loop.setToolTip("Run the sequence again from 0 when it reaches the end (a cue with a hold time advances instead)")
        self.loop.toggled.connect(self.set_loop)
        top.addWidget(self.loop)
        top.addSpacing(12)
        self.add_clip_btn = QPushButton("+ Clip")
        self.add_clip_btn.setToolTip("Put the selected surface's editor media on its track at the playhead")
        self.add_clip_btn.clicked.connect(self.add_clip_from_selection)
        top.addWidget(self.add_clip_btn)
        top.addStretch(1)
        top.addWidget(QLabel("Zoom"))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(6, 400)
        self.zoom.setValue(40)
        self.zoom.setFixedWidth(120)
        self.zoom.setToolTip("Pixels per second  (Ctrl+wheel on the sequence)")
        self.zoom.valueChanged.connect(lambda v: self.view.set_zoom(float(v)))
        top.addWidget(self.zoom)
        lay.addLayout(top)

        # the sequence
        self.area = QScrollArea()
        self.area.setWidgetResizable(False)
        self.area.setFrameShape(QScrollArea.NoFrame)
        self.view = SequenceView(self)
        self.view.clipSelected.connect(self._show_clip)
        self.area.setWidget(self.view)
        lay.addWidget(self.area, 1)

        # inspector
        insp = QHBoxLayout()
        self.clip_label = QLabel("No clip selected")
        self.clip_label.setMinimumWidth(220)
        insp.addWidget(self.clip_label)
        self.spins: dict[str, QDoubleSpinBox] = {}
        for key, label, tip in (("start", "Start", "Seconds into the cue"), ("duration", "Length", "Seconds"),
                                ("fade_in", "Fade in", "Seconds, inside the clip"), ("fade_out", "Fade out", "Seconds, inside the clip")):
            insp.addWidget(QLabel(label))
            sp = QDoubleSpinBox()
            sp.setRange(0.0, 36000)
            sp.setDecimals(2)
            sp.setSingleStep(0.1)
            sp.setToolTip(tip)
            sp.valueChanged.connect(lambda v, k=key: self._spin_changed(k, v))
            insp.addWidget(sp)
            self.spins[key] = sp
        self.params_btn = QPushButton("Parameters…")
        self.params_btn.setToolTip("Edit this animation's parameters in the Animations window (live)")
        self.params_btn.clicked.connect(lambda: self.edit_params(self.selected_clip()))
        insp.addWidget(self.params_btn)
        insp.addStretch(1)
        lay.addLayout(insp)
        self._show_clip(None)

        QShortcut(QKeySequence(Qt.Key_Space), self, context=Qt.WindowShortcut, activated=self.toggle_play)

        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        state.cuesChanged.connect(self._refresh)
        state.surfacesChanged.connect(self.view.relayout)
        state.projectReplaced.connect(self._refresh)
        state.showChanged.connect(self._follow_engine)
        self._refresh()

    # -- which cue ------------------------------------------------------------------------
    def cue(self) -> Cue | None:
        cues = self.state.project.cues
        if not cues:
            return None
        self._cue_index = max(0, min(self._cue_index, len(cues) - 1))
        return cues[self._cue_index]

    def on_this_cue(self) -> bool:
        return self.state.engine.cue_index == self._cue_index and self.cue() is not None

    def _follow_engine(self):
        i = self.state.engine.cue_index
        if i >= 0 and i != self._cue_index:
            self._cue_index = i
            self._refresh()
        self._update_transport()

    def _combo_changed(self, i: int):
        if self._loading or i < 0:
            return
        self._cue_index = i
        self.state.fire_cue(i, at=0.0, paused=True)
        self._refresh()

    def _refresh(self):
        self._loading = True
        cues = self.state.project.cues
        self.cue_combo.clear()
        for k, c in enumerate(cues):
            self.cue_combo.addItem(f"{k + 1}. {c.name}")
        cue = self.cue()
        if cue is not None:
            self.cue_combo.setCurrentIndex(self._cue_index)
            self.length.setValue(cue.total)
            self.loop.setChecked(cue.loop)
        for w in (self.length, self.loop, self.add_clip_btn):
            w.setEnabled(cue is not None)
        self._loading = False
        if self.view.selected and (cue is None or cue.clip_by_id(self.view.selected) is None):
            self.view.selected = None
        self._show_clip(cue.clip_by_id(self.view.selected) if (cue and self.view.selected) else None)
        self.view.relayout()
        self._update_transport()

    def _tick(self):
        if self.on_this_cue():
            self.time_label.setText(fmt_time(self.state.engine.cue_time()))
            if not self.state.engine.paused:
                self.view.update()

    def _update_transport(self):
        eng = self.state.engine
        playing = self.on_this_cue() and not eng.paused
        self.play_btn.setText("⏸" if playing else "▶")
        self.time_label.setText(fmt_time(eng.cue_time()) if self.on_this_cue() else "--:--.--")
        self.view.update()

    # -- transport -------------------------------------------------------------------------
    def _ensure_fired(self, paused: bool = True):
        if not self.on_this_cue() and self.cue() is not None:
            self.state.fire_cue(self._cue_index, at=0.0, paused=paused)

    def seek(self, t: float):
        self._ensure_fired(paused=True)
        self.state.seek(t)
        self._update_transport()

    def toggle_play(self):
        if self.cue() is None:
            return
        if not self.on_this_cue():
            self.state.fire_cue(self._cue_index, at=0.0, paused=False)
        else:
            self.state.toggle_play()
        self._update_transport()

    def zoom_by(self, f: float):
        self.zoom.setValue(int(round(self.view.px_per_s * f)))

    # -- cue edits ---------------------------------------------------------------------------
    def add_cue(self):
        cues = self.state.project.cues
        cue = Cue(name=f"Cue {len(cues) + 1}", length=10.0)
        cues.insert(self._cue_index + 1 if cues else 0, cue)
        self._cue_index = cues.index(cue)
        self.state.cues_edited()
        self.state.fire_cue(self._cue_index, at=0.0, paused=True)
        self._refresh()

    def _length_changed(self, v: float):
        cue = self.cue()
        if self._loading or cue is None:
            return
        cue.length = float(v)
        self.state.cues_edited()
        self.state.engine.refresh()

    def set_loop(self, on: bool):
        cue = self.cue()
        if self._loading or cue is None or cue.loop == bool(on):
            return
        cue.loop = bool(on)
        self.state.cues_edited()

    def fit_length(self):
        cue = self.cue()
        if cue is None or not cue.clips:
            return
        cue.length = max(c.end for c in cue.clips)
        self.state.cues_edited()
        self._refresh()

    def ask_length(self):
        cue = self.cue()
        if cue is None:
            return
        v, ok = QInputDialog.getDouble(self, "Cue length", "Seconds", cue.total, 0.1, 36000, 1)
        if ok:
            cue.length = v
            self.state.cues_edited()
            self._refresh()

    # -- clip edits -----------------------------------------------------------------------------
    def selected_clip(self) -> Clip | None:
        cue = self.cue()
        return cue.clip_by_id(self.view.selected) if (cue and self.view.selected) else None

    def add_clip(self, sid: str, media, at: float | None = None, duration: float | None = None) -> Clip | None:
        cue = self.cue()
        if cue is None:
            return None
        t = self.state.engine.cue_time() if (at is None and self.on_this_cue()) else (at or 0.0)
        t = round(max(0.0, t) * 20) / 20
        if duration is None:
            nxt = [c.start for c in cue.track(sid) if c.start > t + 1e-6]
            duration = min(NEW_CLIP_S, (min(nxt) - t) if nxt else NEW_CLIP_S)
        clip = Clip(surface_id=sid, media=media, start=t, duration=max(0.1, duration))
        cue.clips.append(clip)
        if cue.length is not None and clip.end > cue.length:
            cue.length = clip.end
        self.state.cues_edited()
        self.state.engine.refresh()
        self.view.select_clip(clip.id)
        self._refresh()
        return clip

    def add_clip_from_selection(self):
        s = self.state.selected_surface
        if s is None or s.kind != "media":
            tracks = self.view.tracks()
            if not tracks:
                return
            s = tracks[0]
        self.add_clip(s.id, s.media)

    def clip_changed(self, c: Clip, final: bool):
        self.state.engine.refresh()
        if final:
            self.state.cues_edited()
        self._show_clip(c)
        self.view.update()

    def set_clip_media(self, c: Clip, media):
        c.media = media
        self.clip_changed(c, final=True)

    def ask_fade(self, c: Clip, key: str):
        v, ok = QInputDialog.getDouble(self, "Fade", "Seconds", getattr(c, key), 0.0, c.duration, 2)
        if ok:
            setattr(c, key, v)
            self.clip_changed(c, final=True)

    def split_at_playhead(self):
        cue = self.cue()
        c = self.selected_clip()
        if cue is None or c is None or not self.on_this_cue():
            return
        t = self.state.engine.cue_time()
        if not (c.start + 0.1 < t < c.end - 0.1):
            return
        right = Clip(surface_id=c.surface_id, media=c.media, start=t, duration=c.end - t, fade_in=0.0, fade_out=c.fade_out)
        c.duration = t - c.start
        c.fade_out = 0.0
        cue.clips.append(right)
        self.state.cues_edited()
        self.state.engine.refresh()
        self.view.select_clip(right.id)

    def duplicate_clip(self, c: Clip):
        cue = self.cue()
        if cue is None:
            return
        d = Clip(surface_id=c.surface_id, media=c.media, start=c.end, duration=c.duration, fade_in=c.fade_in, fade_out=c.fade_out)
        cue.clips.append(d)
        if cue.length is not None and d.end > cue.length:
            cue.length = d.end
        self.state.cues_edited()
        self.state.engine.refresh()
        self.view.select_clip(d.id)
        self._refresh()

    def delete_clip(self, c: Clip):
        cue = self.cue()
        if cue is None or c not in cue.clips:
            return
        cue.clips.remove(c)
        if self.view.selected == c.id:
            self.view.selected = None
        self.state.cues_edited()
        self.state.engine.refresh()
        self._refresh()

    def delete_selected(self):
        c = self.selected_clip()
        if c is not None:
            self.delete_clip(c)

    def edit_params(self, c: Clip | None):
        if c is None or not gen.is_generative(c.media):
            return
        self.state.select(c.surface_id)
        self._ensure_fired(paused=True)
        if not c.covers(self.state.engine.cue_time()):
            self.state.seek(c.start + min(0.05, c.duration / 2))
        self.state.windowRequested.emit("animations")

    # -- inspector ------------------------------------------------------------------------------
    def _show_clip(self, c: Clip | None):
        self._loading = True
        on = c is not None
        for sp in self.spins.values():
            sp.setEnabled(on)
        self.params_btn.setEnabled(on and gen.is_generative(c.media))
        if on:
            s = self.state.project.surface_by_id(c.surface_id)
            self.clip_label.setText(f"Clip: {clip_label(c.media)}  on  {s.name if s else '?'}")
            for k, sp in self.spins.items():
                sp.setValue(float(getattr(c, k)))
        else:
            self.clip_label.setText("No clip selected")
        self._loading = False

    def _spin_changed(self, key: str, v: float):
        c = self.selected_clip()
        if self._loading or c is None:
            return
        v = float(v)
        if key == "duration":
            v = max(0.1, v)
        if key in ("fade_in", "fade_out"):
            v = min(v, c.duration)
        setattr(c, key, v)
        self.clip_changed(c, final=True)
