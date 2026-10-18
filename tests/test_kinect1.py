import numpy as np
import pytest

from promap.camera import kinect1, kinect


def test_status_functions_do_not_raise():
    assert isinstance(kinect1.status(), str)
    assert isinstance(kinect1.plugged_in(), bool)
    assert isinstance(kinect1.motor_only(), bool)
    ok, why = kinect1.binding_available()
    assert isinstance(ok, bool) and isinstance(why, str)


def test_adapter_exposes_rgb_and_depth_on_same_grid():
    class FakeK:
        alive = True

        def __init__(self):
            self.n = 0

        def latest(self):
            self.n += 1
            rgb = np.zeros((480, 640, 3), np.uint8)
            depth = np.full((480, 640), 3000.0, np.float32)
            depth[100:200, 100:200] = 1200.0
            return kinect1.V1Frame(rgb, depth, self.n), self.n

        def close(self):
            self.alive = False

    a = kinect1.KinectV1CameraAdapter(FakeK())
    frame, seq = a.latest()
    assert frame.shape == (480, 640, 3) and seq == 1 and a.size == (640, 480)
    d = a.latest_depth()
    assert d.shape == (480, 640)
    fg = kinect.foreground_mask(d, np.full_like(d, 3000.0))
    dets = kinect.mask_to_contours(fg, min_area=100)
    assert len(dets) == 1 and dets[0].bbox[0] == pytest.approx(100, abs=3)
    assert a.wait_fresh(0).shape == (480, 640, 3)
    a.close()
    assert not a.alive


def test_median_ignoring_zeros_matches_nanmedian_and_is_fast():
    import time

    rng = np.random.default_rng(0)
    frames = [rng.uniform(500, 4000, (480, 640)).astype(np.float32) for _ in range(3)]
    for f in frames:
        f[rng.random((480, 640)) < 0.2] = 0.0
    t0 = time.perf_counter()
    fast = kinect1.median_ignoring_zeros(frames)
    dt = time.perf_counter() - t0
    stack = np.where(np.stack(frames) > 0, np.stack(frames), np.nan)
    with np.errstate(all="ignore"):
        ref = np.nan_to_num(np.nanmedian(stack, axis=0), nan=0.0)
    # with an even count of valid samples nanmedian averages the middle two; ours takes the lower: allow either
    valid = (np.stack(frames) > 0).sum(axis=0)
    assert np.allclose(fast[valid != 2], ref[valid != 2])
    assert (fg := fast[valid == 2]).size == 0 or np.all(fg <= ref[valid == 2] + 1e-3)
    assert dt < 0.1
