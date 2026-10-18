import math

from promap.model.project import Project
from promap.model.scene import Scene, SceneNode, Projector
from promap.model.geometry import clip_corners, rect_corners


def test_scene_roundtrip(tmp_path):
    p = Project()
    p.scene.projector.pos = [0, -4, 1]
    p.scene.projector.fov_h = 35
    p.scene.nodes.append(SceneNode(name="Wall", model="plane", scale=[3, 1, 2]))
    p.scene.nodes.append(SceneNode(name="Kinect", kind="camera", fov_h=58))
    path = p.save(tmp_path / "s.promap")
    q = Project.load(path)
    assert q.scene.projector.pos == [0, -4, 1]
    assert [n.name for n in q.scene.nodes] == ["Wall", "Kinect"]
    assert q.scene.cameras[0].name == "Kinect"


def test_old_project_without_scene_loads(tmp_path):
    (tmp_path / "old.promap").write_text('{"format": 1, "name": "old", "surfaces": [], "cues": []}')
    q = Project.load(tmp_path / "old.promap")
    assert isinstance(q.scene, Scene) and q.scene.nodes == []


def test_throw_ratio_round_trip():
    p = Projector(fov_h=40)
    assert math.isclose(Projector.fov_from_throw_ratio(p.throw_ratio), 40.0)
    assert 1.3 < p.throw_ratio < 1.4  # a 40 deg lens is a ~1.37:1 throw, a common short-ish projector


def test_clip_corners_divide_back():
    pts = clip_corners(rect_corners(0, 0, 1920, 1080), (1920, 1080))
    tl, tr, br, bl = [(r[0] / r[3], r[1] / r[3]) for r in pts]
    assert tl == (-1.0, 1.0) and br == (1.0, -1.0)
