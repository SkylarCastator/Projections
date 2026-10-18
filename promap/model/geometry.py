"""Planar geometry for quad warping.

A surface is a quad: four corners in output-pixel space, ordered TL, TR, BR, BL.
Media is mapped onto it with a homography (a projective transform) from the unit
square, which is what makes a flat image look "pinned" to a tilted real-world face.
"""
from __future__ import annotations

import numpy as np

Point = tuple[float, float]


def homography_unit_square_to_quad(corners) -> np.ndarray:
    """Return the 3x3 matrix H with H*(u,v,1) ~ corner for the unit square.

    (0,0)->corners[0] (TL), (1,0)->corners[1] (TR),
    (1,1)->corners[2] (BR), (0,1)->corners[3] (BL).
    Closed form after Heckbert, "Fundamentals of Texture Mapping and Image Warping" (1989).
    """
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = [(float(x), float(y)) for x, y in corners]
    sx = x0 - x1 + x2 - x3
    sy = y0 - y1 + y2 - y3
    dx1, dx2 = x1 - x2, x3 - x2
    dy1, dy2 = y1 - y2, y3 - y2
    det = dx1 * dy2 - dx2 * dy1
    if abs(sx) < 1e-9 and abs(sy) < 1e-9 or abs(det) < 1e-12:
        g = h = 0.0  # parallelogram: plain affine map
    else:
        g = (sx * dy2 - dx2 * sy) / det
        h = (dx1 * sy - sx * dy1) / det
    a = x1 - x0 + g * x1
    b = x3 - x0 + h * x3
    c = x0
    d = y1 - y0 + g * y1
    e = y3 - y0 + h * y3
    f = y0
    return np.array([[a, b, c], [d, e, f], [g, h, 1.0]], dtype=np.float64)


UNIT_SQUARE = np.array([[0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=np.float64)


def homogeneous_corners(corners) -> np.ndarray:
    """Four rows of (x*w, y*w, w): the GPU-side form of the warp.

    Feeding these as clip-space positions makes the rasteriser's own perspective
    division do the projective interpolation, so a plain (u,v) texcoord ends up
    perspective-correct across the quad with no fragment-shader maths.
    """
    H = homography_unit_square_to_quad(corners)
    return (H @ UNIT_SQUARE.T).T  # shape (4, 3)


def quad_to_unit(corners, point: Point) -> tuple[float, float] | None:
    """Inverse map a screen point into (u, v) of the unit square; None if degenerate."""
    H = homography_unit_square_to_quad(corners)
    try:
        Hinv = np.linalg.inv(H)
    except np.linalg.LinAlgError:
        return None
    x, y, w = Hinv @ np.array([point[0], point[1], 1.0])
    if abs(w) < 1e-12:
        return None
    return float(x / w), float(y / w)


def point_in_quad(corners, point: Point) -> bool:
    uv = quad_to_unit(corners, point)
    if uv is None:
        return False
    u, v = uv
    return 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0


def is_convex(corners) -> bool:
    pts = [np.array(c, dtype=float) for c in corners]
    signs = []
    for i in range(4):
        a, b, c = pts[i], pts[(i + 1) % 4], pts[(i + 2) % 4]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        signs.append(cross > 0)
    return all(signs) or not any(signs)


def centroid(corners) -> Point:
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return sum(xs) / 4.0, sum(ys) / 4.0


def rect_corners(x: float, y: float, w: float, h: float) -> list[list[float]]:
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def clip_corners(corners, out_size) -> np.ndarray:
    """(4,4) rows of (x*w, y*w, 0, w) in GL clip space for an output of `out_size` pixels.

    Output space has y down; clip space has y up, so y is flipped here. If the quad's
    winding came out negative, every row is negated so w stays positive for the divide.
    """
    out_w, out_h = out_size
    hc = homogeneous_corners(corners)
    w = hc[:, 2:3]
    x = hc[:, 0:1] / out_w * 2.0 - w
    y = w - hc[:, 1:2] / out_h * 2.0
    if np.all(w < 0):
        x, y, w = -x, -y, -w
    return np.hstack([x, y, np.zeros_like(w), w])


def quad_texcoords(flip_h: bool = False, flip_v: bool = False) -> np.ndarray:
    """Texcoords for TL, TR, BR, BL. Row 0 of an uploaded frame is v=0, so TL samples v=0."""
    u0, u1 = (1.0, 0.0) if flip_h else (0.0, 1.0)
    v0, v1 = (1.0, 0.0) if flip_v else (0.0, 1.0)
    return np.array([[u0, v0], [u1, v0], [u1, v1], [u0, v1]], dtype=np.float32)
