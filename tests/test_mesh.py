import pytest

from promap.model.geometry import quad_to_unit
from promap.model.mesh import Mesh, quad_grid, triangle_mesh, regular_polygon_mesh, point_in_triangle


def test_two_triangles_make_a_square():
    m = quad_grid([[0, 0], [100, 0], [100, 100], [0, 100]], 1)
    assert len(m.verts) == 4 and len(m.tris) == 2
    assert m.hit((50, 50)) is not None
    assert m.hit((150, 50)) is None
    assert m.bounds_corners() == [[0, 0], [100, 0], [100, 100], [0, 100]]
    # corner texture coords are the unit square
    assert sorted(m.uvs()) == [[0.0, 0.0], [0.0, 1.0], [1.0, 0.0], [1.0, 1.0]]


def test_quad_grid_is_perspective_correct_at_vertices():
    corners = [[200, 100], [600, 100], [700, 500], [100, 500]]  # keystone
    m = quad_grid(corners, 4)
    assert len(m.tris) == 32
    for x, y, u, v in m.verts:
        uu, vv = quad_to_unit(corners, (x, y))
        assert pytest.approx(uu, abs=1e-6) == u and pytest.approx(vv, abs=1e-6) == v


def test_extrude_shares_edge_and_continues_uv():
    m = triangle_mesh([[0, 0], [100, 0], [0, 100]])
    t = m.extrude_edge((0, 1))  # edge from (100,0) to (0,100), opposite (0,0)
    assert len(m.verts) == 4 and len(m.tris) == 2
    new = m.verts[-1]
    assert new[:2] == [100.0, 100.0]  # parallelogram completion
    assert new[2:] == [1.0, 1.0]  # affine extension of the uvs too
    assert set(m.tris[t]) & set(m.tris[0]) == {1, 2}  # two shared vertex indices


def test_delete_and_detach_reindex():
    m = quad_grid([[0, 0], [100, 0], [100, 100], [0, 100]], 2)
    n_verts = len(m.verts)
    m.delete_triangle(0)
    assert len(m.tris) == 7 and len(m.verts) == n_verts  # no orphan yet: all verts still used
    part = m.detach([0, 1])
    assert len(part.tris) == 2 and len(m.tris) == 5
    assert max(i for t in part.tris for i in t) < len(part.verts)
    assert max(i for t in m.tris for i in t) < len(m.verts)


def test_merge_welds_coincident_vertices():
    a = triangle_mesh([[0, 0], [100, 0], [0, 100]])
    b = triangle_mesh([[100, 0], [100, 100], [0, 100]])
    a.merge(b)
    assert len(a.tris) == 2 and len(a.verts) == 4


def test_weld_vertex_drops_collapsed_triangle():
    m = triangle_mesh([[0, 0], [100, 0], [0, 100]])
    m.weld_vertex(0, 1)
    assert m.tris == []


def test_subdivide_shares_midpoints():
    m = quad_grid([[0, 0], [100, 0], [100, 100], [0, 100]], 1)
    m.subdivide()
    assert len(m.tris) == 8
    assert len(m.verts) == 4 + 5  # 4 outer midpoints + the shared diagonal midpoint


def test_fit_uvs_and_hit_and_edges():
    m = regular_polygon_mesh(100, 100, 50, 6)
    assert len(m.tris) == 6 and m.hit((100, 100)) is not None
    m.translate(10, 0)
    assert pytest.approx(m.centroid()[0]) == 110.0
    m.fit_uvs()
    assert min(v[2] for v in m.verts) == 0.0 and max(v[2] for v in m.verts) == 1.0
    e = m.nearest_edge((110, 51), 5.0)
    assert e is not None
    s = m.scaled_about(110, 100, 2.0)
    assert pytest.approx(s.bounds()[2] - s.bounds()[0]) == 2 * (m.bounds()[2] - m.bounds()[0])


def test_point_in_triangle_edges():
    assert point_in_triangle((0, 0), (10, 0), (0, 10), (1, 1))
    assert point_in_triangle((0, 0), (10, 0), (0, 10), (5, 0))
    assert not point_in_triangle((0, 0), (10, 0), (0, 10), (9, 9))


def test_roundtrip_dict():
    m = quad_grid([[0, 0], [10, 0], [10, 10], [0, 10]], 1)
    from dataclasses import asdict

    d = asdict(m)
    m2 = Mesh.from_dict(d)
    assert m2.verts == m.verts and m2.tris == m.tris
