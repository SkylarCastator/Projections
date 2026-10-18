"""moderngl renderer: draws RenderItems as perspective-warped textured quads.

Coordinates: items carry corners in output-pixel space (origin top-left,
`out_size` wide/high). The renderer maps that space into a letterboxed viewport
so the editor preview and the projector output share one code path.
"""
from __future__ import annotations

import numpy as np
import moderngl

from promap.model.geometry import clip_corners, quad_texcoords
from promap.media.generative import GenFrame
from promap.render.gen import GenRenderer
from promap.show.engine import RenderItem

VERT = """
#version 330
in vec4 in_pos;   // (x*w, y*w, 0, w) already in NDC: the GPU's perspective divide does the warp
in vec2 in_uv;
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = in_pos;
}
"""

FRAG = """
#version 330
uniform sampler2D tex;
uniform float opacity;
uniform float gain;
uniform bool use_tex;
uniform vec3 color;
uniform vec3 tint;
uniform float key_lo;   // luma key: alpha 0 below key_lo, 1 above key_hi; key_hi <= 0 disables
uniform float key_hi;
in vec2 v_uv;
out vec4 frag;
void main() {
    float a = opacity;
    vec3 c;
    if (use_tex) {
        vec3 t = texture(tex, v_uv).rgb;
        if (key_hi > 0.0) {
            float luma = dot(t, vec3(0.299, 0.587, 0.114));
            a *= smoothstep(key_lo, key_hi, luma);
        }
        c = t * gain * tint;
    } else {
        c = color;
    }
    frag = vec4(c * a, a);   // premultiplied alpha
}
"""

LINE_VERT = """
#version 330
in vec2 in_pos;
void main() { gl_Position = vec4(in_pos, 0.0, 1.0); }
"""
LINE_FRAG = """
#version 330
uniform vec4 color;
out vec4 frag;
void main() { frag = color; }
"""


def letterbox(out_w: int, out_h: int, vp_w: int, vp_h: int) -> tuple[int, int, int, int]:
    """Largest rect of aspect out_w:out_h centred inside vp_w x vp_h -> (x, y, w, h)."""
    if out_w <= 0 or out_h <= 0 or vp_w <= 0 or vp_h <= 0:
        return 0, 0, max(1, vp_w), max(1, vp_h)
    scale = min(vp_w / out_w, vp_h / out_h)
    w, h = int(round(out_w * scale)), int(round(out_h * scale))
    return (vp_w - w) // 2, (vp_h - h) // 2, w, h


class Renderer:
    def __init__(self, ctx: moderngl.Context):
        self.ctx = ctx
        self.prog = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
        self.line_prog = ctx.program(vertex_shader=LINE_VERT, fragment_shader=LINE_FRAG)
        self.vbo = ctx.buffer(reserve=4 * 6 * 4, dynamic=True)
        self.ibo = ctx.buffer(np.array([0, 1, 2, 0, 2, 3], dtype="i4").tobytes())
        self.vao = ctx.vertex_array(self.prog, [(self.vbo, "4f 2f", "in_pos", "in_uv")], self.ibo)
        # triangle meshes: one growable vertex + index buffer rewritten per draw
        self.mesh_cap = 1024  # vertices (and indices) the buffers currently hold
        self.mesh_vbo = ctx.buffer(reserve=self.mesh_cap * 6 * 4, dynamic=True)
        self.mesh_ibo = ctx.buffer(reserve=self.mesh_cap * 3 * 4, dynamic=True)
        self.mesh_vao = ctx.vertex_array(self.prog, [(self.mesh_vbo, "4f 2f", "in_pos", "in_uv")], self.mesh_ibo)
        self.line_vbo = ctx.buffer(reserve=64 * 2 * 4, dynamic=True)
        self.line_vao = ctx.vertex_array(self.line_prog, [(self.line_vbo, "2f", "in_pos")])
        self.textures: dict[str, tuple[moderngl.Texture, object, tuple[int, int]]] = {}
        self.gen = GenRenderer(ctx)  # scripted animations drawn into textures

    # -- textures ------------------------------------------------------------
    def _texture_for(self, layer_id: str, frame, key: object):
        if isinstance(frame, GenFrame):
            return self.gen.texture_for(layer_id, frame)
        h, w = frame.shape[:2]
        entry = self.textures.get(layer_id)
        if entry is not None and entry[2] == (w, h):
            tex, old_key, _ = entry
            if old_key != key:
                tex.write(frame)
                self.textures[layer_id] = (tex, key, (w, h))
            return tex
        if entry is not None:
            entry[0].release()
        tex = self.ctx.texture((w, h), 3, frame, alignment=1)
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = tex.repeat_y = False
        self.textures[layer_id] = (tex, key, (w, h))
        return tex

    def _gc(self, live_ids: set[str]):
        for lid in [k for k in self.textures if k not in live_ids]:
            self.textures.pop(lid)[0].release()

    # -- drawing -------------------------------------------------------------
    def _draw_quad(self, corners, out_size, flip_h=False, flip_v=False):
        pos = clip_corners(corners, out_size)
        data = np.hstack([pos.astype("f4"), quad_texcoords(flip_h, flip_v)]).astype("f4")
        self.vbo.write(data.tobytes())
        self.vao.render(moderngl.TRIANGLES)

    def _draw_mesh(self, mesh, out_size, flip_h=False, flip_v=False):
        """Affine-textured triangles: positions go in as plain NDC (w = 1)."""
        if not mesh.tris:
            return
        out_w, out_h = out_size
        v = np.asarray(mesh.verts, dtype="f4").reshape(-1, 4)
        data = np.empty((len(v), 6), dtype="f4")
        data[:, 0] = v[:, 0] / out_w * 2.0 - 1.0
        data[:, 1] = 1.0 - v[:, 1] / out_h * 2.0
        data[:, 2] = 0.0
        data[:, 3] = 1.0
        data[:, 4] = 1.0 - v[:, 2] if flip_h else v[:, 2]
        data[:, 5] = 1.0 - v[:, 3] if flip_v else v[:, 3]
        idx = np.asarray(mesh.tris, dtype="i4").reshape(-1)
        need = max(len(v), len(idx) // 3 + 1)
        if need > self.mesh_cap:
            self.mesh_cap = max(need, self.mesh_cap * 2)
            self.mesh_vbo.orphan(self.mesh_cap * 6 * 4)
            self.mesh_ibo.orphan(self.mesh_cap * 3 * 4)
        self.mesh_vbo.write(data.tobytes())
        self.mesh_ibo.write(idx.tobytes())
        self.mesh_vao.render(moderngl.TRIANGLES, vertices=len(idx))

    def render(
        self,
        fbo: moderngl.Framebuffer,
        out_size: tuple[int, int],
        items: list[RenderItem],
        viewport: tuple[int, int, int, int],
        clear_color=(0.0, 0.0, 0.0),
        outlines: list[tuple[list, tuple[float, float, float, float]]] | None = None,
    ):
        """Draw `items` (first drawn first) into `fbo` within `viewport` (x, y, w, h, GL pixels)."""
        ctx = self.ctx
        fbo.use()
        ctx.viewport = (0, 0, fbo.width, fbo.height)
        ctx.clear(*clear_color)
        ctx.viewport = viewport
        ctx.enable(moderngl.BLEND)
        ctx.blend_func = moderngl.ONE, moderngl.ONE_MINUS_SRC_ALPHA
        ctx.disable(moderngl.DEPTH_TEST)
        ctx.disable(moderngl.CULL_FACE)

        live: set[str] = set()
        for it in items:
            self.prog["opacity"].value = float(it.opacity)
            self.prog["gain"].value = float(it.gain)
            t = getattr(it.surface, "tint", None) or [1.0, 1.0, 1.0]
            self.prog["tint"].value = (float(t[0]), float(t[1]), float(t[2]))
            if getattr(it.surface, "key_black", False):
                thr = float(getattr(it.surface, "key_threshold", 0.12))
                self.prog["key_lo"].value = max(0.0, thr - 0.04)
                self.prog["key_hi"].value = max(0.001, thr + 0.04)
            else:
                self.prog["key_hi"].value = 0.0
                self.prog["key_lo"].value = 0.0
            if it.frame is None:
                self.prog["use_tex"].value = False
                self.prog["color"].value = (0.0, 0.0, 0.0)
            else:
                live.add(it.layer_id)
                tex = self._texture_for(it.layer_id, it.frame, it.frame_key)
                if tex is None:  # an animation that failed to compile: draw nothing for it
                    continue
                tex.use(0)
                self.prog["tex"].value = 0
                self.prog["use_tex"].value = True
            if it.mesh is not None:
                self._draw_mesh(it.mesh, out_size, it.surface.flip_h, it.surface.flip_v)
            else:
                self._draw_quad(it.corners, out_size, it.surface.flip_h, it.surface.flip_v)
        self._gc(live)
        self.gen.gc(live)

        if outlines:
            out_w, out_h = out_size
            for corners, color in outlines:
                if len(corners) > 64:
                    corners = corners[:64]
                pts = np.array(
                    [[c[0] / out_w * 2 - 1, 1 - c[1] / out_h * 2] for c in corners], dtype="f4"
                )
                self.line_vbo.write(pts.tobytes())
                self.line_prog["color"].value = color
                self.line_vao.render(moderngl.LINE_LOOP, vertices=len(corners))

    def release(self):
        for tex, _, _ in self.textures.values():
            tex.release()
        self.textures.clear()
        self.gen.release()
