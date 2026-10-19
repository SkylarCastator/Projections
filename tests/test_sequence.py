"""Cues as sequences: clips on tracks, the cue clock, fades, seek / pause / loop."""
import time

from PIL import Image

from promap.model.project import Project, Surface, Cue, Clip
from promap.show.engine import ShowEngine


def png(path, color):
    Image.new("RGB", (8, 8), color).save(path)


def test_cue_model_queries_and_migration():
    c = Cue(name="seq", clips=[
        Clip(surface_id="s", media="a.png", start=0, duration=4, fade_in=1),
        Clip(surface_id="s", media="b.png", start=3, duration=4),  # overlaps the tail of a
        Clip(surface_id="t", media="c.png", start=1, duration=1),
    ])
    assert c.total == 7
    assert c.media_at("s", 0.5) == "a.png" and c.media_at("s", 3.5) == "b.png" and c.media_at("s", 6.99) == "b.png"
    assert c.media_at("s", 7) is None and c.media_at("t", 0.5) is None and c.media_at("t", 1.5) == "c.png"
    assert [x.media for x in c.track("s")] == ["a.png", "b.png"]
    assert c.assignments(1.5) == {"s": "a.png", "t": "c.png"}
    c.length = 20
    assert c.total == 20
    c.set_track("s", "z.png")
    assert [x.media for x in c.track("s")] == ["z.png"] and c.track("s")[0].duration == 20 and c.track("s")[0].fade_in == 1
    legacy = Cue.from_dict({"name": "old", "media": {"s": "a.png", "t": None}, "fade": 0.5, "hold": 6})
    assert legacy.total == 6 and [x.surface_id for x in legacy.clips] == ["s"] and legacy.clips[0].fade_in == 0.5
    again = Cue.from_dict(legacy.to_dict())
    assert again.clips == legacy.clips and "media" not in legacy.to_dict()


def _project(tmp_path):
    png(tmp_path / "a.png", (255, 0, 0))
    png(tmp_path / "b.png", (0, 0, 255))
    p = Project(path=tmp_path / "show.json")
    s = Surface(name="S", media="a.png")
    p.surfaces.append(s)
    return p, s


def test_clips_play_in_sequence_with_fades(tmp_path):
    p, s = _project(tmp_path)
    cue = Cue(name="seq", length=6, loop=False, fade=0.0, clips=[
        Clip(surface_id=s.id, media="a.png", start=0, duration=2, fade_in=0, fade_out=1),
        Clip(surface_id=s.id, media="b.png", start=3, duration=2, fade_in=1),
    ])
    p.cues.append(cue)
    eng = ShowEngine(p)
    eng.goto(0)
    t0 = eng._cue_entered_at
    red = lambda items: [i for i in items if i.frame[0, 0, 0] == 255]
    blue = lambda items: [i for i in items if i.frame[0, 0, 2] == 255]
    eng.tick(t0 + 0.5)
    it = eng.render_items(t0 + 0.5)
    assert len(red(it)) == 1 and round(red(it)[0].opacity, 2) == 1.0
    eng.tick(t0 + 1.5)  # inside a's fade-out (last second)
    it = eng.render_items(t0 + 1.5)
    assert round(red(it)[0].opacity, 2) == 0.5
    eng.tick(t0 + 2.5)  # gap: dark
    assert eng.render_items(t0 + 2.5) == []
    eng.tick(t0 + 3.5)  # b fading in
    it = eng.render_items(t0 + 3.5)
    assert len(it) == 1 and round(blue(it)[0].opacity, 2) == 0.5
    eng.tick(t0 + 5.5)  # past b: dark, and no loop -> stays at the end
    assert eng.render_items(t0 + 5.5) == []
    eng.tick(t0 + 7.0)
    assert eng.cue_time(t0 + 7.0) == 6 and eng.render_items(t0 + 7.0) == []


def test_loop_restarts_clips(tmp_path):
    p, s = _project(tmp_path)
    p.cues.append(Cue(name="loop", length=2, clips=[Clip(surface_id=s.id, media="a.png", start=0, duration=1)]))
    eng = ShowEngine(p)
    eng.goto(0)
    t0 = eng._cue_entered_at
    eng.tick(t0 + 0.5)
    first = [l for l in eng.layers if l.fade_out_at is None][0]
    eng.tick(t0 + 1.5)
    assert eng.render_items(t0 + 1.5) == []
    eng.tick(t0 + 2.5)  # second pass: the clip plays again as a fresh layer
    live = [l for l in eng.layers if l.fade_out_at is None]
    assert len(live) == 1 and live[0] is not first and round(eng.cue_time(t0 + 2.5), 2) == 0.5
    assert len(eng.render_items(t0 + 2.5)) == 1


def test_seek_and_pause(tmp_path):
    p, s = _project(tmp_path)
    p.cues.append(Cue(name="seq", length=10, fade=0.0, clips=[
        Clip(surface_id=s.id, media="a.png", start=0, duration=4, fade_in=2),
        Clip(surface_id=s.id, media="b.png", start=6, duration=4),
    ]))
    eng = ShowEngine(p)
    eng.goto(0, at=0.0, paused=True)
    assert eng.paused and eng.cue_time() == 0.0
    eng.seek(7.0)
    eng.tick()
    it = eng.render_items()
    assert len(it) == 1 and it[0].frame[0, 0, 2] == 255 and it[0].opacity == 1.0
    eng.seek(3.0)  # scrubbing back into a: no fade-in replay, the envelope is already complete
    eng.tick()
    it = eng.render_items()
    assert len(it) == 1 and it[0].frame[0, 0, 0] == 255 and round(it[0].opacity, 2) == 1.0
    eng.seek(1.0)
    eng.tick()
    assert round(eng.render_items()[0].opacity, 2) == 0.5  # halfway through a 2 s fade-in
    time.sleep(0.05)
    assert eng.cue_time() == 1.0  # paused: the clock does not move
    eng.play()
    assert not eng.paused and abs(eng.cue_time() - 1.0) < 0.05
    eng.pause()
    t = eng.cue_time()
    time.sleep(0.03)
    assert eng.cue_time() == t
    eng.toggle_play()
    assert not eng.paused


def test_length_and_hold_stay_together():
    c = Cue(name="x", hold=4.0, clips=[Clip(surface_id="s", media="a.png", start=0, duration=4)])
    assert c.total == 4 and c.end_mode == "next"
    c.extend_to(8.0)  # a clip dragged past the end
    assert c.total == 8 and c.hold == 8 and c.length == 8
    c.set_length(6.0)
    assert c.hold == 6
    c.set_end_mode("loop")
    assert c.hold is None and c.loop and c.total == 6
    c.set_end_mode("stop")
    assert not c.loop and c.end_mode == "stop"
    c.set_end_mode("next")
    assert c.hold == 6 and c.end_mode == "next"


def test_next_cue_fires_at_the_sequence_end(tmp_path):
    p, s = _project(tmp_path)
    p.cues += [Cue(name="1", hold=0.5, length=1.0, clips=[Clip(surface_id=s.id, media="a.png", start=0, duration=1)]),
               Cue(name="2")]
    eng = ShowEngine(p)
    eng.goto(0)
    t0 = eng._cue_entered_at
    eng.tick(t0 + 0.7)  # past the stale hold, before the length: still cue 1
    assert eng.cue_index == 0
    eng.tick(t0 + 1.01)
    assert eng.cue_index == 1
