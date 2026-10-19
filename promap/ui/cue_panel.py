"""Cue list (a narrow column of names) and the show transport (a toolbar: Back / GO / preview / blackout)."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QLabel,
    QAbstractItemView, QHeaderView, QMenu, QToolButton, QInputDialog, QToolBar, QSizePolicy,
)

from promap.model.project import Cue
from promap.ui.state import AppState


class CuePanel(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._loading = False
        lay = QVBoxLayout(self)

        lay.setContentsMargins(2, 2, 2, 2)
        self.table = QTableWidget(0, 2)
        self.table.horizontalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 28)
        self.table.setShowGrid(False)
        self.table.setToolTip("Select a cue to edit it. Double-click its number, or right-click → Fire, to jump the show there.")
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.cellChanged.connect(self._cell_changed)
        self.table.cellDoubleClicked.connect(lambda r, c: self.state.goto(r) if c == 0 else None)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        lay.addWidget(self.table, 1)

        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.table.installEventFilter(self)

        bottom = QHBoxLayout()
        self.add_btn = QToolButton()
        self.add_btn.setText("+")
        self.add_btn.setToolTip("Add a cue: empty, or a snapshot of what the surfaces show now")
        self.add_btn.setPopupMode(QToolButton.InstantPopup)
        add_menu = QMenu(self.add_btn)
        add_menu.addAction("Empty cue", lambda: self._add(empty=True))
        add_menu.addAction("Cue from editor  (each surface's current media)", lambda: self._add(empty=False))
        self.add_btn.setMenu(add_menu)
        bottom.addWidget(self.add_btn)
        bottom.addStretch(1)
        lay.addLayout(bottom)
        self.setMinimumWidth(150)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        state.cuesChanged.connect(self._refresh)
        state.showChanged.connect(self._refresh_status)
        state.projectReplaced.connect(self._refresh)
        state.cueSelected.connect(self._follow_selected_cue)
        self._refresh()

    # -- table ---------------------------------------------------------------
    def _refresh(self):
        self._loading = True
        self.table.blockSignals(True)
        cues = self.state.project.cues
        self.table.setRowCount(len(cues))
        for r, c in enumerate(cues):
            num = QTableWidgetItem(str(r + 1))
            num.setFlags(num.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 0, num)
            self.table.setItem(r, 1, QTableWidgetItem(c.name))
        if 0 <= self.state.cue_selected < len(cues):
            self.table.selectRow(self.state.cue_selected)
        self.table.blockSignals(False)
        self._loading = False
        self._refresh_status()

    def _selection_changed(self):
        if self._loading:
            return
        r = self._selected_row()
        if r is not None:
            self.state.select_cue(r)

    def _follow_selected_cue(self, i: int):
        if self._selected_row() != i and 0 <= i < self.table.rowCount():
            self._loading = True
            self.table.selectRow(i)
            self._loading = False

    def _refresh_status(self):
        idx = self.state.engine.cue_index
        self.table.blockSignals(True)  # setBackground also fires cellChanged
        for r in range(self.table.rowCount()):
            brush = QBrush(QColor(40, 90, 40)) if r == idx else QBrush()
            for col in range(2):
                it = self.table.item(r, col)
                if it:
                    it.setBackground(brush)
        self.table.blockSignals(False)

    def _cell_changed(self, row: int, col: int):
        if self._loading:
            return
        cue = self.state.project.cues[row]
        txt = self.table.item(row, col).text().strip()
        if col == 1:
            cue.name = txt or cue.name
        self.state.cues_edited()

    def _selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _row_menu(self, pos):
        """Everything you can do to a cue, on the cue itself."""
        it = self.table.itemAt(pos)
        if it is not None:
            self.table.selectRow(it.row())
        r = self._selected_row()
        cues = self.state.project.cues
        m = QMenu(self)
        if r is not None:
            cue = cues[r]
            m.addSection(f"{r + 1}. {cue.name}")
            m.addAction("Fire  (jump the show here)", lambda: self.state.goto(r))
            m.addAction("Rename…", self._rename)
            m.addSeparator()
            m.addAction("Duplicate", self._duplicate)
            m.addAction("Delete  (Del)", self._delete)
            up = m.addAction("Move up", lambda: self._move(-1))
            up.setEnabled(r > 0)
            down = m.addAction("Move down", lambda: self._move(1))
            down.setEnabled(r < len(cues) - 1)
            m.addSeparator()
            m.addAction("Update from editor  (overwrite with the surfaces' current media)", self._update)
            m.addAction("Load into editor  (put its media back on the surfaces)", self._load)
            m.addSeparator()
        add = m.addMenu("Add cue")
        add.addAction("Empty", lambda: self._add(empty=True))
        add.addAction("From editor", lambda: self._add(empty=False))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def eventFilter(self, obj, ev):
        if obj is self.table and ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Delete, Qt.Key_Backspace) \
                and self.table.state() != QAbstractItemView.EditingState:
            self._delete()
            return True
        return super().eventFilter(obj, ev)

    def _add(self, empty: bool = False):
        cues = self.state.project.cues
        name = f"Cue {len(cues) + 1}"
        cue = Cue(name=name, length=10.0) if empty else Cue.from_assignments(name, self.state.editor_assignments())
        at = self._selected_row()
        cues.insert(len(cues) if at is None else at + 1, cue)
        self.state.cues_edited()
        self.state.select_cue(cues.index(cue))
        self.table.selectRow(cues.index(cue))

    def _rename(self):
        r = self._selected_row()
        if r is None:
            return
        cue = self.state.project.cues[r]
        name, ok = QInputDialog.getText(self, "Rename cue", "Name", text=cue.name)
        if ok and name.strip():
            cue.name = name.strip()
            self.state.cues_edited()
            self.table.selectRow(r)

    def _duplicate(self):
        r = self._selected_row()
        if r is None:
            return
        cues = self.state.project.cues
        c = copy.deepcopy(cues[r])
        c.id = Cue().id
        c.name = f"{c.name} copy"
        for clip in c.clips:
            clip.id = Cue().id
        cues.insert(r + 1, c)
        self.state.cues_edited()
        self.table.selectRow(r + 1)

    def _update(self):
        r = self._selected_row()
        if r is None:
            return
        self.state.project.cues[r].set_assignments(self.state.editor_assignments())
        self.state.cues_edited()
        if self.state.engine.cue_index == r:
            self.state.goto(r)

    def _load(self):
        r = self._selected_row()
        if r is not None:
            self.state.load_cue_into_editor(r)

    def _delete(self):
        r = self._selected_row()
        if r is None:
            return
        del self.state.project.cues[r]
        if self.state.engine.cue_index >= len(self.state.project.cues):
            self.state.preview_mode()
        self.state.cues_edited()

    def _move(self, d: int):
        r = self._selected_row()
        cues = self.state.project.cues
        if r is None:
            return
        j = max(0, min(len(cues) - 1, r + d))
        cues.insert(j, cues.pop(r))
        self.state.cues_edited()
        self.table.selectRow(j)


class CueTransport(QToolBar):
    """Back / GO / Preview / Blackout and the show status, as a toolbar across the Cues window."""

    def __init__(self, state: AppState, parent=None):
        super().__init__("Transport", parent)
        self.state = state
        self.setObjectName("cue_transport")
        self.setMovable(False)
        self.setFloatable(False)
        self.setStyleSheet("QToolBar { spacing: 6px; padding: 3px 6px; }")
        self.back_btn = QPushButton("◀ Back")
        self.go_btn = QPushButton("GO ▶   (Space)")
        self.go_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.preview_btn = QPushButton("Preview mode")
        self.preview_btn.setToolTip("Drop back to the editor's own media assignments (Ctrl+P)")
        self.blackout_btn = QPushButton("Blackout")
        self.blackout_btn.setCheckable(True)
        self.blackout_btn.setToolTip("Everything dark until released (B)")
        self.back_btn.clicked.connect(state.back)
        self.go_btn.clicked.connect(state.go)
        self.preview_btn.clicked.connect(state.preview_mode)
        self.blackout_btn.toggled.connect(self._blackout)
        for w in (self.back_btn, self.go_btn, self.preview_btn, self.blackout_btn):
            self.addWidget(w)
        self.addSeparator()
        self.undo_btn = QToolButton()
        self.undo_btn.setText("↶")
        self.undo_btn.setToolTip("Undo  (Ctrl+Z)")
        self.undo_btn.clicked.connect(state.undo)
        self.redo_btn = QToolButton()
        self.redo_btn.setText("↷")
        self.redo_btn.setToolTip("Redo  (Ctrl+Shift+Z, Ctrl+Y)")
        self.redo_btn.clicked.connect(state.redo)
        for b in (self.undo_btn, self.redo_btn):
            b.setStyleSheet("font-size: 16px; padding: 2px 8px;")
            self.addWidget(b)
        state.undoChanged.connect(self._undo_state)
        self._undo_state()
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.addWidget(spacer)
        self.status = QLabel()
        self.status.setStyleSheet("padding-right: 6px;")
        self.addWidget(self.status)
        state.showChanged.connect(self._refresh)
        state.cuesChanged.connect(self._refresh)
        state.projectReplaced.connect(self._refresh)
        self._refresh()

    def _blackout(self, on: bool):
        if self.state.engine.blackout != on:
            self.state.set_blackout(on)

    def _undo_state(self):
        self.undo_btn.setEnabled(self.state.can_undo)
        self.redo_btn.setEnabled(self.state.can_redo)

    def _refresh(self):
        idx = self.state.engine.cue_index
        n = len(self.state.project.cues)
        if idx < 0:
            text = "Preview mode (surfaces show their own media)"
        else:
            text = f"Cue {idx + 1}/{n}: {self.state.project.cues[idx].name}"
        if self.state.engine.blackout:
            text += "   BLACKOUT"
        self.status.setText(text)
        self.blackout_btn.blockSignals(True)
        self.blackout_btn.setChecked(self.state.engine.blackout)
        self.blackout_btn.blockSignals(False)
