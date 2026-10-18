"""Panda3D scene view: projector + cameras as frustums, props as meshes, and the live
2D output projected from the projector onto the props.

Runs inside the Qt process: the main window calls `step()` from its frame timer, which
pumps Panda3D's task manager once. The composited 2D output is rebuilt inside Panda3D
each frame (same clip-space trick as the OpenGL renderer) into an offscreen buffer whose
texture is then projected through the projector's lens with Panda's projective texturing.
"""
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
from panda3d.core import (
    loadPrcFileData, NodePath, LensNode, PerspectiveLens, Texture, TextureStage, Filename,
    GeomVertexFormat, GeomVertexArrayFormat, GeomVertexData, GeomVertexWriter, Geom, GeomNode,
    GeomTriangles, GeomPoints, GeomVertexArrayFormat, InternalName, OmniBoundingVolume, LineSegs, CardMaker, AmbientLight,
    DirectionalLight, LColor, Vec3, Point3, TransparencyAttrib, MouseButton, TextNode, TextureAttrib,
)

from promap.model.geometry import clip_corners, quad_texcoords
from promap.model.scene import SceneNode, PRIMITIVES

log = logging.getLogger(__name__)

PRC = """
window-title promap 3D
win-size 1280 720
sync-video 0
load-file-type p3assimp
framebuffer-multisample 1
multisamples 4
notify-level-glgsg error
"""

VIEW_ORBIT = "orbit"
VIEW_PROJECTOR = "projector"


def _strip_textures(np_: NodePath) -> NodePath:
    """Bundled/loaded models often carry their own texture; only the projector's should show."""
    for gnp in np_.findAllMatches("**/+GeomNode"):
        gn = gnp.node()
        for i in range(gn.getNumGeoms()):
            gn.setGeomState(i, gn.getGeomState(i).removeAttrib(TextureAttrib))
    np_.clearTexture()
    return np_


def _clip_format() -> GeomVertexFormat:
    arr = GeomVertexArrayFormat()
    arr.addColumn(InternalName.getVertex(), 4, Geom.NT_float32, Geom.C_clip_point)
    arr.addColumn(InternalName.getTexcoord(), 2, Geom.NT_float32, Geom.C_texcoord)
    return GeomVertexFormat.registerFormat(GeomVertexFormat(arr))


class View3D:
    """The Panda3D scene. Standalone it is its own OS window; given `parent_handle` (a native
    window id) it renders as a child window inside that widget, and `resize()` keeps it filling it."""

    def __init__(self, state, parent_handle: int | None = None, size: tuple[int, int] | None = None):
        from direct.showbase.ShowBase import ShowBase
        from direct.gui.OnscreenText import OnscreenText
        from panda3d.core import WindowProperties

        loadPrcFileData("", PRC)
        self.state = state
        self.alive = True
        self.embedded = parent_handle is not None
        self.base = ShowBase(windowType="none")
        base = self.base
        wp = WindowProperties.getDefault()
        if size:
            wp.setSize(*size)
        if parent_handle is not None:
            wp.setParentWindow(int(parent_handle))
            wp.setOrigin(0, 0)
            wp.setUndecorated(True)
        base.openMainWindow(props=wp, type="onscreen")
        if base.win is None:
            raise RuntimeError("Panda3D could not open a window")
        base.disableMouse()
        base.setBackgroundColor(0.11, 0.11, 0.13)
        base.win.setCloseRequestEvent("promap-close3d")
        base.accept("promap-close3d", self.close)
        base.accept("1", lambda: self.set_view(VIEW_ORBIT))
        base.accept("2", lambda: self.set_view(VIEW_PROJECTOR))
        for i in range(3, 10):
            base.accept(str(i), lambda i=i: self._view_camera(i - 3))
        base.accept("f", self._reset_orbit)
        base.accept("wheel_up", lambda: self._zoom(0.85))
        base.accept("wheel_down", lambda: self._zoom(1.18))
        base.accept("g", self._toggle_grid)

        self.view = VIEW_ORBIT
        self.orbit_target = Point3(0, 0, 0.8)
        self.orbit_yaw, self.orbit_pitch, self.orbit_dist = 35.0, -25.0, 7.0
        self._last_mouse = None

        self._clip_fmt = _clip_format()
        self._comp_nodes: dict[str, tuple[NodePath, Texture | None, object, tuple[int, int]]] = {}
        self._props: dict[str, tuple[NodePath, str, tuple]] = {}
        self._cams: dict[str, tuple[NodePath, tuple]] = {}
        self._proj_cache: tuple | None = None
        self._out_size: tuple[int, int] | None = None

        self._build_world()
        self._build_projector()
        self._ensure_buffer()
        self.hud = OnscreenText(  # anchored to the top-left corner so any pane aspect keeps it on screen
            text="", parent=base.a2dTopLeft, pos=(0.04, -0.09), scale=0.055, fg=(1, 1, 1, 0.9),
            align=TextNode.ALeft, mayChange=True,
        )
        self._update_hud()

    # -- scene scaffolding -------------------------------------------------------
    def _build_world(self):
        render = self.base.render
        grid = LineSegs("grid")
        grid.setThickness(1)
        for i in range(-10, 11):
            c = (0.35, 0.35, 0.4, 1) if i else (0.6, 0.6, 0.65, 1)
            grid.setColor(*c)
            grid.moveTo(i, -10, 0)
            grid.drawTo(i, 10, 0)
            grid.moveTo(-10, i, 0)
            grid.drawTo(10, i, 0)
        axes = LineSegs("axes")
        axes.setThickness(2)
        for color, end in [((1, 0.3, 0.3, 1), (1, 0, 0)), ((0.3, 1, 0.3, 1), (0, 1, 0)), ((0.4, 0.5, 1, 1), (0, 0, 1))]:
            axes.setColor(*color)
            axes.moveTo(0, 0, 0)
            axes.drawTo(*end)
        self.grid_np = render.attachNewNode(grid.create())
        self.grid_np.attachNewNode(axes.create())

        amb = AmbientLight("amb")
        amb.setColor((0.18, 0.18, 0.2, 1))
        sun = DirectionalLight("sun")
        sun.setColor((0.45, 0.45, 0.45, 1))
        sun_np = render.attachNewNode(sun)
        sun_np.setHpr(-40, -55, 0)
        self.props_root = render.attachNewNode("props")
        self.props_root.setLight(render.attachNewNode(amb))
        self.props_root.setLight(sun_np)

    def _build_projector(self):
        self.proj_lens = PerspectiveLens()
        self.proj_lens.setNearFar(0.2, 12.0)
        self.proj_np = self.base.render.attachNewNode(LensNode("projector", self.proj_lens))
        self.proj_np.node().showFrustum()
        marker = _strip_textures(self.base.loader.loadModel("models/box"))
        marker.setScale(0.18, 0.28, 0.1)
        marker.setPos(-0.09, -0.14, -0.05)
        marker.setColor(0.2, 0.2, 0.25, 1)
        marker.reparentTo(self.proj_np)
        ts = TextureStage("projected")
        ts.setMode(TextureStage.MAdd)
        ts.setSort(10)
        self.proj_stage = ts

    def _ensure_buffer(self):
        size = self.state.project.output_size
        if size == self._out_size:
            return
        if self._out_size is not None:
            self.base.graphicsEngine.removeWindow(self.buffer)
            self.comp_cam.removeNode()
            for np_, *_ in self._comp_nodes.values():
                np_.removeNode()
            self._comp_nodes.clear()
        w, h = size
        self.out_tex = Texture("promap-output")
        self.buffer = self.base.win.makeTextureBuffer("promap-comp", w, h, self.out_tex)
        self.buffer.setClearColor(LColor(0, 0, 0, 1))
        self.buffer.setSort(-100)
        self.comp_root = NodePath("comp")
        self.comp_root.setDepthTest(False)
        self.comp_root.setDepthWrite(False)
        self.comp_root.setTwoSided(True)
        self.comp_cam = self.base.makeCamera(self.buffer, camName="compcam")
        self.comp_cam.reparentTo(self.comp_root)
        self.comp_cam.node().setScene(self.comp_root)
        for setter in (self.out_tex.setWrapU, self.out_tex.setWrapV):
            setter(Texture.WMBorderColor)
        self.out_tex.setBorderColor(LColor(0, 0, 0, 0))
        self.proj_lens.setAspectRatio(w / h)
        self.props_root.clearProjectTexture(self.proj_stage)
        self.props_root.projectTexture(self.proj_stage, self.out_tex, self.proj_np)
        self._out_size = size

    # -- props & cameras -----------------------------------------------------------------
    def _load_model(self, model: str) -> NodePath:
        loader = self.base.loader
        if model == "plane":
            cm = CardMaker("plane")
            cm.setFrame(-0.5, 0.5, -0.5, 0.5)
            cm.setHasNormals(True)
            np_ = NodePath(cm.generate())
            np_.setTwoSided(True)
            return np_
        if model == "sphere":
            try:
                return loader.loadModel("models/misc/sphere")
            except Exception:
                model = "box"
        if model == "box":
            np_ = _strip_textures(loader.loadModel("models/box"))
            np_.setPos(-0.5, -0.5, -0.5)  # the bundled box spans 0..1; centre it
            return np_
        path = self.state.project.resolve_media(model)
        try:
            return _strip_textures(loader.loadModel(Filename.fromOsSpecific(str(path))))
        except Exception as exc:
            log.warning("cannot load model %s: %s", path, exc)
            np_ = loader.loadModel("models/box")
            np_.setPos(-0.5, -0.5, -0.5)
            np_.setColor(1, 0, 1, 1)
            return np_

    def _sync_props(self):
        scene = self.state.project.scene
        wanted = {n.id for n in scene.nodes}
        for nid in [k for k in self._props if k not in wanted]:
            self._props.pop(nid)[0].removeNode()
        for nid in [k for k in self._cams if k not in wanted]:
            self._cams.pop(nid)[0].removeNode()
        for n in scene.nodes:
            key = (tuple(n.pos), tuple(n.hpr), tuple(n.scale), tuple(n.color), n.fov_h, n.name)
            if n.kind == "camera":
                entry = self._cams.get(n.id)
                if entry is None:
                    lens = PerspectiveLens()
                    lens.setNearFar(0.1, 6.0)
                    lens.setAspectRatio(4 / 3)
                    np_ = self.base.render.attachNewNode(LensNode(f"cam:{n.id}", lens))
                    np_.node().showFrustum()
                    marker = _strip_textures(self.base.loader.loadModel("models/camera"))
                    marker.setScale(0.08)
                    marker.reparentTo(np_)
                    self._cams[n.id] = (np_, ())
                    entry = self._cams[n.id]
                np_, cached = entry
                if cached != key:
                    np_.setPos(*n.pos)
                    np_.setHpr(*n.hpr)
                    np_.node().getLens().setFov(n.fov_h)
                    self._cams[n.id] = (np_, key)
                continue
            entry = self._props.get(n.id)
            if entry is not None and entry[1] != n.model:
                entry[0].removeNode()
                entry = None
            if entry is None:
                parent = self.props_root.attachNewNode(f"prop:{n.id}")
                self._load_model(n.model).reparentTo(parent)
                self._props[n.id] = (parent, n.model, ())
                entry = self._props[n.id]
            parent, model, cached = entry
            if cached != key:
                parent.setPos(*n.pos)
                parent.setHpr(*n.hpr)
                parent.setScale(*[max(1e-3, s) for s in n.scale])
                parent.setColor(*n.color, 1)
                self._props[n.id] = (parent, model, key)

    def _sync_projector(self):
        p = self.state.project.scene.projector
        key = (tuple(p.pos), tuple(p.hpr), p.fov_h, self._out_size)
        if key == self._proj_cache:
            return
        self.proj_np.setPos(*p.pos)
        self.proj_np.setHpr(*p.hpr)
        self.proj_lens.setFov(p.fov_h)
        self._proj_cache = key

    # -- composited 2D output inside Panda ------------------------------------------------
    def _comp_node(self, layer_id: str, n_verts: int = 4, triangles=((0, 1, 2), (0, 2, 3))) -> NodePath:
        vdata = GeomVertexData(layer_id, self._clip_fmt, Geom.UHDynamic)
        vdata.setNumRows(n_verts)
        tris = GeomTriangles(Geom.UHStatic)
        for a, b, c in triangles:
            tris.addVertices(int(a), int(b), int(c))
        geom = Geom(vdata)
        geom.addPrimitive(tris)
        node = GeomNode(layer_id)
        node.addGeom(geom)
        node.setBounds(OmniBoundingVolume())
        node.setFinal(True)
        np_ = self.comp_root.attachNewNode(node)
        np_.setTransparency(TransparencyAttrib.MAlpha)
        return np_

    def _sync_composite(self):
        items = self.state.render_items()
        live = set()
        for order, it in enumerate(items):
            live.add(it.layer_id)
            frame_arr = it.frame
            if frame_arr is not None and not isinstance(frame_arr, np.ndarray):  # scripted animation
                frame_arr = self.state.gen_frames.get(it.layer_id)
                if frame_arr is None:
                    continue
            mesh = it.mesh
            topo = (len(mesh.verts), tuple(tuple(t) for t in mesh.tris)) if mesh is not None else (4, ((0, 1, 2), (0, 2, 3)))
            entry = self._comp_nodes.get(it.layer_id)
            if entry is not None and entry[4] != topo:  # triangles added/removed: rebuild the node
                entry[0].removeNode()
                entry = None
            if entry is None:
                entry = (self._comp_node(it.layer_id, topo[0], topo[1]), None, None, (0, 0), topo)
            np_, tex, key, size, _ = entry
            vdata = np_.node().modifyGeom(0).modifyVertexData()
            vw = GeomVertexWriter(vdata, InternalName.getVertex())
            tw = GeomVertexWriter(vdata, InternalName.getTexcoord())
            if mesh is not None:
                ow, oh = self._out_size
                for x, y, u, v in mesh.verts:
                    vw.setData4(float(x / ow * 2 - 1), float(1 - y / oh * 2), 0.0, 1.0)
                    tw.setData2(float(1 - u if it.surface.flip_h else u), float(1 - v if it.surface.flip_v else v))
            else:
                pos = clip_corners(it.corners, self._out_size)
                uv = quad_texcoords(it.surface.flip_h, it.surface.flip_v)
                for row, (u, v) in zip(pos, uv):
                    vw.setData4(float(row[0]), float(row[1]), 0.0, float(row[3]))
                    tw.setData2(float(u), float(v))
            np_.setBin("fixed", order)
            if it.frame is None:
                np_.clearTexture()
                np_.setColor(0, 0, 0, it.opacity)
                tex = None
            else:
                h, w = frame_arr.shape[:2]
                if tex is None or size != (w, h):
                    tex = Texture(it.layer_id)
                    tex.setup2dTexture(w, h, Texture.TUnsignedByte, Texture.FRgb)
                    tex.setMinfilter(Texture.FTLinear)
                    tex.setMagfilter(Texture.FTLinear)
                    key = None
                    np_.setTexture(tex, 1)
                if key != it.frame_key:
                    tex.setRamImageAs(np.ascontiguousarray(frame_arr).tobytes(), "RGB")
                    key = it.frame_key
                np_.setColor(1, 1, 1, 1)
                np_.setColorScale(it.gain, it.gain, it.gain, it.opacity)
                size = (w, h)
            self._comp_nodes[it.layer_id] = (np_, tex, key, size, topo)
        for lid in [k for k in self._comp_nodes if k not in live]:
            self._comp_nodes.pop(lid)[0].removeNode()

    # -- viewpoint -----------------------------------------------------------------------
    def set_view(self, view: str):
        self.view = view
        self._update_hud()

    def _view_camera(self, index: int):
        cams = self.state.project.scene.cameras
        if index < len(cams):
            self.set_view(cams[index].id)

    def _reset_orbit(self):
        self.orbit_target = Point3(0, 0, 0.8)
        self.orbit_dist = 7.0
        self.set_view(VIEW_ORBIT)

    def _zoom(self, factor: float):
        if self.view == VIEW_ORBIT:
            self.orbit_dist = max(0.3, min(60.0, self.orbit_dist * factor))

    def _toggle_grid(self):
        if self.grid_np.isHidden():
            self.grid_np.show()
        else:
            self.grid_np.hide()

    def _handle_mouse(self):
        mw = self.base.mouseWatcherNode
        if not mw.hasMouse():
            self._last_mouse = None
            return
        m = mw.getMouse()
        cur = (m.getX(), m.getY())
        if self._last_mouse is not None and self.view == VIEW_ORBIT:
            dx, dy = cur[0] - self._last_mouse[0], cur[1] - self._last_mouse[1]
            if mw.isButtonDown(MouseButton.one()):
                self.orbit_yaw -= dx * 180
                self.orbit_pitch = max(-89.0, min(89.0, self.orbit_pitch + dy * 120))
            elif mw.isButtonDown(MouseButton.three()) or mw.isButtonDown(MouseButton.two()):
                cam = self.base.camera
                right = cam.getQuat().getRight()
                up = cam.getQuat().getUp()
                self.orbit_target += (-right * dx + -up * dy) * self.orbit_dist * 0.8
        self._last_mouse = cur

    def _place_camera(self):
        cam, lens = self.base.camera, self.base.camLens
        scene = self.state.project.scene
        if self.view == VIEW_PROJECTOR:
            cam.setPos(*scene.projector.pos)
            cam.setHpr(*scene.projector.hpr)
            lens.setFov(scene.projector.fov_h)
            return
        node = scene.node_by_id(self.view) if self.view not in (VIEW_ORBIT, VIEW_PROJECTOR) else None
        if node is not None and node.kind == "camera":
            cam.setPos(*node.pos)
            cam.setHpr(*node.hpr)
            lens.setFov(node.fov_h)
            return
        if self.view != VIEW_ORBIT:
            self.view = VIEW_ORBIT
            self._update_hud()
        yaw, pitch = math.radians(self.orbit_yaw), math.radians(self.orbit_pitch)
        offset = Vec3(math.sin(yaw) * math.cos(pitch), -math.cos(yaw) * math.cos(pitch), -math.sin(pitch))
        cam.setPos(self.orbit_target + offset * self.orbit_dist)
        cam.lookAt(self.orbit_target)
        lens.setFov(55)

    def _update_hud(self):
        scene = self.state.project.scene
        if self.view == VIEW_ORBIT:
            name = "Orbit (drag: rotate, right-drag: pan, wheel: zoom)"
        elif self.view == VIEW_PROJECTOR:
            p = scene.projector
            name = f"Through projector  fov {p.fov_h:.1f}  throw {p.throw_ratio:.2f}"
        else:
            n = scene.node_by_id(self.view)
            name = f"Through camera: {n.name}" if n else "Orbit"
        cams = ", ".join(f"{i + 3}: {c.name}" for i, c in enumerate(scene.cameras[:7]))
        self.hud.setText(f"{name}\n1: orbit   2: projector   {cams}   f: reset   g: grid")

    # -- per-frame --------------------------------------------------------------------------
    # -- depth point cloud -----------------------------------------------------------------
    _cloud_np = None
    _cloud_seq = -1
    _cloud_fmt = None

    _cloud_old = None
    _cloud_old_frames = 0

    def _retire_old_cloud(self):
        if self._cloud_old is not None:
            self._cloud_old_frames += 1
            if self._cloud_old_frames >= 2:  # the new node has rendered at least once
                self._cloud_old.removeNode()
                self._cloud_old = None

    def _sync_cloud(self):
        self._retire_old_cloud()
        pc = self.state.point_cloud
        if pc is None:
            for attr in ("_cloud_np", "_cloud_old"):
                np_ = getattr(self, attr)
                if np_ is not None:
                    np_.removeNode()
                    setattr(self, attr, None)
            self._cloud_seq = -1
            return
        xyz, rgb, node_id, seq = pc
        if seq == self._cloud_seq or self._cloud_old is not None:
            return  # one swap in flight at a time
        self._cloud_seq = seq
        parent = self._cams[node_id][0] if node_id in self._cams else self.proj_np
        n = len(xyz)
        if self._cloud_fmt is None:
            arr = GeomVertexArrayFormat()
            arr.addColumn(InternalName.getVertex(), 3, Geom.NTFloat32, Geom.CPoint)
            arr.addColumn(InternalName.getColor(), 4, Geom.NTFloat32, Geom.CColor)
            self._cloud_fmt = GeomVertexFormat.registerFormat(GeomVertexFormat(arr))
        vdata = GeomVertexData("cloud", self._cloud_fmt, Geom.UHStatic)
        vdata.setNumRows(n)
        # sensor frame (x right, y up, z forward) -> Panda child frame (x right, y forward, z up)
        buf = np.empty((n, 7), np.float32)
        buf[:, 0] = xyz[:, 0]
        buf[:, 1] = xyz[:, 2]
        buf[:, 2] = xyz[:, 1]
        buf[:, 3:6] = rgb.astype(np.float32) / 255.0
        buf[:, 6] = 1.0
        vdata.modifyArrayHandle(0).setData(buf.tobytes())
        prim = GeomPoints(Geom.UHStatic)
        prim.addConsecutiveVertices(0, n)
        prim.closePrimitive()
        geom = Geom(vdata)
        geom.addPrimitive(prim)
        node = GeomNode("cloud")
        node.addGeom(geom)
        node.setBounds(OmniBoundingVolume())  # never culled while bounds are (re)computed
        node.setFinal(True)
        np_ = parent.attachNewNode(node)
        np_.setRenderModeThickness(3)
        np_.setLightOff()
        np_.setTextureOff()
        # overlap: the previous cloud stays on screen until the new one has been drawn once
        self._cloud_old = self._cloud_np
        self._cloud_np = np_
        self._cloud_old_frames = 0

    # -- tracked skeletons ----------------------------------------------------------------
    _skel_np = None
    _skel_seq = -1

    def _sync_skeleton(self):
        sk = self.state.skeletons
        if sk is None:
            if self._skel_np is not None:
                self._skel_np.removeNode()
                self._skel_np = None
            self._skel_seq = -1
            return
        joints_list, node_id, seq = sk
        if seq == self._skel_seq:
            return
        self._skel_seq = seq
        from promap.camera.pose import BONES

        parent = self._cams[node_id][0] if node_id in self._cams else self.proj_np
        segs = LineSegs("skeleton")
        segs.setThickness(4)
        segs.setColor(1.0, 0.8, 0.1, 1.0)
        for joints in joints_list:
            for a, b in BONES:
                pa, pb = joints[a], joints[b]
                if np.isfinite(pa).all() and np.isfinite(pb).all():
                    segs.moveTo(float(pa[0]), float(pa[2]), float(pa[1]))  # sensor -> Panda child frame
                    segs.drawTo(float(pb[0]), float(pb[2]), float(pb[1]))
        new = parent.attachNewNode(segs.create())
        new.setLightOff()
        old_np = self._skel_np
        self._skel_np = new
        if old_np is not None:
            old_np.removeNode()

    def step(self):
        if not self.alive:
            return
        try:
            self._ensure_buffer()
            self._sync_projector()
            self._sync_props()
            self._sync_cloud()
            self._sync_skeleton()
            self._sync_composite()
            self._handle_mouse()
            self._place_camera()
            self.base.taskMgr.step()
        except Exception:
            log.exception("3D view error; closing it")
            self.close()

    def resize(self, w: int, h: int):
        """Embedded only: follow the Qt container's size."""
        if not self.alive or w < 8 or h < 8:
            return
        from panda3d.core import WindowProperties

        wp = WindowProperties()
        wp.setOrigin(0, 0)
        wp.setSize(int(w), int(h))
        self.base.win.requestProperties(wp)

    def key(self, ch: str):
        """Keyboard forwarded from the Qt container (an embedded child window rarely gets X focus)."""
        if self.alive:
            self.base.messenger.send(ch)

    def screenshot(self, path: str | Path):
        self.base.graphicsEngine.renderFrame()
        self.base.win.saveScreenshot(Filename.fromOsSpecific(str(path)))

    def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.base.destroy()
        except Exception:
            log.exception("error closing 3D view")
