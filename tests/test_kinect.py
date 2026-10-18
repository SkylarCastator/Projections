import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from promap.camera import kinect


def test_status_and_link_speed_do_not_raise():
    s = kinect.status()
    assert isinstance(s, str) and s
    sp = kinect.usb_link_speed()
    assert sp is None or sp > 0


def test_depth_image_and_foreground_mask():
    depth = np.full((424, 512), 3000.0, np.float32)  # wall at 3 m
    depth[100:200, 150:250] = 1500.0  # a prop at 1.5 m
    depth[0:10, :] = 0  # no return
    img = kinect.depth_to_image(depth)
    assert img.shape == (424, 512, 3) and tuple(img[5, 5]) == (0, 0, 0)
    assert img[150, 200, 0] > img[300, 300, 0], "nearer is warmer (more red)"
    fg = kinect.foreground_mask(depth, background_mm=np.full_like(depth, 3000.0))
    assert fg[150, 200] and not fg[300, 300] and not fg[5, 5]
    fg2 = kinect.foreground_mask(depth, max_mm=2000.0)
    assert fg2[150, 200] and not fg2[300, 300]
    dets = kinect.mask_to_contours(fg, min_area=100)
    assert len(dets) == 1
    x, y, w, h = dets[0].bbox
    assert abs(x - 150) <= 3 and abs(y - 100) <= 3 and abs(w - 100) <= 4 and abs(h - 100) <= 4


def test_points_from_depth_pinhole():
    class Params:
        fx = fy = 365.0
        cx, cy = 256.0, 212.0

    class FakeDevice:
        ir_camera_params = Params()

    k = kinect.Kinect.__new__(kinect.Kinect)
    k.device = FakeDevice()
    depth = np.zeros((424, 512), np.float32)
    depth[212, 256] = 2000.0  # principal point, 2 m
    depth[212, 0] = 1000.0
    pts = k.points_from_depth(depth)
    assert np.allclose(pts[212, 256], [0.0, 0.0, 2.0])
    assert np.isnan(pts[0, 0]).all()
    x, y, z = pts[212, 0]
    assert pytest.approx(z) == 1.0 and x < 0 and abs(x) == pytest.approx(256 / 365.0, rel=1e-3)


def test_plane_background_fits_tilted_wall_and_ignores_prop():
    H, W = 240, 320
    gy, gx = np.mgrid[0:H, 0:W].astype(np.float32)
    wall = 2500.0 + 2.0 * gx - 1.0 * gy  # a wall receding to the right
    depth = wall.copy()
    depth[80:160, 100:180] = wall[80:160, 100:180] - 400.0  # a box 40 cm in front
    depth[0:5, :] = 0
    bg = kinect.plane_background(depth)
    assert bg is not None and bg.shape == depth.shape
    assert np.abs(bg[200, 300] - wall[200, 300]) < 5.0, "the prop did not tilt the plane"
    assert np.abs(bg[120, 140] - wall[120, 140]) < 5.0
    fg = kinect.foreground_mask(depth, bg)
    assert fg[120, 140] and not fg[200, 300] and not fg[2, 2]
