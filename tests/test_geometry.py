import numpy as np
import pytest

from promap.model.geometry import (
    homography_unit_square_to_quad,
    homogeneous_corners,
    point_in_quad,
    quad_to_unit,
    is_convex,
    rect_corners,
)

QUADS = [
    rect_corners(100, 100, 400, 300),
    [[0, 0], [500, 50], [450, 400], [30, 380]],  # generic convex quad
    [[200, 100], [600, 100], [700, 500], [100, 500]],  # trapezoid (keystone)
]


@pytest.mark.parametrize("corners", QUADS)
def test_homography_hits_corners(corners):
    H = homography_unit_square_to_quad(corners)
    for (u, v), (x, y) in zip([(0, 0), (1, 0), (1, 1), (0, 1)], corners):
        px, py, pw = H @ np.array([u, v, 1.0])
        assert pytest.approx(px / pw, abs=1e-6) == x
        assert pytest.approx(py / pw, abs=1e-6) == y


@pytest.mark.parametrize("corners", QUADS)
def test_homogeneous_corners_divide_back(corners):
    hc = homogeneous_corners(corners)
    for row, (x, y) in zip(hc, corners):
        assert pytest.approx(row[0] / row[2], abs=1e-6) == x
        assert pytest.approx(row[1] / row[2], abs=1e-6) == y


def test_inverse_and_hit_test():
    corners = QUADS[1]
    cx = sum(c[0] for c in corners) / 4
    cy = sum(c[1] for c in corners) / 4
    assert point_in_quad(corners, (cx, cy))
    assert not point_in_quad(corners, (-50, -50))
    u, v = quad_to_unit(corners, corners[2])
    assert pytest.approx(u, abs=1e-6) == 1.0 and pytest.approx(v, abs=1e-6) == 1.0


def test_convexity():
    assert is_convex(QUADS[1])
    assert not is_convex([[0, 0], [100, 0], [20, 20], [0, 100]])
