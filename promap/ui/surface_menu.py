"""Right-click menu for a surface: the same actions from the Surfaces list and from the canvas."""
from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu

from promap.ui.state import AppState


def surface_menu(state: AppState, sid: str, parent=None) -> QMenu | None:
    """Build the context menu for surface `sid`, or None when it no longer exists."""
    s = state.project.surface_by_id(sid)
    if s is None:
        return None
    n = len(state.project.surfaces)
    i = state.project.surfaces.index(s)
    m = QMenu(parent)
    m.addSection(s.name)

    def add(text, fn, enabled=True, checkable=False, checked=False, tip=""):
        a = QAction(text, m)
        a.setEnabled(enabled)
        if checkable:
            a.setCheckable(True)
            a.setChecked(checked)
        if tip:
            a.setToolTip(tip)
        a.triggered.connect(lambda *_: fn())
        m.addAction(a)
        return a

    add("Duplicate", lambda: state.duplicate_surface(sid))
    add("Delete", lambda: state.remove_surface(sid))
    m.addSeparator()
    add("Bring to front", lambda: state.move_surface(sid, n), enabled=i < n - 1,
        tip="Surfaces are drawn in list order; the last one is on top")
    add("Move up", lambda: state.move_surface(sid, 1), enabled=i < n - 1)
    add("Move down", lambda: state.move_surface(sid, -1), enabled=i > 0)
    add("Send to back", lambda: state.move_surface(sid, -n), enabled=i > 0)
    m.addSeparator()

    def toggle_lock():
        s.locked = not s.locked
        state.surface_edited(sid)

    add("Locked", toggle_lock, checkable=True, checked=s.locked,
        tip="A locked surface ignores the mouse in the canvas (reference photos, finished quads)")
    return m
