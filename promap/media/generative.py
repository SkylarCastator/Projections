"""Scripted animations: GPU shaders with editable parameters, used anywhere a media file is.

A generative media is a string `gen:<effect>?speed=1.2&color1=ff00aa&text=BOO`, so it sits
in a surface's `media` and in cues like a file path and survives save/load. The engine
opens it as a `GenerativeSource` whose frames are `GenFrame` descriptors (effect, params,
time, size) rather than pixel arrays; the renderer draws the effect into a texture on the
GPU each frame (promap.render.gen). Edit a parameter and the next frame reflects it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode

import numpy as np

from promap.media.gen_shaders import BODIES, program_source
from promap.media.sources import FrameSource

PREFIX = "gen:"
LOOP_SECONDS = 3600.0  # the effects are continuous; time just keeps running


@dataclass
class Param:
    key: str
    label: str
    kind: str = "float"  # float | int | color | text | bool
    default: object = 1.0
    lo: float = 0.0
    hi: float = 1.0
    step: float = 0.01
    help: str = ""


def _hex(rgb) -> str:
    return "".join(f"{int(round(max(0.0, min(1.0, c)) * 255)):02x}" for c in rgb)


def parse_color(s) -> tuple[float, float, float]:
    if isinstance(s, (tuple, list)):
        return tuple(float(c) for c in s[:3])
    s = str(s).lstrip("#")
    if len(s) == 6:
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return 1.0, 1.0, 1.0


COMMON = [
    Param("speed", "Speed", "float", 1.0, 0.0, 5.0, 0.05, "time multiplier"),
    Param("scale", "Scale", "float", 1.0, 0.2, 5.0, 0.05, "zoom of the pattern"),
    Param("color1", "Colour 1", "color", "ff5a00"),
    Param("color2", "Colour 2", "color", "2a0a4a"),
    Param("color3", "Colour 3", "color", "f0e0ff"),
    Param("seed", "Seed", "float", 0.0, 0.0, 10.0, 1.0, "another variation"),
]


@dataclass
class Effect:
    name: str
    title: str
    group: str
    extra: list[Param] = field(default_factory=list)  # p1..p3 and text
    blurb: str = ""
    size: tuple[int, int] = (512, 512)
    defaults: dict = field(default_factory=dict)  # overrides for COMMON defaults

    @property
    def params(self) -> list[Param]:
        out = []
        for p in COMMON:
            if p.key in self.defaults:
                q = Param(**{**p.__dict__, "default": self.defaults[p.key]})
                out.append(q)
            else:
                out.append(p)
        return out + self.extra

    def param(self, key: str) -> Param | None:
        return next((p for p in self.params if p.key == key), None)

    def normalize(self, params: dict) -> dict:
        """Full parameter dict with defaults filled in and values typed."""
        out = {}
        for p in self.params:
            v = params.get(p.key, p.default)
            if p.kind == "color":
                out[p.key] = _hex(parse_color(v))
            elif p.kind == "text":
                out[p.key] = str(v)[:16].upper()
            elif p.kind == "bool":
                out[p.key] = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
            elif p.kind == "int":
                out[p.key] = int(float(v))
            else:
                out[p.key] = float(v)
        return out


def P(key, label, default, lo=0.0, hi=1.0, step=0.01, help=""):
    return Param(key, label, "float", default, lo, hi, step, help)


EFFECTS: dict[str, Effect] = {}
for e in [
    Effect("liquid_morph", "Liquid morph", "organic", [P("p1", "Sheen", 0.6, help="specular highlight strength")],
           "Domain-warped noise that flows like paint on water.", defaults={"color1": "0b2a6f", "color2": "ff2d75", "color3": "ffe66d"}),
    Effect("ascii_art", "ASCII art", "retro", [], "A noise field rendered as a grid of characters from a density ramp.",
           defaults={"color1": "33ff66", "color2": "ccffcc", "color3": "000000"}),
    Effect("holographic_foil", "Holographic foil", "material", [P("p1", "Sparkle", 0.5)],
           "Iridescent rainbow sheen with glints, like a foil sticker turning in light.", defaults={"color1": "ffffff"}),
    Effect("bauhaus_shapes", "Bauhaus shapes", "geometric", [P("p1", "Shuffle", 0.5, help="how often tiles change")],
           "A grid of circles, half-circles, squares and triangles in primaries.",
           defaults={"color1": "d7263d", "color2": "1b4fd8", "color3": "f4c20d"}),
    Effect("neon_glow", "Neon glow", "light", [P("p1", "Glow width", 0.5), P("p2", "Flicker", 0.3)],
           "Glowing tubes bending through the frame on a dark background.",
           defaults={"color1": "ff2bd6", "color2": "2bd6ff", "color3": "ffe66d"}),
    Effect("dithering", "Dithering", "retro", [P("p1", "Pixel size", 0.4)],
           "A moving gradient reduced to two colours with an ordered Bayer dither.",
           defaults={"color1": "f0f0e0", "color2": "101020"}),
    Effect("clay_3d", "Clay 3D", "3d", [P("p1", "Blend", 0.5, help="how much the blobs melt together")],
           "Ray-marched soft blobs with matte, clay-like shading.",
           defaults={"color1": "f28c6a", "color2": "7ad7c4", "color3": "e9e4d4"}),
    Effect("halftone_dots", "Halftone dots", "print", [P("p1", "Dot size", 0.5), P("p2", "Screen angle", 0.3, 0.0, 1.57, 0.01)],
           "A rotated dot screen whose dot size follows a moving image.",
           defaults={"color1": "111111", "color2": "fff4e0"}),
    Effect("kinetic_typography", "Kinetic typography", "text",
           [Param("text", "Text", "text", "BOO"), P("p1", "Scroll", 1.0, 0.0, 1.0, 1.0, "1 = scroll across, 0 = centred"), P("p2", "Bounce", 0.6)],
           "Letters that bounce, tilt and scroll.", defaults={"color1": "ffffff", "color2": "ff8a00", "color3": "000000"}),
    Effect("pixel_art", "Pixel art", "retro", [P("p1", "Pixel size", 0.4), P("p2", "Palette steps", 0.5)],
           "Chunky pixels and a short palette over a slow plasma.",
           defaults={"color1": "2b1055", "color2": "ff6b6b", "color3": "ffe66d"}),
    Effect("particle_swarm", "Particle swarm", "particles", [P("p1", "Count", 0.5), P("p2", "Size", 0.4)],
           "Glowing particles drifting on a noise flow.", defaults={"color1": "ffd166", "color2": "06d6a0", "color3": "073b4c"}),
    Effect("risograph_print", "Risograph print", "print", [P("p1", "Grain", 0.6), P("p2", "Misregistration", 0.5)],
           "Overprinted shapes in two or three inks with grain and slight misalignment.",
           defaults={"color1": "ff5e5b", "color2": "0077b6", "color3": "ffd166"}),
    Effect("glitch_effect", "Glitch", "digital", [P("p1", "Block rows", 0.5), P("p2", "Intensity", 0.6)],
           "Row displacement, RGB split, scanlines and dropouts.", defaults={"color1": "00f5d4", "color2": "f15bb5", "color3": "ffffff"}),
    Effect("topographic_lines", "Topographic lines", "lines", [P("p1", "Density", 0.5)],
           "Contour lines of a slowly shifting height field.", defaults={"color1": "f8f4e3", "color2": "a8c686", "color3": "1f3b2d"}),
    Effect("glassmorphism", "Glassmorphism", "material", [P("p1", "Frost", 0.6)],
           "Frosted glass cards drifting over a colour wash.", defaults={"color1": "7b2cbf", "color2": "3a86ff", "color3": "ffffff"}),
    Effect("paper_cutout", "Paper cutout", "print", [P("p1", "Layers", 0.5)],
           "Layered paper shapes with drop shadows.", defaults={"color1": "ff9f1c", "color2": "ffbf69", "color3": "2ec4b6"}),
    Effect("vaporwave_grid", "Vaporwave grid", "retro", [P("p1", "Sun glow", 0.6)],
           "A striped sun over a scrolling perspective grid.", defaults={"color1": "ff6ec7", "color2": "2d00f7", "color3": "00f5ff"}),
    Effect("flow_fields", "Flow fields", "lines", [P("p1", "Line density", 0.5), P("p2", "Particles", 0.3)],
           "Streamlines of a curl-noise flow, phase moving along the flow.", defaults={"color1": "f72585", "color2": "4cc9f0", "color3": "0d0d0d"}),
    Effect("metaballs", "Metaballs", "organic", [P("p1", "Count", 0.5), P("p2", "Rim light", 0.6)],
           "Blobs that merge and split.", defaults={"color1": "06d6a0", "color2": "ffd166", "color3": "118ab2"}),
    Effect("raymarched_fractals", "Ray-marched fractal", "3d", [P("p1", "Fold", 0.5), P("p2", "Twist", 0.0, 0.0, 1.5, 0.01)],
           "A kaleidoscopic IFS fractal, orbiting.", defaults={"color1": "ffffff", "color2": "ff9e00", "color3": "1a0b2e"}),
]:
    assert e.name in BODIES, e.name
    EFFECTS[e.name] = e


# -- media strings ------------------------------------------------------------------
def is_generative(media) -> bool:
    return isinstance(media, str) and media.startswith(PREFIX)


def parse_media(media: str) -> tuple[Effect, dict]:
    body = media[len(PREFIX):]
    name, _, query = body.partition("?")
    eff = EFFECTS.get(name)
    if eff is None:
        raise ValueError(f"unknown animation {name!r}")
    raw = dict(parse_qsl(query, keep_blank_values=True))
    return eff, eff.normalize(raw)


def make_media(name: str, params: dict | None = None) -> str:
    eff = EFFECTS[name]
    full = eff.normalize(params or {})
    # keep only values that differ from the defaults, so strings stay short
    defaults = eff.normalize({})
    diff = {k: v for k, v in full.items() if v != defaults[k]}
    if not diff:
        return PREFIX + name
    return PREFIX + name + "?" + urlencode({k: (v if not isinstance(v, bool) else int(v)) for k, v in diff.items()})


def media_label(media: str) -> str:
    """Short human name for lists: the effect title (+ text for typography)."""
    try:
        eff, params = parse_media(media)
    except ValueError:
        return media
    txt = params.get("text")
    return f"{eff.title}{' ' + repr(txt) if txt else ''}"


# -- frames / source ------------------------------------------------------------------
@dataclass
class GenFrame:
    """What the renderer needs to draw one frame of an effect; stands in for a pixel array."""
    effect: str
    params: dict
    t: float
    size: tuple[int, int]
    shape: tuple = field(init=False)
    dtype = np.uint8

    def __post_init__(self):
        self.shape = (self.size[1], self.size[0], 3)

    def uniforms(self) -> dict:
        p = self.params
        text = [glyph_code(c) for c in str(p.get("text", ""))][:16]
        return {
            "u_time": float(self.t), "u_speed": float(p.get("speed", 1.0)), "u_scale": float(p.get("scale", 1.0)),
            "u_color1": parse_color(p.get("color1", "ffffff")), "u_color2": parse_color(p.get("color2", "000000")),
            "u_color3": parse_color(p.get("color3", "808080")), "u_seed": float(p.get("seed", 0.0)),
            "u_p1": float(p.get("p1", 0.5)), "u_p2": float(p.get("p2", 0.5)), "u_p3": float(p.get("p3", 0.5)),
            "u_text": text + [36] * (16 - len(text)), "u_textlen": len(text),
        }


def glyph_code(ch: str) -> int:
    ch = ch.upper()
    if "A" <= ch <= "Z":
        return ord(ch) - 65
    if "0" <= ch <= "9":
        return 26 + ord(ch) - 48
    return 36  # space / unknown


class GenerativeSource(FrameSource):
    def __init__(self, media: str):
        self.media = media
        self.effect, self.params = parse_media(media)
        self.size = self.effect.size
        self.duration = LOOP_SECONDS

    def set_params(self, params: dict):
        self.params = self.effect.normalize({**self.params, **params})
        self.media = make_media(self.effect.name, self.params)

    def frame_at(self, t: float):
        # the key changes every call: the renderer redraws each frame anyway (it is cheap)
        return GenFrame(self.effect.name, self.params, float(t), self.size), ("gen", self.effect.name, round(t, 4))


def shader_source(name: str) -> str:
    return program_source(name)
