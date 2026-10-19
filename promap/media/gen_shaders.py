"""GLSL fragment shaders for the built-in scripted animations.

Every effect is a function `vec3 effect(vec2 uv, vec2 p, float t)` appended to the common
header below: `uv` in 0..1 (v=0 at the top row of the output), `p` centred and
aspect-corrected (x in -aspect..aspect, y in -1..1, up), `t` = time * speed. Common
uniforms: colours 1-3, scale, seed, and three free parameters p1..p3 whose meaning each
effect declares in its spec (see generative.EFFECTS). Text effects get up to 16 glyph codes.
"""
from __future__ import annotations

HEADER = """
#version 330
uniform vec2 u_res;
uniform float u_time;
uniform float u_speed;
uniform float u_scale;
uniform vec3 u_color1;
uniform vec3 u_color2;
uniform vec3 u_color3;
uniform float u_seed;
uniform float u_p1;
uniform float u_p2;
uniform float u_p3;
uniform int u_text[16];
uniform int u_textlen;
in vec2 v_uv;
out vec4 frag;

#define PI 3.14159265
#define TAU 6.28318531

float hash11(float n) { return fract(sin(n + u_seed * 17.17) * 43758.5453123); }
float hash21(vec2 p) { p = fract(p * vec2(123.34, 456.21)); p += dot(p, p + 45.32 + u_seed); return fract(p.x * p.y); }
vec2 hash22(vec2 p) { float h = hash21(p); return vec2(h, hash21(p + h + 1.7)); }
float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    float a = hash21(i), b = hash21(i + vec2(1, 0)), c = hash21(i + vec2(0, 1)), d = hash21(i + vec2(1, 1));
    return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}
float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    mat2 r = mat2(0.8, 0.6, -0.6, 0.8);
    for (int i = 0; i < 5; i++) { v += a * noise(p); p = r * p * 2.03 + 11.3; a *= 0.5; }
    return v;
}
mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }
vec3 palette(float x) { return mix(mix(u_color1, u_color2, smoothstep(0.0, 0.5, x)), u_color3, smoothstep(0.5, 1.0, x)); }
float sdBox(vec2 p, vec2 b) { vec2 d = abs(p) - b; return length(max(d, 0.0)) + min(max(d.x, d.y), 0.0); }
float sdRoundBox(vec2 p, vec2 b, float r) { return sdBox(p, b - r) - r; }

// 5x7 glyphs, 7 rows of 5 bits, top row first; index: 0-25 A-Z, 26-35 0-9, 36 space, 37-46 density ramp .:-=+*#%@
const int GLYPH[47 * 7] = int[47 * 7](
 14,17,17,31,17,17,17,  30,17,30,17,17,17,30,  14,17,16,16,16,17,14,  30,17,17,17,17,17,30,  31,16,30,16,16,16,31,
 31,16,30,16,16,16,16,  14,17,16,23,17,17,15,  17,17,31,17,17,17,17,  14,4,4,4,4,4,14,      7,2,2,2,2,18,12,
 17,18,20,24,20,18,17,  16,16,16,16,16,16,31,  17,27,21,21,17,17,17,  17,25,21,19,17,17,17,  14,17,17,17,17,17,14,
 30,17,17,30,16,16,16,  14,17,17,17,21,18,13,  30,17,17,30,20,18,17,  15,16,14,1,1,17,14,    31,4,4,4,4,4,4,
 17,17,17,17,17,17,14,  17,17,17,17,17,10,4,   17,17,17,21,21,27,17,  17,10,4,4,10,17,17,    17,17,10,4,4,4,4,
 31,1,2,4,8,16,31,
 14,17,19,21,25,17,14,  4,12,4,4,4,4,14,       14,17,1,6,8,16,31,     31,2,4,2,1,17,14,      2,6,10,18,31,2,2,
 31,16,30,1,1,17,14,    6,8,16,30,17,17,14,    31,1,2,4,8,8,8,        14,17,17,14,17,17,14,  14,17,17,15,1,2,12,
 0,0,0,0,0,0,0,
 0,0,0,0,0,4,0,         0,4,0,0,0,4,0,         0,0,0,14,0,0,0,        0,0,14,0,14,0,0,       0,4,4,31,4,4,0,
 0,21,14,31,14,21,0,    10,31,10,10,31,10,0,   25,26,2,4,8,11,19,     14,21,23,23,16,17,14,  31,31,31,31,31,31,31
);
float glyph(int code, vec2 cell) {  // cell in 0..1 inside the glyph box; 1 where ink
    if (code < 0 || code > 46) return 0.0;
    if (cell.x < 0.0 || cell.x >= 1.0 || cell.y < 0.0 || cell.y >= 1.0) return 0.0;
    int col = int(cell.x * 5.0);
    int row = int(cell.y * 7.0);
    int bits = GLYPH[code * 7 + row];
    return float((bits >> (4 - col)) & 1);
}
"""

MAIN = """
void main() {
    vec2 uv = v_uv;
    float aspect = u_res.x / u_res.y;
    vec2 p = vec2((uv.x - 0.5) * 2.0 * aspect, (0.5 - uv.y) * 2.0);
    float t = u_time * u_speed;
    vec3 c = effect(uv, p, t);
    frag = vec4(clamp(c, 0.0, 1.0), 1.0);
}
"""

BODIES: dict[str, str] = {}

BODIES["liquid_morph"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale;
    vec2 w1 = vec2(fbm(q + vec2(0.0, t * 0.15)), fbm(q + vec2(5.2, 1.3) - t * 0.1));
    vec2 w2 = vec2(fbm(q + 4.0 * w1 + vec2(1.7, 9.2) + t * 0.12), fbm(q + 4.0 * w1 + vec2(8.3, 2.8) - t * 0.08));
    float f = fbm(q + 4.0 * w2);
    float sheen = pow(clamp(1.0 - abs(f - 0.5) * 2.0, 0.0, 1.0), 4.0) * u_p1;
    return palette(f) + sheen * 0.6;
}
"""

BODIES["ascii_art"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    float cells = 24.0 * u_scale;
    vec2 g = vec2(cells * u_res.x / u_res.y, cells);
    vec2 cell = floor(uv * g);
    vec2 inner = fract(uv * g);
    vec2 cp = (cell + 0.5) / g;
    vec2 q = vec2((cp.x - 0.5) * 2.0 * u_res.x / u_res.y, (0.5 - cp.y) * 2.0);
    float v = fbm(q * 1.5 + vec2(t * 0.3, 0.0));
    float d = length(q - vec2(sin(t * 0.7) * 0.8, cos(t * 0.5) * 0.6));
    v = clamp(v * 0.7 + smoothstep(1.2, 0.0, d) * 0.6, 0.0, 1.0);
    int code = 36 + int(round(v * 10.0));
    vec2 gc = (inner - vec2(0.1, 0.1)) / vec2(0.8, 0.8);
    float ink = glyph(code, gc);
    vec3 col = mix(u_color1, u_color2, v);
    return mix(u_color3, col, ink);
}
"""

BODIES["holographic_foil"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale;
    float n = fbm(q * 0.8 + t * 0.1);
    float band = sin((q.x * 0.9 + q.y * 1.7) * 3.0 + n * 6.0 + t);
    float hue = fract(n * 1.5 + band * 0.12 + t * 0.05);
    vec3 rainbow = 0.55 + 0.45 * cos(TAU * (hue + vec3(0.0, 0.33, 0.67)));
    float sparkle = pow(hash21(floor(uv * u_res / 3.0) + floor(t * 6.0)), 40.0 * (1.0 - u_p1 * 0.5)) * 2.0;
    float sheen = pow(0.5 + 0.5 * band, 3.0);
    vec3 col = mix(rainbow, u_color1, 0.25) * (0.6 + 0.6 * sheen) + sparkle * u_p1;
    return col;
}
"""

BODIES["alignment_grid"] = """
// Mapping aid, not a look: a square-celled grid with a border, diagonals, a centre circle and a
// filled top-left cell, so keystone, stretch, flips and a mesh's wobble all show at a glance.
vec3 effect(vec2 uv, vec2 p, float t) {
    float cells = max(2.0, floor(u_p1 + 0.5));
    float lw = max(0.004, u_p2 * 0.5);              // line half-width, in cells
    vec2 g = uv * cells;
    vec2 f = abs(fract(g) - 0.5);
    float line = step(0.5 - lw, max(f.x, f.y));    // cell edges
    vec2 e = min(uv, 1.0 - uv) * cells;
    float border = step(min(e.x, e.y), lw * 2.0);
    vec3 col = mix(u_color2, u_color1, max(line, border));
    if (u_p3 > 0.5) {
        float d1 = abs(uv.x - uv.y), d2 = abs(uv.x + uv.y - 1.0);
        float diag = step(min(d1, d2), lw * 1.4 / cells);
        float ring = step(abs(length((uv - 0.5) * vec2(u_res.x / u_res.y, 1.0)) - 0.25), lw * 1.2 / cells);
        float corner = step(max(g.x, g.y), 1.0) * 0.55;  // top-left cell tinted: orientation
        col = mix(col, u_color3, max(max(diag, ring), corner));
    }
    return col;
}
"""

BODIES["bauhaus_shapes"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    float n = 4.0 * u_scale;
    vec2 g = vec2(n * u_res.x / u_res.y, n);
    vec2 cell = floor(uv * g);
    vec2 c = (fract(uv * g) - 0.5) * 2.0;
    float h = hash21(cell + floor(t * 0.5 * u_p1));
    float kind = floor(h * 4.0);
    float ang = floor(hash21(cell * 3.1 + 7.0) * 4.0) * PI * 0.5 + t * (hash21(cell + 2.0) - 0.5) * 0.6;
    c = rot(ang) * c;
    float d;
    if (kind < 1.0) d = length(c) - 0.8;
    else if (kind < 2.0) d = max(length(c) - 0.8, c.y);
    else if (kind < 3.0) d = max(abs(c.x) - 0.75, abs(c.y) - 0.75);
    else d = max(c.y - 0.6, max(-c.y - 0.8, abs(c.x) * 1.6 - (0.8 - c.y)));
    vec3 pal = h < 0.33 ? u_color1 : (h < 0.66 ? u_color2 : u_color3);
    vec3 bg = vec3(0.94, 0.92, 0.86);
    float ink = 1.0 - smoothstep(-0.02, 0.02, d);
    return mix(bg, pal, ink);
}
"""

BODIES["neon_glow"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale;
    float d1 = abs(q.y - 0.5 * sin(q.x * 1.3 + t) * cos(q.x * 0.4 - t * 0.3));
    float d2 = abs(q.y + 0.4 - 0.5 * sin(q.x * 0.9 - t * 1.3));
    float d3 = length(q - vec2(sin(t * 0.8) * 0.8, cos(t * 0.6) * 0.4)) - 0.35;
    float w = 0.02 + 0.03 * u_p1;
    vec3 col = vec3(0.02, 0.02, 0.04);
    col += u_color1 * (w / max(d1, 1e-3)) * 0.35 + vec3(1.0) * smoothstep(0.012, 0.0, d1);
    col += u_color2 * (w / max(d2, 1e-3)) * 0.35 + vec3(1.0) * smoothstep(0.012, 0.0, d2);
    col += u_color3 * (w / max(abs(d3), 1e-3)) * 0.35 + vec3(1.0) * smoothstep(0.012, 0.0, abs(d3));
    float flicker = 0.92 + 0.08 * sin(t * 37.0 + sin(t * 11.0)) * u_p2;
    return col * flicker;
}
"""

BODIES["dithering"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    float px = 2.0 + 6.0 * u_p1;
    vec2 ip = floor(uv * u_res / px);
    vec2 q = (ip * px / u_res - 0.5) * 2.0 * vec2(u_res.x / u_res.y, -1.0) * u_scale;
    float v = fbm(q * 1.2 + vec2(t * 0.25, sin(t * 0.2))) * 0.8 + 0.5 * (0.5 + 0.5 * sin(q.x * 1.5 + t));
    v = clamp(v - 0.25, 0.0, 1.0);
    int x = int(mod(ip.x, 4.0)), y = int(mod(ip.y, 4.0));
    const int bayer[16] = int[16](0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5);
    float thr = (float(bayer[y * 4 + x]) + 0.5) / 16.0;
    return v > thr ? u_color1 : u_color2;
}
"""

BODIES["clay_3d"] = """
float map(vec3 q, float t) {
    float d = 1e9;
    for (int i = 0; i < 5; i++) {
        float fi = float(i);
        vec3 c = vec3(sin(t * 0.7 + fi * 1.9), cos(t * 0.5 + fi * 2.3), sin(t * 0.3 + fi)) * vec3(0.9, 0.6, 0.4);
        float r = 0.35 + 0.15 * sin(fi * 2.0 + t);
        float di = length(q - c) - r;
        float k = 0.35 * u_p1 + 0.05;
        float h = clamp(0.5 + 0.5 * (di - d) / k, 0.0, 1.0);
        d = mix(di, d, h) - k * h * (1.0 - h);
    }
    return d;
}
vec3 effect(vec2 uv, vec2 p, float t) {
    vec3 ro = vec3(0.0, 0.0, -3.2), rd = normalize(vec3(p / u_scale, 1.6));
    float s = 0.0; vec3 q; float d = 1.0;
    for (int i = 0; i < 64; i++) { q = ro + rd * s; d = map(q, t); if (d < 0.002 || s > 8.0) break; s += d * 0.9; }
    vec3 bg = mix(u_color3, u_color3 * 0.6, uv.y);
    if (d > 0.01) return bg;
    vec2 e = vec2(0.003, 0.0);
    vec3 n = normalize(vec3(map(q + e.xyy, t) - map(q - e.xyy, t), map(q + e.yxy, t) - map(q - e.yxy, t), map(q + e.yyx, t) - map(q - e.yyx, t)));
    vec3 l = normalize(vec3(0.5, 0.8, -0.6));
    float dif = clamp(dot(n, l), 0.0, 1.0);
    float wrap = clamp(dot(n, l) * 0.5 + 0.5, 0.0, 1.0);
    float fres = pow(1.0 - clamp(dot(n, -rd), 0.0, 1.0), 3.0);
    vec3 albedo = mix(u_color1, u_color2, 0.5 + 0.5 * n.y);
    vec3 col = albedo * (0.25 + 0.75 * wrap) + vec3(0.15) * dif * dif + fres * 0.15;
    return mix(col, bg, smoothstep(5.0, 8.0, s));
}
"""

BODIES["halftone_dots"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    float freq = 18.0 * u_scale;
    vec2 q = rot(0.3 + u_p2) * p;
    vec2 cell = floor(q * freq), c = fract(q * freq) - 0.5;
    vec2 cp = (cell + 0.5) / freq;
    vec2 src = rot(-0.3 - u_p2) * cp;
    float v = fbm(src * 1.3 + vec2(t * 0.3, 0.0)) * 0.9 + 0.4 * smoothstep(1.4, 0.0, length(src - vec2(sin(t), cos(t * 0.8)) * 0.6));
    float r = clamp(v, 0.0, 1.0) * 0.72 * (0.5 + u_p1);
    float d = length(c) - r;
    float ink = 1.0 - smoothstep(-0.03, 0.03, d);
    return mix(u_color2, u_color1, ink);
}
"""

BODIES["kinetic_typography"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    int n = max(u_textlen, 1);
    float size = 0.34 * u_scale;
    float adv = size * 1.15;
    float total = adv * float(n);
    vec3 col = u_color3;
    for (int k = 0; k < 16; k++) {
        if (k >= n) break;
        float fk = float(k);
        float phase = t * 2.0 + fk * 0.6;
        float scroll = u_p1 > 0.5 ? mod(t * 0.6 + 0.5, 1.0) * (total + 2.0 * u_res.x / u_res.y) - (u_res.x / u_res.y) - total : -total * 0.5;
        vec2 origin = vec2(scroll + adv * fk, sin(phase) * 0.25 * u_p2);
        float wob = 1.0 + 0.3 * u_p2 * sin(phase * 1.3);
        vec2 local = rot(sin(phase) * 0.25 * u_p2) * (p - origin - vec2(size * 0.5, 0.0));
        vec2 cell = vec2(local.x / (size * wob) + 0.5, 0.5 - local.y / (size * 1.4 * wob));
        float ink = glyph(u_text[k], cell);
        vec3 lc = mix(u_color1, u_color2, 0.5 + 0.5 * sin(phase + fk));
        col = mix(col, lc, ink);
    }
    return col;
}
"""

BODIES["pixel_art"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    float px = floor(4.0 + 20.0 * u_p1);
    vec2 ip = floor(uv * u_res / px) * px / u_res;
    vec2 q = (ip - 0.5) * 2.0 * vec2(u_res.x / u_res.y, -1.0) * u_scale;
    float v = 0.5 + 0.5 * sin(q.x * 2.0 + t) * cos(q.y * 1.7 - t * 0.8) + 0.3 * fbm(q * 1.5 + t * 0.2);
    float levels = 4.0 + floor(u_p2 * 4.0);
    v = floor(clamp(v, 0.0, 0.999) * levels) / (levels - 1.0);
    return palette(v);
}
"""

BODIES["particle_swarm"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec3 col = u_color3 * 0.15;
    int n = int(24.0 + 72.0 * u_p1);
    float sz = 0.012 + 0.03 * u_p2;
    for (int i = 0; i < 96; i++) {
        if (i >= n) break;
        float fi = float(i);
        vec2 seed = hash22(vec2(fi, 3.0));
        float ph = t * (0.3 + seed.x * 0.7);
        vec2 c = vec2(sin(ph + seed.y * TAU) * (0.6 + 0.6 * seed.x), cos(ph * 1.3 + seed.x * TAU) * 0.7);
        c += 0.25 * vec2(fbm(c * 2.0 + t * 0.2 + fi), fbm(c * 2.0 - t * 0.2 + fi + 9.0)) - 0.125;
        c *= u_scale;
        float d = length(p - c);
        vec3 pc = mix(u_color1, u_color2, seed.y);
        col += pc * (sz * sz / (d * d + sz * sz * 0.2)) * 0.5;
    }
    return col;
}
"""

BODIES["risograph_print"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale;
    float grain = (hash21(floor(uv * u_res * 0.5) + floor(t * 3.0)) - 0.5) * 0.35 * (0.3 + u_p1);
    float s1 = smoothstep(0.02, -0.02, length(q - vec2(sin(t * 0.4) * 0.5, 0.2)) - 0.7);
    float s2 = smoothstep(0.02, -0.02, sdRoundBox(rot(t * 0.2) * (q + vec2(0.4 + 0.02 * u_p2, -0.3 + 0.02 * u_p2)), vec2(0.6, 0.35), 0.1));
    float s3 = smoothstep(0.02, -0.02, abs(q.y + 0.6 + 0.2 * sin(q.x * 2.0 + t)) - 0.08);
    s1 = clamp(s1 + grain, 0.0, 1.0); s2 = clamp(s2 + grain, 0.0, 1.0);
    vec3 paper = vec3(0.96, 0.94, 0.88);
    vec3 col = paper;
    col = mix(col, col * u_color1, s1 * 0.9);
    col = mix(col, col * u_color2, s2 * 0.9);
    col = mix(col, col * u_color3, s3 * 0.9);
    return col;
}
"""

BODIES["glitch_effect"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    float blocks = 8.0 + 24.0 * u_p1;
    float row = floor(uv.y * blocks);
    float burst = step(1.0 - 0.35 * u_p2, hash11(floor(t * 8.0)));
    float shift = (hash21(vec2(row, floor(t * 12.0))) - 0.5) * 0.3 * burst * step(0.6, hash21(vec2(row, floor(t * 5.0))));
    vec2 q = p * u_scale;
    float stripes = step(0.5, fract((q.x + shift * 4.0) * 3.0 + q.y * 0.5 + t * 0.3));
    vec3 base = mix(u_color1, u_color2, stripes) * (0.6 + 0.4 * fbm(q * 2.0 + t));
    float sep = 0.01 + 0.03 * burst;
    vec3 col = vec3(mix(u_color1, u_color2, step(0.5, fract((q.x + shift * 4.0 + sep) * 3.0 + q.y * 0.5 + t * 0.3))).r, base.g,
                    mix(u_color1, u_color2, step(0.5, fract((q.x + shift * 4.0 - sep) * 3.0 + q.y * 0.5 + t * 0.3))).b);
    float scan = 0.85 + 0.15 * sin(uv.y * u_res.y * 3.0);
    float dropout = step(0.97, hash21(vec2(row, floor(t * 20.0)))) * burst;
    return mix(col * scan, u_color3, dropout);
}
"""

BODIES["topographic_lines"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale * 0.8;
    float h = fbm(q + vec2(t * 0.08, t * 0.05)) + 0.3 * fbm(q * 2.5 - t * 0.1);
    float levels = 8.0 + 16.0 * u_p1;
    float f = fract(h * levels);
    float w = fwidth(h * levels) * 1.2 + 0.02;
    float line = 1.0 - smoothstep(0.0, w, min(f, 1.0 - f));
    float major = step(0.5, fract(floor(h * levels) / 5.0 + 0.1)) * 0.0 + (mod(floor(h * levels), 5.0) < 0.5 ? 1.0 : 0.6);
    vec3 fill = mix(u_color3, u_color3 * 0.7, h);
    return mix(fill, mix(u_color2, u_color1, major), line * major);
}
"""

BODIES["glassmorphism"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale;
    vec3 bg = mix(u_color1, u_color2, 0.5 + 0.5 * sin(q.x * 0.8 + t * 0.3) * cos(q.y * 0.9 - t * 0.2));
    bg += 0.15 * fbm(q * 1.5 + t * 0.1);
    vec3 col = bg;
    for (int i = 0; i < 4; i++) {
        float fi = float(i);
        vec2 c = vec2(sin(t * 0.3 + fi * 1.7) * 0.7, cos(t * 0.25 + fi * 2.1) * 0.4);
        vec2 sz = vec2(0.45 + 0.15 * sin(fi), 0.28 + 0.1 * cos(fi * 2.0));
        float d = sdRoundBox(rot(0.15 * sin(t * 0.2 + fi)) * (q - c), sz, 0.12);
        float inside = 1.0 - smoothstep(0.0, 0.01, d);
        vec3 blurred = mix(u_color1, u_color2, 0.5 + 0.5 * sin((q.x + fi) * 0.5 + t * 0.3)) * 0.9 + 0.1;
        vec3 glass = mix(col, blurred + u_color3 * 0.2, 0.55 * u_p1 + 0.2);
        float border = smoothstep(0.015, 0.0, abs(d)) * 0.8;
        float highlight = smoothstep(0.3, 0.0, length(q - c + sz * vec2(0.5, -0.5))) * 0.25;
        col = mix(col, glass + border + highlight, inside);
    }
    return col;
}
"""

BODIES["paper_cutout"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale * 0.7;
    vec3 col = u_color3 * 0.55;
    float layers = 3.0 + floor(u_p1 * 5.0);
    vec2 sh = vec2(0.025, -0.035);
    for (int i = 0; i < 8; i++) {
        float fi = float(i);
        if (fi >= layers) break;
        float k = (fi + 1.0) / layers;
        vec2 off = vec2(fi * 3.1, fi * 1.7 + t * 0.06 * (1.0 + k));
        float thr = 0.42 + 0.1 * k;
        float h = fbm(q * (0.9 + 0.5 * k) + off) + 0.12 * sin(q.x * 1.5 + t * 0.3 + fi);
        float hs = fbm((q - sh) * (0.9 + 0.5 * k) + off) + 0.12 * sin((q.x - sh.x) * 1.5 + t * 0.3 + fi);
        float cut = smoothstep(thr - 0.006, thr + 0.006, h);
        float shadow = smoothstep(thr - 0.006, thr + 0.006, hs);
        col = mix(col, col * 0.45, shadow * (1.0 - cut) * 0.9);
        vec3 lc = mix(u_color1, u_color2, k);
        col = mix(col, lc, cut);
    }
    return col;
}
"""

BODIES["vaporwave_grid"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec3 sky = mix(u_color1, u_color2, smoothstep(-0.1, 1.0, p.y));
    float sun = length(p - vec2(0.0, 0.3)) - 0.38;
    float stripes = step(0.55, fract((p.y - 0.3) * 10.0 + t * 0.37)) * step(p.y, 0.3);
    vec3 sunc = mix(u_color3, u_color1, smoothstep(0.3, -0.05, p.y));
    vec3 col = mix(sky, sunc, (1.0 - smoothstep(0.0, 0.008, sun)) * (1.0 - stripes));
    float horizon = 0.0;
    if (p.y < horizon) {
        float z = 1.0 / max(horizon - p.y, 1e-3);
        vec2 g = vec2(p.x * z * 0.5 * u_scale, z * 0.4 * u_scale + t * 1.1);
        float w = 0.04 + 0.02 * clamp(z * 0.1, 0.0, 1.0);
        float lx = smoothstep(w, 0.0, 0.5 - abs(fract(g.x) - 0.5));
        float ly = smoothstep(w, 0.0, 0.5 - abs(fract(g.y) - 0.5));
        float line = clamp(lx + ly, 0.0, 1.0);
        vec3 floorc = mix(u_color2 * 0.12, u_color3, line);
        float fade = smoothstep(0.0, -0.08, p.y);
        col = mix(sky * 0.6, floorc, fade);
    }
    float glow = (1.0 - smoothstep(0.0, 0.5, abs(sun))) * 0.35 * u_p1 * step(horizon, p.y);
    return col + u_color3 * glow;
}
"""

BODIES["flow_fields"] = """
vec2 field(vec2 q, float t) {
    float e = 0.01;
    float n1 = fbm(q + vec2(0.0, e) + t * 0.05), n2 = fbm(q - vec2(0.0, e) + t * 0.05);
    float n3 = fbm(q + vec2(e, 0.0) + t * 0.05), n4 = fbm(q - vec2(e, 0.0) + t * 0.05);
    return vec2(n1 - n2, -(n3 - n4)) / (2.0 * e);  // curl of the noise: divergence-free flow
}
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale * 0.6;
    vec2 v = field(q, t);
    float ang = atan(v.y, v.x);
    float speed = length(v);
    // streamline stripes: phase advances along the flow direction
    vec2 dir = normalize(v + 1e-5);
    float along = dot(q, dir) * (8.0 + 12.0 * u_p1) - t * 2.0;
    float across = dot(q, vec2(-dir.y, dir.x)) * 14.0;
    float stripe = smoothstep(0.35, 0.65, 0.5 + 0.5 * sin(along + sin(across)));
    vec3 col = palette(0.5 + 0.5 * sin(ang));
    float dots = smoothstep(0.9, 1.0, noise(q * 10.0 + dir * t * 3.0)) * u_p2;
    return mix(u_color3 * 0.2, col, stripe * (0.5 + 0.5 * speed * 2.0)) + dots;
}
"""

BODIES["metaballs"] = """
vec3 effect(vec2 uv, vec2 p, float t) {
    vec2 q = p * u_scale;
    float f = 0.0;
    int n = int(3.0 + 6.0 * u_p1);
    for (int i = 0; i < 9; i++) {
        if (i >= n) break;
        float fi = float(i);
        vec2 c = vec2(sin(t * (0.4 + 0.1 * fi) + fi * 1.9), cos(t * (0.3 + 0.07 * fi) + fi * 2.7)) * vec2(0.9, 0.6);
        float r = 0.18 + 0.1 * sin(fi * 1.3 + t);
        f += r * r / max(dot(q - c, q - c), 1e-4);
    }
    float edge = smoothstep(0.9, 1.1, f);
    float rim = smoothstep(0.9, 1.0, f) * (1.0 - smoothstep(1.0, 1.6, f));
    vec3 col = mix(u_color3 * 0.3, palette(clamp((f - 1.0) * 0.5, 0.0, 1.0)), edge);
    return col + rim * u_color2 * u_p2;
}
"""

BODIES["raymarched_fractals"] = """
float kifs(vec3 q, float t) {
    float s = 1.0;
    float fold = 1.0 + 0.5 * u_p1;
    for (int i = 0; i < 7; i++) {
        q = abs(q);
        if (q.x < q.y) q.xy = q.yx;
        if (q.x < q.z) q.xz = q.zx;
        if (q.y < q.z) q.yz = q.zy;
        q = q * 2.0 - vec3(fold, fold, 0.6 + 0.4 * sin(t * 0.3));
        q.xy = rot(0.2 * sin(t * 0.17) + u_p2) * q.xy;
        s *= 2.0;
    }
    return (length(q) - 1.5) / s;
}
vec3 effect(vec2 uv, vec2 p, float t) {
    vec3 ro = vec3(0.0, 0.0, -3.5), rd = normalize(vec3(p / u_scale, 1.8));
    ro.xz = rot(t * 0.15) * ro.xz; rd.xz = rot(t * 0.15) * rd.xz;
    float s = 0.0, d = 1.0; int i;
    for (i = 0; i < 70; i++) { d = kifs(ro + rd * s, t); if (d < 0.0015 || s > 10.0) break; s += d; }
    vec3 bg = u_color3 * 0.25;
    if (d > 0.01) return bg;
    float ao = 1.0 - float(i) / 70.0;
    vec3 q = ro + rd * s;
    vec2 e = vec2(0.002, 0.0);
    vec3 n = normalize(vec3(kifs(q + e.xyy, t) - kifs(q - e.xyy, t), kifs(q + e.yxy, t) - kifs(q - e.yxy, t), kifs(q + e.yyx, t) - kifs(q - e.yyx, t)));
    float dif = clamp(dot(n, normalize(vec3(0.6, 0.7, -0.5))), 0.0, 1.0);
    vec3 col = mix(u_color1, u_color2, 0.5 + 0.5 * n.y) * (0.2 + 0.8 * dif) * ao;
    return mix(col, bg, smoothstep(4.0, 10.0, s));
}
"""


def program_source(name: str) -> str:
    return HEADER + BODIES[name] + MAIN
