"""Timeline: the cue list laid out on a time axis, one track per surface, like a video editor.

Columns are cues. A cue with a hold time is as wide as that hold; one that waits for GO is
drawn hatched at a default width. Each cell shows what that surface plays in that cue
(file name, animation title, or dark). A playhead runs while a held cue counts down.

Mouse: click a cell to select the surface and jump the show to that cue; drag the right
edge of a cue header to change its hold; right-click a cell for a menu (animation, file,
dark, copy from editor); double-click the header to rename. Wheel + Ctrl zooms.
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QBrush, QFont, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget, QMenu, QFileDialog, QInputDialog, QVBoxLayout, QScrollArea

from promap.media import generative as gen
from promap.model.project import Cue
from promap.ui.state import AppState

HEADER_H = 26
TRACK_H = 34
LABEL_W = 150
DEFAULT_HOLD = 8.0  # display width (seconds) of a cue that waits for GO
MIN_W = 36

GROUP_COLORS = {
    "organic": QColor(90, 180, 120), "retro": QColor(200, 140, 70), "material": QColor(120, 150, 220), "geometric": QColor(220, 90, 90),
    "light": QColor(230, 90, 200), "3d": QColor(120, 200, 220), "print": QColor(170, 120, 200), "text": QColor(240, 200, 60),
    "particles": QColor(90, 210, 170), "digital": QColor(80, 220, 200), "lines": QColor(150, 190, 100),
}
FILE_COLOR = QColor(70, 130, 180)
DARK_COLOR = QColor(45, 45, 50)


class TimelineWidget(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.px_per_s = 14.0
        self._drag: tuple[int, float, float] | None = None  # cue index, start x, original hold
        self.setMouseTracking(True)
        self.setMinimumHeight(HEADER_H + TRACK_H * 2)
        state.cuesChanged.connect(self._relayout)
        state.surfacesChanged.connect(self._relayout)
        state.projectReplaced.connect(self._relayout)
        state.showChanged.connect(self.update)
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._relayout()

    # -- layout ----------------------------------------------------------------------------
    def _tracks(self):
        return [s for s in self.state.project.surfaces if s.kind != "reference"]

    def _cue_w(self, cue: Cue) -> float:
        hold = cue.hold if cue.hold is not None else DEFAULT_HOLD
        return max(MIN_W, hold * self.px_per_s)

    def _cue_x(self, index: int) -> float:
        x = LABEL_W
        for c in self.state.project.cues[:index]:
            x += self._cue_w(c)
        return x

    def _relayout(self):
        cues = self.state.project.cues
        w = int(LABEL_W + sum(self._cue_w(c) for c in cues) + 120)
        h = HEADER_H + TRACK_H * max(1, len(self._tracks())) + 8
        self.setMinimumSize(w, h)
        self.resize(max(w, self.width()), h)
        self.update()

    def _tick(self):
        eng = self.state.engine
        if eng.current_cue is not None and eng.current_cue.hold is not None:
            self.update()

    # -- painting ----------------------------------------------------------------------------
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(30, 30, 33))
        cues = self.state.project.cues
        tracks = self._tracks()
        font = QFont()
        font.setPointSize(9)
        p.setFont(font)
        cur = self.state.engine.cue_index
        # track labels
        for r, s in enumerate(tracks):
            y = HEADER_H + r * TRACK_H
            p.fillRect(QRectF(0, y, LABEL_W, TRACK_H), QColor(38, 38, 42) if r % 2 else QColor(42, 42, 46))
            p.setPen(QPen(QColor(230, 230, 230) if s.id == self.state.selected else QColor(170, 170, 170)))
            glyph = "▣" if s.kind == "mask" else ("△" if s.mesh is not None else "▢")
            p.drawText(QRectF(8, y, LABEL_W - 12, TRACK_H), Qt.AlignVCenter | Qt.AlignLeft, f"{glyph} {s.name}")
        # cue columns
        x = LABEL_W
        for i, cue in enumerate(cues):
            w = self._cue_w(cue)
            hdr = QRectF(x, 0, w, HEADER_H)
            p.fillRect(hdr, QColor(90, 90, 110) if i == cur else QColor(60, 60, 70))
            if cue.hold is None:
                p.setPen(QPen(QColor(120, 120, 140), 1, Qt.DashLine))
                for k in range(int(w // 8)):
                    xx = x + k * 8
                    p.drawLine(QPointF(xx, HEADER_H - 1), QPointF(xx + 6, 1))
            p.setPen(QPen(QColor(240, 240, 240)))
            label = f"{i + 1}. {cue.name}" + (f"  {cue.hold:g}s" if cue.hold is not None else "  GO")
            p.drawText(hdr.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, p.fontMetrics().elidedText(label, Qt.ElideRight, int(w) - 10))
            if cue.audio:
                p.setPen(QPen(QColor(120, 220, 160)))
                p.drawText(hdr.adjusted(0, 0, -4, 0), Qt.AlignVCenter | Qt.AlignRight, "♪")
            for r, s in enumerate(tracks):
                y = HEADER_H + r * TRACK_H
                cell = QRectF(x + 1, y + 3, w - 2, TRACK_H - 6)
                media = cue.media.get(s.id)
                if s.kind == "mask":
                    color, text = QColor(80, 40, 40), "mask"
                elif not media:
                    color, text = DARK_COLOR, ""
                elif gen.is_generative(media):
                    eff = gen.parse_media(media)[0] if media[4:].split("?")[0] in gen.EFFECTS else None
                    color = GROUP_COLORS.get(eff.group, FILE_COLOR) if eff else FILE_COLOR
                    text = gen.media_label(media)
                else:
                    color, text = FILE_COLOR, Path(media).name
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(color))
                p.drawRoundedRect(cell, 4, 4)
                if text:
                    p.setPen(QPen(QColor(15, 15, 15) if color.lightness() > 120 else QColor(235, 235, 235)))
                    p.drawText(cell.adjusted(6, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, p.fontMetrics().elidedText(text, Qt.ElideRight, int(w) - 12))
            p.setPen(QPen(QColor(20, 20, 22), 2))
            p.drawLine(QPointF(x + w, 0), QPointF(x + w, self.height()))
            x += w
        # time ruler ticks every 5 s across held cues
        p.setPen(QPen(QColor(110, 110, 120)))
        x = LABEL_W
        for cue in cues:
            if cue.hold is not None:
                for k in range(1, int(cue.hold // 5) + 1):
                    xx = x + k * 5 * self.px_per_s
                    p.drawLine(QPointF(xx, HEADER_H - 5), QPointF(xx, HEADER_H))
            x += self._cue_w(cue)
        # playhead
        eng = self.state.engine
        if 0 <= cur < len(cues):
            cue = cues[cur]
            x0 = self._cue_x(cur)
            frac = 0.0
            if cue.hold is not None and eng._cue_entered_at is not None:
                frac = min(1.0, (time.monotonic() - eng._cue_entered_at) / max(cue.hold, 1e-3))
            px = x0 + frac * self._cue_w(cue)
            p.setPen(QPen(QColor(255, 80, 80), 2))
            p.drawLine(QPointF(px, 0), QPointF(px, self.height()))
        if not cues:
            p.setPen(QPen(QColor(150, 150, 150)))
            p.drawText(QRectF(LABEL_W + 10, 0, 500, HEADER_H + TRACK_H), Qt.AlignVCenter | Qt.AlignLeft,
                       "No cues yet: 'Add from editor' in Cues, or '+ Cue' in Animations")
        p.end()

    # -- hit testing ---------------------------------------------------------------------------
    def _cue_at(self, x: float) -> int | None:
        xx = LABEL_W
        for i, cue in enumerate(self.state.project.cues):
            w = self._cue_w(cue)
            if xx <= x < xx + w:
                return i
            xx += w
        return None

    def _track_at(self, y: float):
        r = int((y - HEADER_H) // TRACK_H)
        tracks = self._tracks()
        return tracks[r] if 0 <= r < len(tracks) else None

    def _near_edge(self, x: float) -> int | None:
        xx = LABEL_W
        for i, cue in enumerate(self.state.project.cues):
            xx += self._cue_w(cue)
            if abs(x - xx) <= 5:
                return i
        return None

    # -- mouse ------------------------------------------------------------------------------------
    def mousePressEvent(self, ev: QMouseEvent):
        pos = ev.position()
        if ev.button() == Qt.LeftButton:
            if pos.y() < HEADER_H:
                edge = self._near_edge(pos.x())
                if edge is not None:
                    cue = self.state.project.cues[edge]
                    self._drag = (edge, pos.x(), cue.hold if cue.hold is not None else DEFAULT_HOLD)
                    return
            i = self._cue_at(pos.x())
            s = self._track_at(pos.y())
            if s is not None:
                self.state.select(s.id)
            if i is not None:
                self.state.goto(i)
        elif ev.button() == Qt.RightButton:
            self._menu(ev)

    def mouseMoveEvent(self, ev: QMouseEvent):
        if self._drag is not None:
            i, x0, hold0 = self._drag
            cue = self.state.project.cues[i]
            hold = max(0.5, hold0 + (ev.position().x() - x0) / self.px_per_s)
            cue.hold = round(hold, 1)
            self.state.cues_edited()
            return
        if ev.position().y() < HEADER_H and self._near_edge(ev.position().x()) is not None:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, ev: QMouseEvent):
        self._drag = None

    def mouseDoubleClickEvent(self, ev: QMouseEvent):
        if ev.position().y() < HEADER_H:
            i = self._cue_at(ev.position().x())
            if i is not None:
                cue = self.state.project.cues[i]
                name, ok = QInputDialog.getText(self, "Rename cue", "Name", text=cue.name)
                if ok and name:
                    cue.name = name
                    self.state.cues_edited()

    def wheelEvent(self, ev: QWheelEvent):
        if ev.modifiers() & Qt.ControlModifier:
            self.px_per_s = max(4.0, min(80.0, self.px_per_s * (1.15 if ev.angleDelta().y() > 0 else 1 / 1.15)))
            self._relayout()
        else:
            ev.ignore()

    def _menu(self, ev: QMouseEvent):
        pos = ev.position()
        i = self._cue_at(pos.x())
        s = self._track_at(pos.y())
        cues = self.state.project.cues
        m = QMenu(self)
        if i is not None and s is not None and s.kind == "media":
            cue = cues[i]
            anim = m.addMenu("Animation")
            for name, eff in gen.EFFECTS.items():
                anim.addAction(eff.title, lambda n=name: self._set_cell(cue, s.id, gen.make_media(n)))
            m.addAction("File…", lambda: self._pick_file(cue, s.id))
            m.addAction("Same as editor", lambda: self._set_cell(cue, s.id, s.media))
            m.addAction("Dark (none)", lambda: self._set_cell(cue, s.id, None))
            m.addSeparator()
        if i is not None:
            cue = cues[i]
            m.addAction("Wait for GO" if cue.hold is not None else "Hold 5 s then advance", lambda: self._toggle_hold(cue))
            m.addAction("Duplicate cue", lambda: self._dup(i))
            m.addAction("Delete cue", lambda: self._delete(i))
            m.addSeparator()
        m.addAction("Add cue from editor", self._add_from_editor)
        m.exec(ev.globalPosition().toPoint())

    def _set_cell(self, cue: Cue, sid: str, media):
        cue.media[sid] = media
        if self.state.engine.current_cue is cue:
            self.state.engine.refresh()
        self.state.cues_edited()

    def _pick_file(self, cue: Cue, sid: str):
        from promap.ui.surface_panel import MEDIA_FILTER

        path, _ = QFileDialog.getOpenFileName(self, "Choose media", str(self.state.project.base_dir), MEDIA_FILTER)
        if path:
            self._set_cell(cue, sid, self.state.project.relativize_media(path))

    def _toggle_hold(self, cue: Cue):
        cue.hold = None if cue.hold is not None else 5.0
        self.state.cues_edited()

    def _dup(self, i: int):
        import copy

        c = copy.deepcopy(self.state.project.cues[i])
        c.id = Cue().id
        c.name = f"{c.name} copy"
        self.state.project.cues.insert(i + 1, c)
        self.state.cues_edited()

    def _delete(self, i: int):
        del self.state.project.cues[i]
        self.state.cues_edited()

    def _add_from_editor(self):
        p = self.state.project
        p.cues.append(Cue(name=f"Cue {len(p.cues) + 1}", media=self.state.editor_assignments()))
        self.state.cues_edited()


class TimelinePanel(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.timeline = TimelineWidget(state)
        self.area.setWidget(self.timeline)
        lay.addWidget(self.area)
