import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from promap.camera import graycode, detect
from promap.model.mesh import Mesh


PROJ = (160, 96)
CAM = (200, 150)
# a camera that sees the projector image tilted and shifted (camera px -> projector px)
H_TRUE = np.array([[0.9, 0.08, -10.0], [-0.05, 0.85, 12.0], [0.0002, 0.0001, 1.0]])


def simulate_capture(pattern: np.ndarray) -> np.ndarray:
    """What the camera would see: the projected pattern warped into the camera, dim ambient, noise."""
    rng = np.random.default_rng(int(pattern.sum()) % 1000)
    seen = cv2.warpPerspective(pattern, np.linalg.inv(H_TRUE), CAM, flags=cv2.INTER_NEAREST, borderValue=0)
    img = 20.0 + 0.6 * seen.astype(np.float32) + rng.normal(0, 2.0, seen.shape)
    return np.clip(img, 0, 255)


def test_pattern_sequence_shape_and_inverse():
    w, h = PROJ
    n = graycode.pattern_count(w, h)
    assert n == len(graycode.pattern_names(w, h))
    assert graycode.pattern(0, w, h).min() == 255 and graycode.pattern(1, w, h).max() == 0
    for i in range(2, n, 2):
        a, b = graycode.pattern(i, w, h), graycode.pattern(i + 1, w, h)
        assert a.shape == (h, w) and np.array_equal(255 - a, b)


def test_decode_recovers_projector_coordinates():
    w, h = PROJ
    caps = [simulate_capture(graycode.pattern(i, w, h)) for i in range(graycode.pattern_count(w, h))]
    px, py, valid = graycode.decode(caps, w, h)
    assert valid.mean() > 0.3
    ys, xs = np.nonzero(valid)
    pts = np.stack([xs, ys], axis=1).astype(np.float32).reshape(-1, 1, 2)
    expect = cv2.perspectiveTransform(pts, H_TRUE).reshape(-1, 2)
    err = np.abs(np.stack([px[ys, xs], py[ys, xs]], axis=1) - expect)
    assert np.median(err) <= 1.0
    cal = graycode.Calibration.from_decoded(px, py, valid, CAM, PROJ, step=2)
    assert cal.inlier_ratio > 0.8
    # the homography maps the camera's view of the projector frame back onto itself
    corners = cal.projector_outline_in_camera()
    back = cv2.perspectiveTransform(corners.reshape(-1, 1, 2), cal.H).reshape(-1, 2)
    assert np.allclose(back, [[0, 0], [w, 0], [w, h], [0, h]], atol=2.0)
    # dense map beats the homography where decoded, and falls back where not
    k = len(xs) // 2  # a pixel well inside the lit region, away from the footprint edge
    mapped = cal.map_points([[xs[k], ys[k]], [-50, -50]])
    assert np.allclose(mapped[0], expect[k], atol=2.0)
    assert np.isfinite(mapped[1]).all()


def test_calibration_roundtrip(tmp_path):
    cal = graycode.Calibration(CAM, PROJ, H_TRUE, 0.9, camera_name="test cam")
    d = cal.to_dict()
    back = graycode.Calibration.from_dict(d)
    assert np.allclose(back.H, H_TRUE) and back.camera_name == "test cam"
    cal.px = np.zeros((2, 2), np.int32)
    cal.py = np.zeros((2, 2), np.int32)
    cal.valid = np.ones((2, 2), bool)
    cal.save_dense(tmp_path / "dense.npz")
    back.load_dense(tmp_path / "dense.npz")
    assert back.valid.all()


def test_triangulate_concave_polygon_covers_area():
    # an L shape: 6 vertices, concave corner at (50, 50)
    poly = np.array([[0, 0], [100, 0], [100, 50], [50, 50], [50, 100], [0, 100]], float)
    tris = detect.triangulate(poly)
    assert len(tris) == 4
    area = sum(abs(detect._area2(poly[a], poly[b], poly[c])) / 2 for a, b, c in tris)
    assert pytest.approx(area) == 7500.0
    # clockwise input works too
    tris_cw = detect.triangulate(poly[::-1])
    assert len(tris_cw) == 4
    m = detect.polygon_mesh(poly)
    assert isinstance(m, Mesh) and len(m.tris) == 4 and m.hit((25, 75)) is not None and m.hit((75, 75)) is None


def test_segment_finds_bright_and_dark_blobs():
    img = np.full((240, 320, 3), 30, np.uint8)
    cv2.rectangle(img, (40, 40), (120, 160), (220, 220, 220), -1)
    cv2.circle(img, (230, 120), 40, (200, 200, 200), -1)
    dets = detect.segment(img, "bright", min_area=200)
    assert len(dets) == 2 and dets[0].area > dets[1].area
    assert 3 <= len(dets[0].contour) <= 6  # the rectangle simplifies to ~4 corners
    inv = 255 - img
    assert len(detect.segment(inv, "dark", min_area=200)) == 2
    bg = np.full_like(img, 30)
    assert len(detect.segment(img, "diff", background=bg, min_area=200)) == 2
    meshes = detect.detections_to_meshes(dets, None)
    assert len(meshes) == 2 and all(m.tris for m in meshes)
