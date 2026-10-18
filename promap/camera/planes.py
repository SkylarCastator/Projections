"""Planes from a depth camera: walls, floor, table tops, box faces.

Input is the organised point cloud (H, W, 3) in the sensor frame (x right, y up, z forward,
metres, NaN where there is no depth). Per-pixel normals come from neighbouring points; the
dominant plane is found by voting (normal direction x offset), refined by least squares on
its inliers, cut down to its largest connected patch, removed, and the search repeats.

Each Plane carries what the two consumers need: the inlier mask and outline in depth pixels
(-> a projector-space mesh through the calibration) and a centre, in-plane axes and extents
(-> a 'plane' prop in the 3D scene, via `plane_to_node`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from promap.camera.detect import Detection


@dataclass
class Plane:
    normal: np.ndarray  # unit, pointing toward the sensor
    offset: float  # n . p = offset for points on the plane (metres)
    mask: np.ndarray  # (H, W) bool inliers, one connected patch
    centre: np.ndarray  # (3,) metres
    axis_u: np.ndarray  # in-plane unit axes (longest extent first)
    axis_v: np.ndarray
    extent: tuple[float, float]  # full size along u, v (metres)
    pixels: int = 0
    outline: np.ndarray | None = field(default=None, repr=False)  # (N, 2) depth px, simplified

    @property
    def label(self) -> str:
        n = self.normal
        up = abs(n[1])
        kind = "floor/ceiling" if up > 0.8 else ("wall" if up < 0.3 else "slope")
        return f"{kind} {self.extent[0]:.2f} x {self.extent[1]:.2f} m"

    def detection(self) -> Detection | None:
        if self.outline is None or len(self.outline) < 3:
            return None
        xs, ys = self.outline[:, 0], self.outline[:, 1]
        return Detection(self.outline.astype(np.float32), float(self.pixels),
                         (int(xs.min()), int(ys.min()), int(xs.max() - xs.min()), int(ys.max() - ys.min())))


def normals_from_points(xyz: np.ndarray, step: int = 2) -> np.ndarray:
    """Per-pixel unit normals (H, W, 3) from central differences; NaN where undefined.

    Normals are flipped to point toward the sensor (negative z component), so a wall seen
    from the front and from a slight angle share the same sign.
    """
    H, W = xyz.shape[:2]
    out = np.full((H, W, 3), np.nan, np.float32)
    dx = xyz[step:-step, 2 * step:, :] - xyz[step:-step, :-2 * step, :]
    dy = xyz[2 * step:, step:-step, :] - xyz[:-2 * step, step:-step, :]
    n = np.cross(dx, dy)
    length = np.linalg.norm(n, axis=2)
    ok = np.isfinite(length) & (length > 1e-9)
    # reject normals across depth jumps (a neighbour on a different surface)
    jump = (np.linalg.norm(dx, axis=2) > 0.15) | (np.linalg.norm(dy, axis=2) > 0.15)
    ok &= ~jump
    n = np.where(ok[..., None], n / np.maximum(length, 1e-9)[..., None], np.nan)
    flip = n[..., 2] > 0
    n[flip] *= -1.0
    out[step:-step, step:-step] = n
    return out


def _largest_component(mask: np.ndarray) -> tuple[np.ndarray, int]:
    import cv2

    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return np.zeros_like(mask), 0
    areas = stats[1:, cv2.CC_STAT_AREA]
    k = int(np.argmax(areas)) + 1
    return labels == k, int(areas[k - 1])


def _fit(points: np.ndarray) -> tuple[np.ndarray, float, np.ndarray]:
    """Least-squares plane through points (N, 3): (unit normal toward sensor, offset, centroid)."""
    c = points.mean(axis=0)
    _, _, vt = np.linalg.svd(points - c, full_matrices=False)
    n = vt[2]
    if n[2] > 0:
        n = -n
    return n, float(n @ c), c


def find_planes(xyz: np.ndarray, max_planes: int = 6, dist_tol: float = 0.03, angle_tol_deg: float = 20.0,
                min_pixels: int = 1500, epsilon: float = 0.01) -> list[Plane]:
    """Extract up to max_planes planar patches, largest first."""
    import cv2

    H, W = xyz.shape[:2]
    normals = normals_from_points(xyz)
    valid = np.isfinite(xyz).all(axis=2) & np.isfinite(normals).all(axis=2)
    remaining = valid.copy()
    cos_tol = math.cos(math.radians(angle_tol_deg))
    planes: list[Plane] = []
    pts_all = xyz.reshape(-1, 3)
    nrm_all = normals.reshape(-1, 3)
    for _ in range(max_planes):
        idx = np.flatnonzero(remaining)
        if len(idx) < min_pixels:
            break
        sub = idx[:: max(1, len(idx) // 30000)]
        P, N = pts_all[sub], nrm_all[sub]
        # vote: normal direction (quantised on a sphere) x offset
        theta = np.arctan2(N[:, 1], N[:, 0])
        phi = np.arccos(np.clip(N[:, 2], -1, 1))
        d = np.einsum("ij,ij->i", N, P)
        bins = np.stack([np.round(theta / math.radians(15)), np.round(phi / math.radians(15)), np.round(d / 0.05)], axis=1)
        _, inv, counts = np.unique(bins, axis=0, return_inverse=True, return_counts=True)
        best = np.argmax(counts)
        seed = sub[inv.ravel() == best]
        n0, d0, _ = _fit(pts_all[seed])
        # refine twice on the full-resolution inliers
        for _ in range(2):
            dist = np.abs(pts_all @ n0 - d0)
            agree = nrm_all @ n0 > cos_tol
            inl = remaining & (dist < dist_tol).reshape(H, W) & agree.reshape(H, W)
            if inl.sum() < min_pixels:
                break
            n0, d0, _ = _fit(pts_all[inl.ravel()])
        dist = np.abs(pts_all @ n0 - d0)
        inl = remaining & (dist < dist_tol).reshape(H, W) & (nrm_all @ n0 > cos_tol).reshape(H, W)
        inl = cv2.morphologyEx(inl.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)) > 0
        inl &= valid
        patch, area = _largest_component(inl)
        if area < min_pixels:
            remaining &= ~inl  # too fragmented: drop these pixels and try the next peak
            continue
        pts = pts_all[patch.ravel()]
        n, off, c = _fit(pts)
        # in-plane axes from the inlier spread
        rel = pts - c
        rel -= np.outer(rel @ n, n)
        _, _, vt = np.linalg.svd(rel[:: max(1, len(rel) // 20000)], full_matrices=False)
        u, v = vt[0], vt[1]
        pu, pv = rel @ u, rel @ v
        extent = (float(np.percentile(pu, 99) - np.percentile(pu, 1)), float(np.percentile(pv, 99) - np.percentile(pv, 1)))
        cnts, _ = cv2.findContours(patch.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        outline = None
        if cnts:
            cmax = max(cnts, key=cv2.contourArea)
            peri = cv2.arcLength(cmax, True)
            outline = cv2.approxPolyDP(cmax, max(1.0, epsilon * peri), True).reshape(-1, 2).astype(np.float32)
        planes.append(Plane(n.astype(np.float32), off, patch, c.astype(np.float32), u.astype(np.float32), v.astype(np.float32),
                            extent, int(area), outline))
        remaining &= ~patch
        # also drop any pixel on this plane so the next vote does not re-find it
        remaining &= ~((dist < dist_tol).reshape(H, W) & (nrm_all @ n > cos_tol).reshape(H, W))
    planes.sort(key=lambda p: -p.pixels)
    return planes


# -- to the 3D scene ---------------------------------------------------------------------
def sensor_to_panda(v: np.ndarray) -> tuple[float, float, float]:
    """Sensor frame (x right, y up, z forward) -> Panda child frame (x right, y forward, z up)."""
    return float(v[0]), float(v[2]), float(v[1])


def plane_to_node(plane: Plane, camera_pos, camera_hpr, name: str = "Plane"):
    """A SceneNode ('plane' primitive) in world coordinates for a plane seen by a camera node.

    The scene's plane card lies in its local X-Z plane and faces -Y. We build the card's
    local axes from the fitted plane (X = axis_u, Z = axis_v, -Y = normal toward the sensor),
    compose with the camera node's pose using Panda's own transform maths, and read back
    position / heading-pitch-roll.
    """
    from panda3d.core import NodePath, LMatrix4, LVecBase3

    from promap.model.scene import SceneNode

    u = np.asarray(sensor_to_panda(plane.axis_u))
    v = np.asarray(sensor_to_panda(plane.axis_v))
    n = np.asarray(sensor_to_panda(plane.normal))
    y_axis = -n  # card faces -Y, i.e. its +Y axis points away from the sensor
    # make a right-handed frame (x = u, z = v, y = x cross z) keeping v's direction
    x_axis = u / np.linalg.norm(u)
    z_axis = v - x_axis * (v @ x_axis)
    z_axis /= np.linalg.norm(z_axis)
    y_rh = np.cross(z_axis, x_axis)
    if y_rh @ y_axis < 0:
        z_axis = -z_axis
        y_rh = -y_rh
    c = sensor_to_panda(plane.centre)
    m = LMatrix4(x_axis[0], x_axis[1], x_axis[2], 0,
                 y_rh[0], y_rh[1], y_rh[2], 0,
                 z_axis[0], z_axis[1], z_axis[2], 0,
                 c[0], c[1], c[2], 1)
    root = NodePath("root")
    cam = root.attachNewNode("cam")
    cam.setPos(*camera_pos)
    cam.setHpr(*camera_hpr)
    node = cam.attachNewNode("plane")
    node.setMat(m)
    pos = node.getPos(root)
    hpr = node.getHpr(root)
    sn = SceneNode(kind="prop", model="plane", name=name)
    sn.pos = [float(pos.x), float(pos.y), float(pos.z)]
    sn.hpr = [float(hpr.x), float(hpr.y), float(hpr.z)]
    sn.scale = [max(0.05, plane.extent[0]), 1.0, max(0.05, plane.extent[1])]
    return sn
