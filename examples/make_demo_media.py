"""Generate demo media so the example project runs without downloads.

Every clip is procedural and takes the same knobs, so you can re-roll a variant:

    python examples/make_demo_media.py                      # everything, defaults
    python examples/make_demo_media.py flames --color "#ff4000" --color2 "#ffe080" --speed 1.5
    python examples/make_demo_media.py rain spiral --seconds 6 --size 512x512 --out /tmp/clips
    python examples/make_demo_media.py --list               # names + which knobs each one uses

Knobs: --color / --color2 (hex or r,g,b), --speed (motion multiplier), --seconds, --fps,
--size WxH, --seed, --density (how much stuff), --name (output file stem). Colour and
speed can also be changed per surface inside promap (Tint, Speed) without regenerating.
"""
from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np
import cv2
from PIL import Image, ImageDraw

OUT = Path(__file__).parent / "media"

# name -> (function, default extension, knobs it honours)
CLIPS: dict[str, tuple] = {}


def clip(ext: str, *knobs: str):
    def deco(fn):
        CLIPS[fn.__name__] = (fn, ext, knobs)
        return fn
    return deco


def parse_color(s: str | None, default=(255, 120, 0)) -> tuple[int, int, int]:
    """'#rrggbb' or 'r,g,b' -> (r, g, b)."""
    if not s:
        return default
    s = s.strip()
    if s.startswith("#"):
        s = s[1:]
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    parts = [int(float(p)) for p in s.split(",")]
    return parts[0], parts[1], parts[2]


def bgr(c):
    return int(c[2]), int(c[1]), int(c[0])


def lerp(a, b, t):
    t = np.clip(t, 0, 1)[..., None]
    return (np.asarray(a, float) * (1 - t) + np.asarray(b, float) * t)


class Video:
    """cv2 VideoWriter with frame count derived from seconds * fps."""

    def __init__(self, path: Path, size, fps: float, seconds: float):
        self.w, self.h = size
        self.n = max(1, int(round(seconds * fps)))
        self.fps = fps
        self.vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (self.w, self.h))

    def write_rgb(self, img):
        self.vw.write(cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR))

    def close(self):
        self.vw.release()


def _noise(rng, shape, octaves=4, persistence=0.55):
    """Cheap fractal value noise: summed upscaled random grids."""
    h, w = shape
    out = np.zeros((h, w), np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        gh, gw = max(2, h >> (octaves - o)), max(2, w >> (octaves - o))
        g = rng.random((gh, gw), dtype=np.float32)
        out += amp * cv2.resize(g, (w, h), interpolation=cv2.INTER_CUBIC)
        total += amp
        amp *= persistence
    return out / total


# -- stills / audio -----------------------------------------------------------
@clip("png", "color", "size")
def ghost(path, *, color=(230, 230, 255), size=(300, 400), **_):
    w, h = size
    img = np.zeros((h, w, 3), np.uint8)
    cv2.ellipse(img, (w // 2, h // 2), (w // 2 - 10, h // 2 - 10), 0, 0, 360, bgr(color), -1)
    cv2.circle(img, (w // 3, h // 3), w // 14, (30, 10, 10), -1)
    cv2.circle(img, (2 * w // 3, h // 3), w // 14, (30, 10, 10), -1)
    cv2.ellipse(img, (w // 2, int(h * 0.6)), (w // 10, h // 9), 0, 0, 360, (30, 10, 10), -1)
    cv2.imwrite(str(path), img)


@clip("png", "size")
def room(path, *, size=(1920, 1080), **_):
    """Stand-in for a photo of the space taken from the projector: a wall, a floor line, a crate."""
    w, h = size
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = (70, 75, 80)
    cv2.rectangle(img, (0, int(h * 0.76)), (w, h), (50, 45, 40), -1)
    cv2.rectangle(img, (int(w * 0.46), int(h * 0.11)), (int(w * 0.93), int(h * 0.85)), (95, 100, 110), -1)
    cv2.rectangle(img, (int(w * 0.625), int(h * 0.37)), (int(w * 0.73), int(h * 0.555)), (30, 30, 30), -1)
    cv2.rectangle(img, (int(w * 0.094), int(h * 0.167)), (int(w * 0.375), int(h * 0.59)), (120, 90, 60), -1)
    cv2.ellipse(img, (int(w * 0.234), int(h * 0.8)), (int(w * 0.089), int(h * 0.176)), 0, 0, 360, (140, 140, 150), -1)
    cv2.putText(img, "reference photo (stand-in)", (20, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2)
    cv2.imwrite(str(path), img)


@clip("wav", "seconds", "speed")
def drone(path, *, seconds=8.0, speed=1.0, rate=48000, **_):
    """Low pulsing drone with a slow heartbeat thump, for the audio/reactive demo. speed = beat rate."""
    t = np.arange(int(seconds * rate)) / rate
    pulse = 0.5 + 0.5 * np.sin(2 * np.pi * 0.5 * speed * t)
    sig = 0.25 * np.sin(2 * np.pi * 55 * t) * pulse + 0.12 * np.sin(2 * np.pi * 110 * t)
    beat = np.zeros_like(t)
    for k in np.arange(0, seconds, 1.0 / speed):
        i = int(k * rate)
        n = int(0.15 * rate)
        beat[i:i + n] += 0.6 * np.sin(2 * np.pi * 48 * t[:n]) * np.exp(-t[:n] * 25)
    pcm = (np.clip(sig + beat, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# -- animated ------------------------------------------------------------------
@clip("gif", "color", "speed", "seconds", "size")
def pulse(path, *, color=(255, 120, 0), speed=1.0, seconds=1.2, size=(256, 256), fps=25, **_):
    """Breathing disc. speed = breaths per 1.2 s."""
    w, h = size
    n = max(2, int(seconds * fps))
    frames = []
    for i in range(n):
        im = Image.new("RGB", (w, h), (0, 0, 0))
        d = ImageDraw.Draw(im)
        r = min(w, h) * (0.2 + 0.25 * (1 + np.sin(2 * np.pi * speed * i / n)) / 2)
        d.ellipse((w / 2 - r, h / 2 - r, w / 2 + r, h / 2 + r), fill=tuple(color))
        frames.append(im)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=int(1000 / fps), loop=0)


@clip("mp4", "color", "color2", "speed", "seconds", "fps", "size")
def bars(path, *, color=(255, 40, 0), color2=(0, 40, 255), speed=1.0, seconds=4.0, fps=30, size=(640, 360), **_):
    """Scrolling colour bars (the original alignment clip) blending color -> color2."""
    v = Video(path, size, fps, seconds)
    w, h = size
    for i in range(v.n):
        img = np.zeros((h, w, 3), np.uint8)
        for k in range(8):
            x = int((k * w / 8 + i * 4 * speed) % w)
            c = lerp(color, color2, np.array(k / 7.0))
            cv2.rectangle(img, (x, 0), (x + w // 16, h), tuple(int(q) for q in c), -1)
        cv2.putText(img, f"frame {i}", (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        v.write_rgb(img)
    v.close()


@clip("mp4", "color", "color2", "speed", "seconds", "fps", "size", "seed", "density")
def flames(path, *, color=(255, 40, 0), color2=(255, 230, 120), speed=1.0, seconds=4.0, fps=30, size=(384, 512),
           seed=0, density=1.0, **_):
    """Rising fire: a heat field fed from the bottom, cooled and blurred upward, through a palette."""
    rng = np.random.default_rng(seed)
    v = Video(path, size, fps, seconds)
    w, h = size
    # "Doom fire": each row copies the row below with a random sideways jitter and loses a
    # random amount of heat, so tongues of flame flicker up and die at ~65% of the height.
    heat = np.zeros((h, w), np.float32)
    steps = max(1, int(round(3 * speed)))  # rows the fire climbs per frame
    decay = 2.0 / (0.65 * h)
    xx = np.arange(w)[None, :]

    def step():
        heat[-1, :] = (rng.random(w, dtype=np.float32) < min(1.0, 0.9 * density)).astype(np.float32)
        src = heat[1:, :]
        dx = rng.integers(-1, 2, src.shape)
        shifted = np.take_along_axis(src, (xx + dx) % w, axis=1)
        heat[:-1, :] = np.maximum(0.0, shifted - rng.random(src.shape, dtype=np.float32) * decay)

    for _ in range(h // steps + 10):  # pre-roll to a steady state
        for _ in range(steps):
            step()
    for _ in range(v.n):
        for _ in range(steps):
            step()
        t = cv2.GaussianBlur(heat, (0, 0), 1.8)
        img = lerp((0, 0, 0), color, t * 2) * (t < 0.5)[..., None] + lerp(color, color2, (t - 0.5) * 2) * (t >= 0.5)[..., None]
        v.write_rgb(img)
    v.close()


@clip("mp4", "color", "speed", "seconds", "fps", "size", "seed", "density")
def rain(path, *, color=(150, 200, 255), speed=1.0, seconds=4.0, fps=30, size=(640, 480), seed=0, density=1.0, **_):
    """Falling streaks. density = drops on screen, speed = fall speed."""
    rng = np.random.default_rng(seed)
    v = Video(path, size, fps, seconds)
    w, h = size
    n = int(120 * density)
    xs = rng.uniform(0, w, n)
    ys = rng.uniform(-h, h, n)
    ln = rng.uniform(12, 30, n)
    vel = rng.uniform(10, 18, n) * speed
    for _ in range(v.n):
        img = np.zeros((h, w, 3), np.float32)
        for k in range(n):
            x, y = int(xs[k]), int(ys[k])
            cv2.line(img, (x, y), (x - 2, y + int(ln[k])), [c / 255 for c in color], 1, cv2.LINE_AA)
        ys = (ys + vel + h) % (2 * h) - h
        v.write_rgb(img * 255)
    v.close()


@clip("gif", "color", "speed", "seconds", "size", "seed")
def eyes(path, *, color=(255, 220, 0), speed=1.0, seconds=6.0, size=(400, 200), fps=20, seed=0, **_):
    """A pair of eyes that glance about and blink. color = iris. speed = how restless."""
    rng = np.random.default_rng(seed)
    w, h = size
    n = int(seconds * fps)
    frames = []
    look = np.zeros(2)
    target = np.zeros(2)
    blink_at = set(rng.integers(0, n, max(1, int(seconds * 0.4 * speed))).tolist())
    blink = 0
    for i in range(n):
        if rng.random() < 0.03 * speed:
            target = rng.uniform(-1, 1, 2)
        look += (target - look) * 0.15 * speed
        if i in blink_at:
            blink = 6
        im = Image.new("RGB", (w, h), (0, 0, 0))
        d = ImageDraw.Draw(im)
        r = h * 0.38
        for cx in (w * 0.3, w * 0.7):
            cy = h / 2
            lid = max(0.08, 1 - abs(blink - 3) / 3) if blink > 0 else 0.0
            open_frac = 1 - lid if blink > 0 else 1.0
            d.ellipse((cx - r, cy - r * open_frac, cx + r, cy + r * open_frac), fill=(245, 245, 235))
            ix, iy = cx + look[0] * r * 0.45, cy + look[1] * r * 0.35 * open_frac
            ir = r * 0.45 * max(0.1, open_frac)
            d.ellipse((ix - ir, iy - ir, ix + ir, iy + ir), fill=tuple(color))
            pr = ir * 0.5
            d.ellipse((ix - pr, iy - pr, ix + pr, iy + pr), fill=(0, 0, 0))
        blink = max(0, blink - 1)
        frames.append(im)
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=int(1000 / fps), loop=0)


@clip("mp4", "color", "speed", "seconds", "fps", "size", "seed", "density")
def lightning(path, *, color=(220, 230, 255), speed=1.0, seconds=6.0, fps=30, size=(640, 480), seed=0, density=1.0, **_):
    """Dark sky with random flashes and a forking bolt. density = strikes per second, speed = decay."""
    rng = np.random.default_rng(seed)
    v = Video(path, size, fps, seconds)
    w, h = size
    glow = 0.0
    bolt = None
    for _ in range(v.n):
        if rng.random() < density * 2.0 / fps:
            glow = 1.0
            pts = [(rng.uniform(w * 0.2, w * 0.8), 0.0)]
            while pts[-1][1] < h:
                x, y = pts[-1]
                pts.append((x + rng.uniform(-w * 0.08, w * 0.08), y + rng.uniform(h * 0.04, h * 0.1)))
            bolt = np.array(pts, np.int32)
        img = np.full((h, w, 3), 8, np.float32)
        img += glow * 140 * np.array(color) / 255
        if bolt is not None and glow > 0.5:
            cv2.polylines(img, [bolt], False, tuple(float(c) for c in color), 2, cv2.LINE_AA)
            cv2.polylines(img, [bolt], False, (255, 255, 255), 1, cv2.LINE_AA)
        glow *= 0.78 ** speed
        v.write_rgb(img)
    v.close()


@clip("mp4", "color", "color2", "speed", "seconds", "fps", "size", "density")
def spiral(path, *, color=(255, 255, 255), color2=(0, 0, 0), speed=1.0, seconds=4.0, fps=30, size=(512, 512), density=1.0, **_):
    """Rotating hypnotic spiral. density = arms, speed = turns per second."""
    v = Video(path, size, fps, seconds)
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = xx - w / 2, yy - h / 2
    ang = np.arctan2(dy, dx)
    rad = np.sqrt(dx * dx + dy * dy) / (min(w, h) / 2)
    arms = max(1, int(round(2 * density)))
    for i in range(v.n):
        phase = 2 * np.pi * speed * i / fps
        t = 0.5 + 0.5 * np.sin(arms * ang + 6 * rad - phase)
        v.write_rgb(lerp(color2, color, t))
    v.close()


@clip("mp4", "color", "speed", "seconds", "fps", "size", "seed", "density")
def drips(path, *, color=(170, 0, 20), speed=1.0, seconds=6.0, fps=30, size=(480, 640), seed=0, density=1.0, **_):
    """Thick drips running down from the top edge and pooling. density = how many."""
    rng = np.random.default_rng(seed)
    v = Video(path, size, fps, seconds)
    w, h = size
    n = max(1, int(8 * density))
    xs = rng.uniform(w * 0.05, w * 0.95, n)
    heads = rng.uniform(-h * 0.5, 0, n)
    vel = rng.uniform(1.5, 4.0, n) * speed
    width = rng.uniform(6, 16, n)
    img = np.zeros((h, w, 3), np.float32)
    for _ in range(v.n):
        img *= 0.999
        for k in range(n):
            y = int(heads[k])
            if 0 <= y < h:
                cv2.circle(img, (int(xs[k]), y), int(width[k] / 2), tuple(float(c) for c in color), -1, cv2.LINE_AA)
                cv2.rectangle(img, (int(xs[k] - width[k] * 0.3), 0), (int(xs[k] + width[k] * 0.3), y), tuple(float(c) * 0.8 for c in color), -1)
            heads[k] += vel[k] * (0.5 + 0.5 * rng.random())
            if heads[k] > h + width[k]:
                heads[k] = rng.uniform(-h * 0.3, 0)
                xs[k] = rng.uniform(w * 0.05, w * 0.95)
        v.write_rgb(img)
    v.close()


@clip("mp4", "color", "speed", "seconds", "fps", "size", "seed")
def fog(path, *, color=(120, 140, 160), speed=1.0, seconds=8.0, fps=24, size=(640, 360), seed=0, **_):
    """Drifting layered fog from fractal noise; loops cleanly."""
    rng = np.random.default_rng(seed)
    v = Video(path, size, fps, seconds)
    w, h = size
    big = _noise(rng, (h, w * 2), octaves=5)
    big2 = _noise(rng, (h, w * 2), octaves=4)
    for i in range(v.n):
        s1 = int((i * 2 * speed) % w)
        s2 = int((i * 1.2 * speed) % w)
        a = np.roll(big, -s1, axis=1)[:, :w]
        b = np.roll(big2, s2, axis=1)[:, :w]
        t = np.clip((a * 0.6 + b * 0.4 - 0.35) * 2.2, 0, 1)
        v.write_rgb(lerp((0, 0, 0), color, t))
    v.close()


# -- CLI ------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="clips to make (default: all)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--name", help="output file stem (only with a single clip)")
    ap.add_argument("--color", help="main colour, '#ff4000' or '255,64,0'")
    ap.add_argument("--color2", help="second colour where the clip has one")
    ap.add_argument("--speed", type=float)
    ap.add_argument("--seconds", type=float)
    ap.add_argument("--fps", type=float)
    ap.add_argument("--size", help="WxH")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--density", type=float)
    args = ap.parse_args(argv)

    if args.list:
        for name, (fn, ext, knobs) in CLIPS.items():
            print(f"{name:10s} .{ext:4s} {', '.join(knobs):45s} {fn.__doc__.strip().splitlines()[0] if fn.__doc__ else ''}")
        return
    names = args.names or list(CLIPS)
    if args.name and len(names) != 1:
        ap.error("--name needs exactly one clip")
    args.out.mkdir(parents=True, exist_ok=True)
    kw = {}
    if args.color:
        kw["color"] = parse_color(args.color)
    if args.color2:
        kw["color2"] = parse_color(args.color2)
    for k in ("speed", "seconds", "fps", "seed", "density"):
        if getattr(args, k) is not None:
            kw[k] = getattr(args, k)
    if args.size:
        w, h = args.size.lower().split("x")
        kw["size"] = (int(w), int(h))
    for name in names:
        if name not in CLIPS:
            ap.error(f"unknown clip {name!r}; --list shows them")
        fn, ext, knobs = CLIPS[name]
        path = args.out / f"{args.name or name}.{ext}"
        fn(path, **{k: v for k, v in kw.items() if k in knobs})
        print("wrote", path)


if __name__ == "__main__":
    main()
