import pytest

from promap.media import generative as gen
from promap.model.project import Project, Surface, Cue
from promap.show.engine import ShowEngine


def test_engine_runs_generative_media_and_live_param_edit():
    p = Project()
    s = Surface(name="A", media="gen:neon_glow?speed=2")
    p.surfaces.append(s)
    eng = ShowEngine(p)
    items = eng.render_items(now=1.0)
    assert len(items) == 1 and isinstance(items[0].frame, gen.GenFrame)
    assert items[0].frame.params["speed"] == 2.0
    layer = [l for l in eng.layers if l.surface_id == s.id][0]
    layer.source.set_params({"speed": 0.5})
    assert eng.render_items(now=2.0)[0].frame.params["speed"] == 0.5
    assert layer.source.media == "gen:neon_glow?speed=0.5"
    # a cue carrying an animation
    p.cues.append(Cue.from_assignments("c", {s.id: "gen:metaballs?p1=0.9"}))
    eng.goto(0)
    fr = [it for it in eng.render_items() if it.frame is not None and getattr(it.frame, "effect", "") == "metaballs"]
    assert fr and fr[0].frame.params["p1"] == 0.9


@pytest.mark.skipif(not pytest.importorskip("PySide6", reason="qt"), reason="qt")
def test_state_update_gen_params_without_restart():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from promap.ui.state import AppState

    st = AppState()
    s = st.add_surface("media")
    st.assign_media(s.id, "gen:liquid_morph")
    live = lambda: [l for l in st.engine.layers if l.surface_id == s.id and l.fade_out_at is None]
    assert len(live()) == 1
    started = live()[0].started
    st.update_gen_params(s.id, {"scale": 2.0, "color1": "#00ff00"})
    assert s.media == "gen:liquid_morph?scale=2.0&color1=00ff00"
    assert live()[0].started == started, "no restart"
    assert st.live_sources(s.id)[0].params["scale"] == 2.0
    # same thing inside a running cue
    from promap.model.project import Cue

    st.project.cues.append(Cue.from_assignments("x", {s.id: "gen:metaballs"}))
    st.goto(0)
    st.update_gen_params(s.id, {"p2": 0.1})
    assert st.project.cues[0].media_at(s.id, 0) == "gen:metaballs?p2=0.1"
    assert st.live_sources(s.id)[0].params["p2"] == 0.1
