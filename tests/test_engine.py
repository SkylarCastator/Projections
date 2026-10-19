import numpy as np
from PIL import Image

from promap.model.project import Project, Surface, Cue
from promap.media.sources import AnimatedImageSource, open_source
from promap.show.engine import ShowEngine


def make_gif(path, n=4, ms=100):
    frames = [Image.new("RGB", (8, 8), (i * 40, 0, 0)) for i in range(n)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=ms, loop=0)


def make_png(path, color=(0, 255, 0)):
    Image.new("RGB", (8, 8), color).save(path)


def test_gif_frame_timing(tmp_path):
    g = tmp_path / "a.gif"
    make_gif(g)
    src = AnimatedImageSource(g)
    assert src.duration == 0.4
    assert src.frame_at(0.0)[1] == 0
    assert src.frame_at(0.15)[1] == 1
    assert src.frame_at(0.39)[1] == 3


def test_crossfade_between_cues(tmp_path):
    make_png(tmp_path / "a.png", (255, 0, 0))
    make_png(tmp_path / "b.png", (0, 0, 255))
    p = Project(path=tmp_path / "show.json")
    s = Surface(name="S", media="a.png")
    p.surfaces.append(s)
    p.cues.append(Cue.from_assignments("A", {s.id: "a.png"}, fade=0.0))
    p.cues.append(Cue.from_assignments("B", {s.id: "b.png"}, fade=1.0))
    eng = ShowEngine(p)
    # preview mode shows surface.media
    items = eng.render_items()
    assert len(items) == 1 and items[0].frame[0, 0, 0] == 255
    eng.goto(0)
    eng.tick()
    assert len([l for l in eng.layers if l.fade_out_at is None]) == 1
    eng.go()  # crossfade to b over 1s
    t0 = eng.layers[-1].started
    items = eng.render_items(t0 + 0.5)
    assert len(items) == 2
    ops = sorted(round(i.opacity, 2) for i in items)
    assert ops == [0.5, 0.5]
    eng.tick(t0 + 1.5)
    items = eng.render_items(t0 + 1.5)
    assert len(items) == 1 and items[0].frame[0, 0, 2] == 255


def test_missing_media_goes_dark_not_crash(tmp_path):
    p = Project(path=tmp_path / "show.json")
    s = Surface(name="S", media="nope.png")
    p.surfaces.append(s)
    eng = ShowEngine(p)
    assert eng.render_items() == []
    assert "nope.png" in eng.load_errors


def test_hold_auto_advances(tmp_path):
    make_png(tmp_path / "a.png")
    p = Project(path=tmp_path / "show.json")
    s = Surface(name="S")
    p.surfaces.append(s)
    p.cues += [Cue.from_assignments("1", {s.id: "a.png"}, hold=0.5), Cue.from_assignments("2", {s.id: None})]
    eng = ShowEngine(p)
    eng.goto(0)
    eng.tick(eng._cue_entered_at + 0.1)
    assert eng.cue_index == 0
    eng.tick(eng._cue_entered_at + 0.6)
    assert eng.cue_index == 1
