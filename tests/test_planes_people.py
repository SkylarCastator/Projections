import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from promap.camera import planes, people, kinect1
from promap.model.project import Project


def synthetic_room():
    """Depth of a wall at 3 m with a box (front face at 2 m) and a floor, through the v1 intrinsics."""
    fx, fy, cx, cy = kinect1.INTRINSICS
    H, W = 480, 640
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    # wall: z = 3
    depth = np.full((H, W), 3000.0, np.float32)
    # floor: y = -0.8 m  ->  z = 0.8 * fy / (y - cy) for rows below the horizon (nearer than the wall in the bottom rows)
    rows = ys > cy + 20
    z_floor = 0.8 * fy / np.maximum(ys - cy, 1.0) * 1000.0
    depth[rows] = np.minimum(depth[rows], z_floor[rows])
    # box front face at z = 2 m, 0.6 x 0.8 m centred slightly left
    bx = (xs - cx) * 2.0 / fx
    by = -(ys - cy) * 2.0 / fy
    box = (np.abs(bx + 0.3) < 0.3) & (np.abs(by + 0.2) < 0.4)
    depth[box] = 2000.0
    return depth


def test_find_planes_recovers_wall_box_and_floor():
    depth = synthetic_room()
    xyz = kinect1.depth_to_points(depth, None, stride=1)[0]
    H, W = depth.shape
    grid = np.full((H, W, 3), np.nan, np.float32)
    ys, xs = np.mgrid[0:H, 0:W]
    ok = depth > 0
    grid[ok] = xyz
    found = planes.find_planes(grid, max_planes=4, min_pixels=3000)
    assert len(found) >= 3
    normals = [p.normal for p in found]
    # a wall (normal along -z), a floor (normal along +y), and the box face (also -z, offset 2 m)
    assert any(abs(n[2]) > 0.95 and abs(p.offset) == pytest.approx(3.0, abs=0.06) for n, p in zip(normals, found))
    assert any(abs(n[2]) > 0.95 and abs(p.offset) == pytest.approx(2.0, abs=0.06) for n, p in zip(normals, found))
    assert any(abs(n[1]) > 0.9 for n in normals)
    box = next(p for p in found if abs(p.normal[2]) > 0.95 and abs(p.offset) == pytest.approx(2.0, abs=0.06))
    assert 0.5 < max(box.extent) < 0.95 and 0.45 < min(box.extent) < 0.75
    assert box.outline is not None and len(box.outline) >= 4 and box.detection() is not None
    assert "wall" in box.label


def test_plane_to_node_places_prop_in_world():
    p = planes.Plane(normal=np.array([0, 0, -1.0], np.float32), offset=-2.0, mask=np.zeros((2, 2), bool),
                     centre=np.array([0.0, 0.0, 2.0], np.float32), axis_u=np.array([1.0, 0, 0], np.float32),
                     axis_v=np.array([0, 1.0, 0], np.float32), extent=(0.6, 0.8), pixels=10)
    node = planes.plane_to_node(p, camera_pos=[0.0, -3.0, 1.0], camera_hpr=[0.0, 0.0, 0.0], name="P")
    # camera at y=-3 looking +y: a plane 2 m ahead sits at y=-1, same height
    assert node.pos == pytest.approx([0.0, -1.0, 1.0], abs=1e-4)
    assert node.scale == pytest.approx([0.6, 1.0, 0.8])
    assert abs(node.hpr[0]) < 1e-3 or abs(abs(node.hpr[0]) - 180) < 1e-3
    assert node.model == "plane"


def test_people_mask_and_live_surface():
    bg = np.full((480, 640), 3000.0, np.float32)
    d = bg.copy()
    d[100:400, 250:350] = 1800.0  # a person 1.2 m in front of the wall
    m = people.people_mask(d, bg, margin_px=6)
    assert m[250, 300] and not m[50, 50]
    assert m[100 - 4, 300], "margin grows the mask beyond the silhouette"
    dets = people.people_outlines(m)
    assert len(dets) == 1
    mesh = people.outlines_to_mesh(dets, None)
    assert len(mesh.tris) >= 2 and mesh.hit((300, 250)) is not None
    proj = Project()
    s = people.live_surface(proj, "mask")
    s2 = people.live_surface(proj, "media")
    assert s is s2 and s.kind == "media" and proj.surfaces[-1] is s
