import json

from promap.model.project import Project, Surface, Cue


def test_roundtrip(tmp_path):
    p = Project(name="Spooky", output_width=1280, output_height=720)
    s = Surface(name="Pumpkin", media="media/pumpkin.gif", corners=[[1, 2], [3, 4], [5, 6], [7, 8]])
    m = Surface(name="Door mask", kind="mask")
    p.surfaces += [s, m]
    p.cues.append(Cue(name="Open", media={s.id: "media/pumpkin.gif", m.id: None}, fade=2.0, hold=5.0))
    path = p.save(tmp_path / "show.json")
    q = Project.load(path)
    assert q.name == "Spooky" and q.output_size == (1280, 720)
    assert [x.name for x in q.surfaces] == ["Pumpkin", "Door mask"]
    assert q.surfaces[0].corners == [[1, 2], [3, 4], [5, 6], [7, 8]]
    assert q.cues[0].media[s.id] == "media/pumpkin.gif"
    assert q.cues[0].hold == 5.0
    assert q.resolve_media("media/pumpkin.gif") == tmp_path / "media" / "pumpkin.gif"
    assert json.loads(path.read_text())["format"] == 1


def test_relativize(tmp_path):
    p = Project()
    p.path = tmp_path / "show.json"
    (tmp_path / "media").mkdir()
    f = tmp_path / "media" / "a.png"
    f.write_bytes(b"")
    assert p.relativize_media(f) == "media/a.png"
