"""Shared application state + change signals that the panels and views subscribe to."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from promap.media.sources import TestPatternSource
from promap.model.mesh import Mesh, quad_grid, triangle_mesh
from promap.model.project import Project, Surface
from promap.model.scene import SceneNode
from promap.show.engine import ShowEngine, RenderItem


class AppState(QObject):
    projectReplaced = Signal()
    surfacesChanged = Signal()  # membership, order or properties changed
    selectionChanged = Signal(object)  # surface id or None
    cuesChanged = Signal()
    showChanged = Signal()  # cue index / blackout / test pattern changed
    sceneChanged = Signal()  # 3D scene nodes / projector edited
    sceneSelectionChanged = Signal(object)  # "projector", a node id, or None
    view3dRequested = Signal(str)  # "open", "close", or a viewpoint name
    windowRequested = Signal(str)  # "camera" | "animations": open that tool window
    cancelRequested = Signal()  # Esc: stop any pattern run and clear the override frame
    undoChanged = Signal()  # undo/redo stacks changed (menu enable state)

    def __init__(self):
        super().__init__()
        self.project = Project()
        self.engine = ShowEngine(self.project)
        self.selected: str | None = None
        self.active_corner: int | None = None  # quad corner index, or mesh vertex index
        self.active_tri: int | None = None  # mesh: triangle under the last click
        self.active_edge: tuple[int, int] | None = None  # mesh: (tri, k) edge to extrude from
        self.uv_mode = False  # canvas edits texture coordinates instead of positions
        self.test_pattern = False
        self.dirty = False
        self._pattern: TestPatternSource | None = None
        self.scene_selected: str | None = None
        self.hardware = None  # HardwareLibrary, set by the main window
        self.audio = None  # AudioEngine, set by the main window
        # undo: snapshots of the project document; edits within MERGE_S of the last pushed one coalesce
        self._undo: list[str] = []
        self._redo: list[str] = []
        self._last_snapshot: str | None = None
        self._last_push_t = 0.0
        self._gesture = 0
        self.UNDO_LIMIT = 200
        self.MERGE_S = 0.4
        self._last_snapshot = self._snapshot()  # the empty project is the first undo target

    # -- project lifecycle ---------------------------------------------------
    def replace_project(self, project: Project):
        self.project = project
        self.engine.set_project(project)
        self.selected = None
        self.active_corner = None
        self.dirty = False
        self._pattern = None
        self._override = None
        self._calibration = None
        self._undo.clear()
        self._redo.clear()
        self._last_snapshot = self._snapshot()
        self.undoChanged.emit()
        self.projectReplaced.emit()
        self.surfacesChanged.emit()
        self.cuesChanged.emit()
        self.selectionChanged.emit(None)
        self.showChanged.emit()
        self.scene_selected = None
        self.sceneChanged.emit()
        self.sceneSelectionChanged.emit(None)

    def new_project(self):
        self.replace_project(Project())

    def load(self, path: str | Path):
        self.replace_project(Project.load(path))

    def save(self, path: str | Path | None = None) -> Path:
        p = self.project.save(path)
        self.dirty = False
        return p

    # -- surfaces ------------------------------------------------------------
    @property
    def selected_surface(self) -> Surface | None:
        return self.project.surface_by_id(self.selected) if self.selected else None

    def select(self, sid: str | None, corner: int | None = None, tri: int | None = None,
               edge: tuple[int, int] | None = None):
        changed = sid != self.selected or corner != self.active_corner or tri != self.active_tri or edge != self.active_edge
        if sid != self.selected:
            self.uv_mode = False
        self.selected, self.active_corner, self.active_tri, self.active_edge = sid, corner, tri, edge
        if changed:
            self.selectionChanged.emit(sid)

    def add_surface(self, kind: str = "media", mesh: str | None = None) -> Surface:
        """Add a quad (mesh=None), a single triangle ("triangle") or a two-triangle square ("square")."""
        n = len(self.project.surfaces) + 1
        w, h = self.project.output_size
        label = "Mask" if kind == "mask" else ("Triangle" if mesh == "triangle" else "Surface")
        s = Surface(name=f"{label} {n}", kind=kind)
        s.corners = [[w * 0.3, h * 0.3], [w * 0.7, h * 0.3], [w * 0.7, h * 0.7], [w * 0.3, h * 0.7]]
        if mesh == "triangle":
            s.mesh = triangle_mesh([[w * 0.4, h * 0.3], [w * 0.6, h * 0.65], [w * 0.4, h * 0.65]])
            s.mesh.fit_uvs()
        elif mesh == "square":
            s.mesh = quad_grid(s.corners, 1)
        self.project.surfaces.append(s)
        self.surface_edited(s.id)
        self.select(s.id)
        return s

    # -- mesh editing ----------------------------------------------------------
    def _mesh_surface(self, sid: str | None = None) -> Surface | None:
        s = self.project.surface_by_id(sid) if sid else self.selected_surface
        return s if s is not None and s.mesh is not None else None

    def convert_to_mesh(self, sid: str | None = None, n: int = 1):
        """Turn a quad surface into an n x n triangle grid that reproduces its warp."""
        s = self.project.surface_by_id(sid) if sid else self.selected_surface
        if s is None or s.mesh is not None or s.kind == "reference":
            return
        s.mesh = quad_grid(s.corners, n)
        self.select(s.id)
        self.surface_edited(s.id, geometry_only=True)
        self.selectionChanged.emit(s.id)

    def convert_to_quad(self, sid: str | None = None):
        s = self._mesh_surface(sid)
        if s is None:
            return
        s.corners = s.mesh.bounds_corners()
        s.mesh = None
        self.select(s.id)
        self.surface_edited(s.id, geometry_only=True)
        self.selectionChanged.emit(s.id)

    def extrude(self, sid: str | None = None):
        """Grow a triangle off the active edge, or drop a free triangle if no edge is picked."""
        s = self._mesh_surface(sid)
        if s is None:
            return
        m = s.mesh
        if self.active_edge is not None and self.active_edge[0] < len(m.tris):
            t = m.extrude_edge(self.active_edge)
        else:
            cx, cy = m.centroid()
            x0, y0, x1, y1 = m.bounds()
            r = max(40.0, (x1 - x0) * 0.3)
            t = m.add_triangle([[x1 + 20, y0], [x1 + 20 + r, y0], [x1 + 20, y0 + r]])
        self.select(s.id, len(m.verts) - 1, t, None)
        self.surface_edited(s.id, geometry_only=True)

    def delete_triangle(self, sid: str | None = None):
        s = self._mesh_surface(sid)
        if s is None or self.active_tri is None or self.active_tri >= len(s.mesh.tris):
            return
        s.mesh.delete_triangle(self.active_tri)
        if not s.mesh.tris:
            self.remove_surface(s.id)
            return
        self.select(s.id, None, None, None)
        self.surface_edited(s.id, geometry_only=True)

    def detach_triangle(self, sid: str | None = None):
        s = self._mesh_surface(sid)
        if s is None or self.active_tri is None or self.active_tri >= len(s.mesh.tris) or len(s.mesh.tris) < 2:
            return
        part = s.mesh.detach([self.active_tri])
        d = s.clone()
        d.name = f"{s.name} part"
        d.mesh = part
        self.project.surfaces.insert(self.project.surfaces.index(s) + 1, d)
        self.surface_edited(d.id)
        self.select(d.id)

    def subdivide(self, sid: str | None = None):
        s = self._mesh_surface(sid)
        if s is None:
            return
        s.mesh.subdivide()
        self.select(s.id, None, None, None)
        self.surface_edited(s.id, geometry_only=True)

    def fit_uvs(self, sid: str | None = None):
        s = self._mesh_surface(sid)
        if s is None:
            return
        s.mesh.fit_uvs()
        self.surface_edited(s.id, geometry_only=True)
        self.selectionChanged.emit(s.id)

    def weld(self, sid: str | None = None, tol: float = 3.0):
        s = self._mesh_surface(sid)
        if s is None:
            return
        s.mesh.weld(tol)
        self.select(s.id, None, None, None)
        self.surface_edited(s.id, geometry_only=True)

    def merge_with_above(self, sid: str | None = None):
        """Fold the selected mesh into the mesh listed above it; the upper one keeps its media."""
        s = self._mesh_surface(sid)
        if s is None:
            return
        lst = self.project.surfaces
        i = lst.index(s)
        target = next((t for t in reversed(lst[:i]) if t.mesh is not None and t.kind == s.kind), None)
        if target is None:
            return
        target.mesh.merge(s.mesh)
        lst.remove(s)
        for c in self.project.cues:
            c.media.pop(s.id, None)
        self.surface_edited(target.id)
        self.select(target.id)

    # -- scripted animations ------------------------------------------------------------
    def live_sources(self, sid: str) -> list:
        """The engine's live generative sources for a surface (0, 1, or 2 during a crossfade)."""
        from promap.media.generative import GenerativeSource

        return [l.source for l in self.engine.layers
                if l.surface_id == sid and l.fade_out_at is None and isinstance(l.source, GenerativeSource)]

    def update_gen_params(self, sid: str, params: dict):
        """Change an animation's parameters in place: no restart, the next frame uses them.

        Updates the surface's media string (preview mode) or the current cue's entry so the
        change is saved, and the live source so it shows immediately.
        """
        from promap.media.generative import make_media, is_generative, parse_media

        s = self.project.surface_by_id(sid)
        if s is None:
            return
        cue = self.engine.current_cue
        target = cue.media.get(sid) if cue is not None else s.media
        if not is_generative(target):
            return
        eff, cur = parse_media(target)
        new = make_media(eff.name, {**cur, **params})
        for src in self.live_sources(sid):
            src.set_params(params)
        if cue is not None:
            cue.media[sid] = new
            # keep the layer's media string in step so refresh() does not restart it
            for l in self.engine.layers:
                if l.surface_id == sid and l.fade_out_at is None:
                    l.media = new
            self.dirty = True
            self.cuesChanged.emit()
        else:
            s.media = new
            for l in self.engine.layers:
                if l.surface_id == sid and l.fade_out_at is None:
                    l.media = new
            self.dirty = True
            self.surfacesChanged.emit()

    def assign_media(self, sid: str, media: str | None):
        """Set a surface's media in the editor (preview mode) or in the running cue."""
        s = self.project.surface_by_id(sid)
        if s is None:
            return
        cue = self.engine.current_cue
        if cue is not None:
            cue.media[sid] = media
            self.engine.refresh()
            self.cues_edited()
        else:
            s.media = media
            self.surface_edited(sid)

    def set_uv_mode(self, on: bool):
        s = self._mesh_surface()
        self.uv_mode = bool(on) and s is not None
        self.selectionChanged.emit(self.selected)

    def outlines(self, selected_color=(1.0, 0.9, 0.0, 1.0), color=(0.0, 0.8, 1.0, 1.0)):
        """Polylines for the test pattern: quad outlines, or every triangle of a mesh."""
        out = []
        for s in self.project.surfaces:
            if not s.is_output:
                continue
            c = selected_color if s.id == self.selected else color
            if s.mesh is not None:
                out += [(tri, c) for tri in s.mesh.outlines()]
            else:
                out.append((s.corners, c))
        return out

    def remove_surface(self, sid: str):
        s = self.project.surface_by_id(sid)
        if s is None:
            return
        self.project.surfaces.remove(s)
        for c in self.project.cues:
            c.media.pop(sid, None)
        if self.selected == sid:
            self.select(None)
        self.surface_edited(None)

    def duplicate_surface(self, sid: str):
        s = self.project.surface_by_id(sid)
        if s is None:
            return
        c = s.clone()
        self.project.surfaces.insert(self.project.surfaces.index(s) + 1, c)
        self.surface_edited(c.id)
        self.select(c.id)

    def move_surface(self, sid: str, delta: int):
        lst = self.project.surfaces
        s = self.project.surface_by_id(sid)
        if s is None:
            return
        i = lst.index(s)
        j = max(0, min(len(lst) - 1, i + delta))
        lst.insert(j, lst.pop(i))
        self.surface_edited(sid)

    def surface_edited(self, sid: str | None, geometry_only: bool = False, undoable: bool = True):
        """Call after mutating a Surface. Geometry edits skip the engine refresh (cheap path)."""
        self.dirty = True
        if not geometry_only:
            self.engine.refresh()
        if undoable:
            self._record()
        self.surfacesChanged.emit()

    # -- undo / redo ---------------------------------------------------------------
    def _snapshot(self) -> str:
        import json

        return json.dumps(self.project.to_dict(), sort_keys=True, separators=(",", ":"))

    def _record(self):
        """Push the pre-edit state when the document changed. Rapid edits (a slider drag) merge."""
        if self._gesture:
            return
        if self._last_snapshot is None:
            self._last_snapshot = self._snapshot()
            return
        now = time.monotonic()
        snap = self._snapshot()
        if snap == self._last_snapshot:
            return
        if now - self._last_push_t > self.MERGE_S or not self._undo:
            self._undo.append(self._last_snapshot)
            del self._undo[:-self.UNDO_LIMIT]
            self._redo.clear()
        self._last_push_t = now
        self._last_snapshot = snap
        self.undoChanged.emit()

    def begin_gesture(self):
        """A drag starts: edits until end_gesture() become one undo step."""
        if self._gesture == 0 and self._last_snapshot is None:
            self._last_snapshot = self._snapshot()
        self._gesture += 1

    def end_gesture(self):
        self._gesture = max(0, self._gesture - 1)
        if self._gesture == 0:
            self._last_push_t = 0.0  # never merge a gesture with what came before
            self._record()

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def _restore(self, snap: str):
        import json

        proj = Project.from_dict(json.loads(snap))
        proj.path = self.project.path
        sel, corner = self.selected, self.active_corner
        self.project = proj
        self.engine.set_project(proj)
        self._pattern = None
        self._last_snapshot = snap
        self._last_push_t = 0.0
        self.dirty = True
        self.selected = sel if proj.surface_by_id(sel) else None
        self.active_corner = corner if self.selected else None
        self.active_tri = self.active_edge = None
        self.projectReplaced.emit()
        self.surfacesChanged.emit()
        self.cuesChanged.emit()
        self.sceneChanged.emit()
        self.selectionChanged.emit(self.selected)
        self.showChanged.emit()
        self.undoChanged.emit()

    def undo(self):
        if not self._undo:
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())

    def redo(self):
        if not self._redo:
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())

    # -- cues ----------------------------------------------------------------
    def cues_edited(self):
        self.dirty = True
        self._record()
        self.cuesChanged.emit()

    def editor_assignments(self) -> dict[str, str | None]:
        return {s.id: s.media for s in self.project.surfaces if s.kind == "media"}

    def add_reference_image(self, path: str | Path) -> Surface:
        """A photo of the space, drawn under everything in the editor only, warpable like any quad."""
        w, h = self.project.output_size
        s = Surface(name=f"Ref: {Path(path).name}", kind="reference", opacity=0.5,
                    corners=[[0, 0], [w, 0], [w, h], [0, h]], media=self.project.relativize_media(path))
        self.project.surfaces.insert(0, s)
        self.surface_edited(s.id)
        self.select(s.id)
        return s

    def load_cue_into_editor(self, index: int):
        cue = self.project.cues[index]
        for s in self.project.surfaces:
            if s.kind == "media":
                s.media = cue.media.get(s.id)
        self.engine.cue_index = -1
        self.surface_edited(None)
        self.showChanged.emit()

    # -- show control --------------------------------------------------------
    def go(self):
        self.engine.go()
        self.showChanged.emit()

    def back(self):
        self.engine.back()
        self.showChanged.emit()

    def goto(self, index: int):
        self.engine.goto(index)
        self.showChanged.emit()

    def preview_mode(self):
        self.engine.preview_mode()
        self.showChanged.emit()

    def set_blackout(self, on: bool):
        self.engine.blackout = on
        self.showChanged.emit()

    def set_test_pattern(self, on: bool):
        self.test_pattern = on
        self.showChanged.emit()

    # -- 3D scene --------------------------------------------------------------
    def select_scene(self, key: str | None):
        if key != self.scene_selected:
            self.scene_selected = key
            self.sceneSelectionChanged.emit(key)

    @property
    def selected_scene_node(self) -> SceneNode | None:
        if self.scene_selected in (None, "projector"):
            return None
        return self.project.scene.node_by_id(self.scene_selected)

    def scene_edited(self):
        self.dirty = True
        self._record()
        self.sceneChanged.emit()

    def add_scene_node(self, kind: str, model: str = "box") -> SceneNode:
        nodes = self.project.scene.nodes
        n = SceneNode(kind=kind, model=model)
        if kind == "camera":
            n.name = f"Camera {len(self.project.scene.cameras) + 1}"
            n.pos = [1.0, -2.5, 1.0]
        else:
            n.name = f"{Path(model).stem.title() if '/' in model or '.' in model else model.title()} {len(nodes) + 1}"
        nodes.append(n)
        self.scene_edited()
        self.select_scene(n.id)
        return n

    def remove_scene_node(self, nid: str):
        n = self.project.scene.node_by_id(nid)
        if n is None:
            return
        self.project.scene.nodes.remove(n)
        if self.scene_selected == nid:
            self.select_scene(None)
        self.scene_edited()

    # -- per frame -----------------------------------------------------------
    def tick(self):
        now = time.monotonic()
        self.engine.tick(now)
        if self.engine.cue_index != self._last_cue:
            self._last_cue = self.engine.cue_index
            self.showChanged.emit()

    _last_cue = -1

    view3d_open = False  # set by the main window; the canvas then reads scripted frames back for Panda
    gen_frames: dict = {}  # layer id -> numpy frame of a scripted animation, refreshed each canvas paint

    # -- depth point cloud (shown in the 3D view under a camera node) --------------------
    point_cloud: tuple | None = None  # (xyz (N,3) float32 metres, rgb (N,3) uint8, camera node id, seq)
    _cloud_seq = 0

    def set_point_cloud(self, xyz, rgb, node_id: str | None):
        self._cloud_seq += 1
        self.point_cloud = (xyz, rgb, node_id, self._cloud_seq) if xyz is not None else None

    # -- tracked skeletons (drawn in the 3D view under the same camera node) ------------------
    skeletons: tuple | None = None  # (list of (33,3) metre arrays with NaN, camera node id, seq)
    _skel_seq = 0

    def set_skeletons(self, joints_list, node_id: str | None):
        self._skel_seq += 1
        self.skeletons = (list(joints_list), node_id, self._skel_seq) if joints_list is not None else None

    # -- camera / calibration --------------------------------------------------------
    _override: RenderItem | None = None
    _calibration = None  # promap.camera.graycode.Calibration, loaded lazily from the project

    def set_override_frame(self, frame, key: object = None):
        """Show one full-output image on every view (used while projecting calibration patterns)."""
        if frame is None:
            self._override = None
        else:
            w, h = self.project.output_size
            full = Surface(id="__override__", name="override", corners=[[0, 0], [w, 0], [w, h], [0, h]])
            self._override = RenderItem("__override__", full, frame, key if key is not None else id(frame), 1.0)

    @property
    def calibration(self):
        if self._calibration is None and self.project.camera:
            from promap.camera.graycode import Calibration

            cal = Calibration.from_dict(self.project.camera)
            p = self.project.dense_map_path
            if p is not None:
                cal.load_dense(p)
            self._calibration = cal
        return self._calibration

    def set_calibration(self, cal):
        self._calibration = cal
        self.project.camera = cal.to_dict() if cal is not None else None
        p = self.project.dense_map_path
        if cal is not None and p is not None:
            cal.save_dense(p)
        self.dirty = True
        self.surfacesChanged.emit()

    def add_mesh_surface(self, mesh: Mesh, name: str, item_type: str = "", notes: str = "") -> Surface:
        s = Surface(name=name, kind="media", mesh=mesh, item_type=item_type, notes=notes)
        s.corners = mesh.bounds_corners()
        self.project.surfaces.append(s)
        self.surface_edited(s.id)
        self.select(s.id)
        return s

    def render_items(self, editor: bool = False) -> list[RenderItem]:
        """Items to draw. Reference images are included (underneath) only for the editor."""
        if self._override is not None:
            return [self._override]
        items: list[RenderItem] = []
        if editor:
            items.extend(it for it in self.engine.render_items() if it.surface.kind == "reference")
        if self.test_pattern:
            if self._pattern is None or self._pattern.size != self.project.output_size:
                self._pattern = TestPatternSource(self.project.output_size)
            w, h = self.project.output_size
            full = Surface(id="__pattern__", name="pattern", corners=[[0, 0], [w, 0], [w, h], [0, h]])
            frame, key = self._pattern.frame_at(0.0)
            items.append(RenderItem("__pattern__", full, frame, key, 1.0))
        items.extend(it for it in self.engine.render_items() if it.surface.is_output)
        return items
