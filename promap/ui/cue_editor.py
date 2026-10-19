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
  right-click      clip menu (media, split, duplicate, delete) or cue menu (length, at-end)
  end marker       drag the yellow line to set the cue's length (clips dragged past it extend it)
Keys: Space play/pause, Home start, ←/→ nudge 0.1 s (Shift 1 s), S split at playhead, Delete,
Ctrl+C / Ctrl+X / Ctrl+V copy, cut and paste a clip (at the playhead, on its own track), Ctrl+Z / Ctrl+Shift+Z.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer, Signal, QEvent
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QMouseEvent, QWheelEvent, QKeyEvent, QPolygonF, QShortcut, QKeySequence, QCursor
from PySide6.QtWidgets import (
    QWidget, QMenu, QFileDialog, QInputDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QToolButton, QLabel,
    QComboBox, QDoubleSpinBox, QCheckBox, QSlider, QPushButton, QSizePolicy, QLineEdit, QApplication,
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
EDGE_SCROLL_PX = 36  # dragging within this many px of the viewport's side scrolls it
EDGE_SCROLL_STEP = 18
CLIP_MIME = "promap-clip:"  # clipboard text prefix: a clip as JSON, pasteable across cues and projects

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
        self._scroll_timer = QTimer(self)  # keeps scrolling while a drag sits at the viewport's edge
        self._scroll_timer.setInterval(30)
        self._scroll_timer.timeout.connect(self._edge_scroll_tick)

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

    def _ox(self) -> int:
        """Horizontal scroll offset: the label column is drawn at it so names stay put."""
        return self.ed.area.horizontalScrollBar().value()

    def _viewport_w(self) -> int:
        return max(self.ed.area.viewport().width(), 200)

    def relayout(self):
        cue = self.cue()
        total = cue.total if cue else 10.0
        w = max(int(self.x_at(total) + 240), self._viewport_w())  # tracks always reach the right edge
        h = max(int(RULER_H + TRACK_H * max(1, len(self.tracks())) + 10), self.ed.area.viewport().height())
        self.setMinimumSize(w, h)
        self.resize(w, h)
        self.update()

    def ensure_visible_t(self, t: float, margin: int = 60):
        """Scroll so cue time t is on screen (the running playhead, a seek)."""
        x = int(self.x_at(t))
        bar = self.ed.area.horizontalScrollBar()
        ox, vw = bar.value(), self._viewport_w()
        if x > ox + vw - margin:
            bar.setValue(min(bar.maximum(), x - vw + margin))
        elif x < ox + LABEL_W + margin:
            bar.setValue(max(0, x - LABEL_W - margin))

    def _edge_scroll_step(self, pos: QPointF) -> int:
        """Scroll when a drag sits near the viewport's left/right edge; returns the px scrolled."""
        bar = self.ed.area.horizontalScrollBar()
        ox, vw = bar.value(), self._viewport_w()
        if pos.x() > ox + vw - EDGE_SCROLL_PX:
            step = EDGE_SCROLL_STEP
            if bar.value() >= bar.maximum():  # make room: the content grows as the drag goes on
                self.setMinimumWidth(self.width() + step)
                self.resize(self.width() + step, self.height())
            bar.setValue(bar.value() + step)
        elif pos.x() < ox + LABEL_W + EDGE_SCROLL_PX and ox > 0:
            step = -EDGE_SCROLL_STEP
            bar.setValue(max(0, bar.value() + step))
        else:
            return 0
        return bar.value() - ox

    def _edge_scroll_tick(self):
        if self._drag is None:
            self._scroll_timer.stop()
            return
        pos = QPointF(self.mapFromGlobal(QCursor.pos()))
        if self._edge_scroll_step(pos):
            self._apply_drag(QPointF(self.mapFromGlobal(QCursor.pos())))  # the widget moved under the cursor

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
        ox = self._ox()
        content = QRectF(ox + LABEL_W, 0, self.width() - ox - LABEL_W, self.height())
        # tracks
        for r, s in enumerate(tracks):
            y = self.row_y(r)
            p.fillRect(QRectF(LABEL_W, y, self.width() - LABEL_W, TRACK_H), QColor(36, 36, 40) if r % 2 else QColor(40, 40, 44))
            p.setPen(QPen(QColor(22, 22, 25)))
            p.drawLine(QPointF(0, y + TRACK_H), QPointF(self.width(), y + TRACK_H))
        p.setClipRect(content)
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
                       "No cue: press + in the Cue list")
        # pinned label column, drawn last at the scroll offset so it covers whatever scrolled under it
        p.setClipping(False)
        p.fillRect(QRectF(ox, 0, LABEL_W, RULER_H), QColor(30, 30, 34))
        for r, s in enumerate(tracks):
            y = self.row_y(r)
            p.fillRect(QRectF(ox, y, LABEL_W, TRACK_H), QColor(46, 46, 52) if s.id == sel_sid else QColor(42, 42, 47))
            p.setPen(QPen(QColor(235, 235, 235) if s.id == sel_sid else QColor(175, 175, 175)))
            glyph = "△" if s.mesh is not None else "▢"
            p.drawText(QRectF(ox + 10, y, LABEL_W - 14, TRACK_H), Qt.AlignVCenter | Qt.AlignLeft,
                       p.fontMetrics().elidedText(f"{glyph} {s.name}", Qt.ElideRight, LABEL_W - 18))
            p.setPen(QPen(QColor(22, 22, 25)))
            p.drawLine(QPointF(ox, y + TRACK_H), QPointF(ox + LABEL_W, y + TRACK_H))
        p.setPen(QPen(QColor(20, 20, 22), 2))
        p.drawLine(QPointF(ox + LABEL_W, 0), QPointF(ox + LABEL_W, self.height()))
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
        cue = self.cue()
        if pos.x() < self._ox() + LABEL_W:
            return None  # the pinned label column
        if cue is not None and abs(pos.x() - self.x_at(cue.total)) <= HANDLE and pos.y() >= RULER_H - 14:
            return ("end", cue.total)  # the yellow end marker (grab it below the ruler's time labels)
        if pos.y() < RULER_H:
            return ("ruler", self.t_at(pos.x()))
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
        if 0 <= r < len(tracks):
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
        cands = [0.0] + ([] if (self._drag and self._drag.get("mode") == "end") else [cue.total])
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
            self._scroll_timer.start()
            return
        if kind == "end":
            self.state.begin_gesture()
            self._drag = {"mode": "end"}
            self._scroll_timer.start()
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
        self._scroll_timer.start()

    def mouseMoveEvent(self, ev: QMouseEvent):
        pos = ev.position()
        if self._drag is None:
            h = self.hit(pos)
            k = h[0] if h else None
            self.setCursor({"left": Qt.SizeHorCursor, "right": Qt.SizeHorCursor, "fade_in": Qt.SizeHorCursor, "end": Qt.SplitHCursor,
                            "fade_out": Qt.SizeHorCursor, "body": Qt.OpenHandCursor, "ruler": Qt.PointingHandCursor}.get(k, Qt.ArrowCursor))
            return
        self._apply_drag(pos)

    def _apply_drag(self, pos: QPointF):
        d = self._drag
        if d is None:
            return
        if d["mode"] == "scrub":
            self.ed.seek(self.t_at(pos.x()))
            return
        if d["mode"] == "end":
            self.ed.set_length_live(self._snap(self.t_at(pos.x())), final=False)
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
        self._scroll_timer.stop()
        if d is None or d["mode"] == "scrub":
            return
        self.state.end_gesture()
        if d["mode"] == "end":
            cue = self.cue()
            if cue is not None:
                self.ed.set_length_live(cue.total, final=True)
            return
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
        elif k == Qt.Key_C and ev.modifiers() & Qt.ControlModifier:
            self.ed.copy_selected()
        elif k == Qt.Key_X and ev.modifiers() & Qt.ControlModifier:
            self.ed.cut_selected()
        elif k == Qt.Key_V and ev.modifiers() & Qt.ControlModifier:
            self.ed.paste()
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
                m.addAction("Copy  (Ctrl+C)", lambda: self.ed.copy_clip(c))
                m.addAction("Cut  (Ctrl+X)", lambda: self.ed.cut_clip(c))
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
            if self.ed.clipboard_clip() is not None:
                m.addSeparator()
                m.addAction("Paste here  (Ctrl+V pastes at the playhead)", lambda: self.ed.paste(sid=s.id, at=t))
        if cue is not None and not media_only:
            m.addSeparator()
            m.addAction("Fit cue length to last clip", self.ed.fit_length)
            m.addAction("Set cue length…", self.ed.ask_length)
            end = m.addMenu("At the end")
            for label, key in (("Loop", "loop"), ("Next cue", "next"), ("Stop", "stop")):
                a = end.addAction(label)
                a.setCheckable(True)
                a.setChecked(cue.end_mode == key)
                a.triggered.connect(lambda _=False, k=key: self.ed.set_end_mode(k))
        if not m.isEmpty():
            m.exec(ev.globalPosition().toPoint())


class CueEditor(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        # transport / cue row
        top = QHBoxLayout()
        self.cue_name = QLabel("No cue")
        self.cue_name.setStyleSheet("font-weight: bold; padding: 0 6px;")
        self.cue_name.setMinimumWidth(160)
        top.addWidget(self.cue_name)
        top.addSpacing(8)

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
        top.addWidget(QLabel("at end"))
        self.end_mode = QComboBox()
        for label, key in (("loop", "loop"), ("next cue", "next"), ("stop", "stop")):
            self.end_mode.addItem(label, key)
        self.end_mode.setToolTip("What happens when the playhead reaches the cue's length: start again, fire the next cue, "
                                 "or park on the last frame until GO")
        self.end_mode.currentIndexChanged.connect(self._end_mode_changed)
        top.addWidget(self.end_mode)
        top.addStretch(1)
        self.add_clip_btn = QPushButton("+ Clip")
        self.add_clip_btn.setToolTip("Put the selected surface's editor media on its track at the playhead")
        self.add_clip_btn.clicked.connect(self.add_clip_from_selection)
        top.addWidget(self.add_clip_btn)
        top.addSpacing(8)
        top.addWidget(QLabel("Zoom"))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(6, 400)
        self.zoom.setValue(40)
        self.zoom.setFixedWidth(120)
        self.zoom.setToolTip("Pixels per second  (Ctrl+wheel on the sequence)")
        self.zoom.valueChanged.connect(lambda v: self.view.set_zoom(float(v)))
        top.addWidget(self.zoom)
        lay.addLayout(top)

        # cue settings: how it comes in, what it sounds like
        cue_row = QHBoxLayout()
        cue_row.addWidget(QLabel("Fade from previous"))
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0.0, 60.0)
        self.fade.setDecimals(1)
        self.fade.setSuffix(" s")
        self.fade.setToolTip("Seconds the previous cue's layers take to fade out when this cue fires")
        self.fade.valueChanged.connect(lambda v: self._cue_field("fade", float(v)))
        cue_row.addWidget(self.fade)
        cue_row.addSpacing(16)
        cue_row.addWidget(QLabel("Soundtrack"))
        self.audio = QLineEdit()
        self.audio.setReadOnly(True)
        self.audio.setPlaceholderText("none")
        self.audio.setToolTip("Audio file for this cue. The same file in consecutive cues keeps playing; a different one crossfades.")
        cue_row.addWidget(self.audio, 1)
        audio_pick = QPushButton("…")
        audio_pick.setFixedWidth(28)
        audio_pick.clicked.connect(self._pick_audio)
        cue_row.addWidget(audio_pick)
        audio_clear = QPushButton("×")
        audio_clear.setFixedWidth(28)
        audio_clear.setToolTip("No soundtrack")
        audio_clear.clicked.connect(lambda: self._cue_field("audio", None, restart_audio=True))
        cue_row.addWidget(audio_clear)
        cue_row.addWidget(QLabel("Gain"))
        self.gain = QDoubleSpinBox()
        self.gain.setRange(0.0, 4.0)
        self.gain.setDecimals(2)
        self.gain.setSingleStep(0.05)
        self.gain.valueChanged.connect(lambda v: self._cue_field("audio_gain", float(v), restart_audio=True))
        cue_row.addWidget(self.gain)
        self.audio_loop = QCheckBox("Loop audio")
        self.audio_loop.toggled.connect(lambda on: self._cue_field("audio_loop", bool(on), restart_audio=True))
        cue_row.addWidget(self.audio_loop)
        self.cue_widgets = [self.fade, self.audio, audio_pick, audio_clear, self.gain, self.audio_loop]
        lay.addLayout(cue_row)

        # the sequence
        self.area = QScrollArea()
        self.area.setWidgetResizable(False)
        self.area.setFrameShape(QScrollArea.NoFrame)
        self.view = SequenceView(self)
        self.view.clipSelected.connect(self._show_clip)
        self.area.setWidget(self.view)
        self.area.viewport().installEventFilter(self)
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
        state.cueSelected.connect(self._cue_selected)
        self._refresh()

    def eventFilter(self, obj, ev):
        if obj is self.area.viewport() and ev.type() == QEvent.Resize:
            self.view.relayout()
        return super().eventFilter(obj, ev)

    # -- which cue ------------------------------------------------------------------------
    def cue(self) -> Cue | None:
        cues = self.state.project.cues
        if not cues:
            return None
        if not (0 <= self.state.cue_selected < len(cues)):
            self.state.select_cue(0)
        return cues[self.state.cue_selected]

    @property
    def _cue_index(self) -> int:
        return self.state.cue_selected

    def on_this_cue(self) -> bool:
        return self.state.engine.cue_index == self._cue_index and self.cue() is not None

    def _follow_engine(self):
        i = self.state.engine.cue_index
        if i >= 0 and i != self._cue_index:
            self.state.select_cue(i)  # the show moved on: edit what is playing
        self._update_transport()

    def _cue_selected(self, _i: int):
        self.view.selected = None
        self._refresh()

    def _refresh(self):
        self._loading = True
        cue = self.cue()
        if cue is not None:
            self.cue_name.setText(f"{self._cue_index + 1}. {cue.name}")
            self.length.setValue(cue.total)
            self.end_mode.setCurrentIndex(self.end_mode.findData(cue.end_mode))
            self.fade.setValue(cue.fade)
            self.audio.setText(cue.audio or "")
            self.gain.setValue(cue.audio_gain)
            self.audio_loop.setChecked(cue.audio_loop)
        else:
            self.cue_name.setText("No cue")
        for w in (self.length, self.end_mode, self.add_clip_btn, *self.cue_widgets):
            w.setEnabled(cue is not None)
        self._loading = False
        if self.view.selected and (cue is None or cue.clip_by_id(self.view.selected) is None):
            self.view.selected = None
        self._show_clip(cue.clip_by_id(self.view.selected) if (cue and self.view.selected) else None)
        self.view.relayout()
        self._update_transport()

    def _cue_field(self, key: str, value, restart_audio: bool = False):
        cue = self.cue()
        if self._loading or cue is None or getattr(cue, key) == value:
            return
        setattr(cue, key, value)
        self.state.cues_edited()
        if restart_audio and self.on_this_cue():
            self.state.fire_cue(self._cue_index, at=self.state.engine.cue_time(), paused=self.state.engine.paused)
        self._refresh()

    def _pick_audio(self):
        cue = self.cue()
        if cue is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose soundtrack", str(self.state.project.base_dir),
                                              "Audio (*.wav *.mp3 *.ogg *.flac *.m4a *.aac *.opus);;All files (*)")
        if path:
            self._cue_field("audio", self.state.project.relativize_media(path), restart_audio=True)

    def _tick(self):
        if self.on_this_cue():
            t = self.state.engine.cue_time()
            self.time_label.setText(fmt_time(t))
            if not self.state.engine.paused:
                if self.view._drag is None:
                    self.view.ensure_visible_t(t)
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
        self.state.cues_edited()
        self.state.select_cue(cues.index(cue))
        self._refresh()

    def _length_changed(self, v: float):
        cue = self.cue()
        if self._loading or cue is None or abs(cue.total - float(v)) < 1e-6:
            return
        cue.set_length(float(v))
        self.state.cues_edited()
        self.state.engine.refresh()
        self.view.relayout()

    def _end_mode_changed(self, _i: int):
        cue = self.cue()
        if self._loading or cue is None:
            return
        mode = self.end_mode.currentData()
        if mode != cue.end_mode:
            cue.set_end_mode(mode)
            self.state.cues_edited()

    def set_end_mode(self, mode: str):
        cue = self.cue()
        if cue is None or cue.end_mode == mode:
            return
        cue.set_end_mode(mode)
        self.state.cues_edited()
        self._refresh()

    def set_length_live(self, seconds: float, final: bool):
        """End-marker drag."""
        cue = self.cue()
        if cue is None:
            return
        cue.set_length(max(0.1, round(seconds * 20) / 20))
        self.state.engine.refresh()
        if final:
            self.state.cues_edited()
            self._refresh()
        else:
            self._loading = True
            self.length.setValue(cue.total)
            self._loading = False
            self.view.relayout()

    def fit_length(self):
        cue = self.cue()
        if cue is None or not cue.clips:
            return
        cue.set_length(max(c.end for c in cue.clips))
        self.state.cues_edited()
        self._refresh()

    def ask_length(self):
        cue = self.cue()
        if cue is None:
            return
        v, ok = QInputDialog.getDouble(self, "Cue length", "Seconds", cue.total, 0.1, 36000, 1)
        if ok:
            cue.set_length(v)
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
        cue.extend_to(clip.end)
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
        cue = self.cue()
        if cue is not None and c.end > cue.total + 1e-6:
            cue.extend_to(c.end)  # a clip dragged past the end takes the end with it
            self._loading = True
            self.length.setValue(cue.total)
            self._loading = False
            self.view.relayout()
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
        cue.extend_to(d.end)
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

    # -- clipboard -------------------------------------------------------------------------------
    def copy_clip(self, c: Clip):
        import json
        from dataclasses import asdict

        QApplication.clipboard().setText(CLIP_MIME + json.dumps(asdict(c)))

    def clipboard_clip(self) -> Clip | None:
        import json

        text = QApplication.clipboard().text() or ""
        if not text.startswith(CLIP_MIME):
            return None
        try:
            d = json.loads(text[len(CLIP_MIME):])
            return Clip(**{k: v for k, v in d.items() if k in Clip.__dataclass_fields__})
        except (ValueError, TypeError):
            return None

    def copy_selected(self):
        c = self.selected_clip()
        if c is not None:
            self.copy_clip(c)

    def cut_clip(self, c: Clip):
        self.copy_clip(c)
        self.delete_clip(c)

    def cut_selected(self):
        c = self.selected_clip()
        if c is not None:
            self.cut_clip(c)

    def paste(self, sid: str | None = None, at: float | None = None) -> Clip | None:
        """Paste the copied clip: on `sid` (else its own track if that surface exists, else the
        selected one) at `at` (else the playhead)."""
        cue = self.cue()
        src = self.clipboard_clip()
        if cue is None or src is None:
            return None
        if sid is None:
            sid = src.surface_id if self.state.project.surface_by_id(src.surface_id) else None
        if sid is None:
            sel = self.state.selected_surface
            sid = sel.id if (sel is not None and sel.kind == "media") else None
        if sid is None:
            return None
        t = self.state.engine.cue_time() if (at is None and self.on_this_cue()) else (at or 0.0)
        clip = Clip(surface_id=sid, media=src.media, start=round(max(0.0, t) * 20) / 20, duration=src.duration,
                    fade_in=src.fade_in, fade_out=src.fade_out)
        cue.clips.append(clip)
        cue.extend_to(clip.end)
        self.state.cues_edited()
        self.state.engine.refresh()
        self.view.select_clip(clip.id)
        self._refresh()
        return clip

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
