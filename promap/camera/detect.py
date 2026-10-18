"""Find objects in a camera frame and turn them into triangle meshes.

Pipeline: segment (bright / dark / difference-from-background / edges) -> contours ->
simplify (Douglas-Peucker) -> map camera px to projector px through the calibration ->
triangulate the polygon (ear clipping) -> `Mesh` with the media stretched over its bounds.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from promap.model.mesh import Mesh

MODES = ("bright", "dark", "diff", "edges")


@dataclass
class Detection:
    contour: np.ndarray  # (N, 2) camera px, simplified
    area: float
    bbox: tuple[int, int, int, int]  # x, y, w, h in camera px
    label: int = 0  # cluster id when produced by a scene scan


def segment(frame_rgb: np.ndarray, mode: str = "bright", background: np.ndarray | None = None, thresh: int = 0,
            blur: int = 5, min_area: float = 800.0, max_count: int = 32, epsilon: float = 0.01,
            roi: np.ndarray | None = None) -> list[Detection]:
    """Objects in the frame as simplified contours, largest first.

    mode   "bright": lighter than the threshold (props under a white projector wash)
           "dark":   darker than the threshold (dark objects in front of a lit wall)
           "diff":   differs from `background` (an earlier frame of the empty scene)
           "edges":  Canny edges closed into blobs
    thresh 0 picks Otsu's threshold automatically (not for "diff", where it is the
           per-pixel difference in 0..255; 0 there means 25).
    epsilon simplification tolerance as a fraction of the contour's perimeter.
    roi    optional uint8 mask: only look inside it (e.g. the projector's footprint).
    """
    import cv2

    gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)
    if blur > 1:
        k = blur | 1
        gray = cv2.GaussianBlur(gray, (k, k), 0)
    if mode == "diff":
        if background is None:
            raise ValueError("diff mode needs a background frame")
        bg = cv2.cvtColor(background, cv2.COLOR_RGB2GRAY)
        if blur > 1:
            bg = cv2.GaussianBlur(bg, (k, k), 0)
        d = cv2.absdiff(gray, bg)
        _, mask = cv2.threshold(d, thresh or 25, 255, cv2.THRESH_BINARY)
    elif mode == "edges":
        edges = cv2.Canny(gray, 50, 150)
        mask = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)
        # fill closed outlines so contours describe areas, not rings
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        mask = np.zeros_like(mask)
        cv2.drawContours(mask, cnts, -1, 255, -1)
    else:
        flag = cv2.THRESH_BINARY if mode == "bright" else cv2.THRESH_BINARY_INV
        if thresh <= 0:
            flag |= cv2.THRESH_OTSU
        _, mask = cv2.threshold(gray, thresh, 255, flag)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    if roi is not None:
        mask = cv2.bitwise_and(mask, roi)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    # a blob that is essentially the whole search area is the lit wall itself, not an object
    max_area = 0.85 * float((roi > 0).sum()) if roi is not None else 0.85 * mask.size
    out: list[Detection] = []
    for c in cnts:
        area = float(cv2.contourArea(c))
        if area < min_area or area > max_area:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, max(1.0, epsilon * peri), True).reshape(-1, 2).astype(np.float32)
        if len(approx) < 3:
            continue
        x, y, w, h = cv2.boundingRect(c)
        out.append(Detection(approx, area, (int(x), int(y), int(w), int(h))))
    out.sort(key=lambda d: -d.area)
    return out[:max_count]


def simplify_polygon(points, epsilon: float = 2.0) -> np.ndarray:
    """Douglas-Peucker on a closed outline (any coordinate space); epsilon in the same units."""
    import cv2

    pts = np.asarray(points, np.float32).reshape(-1, 1, 2)
    if len(pts) < 3:
        return pts.reshape(-1, 2)
    out = cv2.approxPolyDP(pts, max(1e-3, float(epsilon)), True).reshape(-1, 2)
    if len(out) < 3:  # degenerate after simplification: keep the hull instead
        out = cv2.convexHull(pts).reshape(-1, 2)
    return out


# -- triangulation ---------------------------------------------------------------
def _area2(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _inside(p, a, b, c) -> bool:
    return _area2(a, b, p) >= 0 and _area2(b, c, p) >= 0 and _area2(c, a, p) >= 0


def triangulate(polygon) -> list[list[int]]:
    """Ear-clipping triangulation of a simple polygon (any winding). Returns index triples."""
    pts = [tuple(map(float, p)) for p in polygon]
    n = len(pts)
    if n < 3:
        return []
    idx = list(range(n))
    signed = sum(_area2((0, 0), pts[i], pts[(i + 1) % n]) for i in range(n))
    if signed < 0:  # make it counter-clockwise in the maths sense (positive area)
        idx.reverse()
    tris: list[list[int]] = []
    guard = 0
    while len(idx) > 3 and guard < 10 * n:
        guard += 1
        found = False
        m = len(idx)
        for k in range(m):
            ia, ib, ic = idx[(k - 1) % m], idx[k], idx[(k + 1) % m]
            a, b, c = pts[ia], pts[ib], pts[ic]
            if _area2(a, b, c) <= 1e-9:  # reflex or degenerate corner: not an ear
                continue
            if any(_inside(pts[j], a, b, c) for j in idx if j not in (ia, ib, ic)):
                continue
            tris.append([ia, ib, ic])
            del idx[k]
            found = True
            break
        if not found:  # numerically nasty polygon: clip the least-bad corner and carry on
            k = max(range(m), key=lambda k: _area2(pts[idx[(k - 1) % m]], pts[idx[k]], pts[idx[(k + 1) % m]]))
            tris.append([idx[(k - 1) % m], idx[k], idx[(k + 1) % m]])
            del idx[k]
    if len(idx) == 3:
        tris.append(list(idx))
    return tris


def polygon_mesh(points) -> Mesh:
    """A Mesh over a polygon given in output (projector) pixels; media fitted to its bounds."""
    pts = np.asarray(points, np.float64).reshape(-1, 2)
    tris = triangulate(pts)
    m = Mesh(verts=[[float(x), float(y), 0.0, 0.0] for x, y in pts], tris=tris)
    m.fit_uvs()
    return m


def detections_to_meshes(dets: list[Detection], calibration) -> list[Mesh]:
    """Map each detection's contour into projector space and triangulate it."""
    out = []
    for d in dets:
        proj = calibration.map_points(d.contour) if calibration is not None else d.contour
        m = polygon_mesh(proj)
        if m.tris:
            out.append(m)
    return out


def draw_detections(frame_rgb: np.ndarray, dets: list[Detection], outline=None) -> np.ndarray:
    """Preview: contours and index labels drawn over a copy of the frame."""
    import cv2

    img = frame_rgb.copy()
    if outline is not None:
        cv2.polylines(img, [np.asarray(outline, np.int32).reshape(-1, 1, 2)], True, (255, 220, 0), 2)
    for i, d in enumerate(dets):
        cv2.polylines(img, [d.contour.astype(np.int32).reshape(-1, 1, 2)], True, (0, 255, 120), 2)
        x, y, _, _ = d.bbox
        cv2.putText(img, str(i + 1), (x + 4, y + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 120), 2)
    return img
