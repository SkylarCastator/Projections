"""Projector <-> camera calibration with Gray-code structured light.

The projector shows a short sequence of black/white stripe patterns (Gray code: adjacent
stripes differ in one bit, so a decoding error costs one stripe, not a whole binary
place). The camera photographs each one. Decoding gives, for every camera pixel that
the projector lights, the projector pixel that lit it: a dense camera -> projector map.

From that map we fit a homography (one 3x3 for the dominant plane, good for a wall) and
keep the dense map for everything else, so a contour found in the camera image can be
dropped straight into projector space, plane or no plane.

References: Inokuchi, Sato & Matsuda, "Range imaging system for 3-D object recognition"
(ICPR 1984) introduced Gray-code structured light; Scharstein & Szeliski, "High-accuracy
stereo depth maps using structured light" (CVPR 2003) is the standard write-up of the
code + inverse-code thresholding used here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


def _nbits(n: int) -> int:
    return max(1, math.ceil(math.log2(max(2, n))))


def pattern_names(w: int, h: int) -> list[str]:
    names = ["white", "black"]
    for axis, n in (("x", w), ("y", h)):
        for b in range(_nbits(n) - 1, -1, -1):
            names += [f"{axis}{b}", f"{axis}{b}_inv"]
    return names


def pattern_count(w: int, h: int) -> int:
    return 2 + 2 * (_nbits(w) + _nbits(h))


def pattern(index: int, w: int, h: int) -> np.ndarray:
    """Pattern `index` of the sequence as an (h, w) uint8 image (0 or 255)."""
    if index == 0:
        return np.full((h, w), 255, np.uint8)
    if index == 1:
        return np.zeros((h, w), np.uint8)
    i = index - 2
    nx = _nbits(w)
    if i < 2 * nx:
        axis, bit, coords = "x", nx - 1 - i // 2, np.arange(w)
    else:
        i -= 2 * nx
        axis, bit, coords = "y", _nbits(h) - 1 - i // 2, np.arange(h)
    gray = coords ^ (coords >> 1)
    stripe = ((gray >> bit) & 1).astype(np.uint8) * 255
    if index % 2 == 1:  # every code pattern is followed by its inverse
        stripe = 255 - stripe
    if axis == "x":
        return np.repeat(stripe[None, :], h, axis=0)
    return np.repeat(stripe[:, None], w, axis=1)


def pattern_rgb(index: int, w: int, h: int) -> np.ndarray:
    p = pattern(index, w, h)
    return np.ascontiguousarray(np.repeat(p[:, :, None], 3, axis=2))


def _gray_to_binary(g: np.ndarray, nbits: int) -> np.ndarray:
    b = g.copy()
    for shift in range(1, nbits + 1):
        b ^= g >> shift
    return b


def decode(captures: list[np.ndarray], w: int, h: int, min_contrast: float = 12.0, min_margin: float = 0.08,
           smooth: int = 5, outlier_px: float = 8.0):
    """captures: one grayscale (float or uint8) camera image per pattern, in sequence order.

    Returns (px, py, valid): int32 projector coordinates per camera pixel and a mask of
    pixels where the projector light was seen and the code decoded cleanly.

    A camera usually cannot resolve the finest stripes (one projector pixel can be a
    fraction of a camera pixel), so a bit only gates validity when most lit pixels could
    decide it with margin; unresolved bits just take the sign, costing at most a stripe
    at that level. A median filter then cleans the per-pixel result and pixels that
    disagree with their neighbourhood by more than `outlier_px` are dropped.
    """
    import cv2

    caps = [np.asarray(c, dtype=np.float32) for c in captures]
    if len(caps) != pattern_count(w, h):
        raise ValueError(f"expected {pattern_count(w, h)} captures, got {len(caps)}")
    white, black = caps[0], caps[1]
    contrast = white - black
    lit = contrast > min_contrast
    valid = lit.copy()
    safe = np.maximum(contrast, 1e-3)
    n_lit = max(1, int(lit.sum()))
    i = 2
    out = []
    for n in (w, h):
        nb = _nbits(n)
        g = np.zeros(white.shape, np.int32)
        for b in range(nb - 1, -1, -1):
            code, inv = caps[i], caps[i + 1]
            i += 2
            diff = (code - inv) / safe
            decided = np.abs(diff) > min_margin
            if (decided & lit).sum() / n_lit > 0.6:  # the camera resolves this stripe width
                valid &= decided
            g |= (diff > 0).astype(np.int32) << b
        coord = _gray_to_binary(g, nb)
        valid &= coord < n
        out.append(coord)
    px, py = out
    if smooth and smooth >= 3:
        k = 3 if smooth < 5 else 5  # cv2.medianBlur on float32 supports 3 and 5 only
        # one camera pixel may span several projector pixels: allow that much slope inside the window
        tol = max(outlier_px, 3.0 * (k // 2) * projector_px_per_camera_px(px, py, valid))
        for arr in (px, py):
            med = cv2.medianBlur(arr.astype(np.float32), k)
            valid &= np.abs(arr - med) <= tol
            arr[:] = np.where(valid, np.rint(med).astype(np.int32), arr)
        # pixels on stripe boundaries are undecided for one bit and leave a lattice of holes;
        # fill a hole from its valid neighbours when it has enough of them and was lit
        for win, need in ((3, 5), (5, 12)):
            vf = valid.astype(np.float32)
            cnt = cv2.boxFilter(vf, -1, (win, win), normalize=False)
            fill = lit & ~valid & (cnt >= need)
            if not fill.any():
                continue
            for arr in (px, py):
                s = cv2.boxFilter(arr.astype(np.float32) * vf, -1, (win, win), normalize=False)
                arr[fill] = np.rint(s[fill] / cnt[fill]).astype(np.int32)
            valid |= fill
    return px, py, valid


def projector_px_per_camera_px(px, py, valid) -> float:
    """How many projector pixels one camera pixel covers (median local gradient of the maps)."""
    vx = valid[:, 1:] & valid[:, :-1]
    vy = valid[1:, :] & valid[:-1, :]
    gx = np.abs(px[:, 1:] - px[:, :-1])[vx]
    gy = np.abs(py[1:, :] - py[:-1, :])[vy]
    g = np.concatenate([gx, gy]) if gx.size + gy.size else np.array([1.0])
    return float(max(1.0, np.median(g)))


@dataclass
class Calibration:
    cam_size: tuple[int, int]
    proj_size: tuple[int, int]
    H: np.ndarray  # 3x3 camera px -> projector px
    inlier_ratio: float = 0.0
    px: np.ndarray | None = field(default=None, repr=False)
    py: np.ndarray | None = field(default=None, repr=False)
    valid: np.ndarray | None = field(default=None, repr=False)
    camera_name: str = ""

    # -- fitting ---------------------------------------------------------------
    @classmethod
    def from_decoded(cls, px, py, valid, cam_size, proj_size, step: int = 4, camera_name: str = "") -> "Calibration":
        import cv2

        ys, xs = np.nonzero(valid[::step, ::step])
        ys, xs = ys * step, xs * step
        if len(xs) < 16:
            raise ValueError("too few decoded pixels: is the projector lighting the camera's view?")
        src = np.stack([xs, ys], axis=1).astype(np.float32)
        dst = np.stack([px[ys, xs], py[ys, xs]], axis=1).astype(np.float32)
        # inlier threshold of ~1.5 camera pixels, expressed in projector pixels
        thresh = 1.5 * projector_px_per_camera_px(px, py, valid)
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, thresh)
        if H is None:
            raise ValueError("homography fit failed")
        ratio = float(mask.mean()) if mask is not None else 0.0
        return cls(tuple(cam_size), tuple(proj_size), H, ratio, px.astype(np.int32), py.astype(np.int32), valid.astype(bool), camera_name)

    @property
    def coverage(self) -> float:
        """Fraction of the camera image lit by the projector."""
        return float(self.valid.mean()) if self.valid is not None else 0.0

    def projector_outline_in_camera(self) -> np.ndarray:
        """Where the projector's frame lands in the camera image (4 points), via H^-1."""
        import cv2

        w, h = self.proj_size
        pts = np.array([[[0, 0]], [[w, 0]], [[w, h]], [[0, h]]], np.float32)
        return cv2.perspectiveTransform(pts, np.linalg.inv(self.H)).reshape(-1, 2)

    # -- mapping -----------------------------------------------------------------
    def map_points(self, pts, radius: int = 6) -> np.ndarray:
        """Camera px -> projector px. Uses the dense map where decoded, else the homography."""
        import cv2

        pts = np.asarray(pts, np.float32).reshape(-1, 2)
        out = cv2.perspectiveTransform(pts.reshape(-1, 1, 2), self.H).reshape(-1, 2)
        if self.valid is None:
            return out
        H_, W_ = self.valid.shape
        for k, (x, y) in enumerate(pts):
            xi, yi = int(round(x)), int(round(y))
            x0, x1 = max(0, xi - radius), min(W_, xi + radius + 1)
            y0, y1 = max(0, yi - radius), min(H_, yi + radius + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            v = self.valid[y0:y1, x0:x1]
            if v.sum() >= 4:
                out[k] = (np.median(self.px[y0:y1, x0:x1][v]), np.median(self.py[y0:y1, x0:x1][v]))
        return out

    def warp_camera_to_projector(self, frame: np.ndarray) -> np.ndarray:
        """The camera image re-projected into projector pixel space (a ready-made reference photo)."""
        import cv2

        return cv2.warpPerspective(frame, self.H, self.proj_size, flags=cv2.INTER_LINEAR)

    # -- persistence -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {"cam_size": list(self.cam_size), "proj_size": list(self.proj_size), "H": self.H.tolist(),
                "inlier_ratio": self.inlier_ratio, "camera_name": self.camera_name}

    @classmethod
    def from_dict(cls, d: dict) -> "Calibration":
        return cls(tuple(d["cam_size"]), tuple(d["proj_size"]), np.array(d["H"], np.float64),
                   float(d.get("inlier_ratio", 0.0)), camera_name=d.get("camera_name", ""))

    def save_dense(self, path: str | Path):
        if self.px is not None:
            np.savez_compressed(path, px=self.px, py=self.py, valid=self.valid)

    def load_dense(self, path: str | Path):
        p = Path(path)
        if p.exists():
            z = np.load(p)
            self.px, self.py, self.valid = z["px"], z["py"], z["valid"]
