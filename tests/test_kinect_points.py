import numpy as np
import pytest

from promap.camera import kinect1


def test_depth_to_points_geometry_and_colours():
    fx, fy, cx, cy = kinect1.INTRINSICS
    depth = np.zeros((480, 640), np.float32)
    depth[int(cy), int(cx)] = 2000.0  # straight ahead, 2 m
    depth[int(cy), 0] = 1000.0  # far left, 1 m
    depth[0, 0] = 9000.0  # beyond max range: dropped
    rgb = np.zeros((480, 640, 3), np.uint8)
    rgb[int(cy), int(cx)] = (10, 20, 30)
    xyz, col = kinect1.depth_to_points(depth, rgb, stride=1)
    assert xyz.shape == (2, 3) and col.shape == (2, 3)
    ahead = xyz[np.argmin(np.abs(xyz[:, 2] - 2.0))]
    assert np.allclose(ahead, [(int(cx) - cx) * 2 / fx, -(int(cy) - cy) * 2 / fy, 2.0], atol=1e-3)
    left = xyz[np.argmin(np.abs(xyz[:, 2] - 1.0))]
    assert left[0] < 0 and pytest.approx(left[0], rel=1e-3) == -cx / fx
    assert (10, 20, 30) in [tuple(c) for c in col.tolist()]


def test_depth_to_points_stride_subsamples():
    depth = np.full((480, 640), 1500.0, np.float32)
    xyz, col = kinect1.depth_to_points(depth, None, stride=4)
    assert len(xyz) == (480 // 4) * (640 // 4) and col.shape == (len(xyz), 3)
    assert np.allclose(xyz[:, 2], 1.5)
