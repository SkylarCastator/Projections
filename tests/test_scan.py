import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from promap.camera import scan, graycode

CAM = (160, 120)


def fake_scene_captures():
    """A grey wall, a red box (same brightness as the wall) and a dark blob, lit by each pattern."""
    W, H = CAM
    refl = np.full((H, W, 3), 0.6, np.float32)  # wall: neutral
    refl[40:90, 20:60] = (0.7, 0.1, 0.1)  # red box
    refl[30:70, 100:140] = 0.08  # dark blob
    ambient = 15.0
    lit = np.zeros((H, W), bool)
    lit[10:110, 10:150] = True  # projector footprint
    caps = []
    for name in scan.SCAN_NAMES:
        light = {"black": (0, 0, 0), "white": (1, 1, 1), "red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1)}.get(name)
        if light is None:  # stripes: 3-px bands, inverse shifted
            stripe = ((np.arange(W) // 3) % 2).astype(np.float32)
            if name.endswith("_inv"):
                stripe = 1 - stripe
            light = np.repeat(stripe[None, :, None], H, axis=0) * np.ones(3)
        img = ambient + 220.0 * refl * np.asarray(light, np.float32) * lit[:, :, None]
        caps.append(np.clip(img, 0, 255))
    return caps, refl, lit


def test_scan_patterns_shapes():
    for i in range(len(scan.SCAN_NAMES)):
        p = scan.scan_pattern_rgb(i, 64, 32)
        assert p.shape == (32, 64, 3) and p.dtype == np.uint8
    s = scan.scan_pattern_rgb(scan.SCAN_NAMES.index("stripes"), 64, 32, period=8)
    si = scan.scan_pattern_rgb(scan.SCAN_NAMES.index("stripes_inv"), 64, 32, period=8)
    assert np.array_equal(255 - s, si) and s[0, :8, 0].tolist() == [0, 0, 0, 0, 255, 255, 255, 255]


def test_scan_features_and_segmentation():
    caps, refl, lit = fake_scene_captures()
    sc = scan.SceneScan.from_captures(caps, calibration=None)
    assert sc.lit[50, 80] and not sc.lit[2, 2]
    # albedo: wall bright, dark blob dark
    assert sc.albedo[50, 80] > 0.6 and sc.albedo[50, 120] < 0.15
    # colour: red box is red, wall is neutral
    assert sc.color[60, 40, 0] > 0.7 and abs(sc.color[50, 80, 0] - 1 / 3) < 0.05
    # direct light dominates in this synthetic scene (no bounce)
    assert sc.direct[50, 80] > 0.9
    dets = sc.segment(k=3, min_area=100, drop_largest=True)
    labels = {d.label for d in dets}
    assert len(dets) >= 2 and len(labels) >= 2
    areas = sorted(d.area for d in dets)
    assert areas[-1] < 0.5 * lit.sum(), "the wall itself was dropped"
    for d in dets:
        x, y, w, h = d.bbox
        assert lit[y + h // 2, x + w // 2]
    for name in ("albedo", "color", "direct", "depth", "shadow", "segments", "camera"):
        v = sc.view(name)
        assert v.shape == (CAM[1], CAM[0], 3) and v.dtype == np.uint8


def test_scan_depth_from_calibration_residual():
    """A region whose dense map sits off the wall homography gets a non-zero depth."""
    W, H = CAM
    Hm = np.array([[2.0, 0.0, 10.0], [0.0, 2.0, 5.0], [0.0, 0.0, 1.0]])
    ys, xs = np.mgrid[0:H, 0:W]
    px = (2.0 * xs + 10).astype(np.int32)
    py = (2.0 * ys + 5).astype(np.int32)
    py[40:80, 40:80] += 6  # a box "in front": parallax shifts it vertically
    valid = np.ones((H, W), bool)
    cal = graycode.Calibration(CAM, (400, 300), Hm, 0.9, px, py, valid)
    caps, _, _ = fake_scene_captures()
    sc = scan.SceneScan.from_captures(caps, calibration=cal)
    assert abs(sc.depth[100, 100]) < 0.5
    assert abs(sc.depth[60, 60]) > 2.0
    assert sc.roi[60, 60]


def test_scan_roundtrip(tmp_path):
    caps, _, _ = fake_scene_captures()
    sc = scan.SceneScan.from_captures(caps)
    sc.save(tmp_path / "s.npz")
    back = scan.SceneScan.load(tmp_path / "s.npz")
    assert back is not None and np.allclose(back.albedo, sc.albedo) and back.camera_frame is not None
    assert scan.SceneScan.load(tmp_path / "missing.npz") is None
