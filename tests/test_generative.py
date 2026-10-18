import numpy as np
import pytest

from promap.media import generative as g
from promap.media.sources import open_source, SourceCache
from promap.model.project import Project


def test_media_string_roundtrip_and_defaults():
    m = g.make_media("neon_glow", {"speed": 2.0, "color1": "#ff0000", "p2": 0.3})
    assert m.startswith("gen:neon_glow?")
    eff, params = g.parse_media(m)
    assert eff.name == "neon_glow" and params["speed"] == 2.0 and params["color1"] == "ff0000" and params["p2"] == 0.3
    assert params["scale"] == 1.0, "defaults filled in"
    assert g.make_media("neon_glow") == "gen:neon_glow"
    assert g.media_label("gen:kinetic_typography?text=BOO") == "Kinetic typography 'BOO'"
    assert g.is_generative(m) and not g.is_generative("media/x.mp4")
    with pytest.raises(ValueError):
        g.parse_media("gen:nope")


def test_source_and_project_treat_gen_as_media():
    src = open_source("gen:metaballs?p1=0.8")
    assert isinstance(src, g.GenerativeSource) and not src.is_still
    frame, key = src.frame_at(1.5)
    assert isinstance(frame, g.GenFrame) and frame.t == 1.5 and frame.shape == (512, 512, 3)
    u = frame.uniforms()
    assert u["u_p1"] == 0.8 and len(u["u_text"]) == 16
    src.set_params({"color1": "00ff00"})
    assert "color1=00ff00" in src.media
    cache = SourceCache()
    assert cache.open("gen:metaballs") is not cache.open("gen:metaballs"), "one live source per layer"
    p = Project()
    assert p.resolve_media("gen:metaballs") == "gen:metaballs"
    assert g.glyph_code("b") == 1 and g.glyph_code("7") == 33 and g.glyph_code("!") == 36


def test_text_uniforms():
    f = g.GenFrame("kinetic_typography", g.EFFECTS["kinetic_typography"].normalize({"text": "boo 2"}), 0.0, (64, 64))
    u = f.uniforms()
    assert u["u_textlen"] == 5 and u["u_text"][:5] == [1, 14, 14, 36, 28]


moderngl = pytest.importorskip("moderngl")


@pytest.fixture(scope="module")
def ctx():
    try:
        c = moderngl.create_standalone_context(require=330)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"no GL: {exc}")
    yield c
    c.release()


@pytest.mark.parametrize("name", sorted(g.EFFECTS))
def test_every_effect_compiles_and_animates(ctx, name):
    from promap.render.gen import GenRenderer

    r = GenRenderer(ctx)
    r.readback = True
    eff = g.EFFECTS[name]
    params = eff.normalize({"text": "BOO"} if eff.param("text") else {})
    f0 = g.GenFrame(name, params, 0.0, (128, 96))
    f1 = g.GenFrame(name, params, 2.5, (128, 96))
    assert r.texture_for("L", f0) is not None, r.errors.get(name)
    a = r.readbacks["L"].copy()
    r.texture_for("L", f1)
    b = r.readbacks["L"].copy()
    assert a.shape == (96, 128, 3)
    assert max(a.std(), b.std()) > 4, f"{name}: frame is flat"
    assert np.abs(a.astype(int) - b.astype(int)).mean() > 0.5, f"{name}: nothing moves"
    r.release()
