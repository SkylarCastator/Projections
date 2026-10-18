"""Draws scripted animations (promap.media.generative) into textures on the GPU.

One program per effect (compiled on first use), one framebuffer per layer at the effect's
size. `texture_for(layer_id, genframe)` renders the frame and returns the texture the
main renderer samples. When `readback` is on (the 3D view is open), the rendered pixels
are also copied back to numpy so Panda can upload them as a plain image.
"""
from __future__ import annotations

import logging

import moderngl
import numpy as np

from promap.media.generative import GenFrame, shader_source

log = logging.getLogger(__name__)

QUAD_VERT = """
#version 330
in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = vec2(in_pos.x * 0.5 + 0.5, 0.5 - in_pos.y * 0.5);  // v = 0 at the top row, like uploaded images
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""


class GenRenderer:
    def __init__(self, ctx: moderngl.Context):
        self.ctx = ctx
        self.programs: dict[str, moderngl.Program | None] = {}
        self.errors: dict[str, str] = {}
        self.vbo = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4").tobytes())
        self.vaos: dict[str, moderngl.VertexArray] = {}
        self.fbos: dict[str, tuple[moderngl.Framebuffer, moderngl.Texture, tuple[int, int]]] = {}
        self.readback = False
        self.readbacks: dict[str, np.ndarray] = {}

    def _program(self, name: str):
        if name in self.programs:
            return self.programs[name]
        try:
            prog = self.ctx.program(vertex_shader=QUAD_VERT, fragment_shader=shader_source(name))
        except Exception as exc:
            log.error("animation %s failed to compile: %s", name, exc)
            self.errors[name] = str(exc)
            prog = None
        self.programs[name] = prog
        if prog is not None:
            self.vaos[name] = self.ctx.vertex_array(prog, [(self.vbo, "2f", "in_pos")])
        return prog

    def _fbo(self, layer_id: str, size: tuple[int, int]):
        entry = self.fbos.get(layer_id)
        if entry is not None and entry[2] == size:
            return entry[0], entry[1]
        if entry is not None:
            entry[0].release()
            entry[1].release()
        tex = self.ctx.texture(size, 3)
        tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        tex.repeat_x = tex.repeat_y = False
        fbo = self.ctx.framebuffer(color_attachments=[tex])
        self.fbos[layer_id] = (fbo, tex, size)
        return fbo, tex

    def texture_for(self, layer_id: str, frame: GenFrame) -> moderngl.Texture | None:
        prog = self._program(frame.effect)
        if prog is None:
            return None
        fbo, tex = self._fbo(layer_id, frame.size)
        prev_fbo = self.ctx.fbo
        prev_viewport = self.ctx.viewport
        fbo.use()
        self.ctx.viewport = (0, 0, frame.size[0], frame.size[1])
        self.ctx.disable(moderngl.BLEND)
        prog["u_res"].value = (float(frame.size[0]), float(frame.size[1]))
        for k, v in frame.uniforms().items():
            if k in prog:
                try:
                    prog[k].value = v
                except Exception:  # pragma: no cover - a uniform the driver optimised away
                    pass
        self.vaos[frame.effect].render(moderngl.TRIANGLE_STRIP)
        if self.readback:
            data = np.frombuffer(fbo.read(components=3), dtype=np.uint8).reshape(frame.size[1], frame.size[0], 3)
            self.readbacks[layer_id] = np.ascontiguousarray(data[::-1])
        try:
            if prev_fbo is not None:
                prev_fbo.use()
                self.ctx.viewport = prev_viewport
        except Exception:  # the previous framebuffer was released (standalone contexts, tests)
            pass
        self.ctx.enable(moderngl.BLEND)
        return tex

    def gc(self, live_ids: set[str]):
        for lid in [k for k in self.fbos if k not in live_ids]:
            fbo, tex, _ = self.fbos.pop(lid)
            fbo.release()
            tex.release()
            self.readbacks.pop(lid, None)

    def release(self):
        for fbo, tex, _ in self.fbos.values():
            fbo.release()
            tex.release()
        self.fbos.clear()
        for vao in self.vaos.values():
            vao.release()
        for prog in self.programs.values():
            if prog is not None:
                prog.release()
        self.vaos.clear()
        self.programs.clear()
