import math

from promap.model.hardware import Device, HardwareLibrary, seed_devices
from promap.model.project import Project, Surface
from promap.show.engine import ShowEngine


def test_library_roundtrip(tmp_path):
    lib = HardwareLibrary(devices=seed_devices())
    lib.add(Device(name="Epson", kind="projector", make="Epson", model="EB-X49", throw_ratio=1.48))
    p = lib.save(tmp_path / "hw.json")
    lib2 = HardwareLibrary.load(p)
    assert [d.name for d in lib2.devices][-1] == "Epson"
    assert lib2.by_id("kinectv2").depth_max == 4.5


def test_missing_library_seeds(tmp_path):
    lib = HardwareLibrary.load(tmp_path / "none.json")
    assert any(d.id == "kinectv2" for d in lib.devices)


def test_fov_from_throw():
    d = Device(throw_ratio=1.5, fov_h=None)
    assert math.isclose(d.horizontal_fov, math.degrees(2 * math.atan(1 / 3)))
    assert Device(kind="camera", throw_ratio=None, fov_h=70.6).horizontal_fov == 70.6


def test_reference_surface_never_reaches_output(tmp_path):
    from PIL import Image

    Image.new("RGB", (4, 4), (255, 255, 255)).save(tmp_path / "room.png")
    p = Project(path=tmp_path / "s.promap")
    ref = Surface(name="ref", kind="reference", media="room.png", locked=True, item_type="wall")
    p.surfaces.append(ref)
    eng = ShowEngine(p)
    items = eng.render_items()
    assert [i.surface.kind for i in items] == ["reference"]
    assert [i for i in items if i.surface.is_output] == []
    q = Project.load(p.save())
    assert q.surfaces[0].locked and q.surfaces[0].item_type == "wall"
