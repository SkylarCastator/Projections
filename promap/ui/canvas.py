"""Editor canvas: live preview of the output plus draggable surface corners / mesh vertices.

Quads have four corner handles. Meshes have a handle per vertex, an outline per
triangle, a highlighted active triangle (click inside it) and an active edge (click on
it) that `A` extrudes a new triangle from. Dragging a mesh vertex onto another vertex
of the same mesh welds them, which is how two loose triangles become one square.
`U` flips the canvas into UV mode for a mesh: the media is shown flat and the vertices
are dragged in texture space instead of output space.
"""
from __future__ import annotations

import moderngl
from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QPainter, QPen, QColor, QBrush, QMouseEvent, QKeyEvent, QFont, QPolygonF, QWheelEvent, QPainterPath
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from promap.model.geometry import point_in_quad, is_convex
from promap.model.project import Surface
from promap.render.renderer import Renderer, letterbox
from promap.show.engine import RenderItem
from promap.ui.state import AppState

HANDLE_R = 7.0
SNAP_PX = 9.0  # widget pixels within which a dragged mesh vertex snaps to another vertex
EDGE_PX = 6.0
COLORS = {"media": QColor(0, 200, 255), "mask": QColor(255, 80, 80), "reference": QColor(140, 140, 140), "selected": QColor(255, 220, 0)}


class EditorCanvas(QOpenGLWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.ctx: moderngl.Context | None = None
        self.renderer: Renderer | None = None
        # sid, point index (None = whole surface), press pos, original points
        self._drag: tuple[str, int | None, QPointF, list] | None = None
        self._snap_target: int | None = None  # mesh vertex the dragged vertex will weld to
        self.zoom = 1.0  # view zoom relative to "fit"
        self.pan = QPointF(0, 0)  # view offset in widget px
        self._pan_drag: tuple[QPointF, QPointF] | None = None
        self.lasso_mode = False
        self._lasso: list[tuple[float, float]] = []  # output-space points being drawn
        self._lasso_dragging = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(320, 180)
        self.frames = 0

    # -- GL ------------------------------------------------------------------
    def initializeGL(self):
        self.ctx = moderngl.create_context()
        self.renderer = Renderer(self.ctx)

    def paintGL(self):
        painter = QPainter(self)
        painter.beginNativePainting()
        fbo = self.ctx.detect_framebuffer()
        out_w, out_h = self.state.project.output_size
        bx, by, bs = self._box()  # widget px; the framebuffer may be scaled by the device pixel ratio
        dpr = fbo.width / max(1, self.width())
        x, y, w, h = int(round(bx * dpr)), int(round(by * dpr)), int(round(out_w * bs * dpr)), int(round(out_h * bs * dpr))
        self.ctx.scissor = None
        items = self._uv_items() if self._uv_surface() else self.state.render_items(editor=True)
        # the 3D view only needs scripted frames every few canvas frames (readback is the costly part)
        self.renderer.gen.readback = self.state.view3d_open and self.frames % 3 == 0
        self.renderer.render(
            fbo, (out_w, out_h), items, (x, fbo.height - y - h, w, h),
            clear_color=(0.08, 0.08, 0.09),
        )
        if self.renderer.gen.readback:
            self.state.gen_frames = dict(self.renderer.gen.readbacks)
        painter.endNativePainting()
        self._draw_overlay(painter)
        painter.end()
        self.frames += 1

    # -- UV mode ----------------------------------------------------------------
    def _uv_surface(self) -> Surface | None:
        s = self.state.selected_surface
        return s if self.state.uv_mode and s is not None and s.mesh is not None else None

    def _uv_items(self) -> list[RenderItem]:
        """The selected mesh's current frame stretched over the whole output, undistorted."""
        s = self._uv_surface()
        w, h = self.state.project.output_size
        full = Surface(id="__uv__", name="uv", corners=[[0, 0], [w, 0], [w, h], [0, h]], flip_h=s.flip_h, flip_v=s.flip_v)
        for it in self.state.engine.render_items():
            if it.surface.id == s.id and it.frame is not None:
                return [RenderItem("__uv__", full, it.frame, it.frame_key, 1.0)]
        return []

    # -- mapping output <-> widget -------------------------------------------
    def _box(self) -> tuple[float, float, float]:
        """(x, y, scale): where output pixel (0,0) lands in the widget and widget px per output px."""
        out_w, out_h = self.state.project.output_size
        x, y, w, h = letterbox(out_w, out_h, self.width(), self.height())
        s = w / out_w * self.zoom
        cx, cy = self.width() / 2, self.height() / 2
        # zoom about the widget centre, then pan
        return cx - (cx - x) * self.zoom + self.pan.x(), cy - (cy - y) * self.zoom + self.pan.y(), s

    def zoom_by(self, factor: float, about: QPointF | None = None):
        """Multiply the zoom, keeping the output point under `about` (widget px) fixed."""
        about = about or QPointF(self.width() / 2, self.height() / 2)
        before = self.to_output(about)
        self.zoom = max(0.2, min(20.0, self.zoom * factor))
        after = self.to_widget(before)
        self.pan += about - after
        self.update()

    def zoom_fit(self):
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self.update()

    def wheelEvent(self, ev: QWheelEvent):
        d = ev.angleDelta().y()
        if d == 0:
            return
        self.zoom_by(1.15 if d > 0 else 1 / 1.15, ev.position())

    def to_widget(self, p) -> QPointF:
        x, y, s = self._box()
        return QPointF(x + p[0] * s, y + p[1] * s)

    def to_output(self, p: QPointF) -> tuple[float, float]:
        x, y, s = self._box()
        return (p.x() - x) / s, (p.y() - y) / s

    def _points(self, surf: Surface) -> list[list[float]]:
        """Editable points of a surface in output space (uv mode: texture coords scaled to the output)."""
        if surf is self._uv_surface():
            w, h = self.state.project.output_size
            return [[v[2] * w, v[3] * h] for v in surf.mesh.verts]
        return surf.handle_points()

    def _set_point(self, surf: Surface, i: int, x: float, y: float):
        if surf is self._uv_surface():
            w, h = self.state.project.output_size
            surf.mesh.verts[i][2], surf.mesh.verts[i][3] = x / w, y / h
        else:
            surf.move_point(i, x, y)

    # -- overlay ---------------------------------------------------------------
    def _draw_overlay(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        x, y, s = self._box()
        out_w, out_h = self.state.project.output_size
        painter.setPen(QPen(QColor(70, 70, 75), 1))
        painter.drawRect(QRectF(x, y, out_w * s, out_h * s))
        banner = None
        if self.state._override is not None:
            banner = "Camera window is projecting a pattern: surfaces are hidden until it finishes. Esc here, or Cancel / untick 'Show white' in the Camera window"
        elif self.state.engine.blackout:
            banner = "BLACKOUT (B or Esc to clear)"
        elif self.state.engine.current_cue is not None and not any(c.media for c in self.state.engine.current_cue.clips):
            banner = f"cue '{self.state.engine.current_cue.name}' assigns no media: everything is dark by design (Preview mode shows the editor's media)"
        if banner:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(180, 40, 40, 200)))
            painter.drawRect(QRectF(x, y, out_w * s, 22))
            painter.setPen(QPen(QColor(255, 255, 255)))
            painter.drawText(QRectF(x + 8, y, out_w * s - 16, 22), Qt.AlignVCenter | Qt.AlignLeft, banner)
        errors = self.state.engine.load_errors
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        uv_surf = self._uv_surface()
        surfaces = [uv_surf] if uv_surf else self.state.project.surfaces
        for surf in surfaces:
            sel = surf.id == self.state.selected
            color = COLORS["selected"] if sel else COLORS[surf.kind]
            pts = [self.to_widget(c) for c in self._points(surf)]
            pen = QPen(color, 2 if sel else 1)
            if not surf.visible:
                pen.setStyle(Qt.DotLine)
            if surf.mesh is None and not is_convex(surf.corners):
                pen.setColor(QColor(255, 0, 255))
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            if surf.mesh is not None:
                self._draw_mesh_overlay(painter, surf, pts, color, sel)
            else:
                for i in range(4):
                    painter.drawLine(pts[i], pts[(i + 1) % 4])
            for i, p in enumerate(pts):
                active = sel and self.state.active_corner == i
                painter.setPen(QPen(color, 1))
                painter.setBrush(QBrush(color) if active else QBrush(QColor(0, 0, 0, 160)))
                r = HANDLE_R if surf.mesh is None or sel else HANDLE_R * 0.6
                painter.drawRect(QRectF(p.x() - r, p.y() - r, r * 2, r * 2))
            if uv_surf:
                label = f"UV mode: {surf.name}  (U to leave)"
            else:
                label = surf.name + (f"  [{surf.item_type}]" if surf.item_type else "") + ("  🔒" if surf.locked else "")
                if surf.mesh is not None:
                    label += f"  △{len(surf.mesh.tris)}"
                if surf.kind != "mask" and surf.media and surf.media in errors:
                    label += "  [!] media missing"
            c = self.to_widget(surf.centroid() if not uv_surf else (out_w / 2, out_h / 2))
            metrics = painter.fontMetrics()
            tw = metrics.horizontalAdvance(label) + 10
            box = QRectF(c.x() - tw / 2, c.y() - metrics.height() / 2 - 2, tw, metrics.height() + 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QPen(color))
            painter.drawText(box, Qt.AlignCenter, label)
        if self.lasso_mode:
            painter.setPen(QPen(QColor(255, 230, 0), 2, Qt.DashLine))
            painter.setBrush(QBrush(QColor(255, 230, 0, 40)))
            if len(self._lasso) >= 2:
                path = QPainterPath(self.to_widget(self._lasso[0]))
                for q in self._lasso[1:]:
                    path.lineTo(self.to_widget(q))
                painter.drawPath(path)
            for q in self._lasso[-1:] + self._lasso[:1]:
                c = self.to_widget(q)
                painter.drawEllipse(c, 4, 4)
            painter.setPen(QPen(QColor(255, 230, 0)))
            painter.setBrush(QBrush(QColor(0, 0, 0, 170)))
            msg = "LASSO: drag to draw freehand, or click points; double-click / Enter closes, Esc cancels"
            tw = painter.fontMetrics().horizontalAdvance(msg) + 12
            painter.drawRoundedRect(QRectF(x + 8, y + 26, tw, 20), 3, 3)
            painter.drawText(QRectF(x + 14, y + 26, tw, 20), Qt.AlignVCenter | Qt.AlignLeft, msg)
        if self._snap_target is not None and self._drag is not None:
            surf = self.state.project.surface_by_id(self._drag[0])
            if surf is not None and surf.mesh is not None and self._snap_target < len(surf.mesh.verts):
                p = self.to_widget(self._points(surf)[self._snap_target])
                painter.setPen(QPen(QColor(0, 255, 120), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(p, HANDLE_R + 4, HANDLE_R + 4)

    def _draw_mesh_overlay(self, painter: QPainter, surf: Surface, pts: list[QPointF], color: QColor, sel: bool):
        m = surf.mesh
        for ti, t in enumerate(m.tris):
            poly = QPolygonF([pts[t[0]], pts[t[1]], pts[t[2]]])
            if sel and self.state.active_tri == ti:
                painter.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 50)))
                painter.setPen(QPen(color, 2))
            else:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(color, 2 if sel else 1))
            painter.drawPolygon(poly)
        if sel and self.state.active_edge is not None and self.state.active_edge[0] < len(m.tris):
            a, b = m.edge_vertices(self.state.active_edge)
            painter.setPen(QPen(QColor(0, 255, 120), 4))
            painter.drawLine(pts[a], pts[b])

    # -- hit testing -----------------------------------------------------------
    def _hit_handle(self, pos: QPointF):
        uv = self._uv_surface()
        order = [uv] if uv else list(self.state.project.surfaces)
        sel = self.state.selected_surface
        if not uv and sel is not None:
            order.remove(sel)
            order.insert(0, sel)
        for surf in order:
            if surf.locked:
                continue
            for i, c in enumerate(self._points(surf)):
                p = self.to_widget(c)
                if abs(p.x() - pos.x()) <= HANDLE_R + 2 and abs(p.y() - pos.y()) <= HANDLE_R + 2:
                    return surf.id, i
        return None

    def _hit_surface(self, pos: QPointF):
        """(sid, tri index or None) for the top-most surface under pos."""
        uv = self._uv_surface()
        op = self.to_output(pos)
        if uv:
            return (uv.id, uv.mesh.hit(op)) if uv.mesh.hit(op) is not None else None
        for surf in reversed(self.state.project.surfaces):
            if surf.locked:
                continue
            if surf.mesh is not None:
                t = surf.mesh.hit(op)
                if t is not None:
                    return surf.id, t
            elif point_in_quad(surf.corners, op):
                return surf.id, None
        return None

    def _hit_edge(self, pos: QPointF):
        """Edge of the selected mesh under pos, as (tri, k)."""
        s = self.state.selected_surface
        if s is None or s.mesh is None or s.locked:
            return None
        _, _, scale = self._box()
        return s.mesh.nearest_edge(self.to_output(pos), EDGE_PX / scale)

    def _snap_candidate(self, surf: Surface, idx: int, x: float, y: float) -> int | None:
        """Another vertex of the same mesh within SNAP_PX of (x, y) in output space."""
        if surf.mesh is None or self._uv_surface() is surf:
            return None
        _, _, scale = self._box()
        tol = SNAP_PX / scale
        best, best_d = None, tol
        for j, v in enumerate(surf.mesh.verts):
            if j == idx:
                continue
            d = max(abs(v[0] - x), abs(v[1] - y))
            if d < best_d:
                best, best_d = j, d
        return best

    # -- mouse -------------------------------------------------------------------
    def mousePressEvent(self, ev: QMouseEvent):
        pos = ev.position()
        if ev.button() == Qt.MiddleButton or (ev.button() == Qt.LeftButton and ev.modifiers() & Qt.AltModifier):
            self._pan_drag = (pos, QPointF(self.pan))
            self.setCursor(Qt.ClosedHandCursor)
            return
        if ev.button() == Qt.RightButton and not self.lasso_mode:
            hit = self._hit_surface(pos)
            if hit:
                self.state.select(hit[0])
                from promap.ui.surface_menu import surface_menu

                m = surface_menu(self.state, hit[0], self)
                if m is not None:
                    m.exec(ev.globalPosition().toPoint())
            return
        if ev.button() != Qt.LeftButton:
            return
        if self.lasso_mode:
            self._lasso.append(self.to_output(pos))
            self._lasso_dragging = True
            self._lasso_drag_start = pos
            self.update()
            return
        self.state.begin_gesture()
        hit = self._hit_handle(pos)
        if hit:
            sid, idx = hit
            surf = self.state.project.surface_by_id(sid)
            self.state.select(sid, idx, self.state.active_tri if sid == self.state.selected else None, None)
        else:
            edge = self._hit_edge(pos)
            if edge is not None:
                sid, idx = self.state.selected, None
                self.state.select(sid, None, edge[0], edge)
            else:
                hs = self._hit_surface(pos)
                sid, tri = hs if hs else (None, None)
                idx = None
                self.state.select(sid, None, tri, None)
        if sid:
            surf = self.state.project.surface_by_id(sid)
            self._drag = (sid, idx, pos, [list(c) for c in self._points(surf)])
        self.setFocus()
        self.update()

    def mouseMoveEvent(self, ev: QMouseEvent):
        if self._pan_drag is not None:
            start, pan0 = self._pan_drag
            self.pan = pan0 + (ev.position() - start)
            self.update()
            return
        if self.lasso_mode:
            if self._lasso_dragging and (ev.position() - self._lasso_drag_start).manhattanLength() > 6:
                q = self.to_output(ev.position())
                if not self._lasso or (abs(q[0] - self._lasso[-1][0]) + abs(q[1] - self._lasso[-1][1])) > 2.0:
                    self._lasso.append(q)
                self._freehand = True
                self.update()
            return
        if self._drag is None:
            hit = self._hit_handle(ev.position())
            if hit:
                cur = Qt.CrossCursor
            elif self._hit_edge(ev.position()) is not None:
                cur = Qt.PointingHandCursor
            else:
                cur = Qt.SizeAllCursor if self._hit_surface(ev.position()) else Qt.ArrowCursor
            self.setCursor(cur)
            return
        sid, idx, start, orig = self._drag
        surf = self.state.project.surface_by_id(sid)
        if surf is None:
            return
        _, _, s = self._box()
        d = ev.position() - start
        dx, dy = d.x() / s, d.y() / s
        if ev.modifiers() & Qt.ShiftModifier:
            dx, dy = round(dx / 10) * 10, round(dy / 10) * 10
        if idx is None:
            if surf is self._uv_surface():
                for i, c in enumerate(orig):
                    self._set_point(surf, i, c[0] + dx, c[1] + dy)
            else:
                cx, cy = orig[0]
                px, py = surf.handle_points()[0]
                surf.translate(cx + dx - px, cy + dy - py)
            self._snap_target = None
        else:
            x, y = orig[idx][0] + dx, orig[idx][1] + dy
            self._snap_target = self._snap_candidate(surf, idx, x, y)
            if self._snap_target is not None:
                v = surf.mesh.verts[self._snap_target]
                x, y = v[0], v[1]
            self._set_point(surf, idx, x, y)
        self.state.surface_edited(sid, geometry_only=True)
        self.update()

    def mouseReleaseEvent(self, ev: QMouseEvent):
        if self._pan_drag is not None:
            self._pan_drag = None
            self.setCursor(Qt.ArrowCursor)
            return
        if self.lasso_mode:
            if getattr(self, "_freehand", False):
                self._finish_lasso()  # a drag closes the loop immediately
            self._lasso_dragging = False
            self._freehand = False
            return
        if self._drag is not None:
            self.state.end_gesture()
        if self._drag is not None and self._snap_target is not None:
            sid, idx, _, _ = self._drag
            surf = self.state.project.surface_by_id(sid)
            if surf is not None and surf.mesh is not None and idx is not None:
                surf.mesh.weld_vertex(self._snap_target, idx)
                keep = self._snap_target if self._snap_target < idx else self._snap_target
                self.state.select(sid, min(keep, len(surf.mesh.verts) - 1), None, None)
                self.state.surface_edited(sid, geometry_only=True)
        self._drag = None
        self._snap_target = None
        self.update()

    # -- lasso ---------------------------------------------------------------------------
    def set_lasso_mode(self, on: bool):
        self.lasso_mode = on
        self._lasso = []
        self._freehand = False
        self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)
        self.update()

    def _finish_lasso(self):
        from promap.camera.detect import simplify_polygon, polygon_mesh

        pts = simplify_polygon(self._lasso, epsilon=3.0 / max(self._box()[2], 1e-6))
        self._lasso = []
        self._freehand = False
        if len(pts) >= 3:
            mesh = polygon_mesh(pts)
            if mesh.tris:
                n = sum(1 for s_ in self.state.project.surfaces if s_.name.startswith("Lasso")) + 1
                self.state.add_mesh_surface(mesh, f"Lasso {n}", notes="drawn with the lasso tool")
        self.set_lasso_mode(False)

    def mouseDoubleClickEvent(self, ev: QMouseEvent):
        """Double-click an edge of the selected mesh to extrude a triangle from it; in lasso mode, close the polygon."""
        if self.lasso_mode:
            if self._lasso:
                self._lasso.pop()  # the double-click added a duplicate point
            self._finish_lasso()
            return
        edge = self._hit_edge(ev.position())
        if edge is not None:
            self.state.select(self.state.selected, None, edge[0], edge)
            self.state.extrude()
            self.update()

    # -- keyboard --------------------------------------------------------------------
    def keyPressEvent(self, ev: QKeyEvent):
        surf = self.state.selected_surface
        if surf is not None and surf.locked:
            surf = None
        key = ev.key()
        if self.lasso_mode:
            if key == Qt.Key_Escape:
                self.set_lasso_mode(False)
            elif key in (Qt.Key_Return, Qt.Key_Enter):
                self._finish_lasso()
            return
        if key in (Qt.Key_Plus, Qt.Key_Equal):
            self.zoom_by(1.25)
            return
        if key == Qt.Key_Minus:
            self.zoom_by(0.8)
            return
        if key == Qt.Key_0:
            self.zoom_fit()
            return
        if key == Qt.Key_Escape:
            self.state.cancelRequested.emit()
            self.state.set_override_frame(None)
            if self.state.engine.blackout:
                self.state.set_blackout(False)
            self.update()
            return
        if surf is not None and surf.mesh is not None:
            if key == Qt.Key_A:
                self.state.extrude()
                self.update()
                return
            if key in (Qt.Key_Delete, Qt.Key_X) and self.state.active_tri is not None:
                self.state.delete_triangle()
                self.update()
                return
            if key == Qt.Key_U:
                self.state.set_uv_mode(not self.state.uv_mode)
                self.update()
                return
        step = 10.0 if ev.modifiers() & Qt.ShiftModifier else 1.0
        moves = {Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0), Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step)}
        if surf is None or key not in moves:
            if key == Qt.Key_Tab and surf is not None:
                n = len(surf.handle_points())
                self.state.select(surf.id, ((self.state.active_corner if self.state.active_corner is not None else -1) + 1) % n,
                                  self.state.active_tri, None)
                self.update()
                return
            super().keyPressEvent(ev)
            return
        dx, dy = moves[key]
        pts = self._points(surf)
        if self.state.active_corner is None or self.state.active_corner >= len(pts):
            if surf is self._uv_surface():
                for i, c in enumerate(pts):
                    self._set_point(surf, i, c[0] + dx, c[1] + dy)
            else:
                surf.translate(dx, dy)
        else:
            c = pts[self.state.active_corner]
            self._set_point(surf, self.state.active_corner, c[0] + dx, c[1] + dy)
        self.state.surface_edited(surf.id, geometry_only=True)
        self.update()
