"""Cue list and transport (GO / Back / preview / blackout)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem, QPushButton, QLabel,
    QAbstractItemView, QHeaderView, QFileDialog,
)

from promap.model.project import Cue
from promap.ui.state import AppState


class CuePanel(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._loading = False
        lay = QVBoxLayout(self)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["#", "Name", "Fade (s)", "Hold (s)", "Audio", "Gain"])
        self.table.horizontalHeaderItem(4).setToolTip("Soundtrack for this cue. The same file in consecutive cues keeps playing; a different one crossfades.")
        self.table.horizontalHeaderItem(3).setToolTip("Seconds before auto-advancing to the next cue. Blank = wait for GO.")
        self.table.horizontalHeaderItem(2).setToolTip("Crossfade length in seconds when entering this cue.")
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.cellChanged.connect(self._cell_changed)
        self.table.cellDoubleClicked.connect(lambda r, c: self.state.goto(r) if c == 0 else None)
        lay.addWidget(self.table, 1)

        edit = QHBoxLayout()
        for text, fn, tip in [
            ("Add from editor", self._add, "Snapshot each surface's current media into a new cue"),
            ("Update from editor", self._update, "Overwrite the selected cue with the editor's media assignments"),
            ("Load into editor", self._load, "Put the selected cue's assignments back onto the surfaces for tweaking"),
            ("Delete", self._delete, ""),
            ("▲", lambda: self._move(-1), ""), ("▼", lambda: self._move(1), ""),
            ("Audio…", self._set_audio, "Choose a soundtrack file for the selected cue"),
            ("No audio", self._clear_audio, ""),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            edit.addWidget(b)
        lay.addLayout(edit)

        transport = QHBoxLayout()
        self.back_btn = QPushButton("◀ Back")
        self.go_btn = QPushButton("GO ▶   (Space)")
        self.go_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        self.preview_btn = QPushButton("Preview mode")
        self.blackout_btn = QPushButton("Blackout")
        self.blackout_btn.setCheckable(True)
        self.back_btn.clicked.connect(state.back)
        self.go_btn.clicked.connect(state.go)
        self.preview_btn.clicked.connect(state.preview_mode)
        self.blackout_btn.toggled.connect(state.set_blackout)
        self.status = QLabel()
        for w in (self.back_btn, self.go_btn, self.preview_btn, self.blackout_btn):
            transport.addWidget(w)
        transport.addStretch(1)
        transport.addWidget(self.status)
        lay.addLayout(transport)

        state.cuesChanged.connect(self._refresh)
        state.showChanged.connect(self._refresh_status)
        state.projectReplaced.connect(self._refresh)
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
            self.table.setItem(r, 2, QTableWidgetItem(f"{c.fade:g}"))
            self.table.setItem(r, 3, QTableWidgetItem("" if c.hold is None else f"{c.hold:g}"))
            self.table.setItem(r, 4, QTableWidgetItem(c.audio or ""))
            self.table.setItem(r, 5, QTableWidgetItem(f"{c.audio_gain:g}"))
        self.table.blockSignals(False)
        self._loading = False
        self._refresh_status()

    def _refresh_status(self):
        idx = self.state.engine.cue_index
        self.table.blockSignals(True)  # setBackground also fires cellChanged
        for r in range(self.table.rowCount()):
            brush = QBrush(QColor(40, 90, 40)) if r == idx else QBrush()
            for col in range(6):
                it = self.table.item(r, col)
                if it:
                    it.setBackground(brush)
        self.table.blockSignals(False)
        n = len(self.state.project.cues)
        if idx < 0:
            text = "Preview mode (surfaces show their own media)"
        else:
            text = f"Cue {idx + 1}/{n}: {self.state.project.cues[idx].name}"
        if self.state.engine.blackout:
            text += "   BLACKOUT"
        self.status.setText(text)
        self.blackout_btn.setChecked(self.state.engine.blackout)

    def _cell_changed(self, row: int, col: int):
        if self._loading:
            return
        cue = self.state.project.cues[row]
        txt = self.table.item(row, col).text().strip()
        try:
            if col == 1:
                cue.name = txt or cue.name
            elif col == 2:
                cue.fade = max(0.0, float(txt or 0))
            elif col == 3:
                cue.hold = None if txt == "" else max(0.0, float(txt))
            elif col == 4:
                cue.audio = txt or None
            elif col == 5:
                cue.audio_gain = max(0.0, float(txt or 1))
        except ValueError:
            pass
        self.state.cues_edited()

    def _selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else None

    def _add(self):
        cues = self.state.project.cues
        cue = Cue(name=f"Cue {len(cues) + 1}", media=self.state.editor_assignments())
        at = self._selected_row()
        cues.insert(len(cues) if at is None else at + 1, cue)
        self.state.cues_edited()
        self.table.selectRow(cues.index(cue))

    def _update(self):
        r = self._selected_row()
        if r is None:
            return
        self.state.project.cues[r].media = self.state.editor_assignments()
        self.state.cues_edited()
        if self.state.engine.cue_index == r:
            self.state.goto(r)

    def _load(self):
        r = self._selected_row()
        if r is not None:
            self.state.load_cue_into_editor(r)

    def _set_audio(self):
        r = self._selected_row()
        if r is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose soundtrack", str(self.state.project.base_dir),
                                              "Audio (*.wav *.mp3 *.ogg *.flac *.m4a *.aac *.opus);;All files (*)")
        if not path:
            return
        self.state.project.cues[r].audio = self.state.project.relativize_media(path)
        self.state.cues_edited()
        self.table.selectRow(r)
        if self.state.engine.cue_index == r:
            self.state.goto(r)

    def _clear_audio(self):
        r = self._selected_row()
        if r is None:
            return
        self.state.project.cues[r].audio = None
        self.state.cues_edited()
        self.table.selectRow(r)
        if self.state.engine.cue_index == r:
            self.state.goto(r)

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
