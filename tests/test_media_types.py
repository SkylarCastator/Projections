import pytest


@pytest.fixture
def panel():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from promap.ui.state import AppState
    from promap.ui.surface_panel import SurfacePanel

    st = AppState()
    return st, SurfacePanel(st)


def test_media_kind():
    from promap.ui.surface_panel import media_kind

    assert [media_kind(m) for m in (None, "", "a.PNG", "b.mp4", "c.gif", "d.webp", "gen:glitch", "odd.xyz")] == [
        "none", "none", "image", "video", "anim", "anim", "gen", "image"]


def test_panel_follows_media_type(panel):
    st, p = panel
    from promap.media.generative import EFFECTS, make_media

    s = st.add_surface("media")
    st.select(s.id)
    rows = lambda: (p.media_type.currentData(), p.form.isRowVisible(p.media_pick), p.loop.isVisibleTo(p),
                    p.video_audio.isVisibleTo(p), p.form.isRowVisible(p.speed), p.media_pick.currentIndex())
    assert rows() == ("none", False, False, False, False, 0)
    first = next(iter(EFFECTS))
    for media, want in [
        ("media/a.png", ("image", True, False, False, False, 0)),
        ("media/b.mp4", ("video", True, True, True, True, 0)),
        ("media/c.gif", ("anim", True, True, False, True, 0)),
        (make_media(first), ("gen", True, False, False, True, 1)),
    ]:
        s.media = media
        p._refresh_props(s.id)
        assert rows() == want, media
    assert p.anim_combo.currentData() == first and p.form.isRowVisible(p.media_type)

    # choosing a type / an animation from the panel writes the media string
    p.media_type.setCurrentIndex(p.media_type.findData("none"))
    assert s.media is None and rows()[0] == "none"
    p.media_type.setCurrentIndex(p.media_type.findData("gen"))
    assert s.media.startswith("gen:") and p.media_pick.currentIndex() == 1
    second = list(EFFECTS)[1]
    p.anim_combo.setCurrentIndex(p.anim_combo.findData(second))
    assert s.media == make_media(second)

    # masks and reference photos: no type row
    m = st.add_surface("mask")
    st.select(m.id)
    assert not p.form.isRowVisible(p.media_type) and not p.form.isRowVisible(p.media_pick)
    r = st.add_reference_image("media/room.png")
    st.select(r.id)
    assert not p.form.isRowVisible(p.media_type) and p.form.isRowVisible(p.media_pick) and p.media.text().endswith("room.png")


def test_parameters_button_asks_for_animations_window(panel):
    st, p = panel
    got = []
    st.windowRequested.connect(got.append)
    s = st.add_surface("media")
    st.select(s.id)
    from PySide6.QtWidgets import QPushButton
    btn = next(b for b in p.media_pick.widget(1).findChildren(QPushButton))
    btn.click()
    assert got == ["animations"]
