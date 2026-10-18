"""People in a static scene, from depth: anything that was not there when the background was stored.

The assumption promap makes is that the Kinect looks at a scene that does not move (walls,
props). So "a person" is simply depth that is nearer than the stored depth background by
more than a margin, cleaned up and grown by a safety border. The result is one or more
outlines in depth pixels; through the calibration they become projector-space meshes that
can be a live *mask* (keep light off the person) or a live *spotlight* (put media on them).
"""
from __future__ import annotations

import numpy as np

from promap.camera import kinect
from promap.camera.detect import Detection
from promap.model.mesh import Mesh
from promap.model.project import Surface

LIVE_ID = "people_live"


def people_mask(depth_mm: np.ndarray, background_mm: np.ndarray, step_mm: float = 100.0, margin_px: int = 12,
                min_mm: float = 400.0, max_mm: float = 5000.0) -> np.ndarray:
    """Bool mask of pixels that are `step_mm` nearer than the stored background, grown by margin_px."""
    import cv2

    fg = kinect.foreground_mask(depth_mm, background_mm, max_mm=max_mm, min_mm=min_mm, step_mm=step_mm)
    m = fg.astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
    if margin_px > 0:
        k = 2 * int(margin_px) + 1
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    return m > 0


def people_outlines(mask: np.ndarray, min_area: float = 2500.0, epsilon: float = 0.015) -> list[Detection]:
    return kinect.mask_to_contours(mask, min_area=min_area, epsilon=epsilon)


def outlines_to_mesh(dets: list[Detection], calibration) -> Mesh:
    """All outlines as one mesh in projector pixels (empty mesh when nobody is there)."""
    from promap.camera.detect import detections_to_meshes

    m = Mesh()
    for part in detections_to_meshes(dets, calibration):
        m.merge(part, tol=0.0)
    return m


def live_surface(project, kind: str = "mask") -> Surface:
    """The one surface that follows people; created on first use, kept at the top of the draw order."""
    s = project.surface_by_id(LIVE_ID)
    if s is None:
        s = Surface(id=LIVE_ID, name="People (live)", kind=kind, mesh=Mesh(), item_type="actor",
                    notes="updated from the depth camera; do not edit by hand")
        s.corners = [[0, 0], [1, 0], [1, 1], [0, 1]]
        project.surfaces.append(s)
    elif s.kind != kind:
        s.kind = kind
    if project.surfaces[-1] is not s:  # masks must draw last to cover everything
        project.surfaces.remove(s)
        project.surfaces.append(s)
    return s
