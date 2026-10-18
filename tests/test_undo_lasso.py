import time

import numpy as np
import pytest

from promap.camera import detect


def test_simplify_polygon_and_mesh_from_lasso():
    # a noisy circle of 200 points -> a handful of vertices -> a mesh that covers the centre
    ang = np.linspace(0, 2 * np.pi, 200, endpoint=False)
    rng = np.random.default_rng(1)
    pts = np.stack([300 + 100 * np.cos(ang) + rng.normal(0, 0.8, 200), 200 + 80 * np.sin(ang) + rng.normal(0, 0.8, 200)], axis=1)
    simp = detect.simplify_polygon(pts, epsilon=4.0)
    assert 6 <= len(simp) <= 40
    m = detect.polygon_mesh(simp)
    assert m.tris and m.hit((300, 200)) is not None and m.hit((450, 200)) is None
    assert len(detect.simplify_polygon([[0, 0], [1, 0]], 2.0)) == 2


@pytest.fixture
def state():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from promap.ui.state import AppState

    return AppState()


def test_undo_redo_surfaces_and_cues(state):
    st = state
    assert not st.can_undo
    s = st.add_surface("media")
    assert st.can_undo and len(st.project.surfaces) == 1
    st.MERGE_S = 0.0  # make every edit its own step for this test
    s.name = "Pumpkin"
    st.surface_edited(s.id, geometry_only=True)
    st.remove_surface(s.id)
    assert len(st.project.surfaces) == 0
    st.undo()
    assert [x.name for x in st.project.surfaces] == ["Pumpkin"]
    st.undo()
    assert st.project.surfaces[0].name != "Pumpkin"
    st.undo()
    assert len(st.project.surfaces) == 0 and not st.can_undo
    st.redo()
    assert len(st.project.surfaces) == 1 and st.can_redo
    st.redo()
    st.redo()
    assert len(st.project.surfaces) == 0 and not st.can_redo
    # cues
    from promap.model.project import Cue

    st.project.cues.append(Cue(name="c1"))
    st.cues_edited()
    st.undo()
    assert st.project.cues == []
    st.redo()
    assert st.project.cues[0].name == "c1"


def test_gesture_is_one_undo_step_and_rapid_edits_merge(state):
    st = state
    s = st.add_surface("media")
    depth0 = len(st._undo)
    st.begin_gesture()
    for i in range(10):
        s.corners[0] = [float(i), float(i)]
        st.surface_edited(s.id, geometry_only=True)
    st.end_gesture()
    assert len(st._undo) == depth0 + 1
    st.undo()
    assert st.project.surfaces[0].corners[0] != [9.0, 9.0]
    st.redo()
    assert st.project.surfaces[0].corners[0] == [9.0, 9.0]
    # rapid slider-like edits within MERGE_S coalesce into one step
    s = st.project.surfaces[0]
    depth1 = len(st._undo)
    for v in (0.1, 0.2, 0.3):
        s.opacity = v
        st.surface_edited(s.id, geometry_only=True)
    assert len(st._undo) == depth1 + 1
    time.sleep(st.MERGE_S + 0.05)
    s.opacity = 0.9
    st.surface_edited(s.id, geometry_only=True)
    assert len(st._undo) == depth1 + 2
    # non-undoable edits (live people surface) leave the stack alone
    s.opacity = 0.5
    st.surface_edited(s.id, geometry_only=True, undoable=False)
    assert len(st._undo) == depth1 + 2
