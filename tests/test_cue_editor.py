from PySide6.QtCore import QPointF, Qt
import pytest


@pytest.fixture
def ed(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from promap.ui.state import AppState
    from promap.ui.cue_editor import CueEditor

    st = AppState()
    st.MERGE_S = 0.0  # every edit is its own undo step in this test
    st.project.path = tmp_path / "show.promap"
    s = st.add_surface("media")
    s.name = "Wall"
    s.media = "gen:glitch" if "glitch" in __import__("promap.media.generative", fromlist=["EFFECTS"]).EFFECTS else None
    st.select(s.id)
    e = CueEditor(st)
    e.resize(1000, 300)
    return st, e, s


def test_editor_builds_cues_and_clips(ed):
    st, e, s = ed
    assert e.cue() is None and not e.add_clip_btn.isEnabled()
    e.add_cue()
    cue = e.cue()
    assert cue is not None and st.engine.cue_index == 0 and st.engine.paused and e.on_this_cue()
    assert e.cue_combo.count() == 1 and e.length.value() == 10.0 and e.loop.isChecked()
    c = e.add_clip(s.id, "media/a.mp4", at=2.0)
    assert c.start == 2.0 and c.duration == 5.0 and e.view.selected == c.id
    assert e.spins["start"].value() == 2.0 and e.clip_label.text().startswith("Clip: a.mp4")
    # a second clip on the same track stops at the next one
    d = e.add_clip(s.id, "media/b.png", at=0.0)
    assert d.duration == 2.0
    # the inspector writes back, with fades clamped to the clip
    e.view.select_clip(c.id)
    e.spins["fade_in"].setValue(9.0)
    assert c.fade_in == 5.0
    e.spins["duration"].setValue(3.0)
    assert c.duration == 3.0 and c.fade_in == 5.0  # (fade clamp happens on the next fade edit)
    # split at the playhead
    e.seek(3.5)
    assert abs(st.engine.cue_time() - 3.5) < 1e-6
    e.view.select_clip(c.id)
    e.split_at_playhead()
    parts = cue.track(s.id)
    assert [round(x.start, 2) for x in parts] == [0.0, 2.0, 3.5] and round(parts[1].duration, 2) == 1.5
    # delete + undo
    e.delete_selected()
    assert len(cue.clips) == 2
    st.undo()
    assert len(e.cue().clips) == 3


def test_hit_testing_and_drag_maths(ed):
    st, e, s = ed
    from promap.ui.cue_editor import LABEL_W, RULER_H, TRACK_H, CLIP_PAD, HANDLE

    e.add_cue()
    c = e.add_clip(s.id, "media/a.mp4", at=1.0, duration=4.0)
    c.fade_in = 1.0
    v = e.view
    v.set_zoom(50.0)
    y_mid = RULER_H + TRACK_H / 2
    assert v.hit(QPointF(LABEL_W + 20, 10))[0] == "ruler"
    assert v.hit(QPointF(v.x_at(2.5), y_mid))[0] == "body"
    assert v.hit(QPointF(v.x_at(1.0), y_mid))[0] == "left"
    assert v.hit(QPointF(v.x_at(5.0), y_mid))[0] == "right"
    assert v.hit(QPointF(v.x_at(2.0), RULER_H + CLIP_PAD + 2))[0] == "fade_in"
    assert v.hit(QPointF(v.x_at(5.0), RULER_H + CLIP_PAD + 2))[0] == "fade_out"
    assert v.hit(QPointF(v.x_at(8.0), y_mid))[0] == "track"
    assert v.hit(QPointF(20, y_mid)) is None
    # simulate a body drag of +1 s through the mouse handlers
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent

    def ev(kind, x, y, btn=Qt.LeftButton):
        return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), btn, btn, Qt.NoModifier)

    v.mousePressEvent(ev(QEvent.MouseButtonPress, v.x_at(2.5), y_mid))
    v.mouseMoveEvent(ev(QEvent.MouseMove, v.x_at(3.5), y_mid))
    v.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, v.x_at(3.5), y_mid))
    assert c.start == 2.0 and c.duration == 4.0
    # trim the right edge back by 1 s, then drag the fade-in handle to 2 s
    v.mousePressEvent(ev(QEvent.MouseButtonPress, v.x_at(6.0), y_mid))
    v.mouseMoveEvent(ev(QEvent.MouseMove, v.x_at(5.0), y_mid))
    v.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, v.x_at(5.0), y_mid))
    assert c.duration == 3.0
    v.mousePressEvent(ev(QEvent.MouseButtonPress, v.x_at(3.0), RULER_H + CLIP_PAD + 2))
    v.mouseMoveEvent(ev(QEvent.MouseMove, v.x_at(4.0), RULER_H + CLIP_PAD + 2))
    v.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, v.x_at(4.0), RULER_H + CLIP_PAD + 2))
    assert c.fade_in == 2.0
    # scrub in the ruler
    v.mousePressEvent(ev(QEvent.MouseButtonPress, v.x_at(4.2), 10))
    assert abs(st.engine.cue_time() - 4.2) < 1e-6
    v.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, v.x_at(4.2), 10))
    # one undo step for the whole drag
    depth = len(st._undo)
    v.mousePressEvent(ev(QEvent.MouseButtonPress, v.x_at(3.5), y_mid))
    for x in (3.6, 3.7, 3.8):
        v.mouseMoveEvent(ev(QEvent.MouseMove, v.x_at(x), y_mid))
    v.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, v.x_at(3.8), y_mid))
    assert len(st._undo) == depth + 1
