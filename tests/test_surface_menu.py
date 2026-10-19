import pytest


@pytest.fixture
def state():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from promap.ui.state import AppState

    return AppState()


def _trigger(menu, text):
    acts = {a.text(): a for a in menu.actions()}
    assert text in acts, list(acts)
    assert acts[text].isEnabled(), text
    acts[text].trigger()


def test_surface_menu_actions(state):
    from promap.ui.surface_menu import surface_menu

    a = state.add_surface("media")
    b = state.add_surface("mask")
    c = state.add_surface("media")
    names = lambda: [s.id for s in state.project.surfaces]
    m = surface_menu(state, c.id)
    acts = {x.text(): x for x in m.actions() if x.text()}
    assert not acts["Bring to front"].isEnabled() and acts["Send to back"].isEnabled()
    _trigger(surface_menu(state, a.id), "Bring to front")
    assert names() == [b.id, c.id, a.id]
    _trigger(surface_menu(state, a.id), "Move down")
    assert names() == [b.id, a.id, c.id]
    _trigger(surface_menu(state, c.id), "Send to back")
    assert names() == [c.id, b.id, a.id]
    _trigger(surface_menu(state, b.id), "Duplicate")
    assert len(names()) == 4 and state.project.surfaces[2].kind == "mask"
    _trigger(surface_menu(state, b.id), "Locked")
    assert b.locked
    _trigger(surface_menu(state, b.id), "Delete")
    assert b.id not in names() and len(names()) == 3
    assert surface_menu(state, "nope") is None


def test_main_window_has_tool_palette_and_list_menu(state):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QToolBar, QMenu
    from promap.ui.main_window import MainWindow

    w = MainWindow(state)
    tb = w.findChild(QToolBar, "tools")
    assert tb is not None and w.toolBarArea(tb) == Qt.LeftToolBarArea
    glyphs = [a.text() for a in tb.actions() if a.text()]
    assert glyphs == ["▢", "△", "▣", "▦", "⌇"]
    tb.actions()[0].trigger()
    assert len(state.project.surfaces) == 1 and state.project.surfaces[0].kind == "media"
    edit = next(m for m in w.menuBar().findChildren(QMenu) if m.title() == "&Edit")
    assert not any(a.text().startswith("Lasso") for a in edit.actions())  # palette only
    w.lasso_act.setChecked(True)
    assert w.canvas.lasso_mode
    w.lasso_act.setChecked(False)
    assert w.surface_panel.list.contextMenuPolicy() == Qt.CustomContextMenu
    w.state.dirty = False
    w.close()


def test_docks_detachable_and_camera_detach(state):
    from PySide6.QtWidgets import QDockWidget
    from promap.ui.main_window import MainWindow

    w = MainWindow(state)
    w.show()
    assert set(w.docks) == {"Surfaces"}
    assert w.cues_window.isHidden() and w.cues_window.isWindow()
    w.open_cues_window()
    assert w.cues_window.isVisible() and {d.windowTitle() for d in w.cues_window.findChildren(QDockWidget)} == {"Cue list"}
    assert w.cues_window.centralWidget() is w.cue_editor
    w.cues_window.close()
    for d in w.docks.values():
        assert d.features() & QDockWidget.DockWidgetFloatable and d.features() & QDockWidget.DockWidgetClosable
        assert d.objectName()
    assert w.camera_window.isHidden() and w.camera_window.isWindow()  # closed by default, top-level
    assert w.animations_window.isHidden() and w.animations_window.isWindow()
    w.open_animations_window()
    assert w.animations_window.isVisible() and w.animations_window.centralWidget().widget() is w.animation_panel
    w.animations_window.close()
    w.open_camera_window()
    assert w.camera_window.isVisible() and w.camera_window.centralWidget().widget() is w.camera_panel
    w.camera_window.close()
    assert w.camera_window.isHidden()
    d = w.docks["Surfaces"]
    d.setFloating(True)
    d.close()
    w.reset_layout()
    assert not d.isHidden() and not d.isFloating()
    w.state.dirty = False
    w.close()


def test_3d_lives_in_view_menu(state):
    from PySide6.QtWidgets import QMenu
    from promap.ui.main_window import MainWindow

    w = MainWindow(state)
    titles = [m.title() for m in w.menuBar().findChildren(QMenu) if m.title().startswith("&") or m.title().startswith("S&")]
    assert "&3D" not in titles and "S&urface" not in titles and "&Hardware" not in titles and "&View" in titles
    edit = next(m for m in w.menuBar().findChildren(QMenu) if m.title() == "&Edit")
    assert any(a.text() == "Preferences…" for a in edit.actions())
    gen = next(m for m in w.menuBar().findChildren(QMenu) if m.title() == "&Generate")
    assert not any("Settings" in a.text() for a in gen.actions())
    view = next(m for m in w.menuBar().findChildren(QMenu) if m.title() == "&View")
    texts = [a.text() for a in view.actions()]
    assert "Animations window" in texts and "Camera window" in texts and "Cues window" in texts
    assert not any("3D" in t or "Zoom" in t for t in texts)  # those live in the viewer strip / shortcuts
    assert w.view3d_act.isCheckable() and not w.view3d_act.isChecked() and w.view3d_act.shortcut().toString() == "F3"
    assert w.stack.currentWidget() is w.canvas
    shown = lambda: [a.text() for a in w.view_bar.actions() if a.isVisible() and a.text()]
    assert [t for t in shown() if t in ("−", "+", "Fit")] == ["−", "+", "Fit"]
    assert "Orbit" not in shown()
    w.tabs.setCurrentIndex(2)
    assert "Orbit" in shown() and "Pop out ↗" in shown() and "Fit" not in shown()
    w.tabs.setCurrentIndex(0)
    assert "Fit" in shown() and "Orbit" not in shown()
    z = w.canvas.zoom
    next(a for a in w.view_bar.actions() if a.text() == "+").trigger()
    assert w.canvas.zoom > z
    w.state.dirty = False
    w.close()


def test_viewer_tabs_switch_view_and_dock(state, monkeypatch):
    from promap.ui.main_window import MainWindow

    w = MainWindow(state)
    w.show()
    labels = [w.tabs.tabText(i) for i in range(w.tabs.count())]
    assert labels[0].startswith("Output ") and labels[1:] == ["All", "3D"]
    assert w.tabs.currentIndex() == 0 and w.stack.currentWidget() is w.canvas
    opened = []
    monkeypatch.setattr(w, "open_view3d", lambda embedded=True: opened.append(embedded))
    w.tabs.setCurrentIndex(2)
    assert w.stack.currentWidget() is w.view3d_widget and opened == [True]
    assert w.side_dock.windowTitle() == "3D scene" and w.side_stack.currentWidget().widget() is w.scene_panel
    w.tabs.setCurrentIndex(1)
    assert w.stack.currentWidget() is w.canvas
    assert w.side_dock.windowTitle() == "Surfaces" and w.side_stack.currentWidget().widget() is w.surface_panel
    # a renamed output screen shows up in the tab
    state.project.output_screen = "HDMI-1"
    w._refresh_tabs()
    assert w.tabs.tabText(0) == "HDMI-1" and w.tabs.currentIndex() == 1
    w.state.dirty = False
    w.close()


def test_open_view3d_builds_one_instance(state, monkeypatch):
    import promap.scene.view3d as v3d
    from promap.ui.main_window import MainWindow

    made = []

    class FakeView3D:
        def __init__(self, st, parent_handle=None, size=None):
            made.append(parent_handle)
            self.alive, self.embedded = True, parent_handle is not None

        def resize(self, w, h):
            pass

        def close(self):
            self.alive = False

    monkeypatch.setattr(v3d, "View3D", FakeView3D)
    w = MainWindow(state)
    w.show()
    w.open_view3d(embedded=True)  # selects the 3D tab, whose change handler also asks to open
    assert len(made) == 1 and made[0] is not None and w._tab_kind() == "3d" and w.view3d_act.isChecked()
    w.tabs.setCurrentIndex(0)  # leaving the tab keeps the scene alive but unchecks the menu item
    assert w.view3d.alive and not w.view3d_act.isChecked()
    w.tabs.setCurrentIndex(2)
    assert len(made) == 1
    w.close_view3d()
    assert w.view3d is None and w._tab_kind() != "3d"
    w.state.dirty = False
    w.close()


def test_preferences_dialog_tabs(state):
    from promap.model.hardware import HardwareLibrary
    from promap.model.settings import Settings
    from promap.ui.preferences_dialog import PreferencesDialog

    d = PreferencesDialog(state, HardwareLibrary(), Settings(), tab="Generation")
    assert [d.tabs.tabText(i) for i in range(d.tabs.count())] == ["Hardware", "Generation"]
    assert d.tabs.currentIndex() == 1 and d.generation_page.url.text()
    d.generation_page.reject()  # the page's Cancel closes the whole dialog
    assert d.result() == 0 and d.isHidden()
