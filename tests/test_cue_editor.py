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
    assert cue is not None and st.cue_selected == 0 and st.engine.cue_index == -1  # selecting never fires
    assert e.cue_name.text() == "1. Cue 1" and e.length.value() == 10.0 and e.end_mode.currentData() == "loop"
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


def test_cue_list_menu_and_plus_button(ed):
    st, e, s = ed
    from PySide6.QtWidgets import QPushButton
    from promap.ui.cue_panel import CuePanel

    panel = CuePanel(st)
    assert not any(isinstance(b, QPushButton) and b.text() in ("Add from editor", "Delete", "▲") for b in panel.findChildren(QPushButton))
    acts = {a.text(): a for a in panel.add_btn.menu().actions()}
    acts["Empty cue"].trigger()
    assert len(st.project.cues) == 1 and st.project.cues[0].clips == []
    s.media = "media/x.mp4"
    next(a for t, a in acts.items() if t.startswith("Cue from editor")).trigger()
    assert len(st.project.cues) == 2 and st.project.cues[1].media_at(s.id, 0) == "media/x.mp4"
    panel.table.selectRow(1)
    panel._duplicate()
    assert [c.name for c in st.project.cues] == ["Cue 1", "Cue 2", "Cue 2 copy"]
    assert st.project.cues[2].clips[0].id != st.project.cues[1].clips[0].id
    panel.table.selectRow(2)
    panel._move(-1)
    assert [c.name for c in st.project.cues] == ["Cue 1", "Cue 2 copy", "Cue 2"]
    panel.table.selectRow(0)
    panel._delete()
    assert [c.name for c in st.project.cues] == ["Cue 2 copy", "Cue 2"]


def test_editor_end_mode_marker_and_auto_extend(ed):
    st, e, s = ed
    from promap.ui.cue_editor import RULER_H, TRACK_H
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent

    e.add_cue()
    cue = e.cue()
    e.end_mode.setCurrentIndex(e.end_mode.findData("next"))
    assert cue.end_mode == "next" and cue.hold == 10.0
    c = e.add_clip(s.id, "media/a.mp4", at=8.0)  # 8..13 pushes the end to 13
    assert cue.total == 13.0 and cue.hold == 13.0 and e.length.value() == 13.0
    v = e.view
    v.set_zoom(40.0)
    y_mid = RULER_H + TRACK_H / 2
    assert v.hit(QPointF(v.x_at(13.0), RULER_H - 5))[0] == "end"

    def ev(kind, x, y):
        return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    v.mousePressEvent(ev(QEvent.MouseButtonPress, v.x_at(13.0), RULER_H - 5))
    v.mouseMoveEvent(ev(QEvent.MouseMove, v.x_at(20.0), RULER_H - 5))
    v.mouseReleaseEvent(ev(QEvent.MouseButtonRelease, v.x_at(20.0), RULER_H - 5))
    assert cue.total == 20.0 and cue.hold == 20.0 and e.length.value() == 20.0
    e.length.setValue(15.0)
    assert cue.total == 15.0 and cue.hold == 15.0
    e.end_mode.setCurrentIndex(e.end_mode.findData("stop"))
    assert cue.hold is None and not cue.loop and cue.total == 15.0


def test_list_selection_drives_editor_and_cue_settings(ed, tmp_path):
    st, e, s = ed
    from promap.ui.cue_panel import CuePanel

    panel = CuePanel(st)
    panel._add(empty=True)
    panel._add(empty=True)
    st.project.cues[0].name, st.project.cues[1].name = "First", "Second"
    st.cues_edited()
    assert panel.table.columnCount() == 2
    panel.table.selectRow(0)
    assert st.cue_selected == 0 and e.cue_name.text() == "1. First"
    panel.table.selectRow(1)
    assert st.cue_selected == 1 and e.cue_name.text() == "2. Second" and st.engine.cue_index == -1
    # cue settings live in the editor and write to the selected cue
    e.fade.setValue(2.5)
    e.gain.setValue(0.4)
    e.audio_loop.setChecked(False)
    c = st.project.cues[1]
    assert c.fade == 2.5 and c.audio_gain == 0.4 and c.audio_loop is False and st.project.cues[0].fade == 1.0
    (tmp_path / "a.wav").write_bytes(b"")
    e._cue_field("audio", "a.wav", restart_audio=True)
    assert c.audio == "a.wav" and e.audio.text() == "a.wav"
    # the show moving on selects the playing cue in both
    st.goto(0)
    assert st.cue_selected == 0 and panel._selected_row() == 0 and e.cue_name.text() == "1. First"


def test_tracks_fill_the_viewport_and_edge_scroll(ed):
    from PySide6.QtWidgets import QApplication
    from promap.ui.cue_editor import LABEL_W, RULER_H, TRACK_H, EDGE_SCROLL_PX

    st, e, s = ed
    e.add_cue()
    e.show()
    QApplication.processEvents()
    v = e.view
    v.set_zoom(6.0)  # a 10 s cue is only 60 px of clips: the tracks must still reach the right edge
    assert v.width() >= e.area.viewport().width()
    # long content: dragging a clip at the right edge of the viewport scrolls, and the names stay pinned
    v.set_zoom(400.0)
    QApplication.processEvents()
    c = e.add_clip(s.id, "media/a.mp4", at=0.0, duration=2.0)
    bar = e.area.horizontalScrollBar()
    assert bar.maximum() > 0 and bar.value() == 0
    y_mid = RULER_H + TRACK_H / 2
    v._drag = {"mode": "body", "clip": c, "x0": v.x_at(1.0), "y0": y_mid, "start": c.start, "dur": c.duration,
               "fi": 0.0, "fo": 0.0, "sid": c.surface_id, "moved": False}
    right_edge = QPointF(bar.value() + e.area.viewport().width() - EDGE_SCROLL_PX / 2, y_mid)
    moved = v._edge_scroll_step(right_edge)
    assert moved > 0 and bar.value() == moved
    assert v.hit(QPointF(bar.value() + 10, y_mid)) is None  # the pinned label column covers the content
    assert v.hit(QPointF(bar.value() + LABEL_W + 5, 10))[0] == "ruler"
    v._drag = None
    # a seek beyond the viewport scrolls the playhead into view
    e.seek(e.cue().total - 0.1)
    v.ensure_visible_t(st.engine.cue_time())
    assert bar.value() > 0 and v.x_at(st.engine.cue_time()) <= bar.value() + e.area.viewport().width()


def test_copy_cut_paste_and_undo_keep_the_cue(ed):
    from PySide6.QtWidgets import QApplication
    from promap.ui.cue_panel import CueTransport

    st, e, s = ed
    e.add_cue()
    cue = e.cue()
    c = e.add_clip(s.id, "media/a.mp4", at=1.0, duration=2.0)
    c.fade_in, c.fade_out = 0.5, 0.25
    e.view.select_clip(c.id)
    e.copy_selected()
    assert QApplication.clipboard().text().startswith("promap-clip:") and e.clipboard_clip().media == "media/a.mp4"
    e.seek(6.0)
    pasted = e.paste()
    assert pasted.start == 6.0 and pasted.duration == 2.0 and pasted.fade_in == 0.5 and pasted.surface_id == s.id
    assert len(cue.clips) == 2 and e.view.selected == pasted.id and cue.total == 10.0
    # paste at an explicit place (the track menu) extends the cue when needed
    p2 = e.paste(sid=s.id, at=12.0)
    assert p2.start == 12.0 and cue.total == 14.0
    # cut removes and keeps the clipboard; paste into another cue works too
    e.view.select_clip(c.id)
    e.cut_selected()
    assert c not in cue.clips and e.clipboard_clip().media == "media/a.mp4"
    e.add_cue()
    assert e.cue() is not cue
    e.paste(at=0.0)
    assert e.cue().clips[0].media == "media/a.mp4"
    # undo while the cue is live keeps the engine in that cue
    st.fire_cue(0, at=1.5, paused=True)
    assert st.engine.cue_index == 0
    st.undo()
    assert st.engine.cue_index == 0 and st.engine.paused and abs(st.engine.cue_time() - 1.5) < 1e-6
    # transport buttons mirror the stacks
    tb = CueTransport(st)
    assert tb.undo_btn.isEnabled() and tb.redo_btn.isEnabled()
    while st.can_undo:
        st.undo()
    assert not tb.undo_btn.isEnabled() and tb.redo_btn.isEnabled()
