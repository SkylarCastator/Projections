"""Hardware library editor: projectors, cameras, depth sensors; apply one to the project."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QVBoxLayout, QListWidget, QListWidgetItem, QPushButton, QFormLayout, QLineEdit,
    QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QLabel, QMessageBox, QGroupBox,
)

from promap.model.hardware import Device, HardwareLibrary, KINDS
from promap.model.scene import Projector
from promap.ui.state import AppState


class _OptDouble(QDoubleSpinBox):
    """A spin box whose minimum reads as 'unset'."""

    def __init__(self, lo, hi, step, decimals=2, suffix=""):
        super().__init__()
        self.setRange(lo, hi)
        self.setSingleStep(step)
        self.setDecimals(decimals)
        self.setSuffix(suffix)
        self.setSpecialValueText("–")

    def get(self):
        return None if self.value() == self.minimum() else self.value()

    def put(self, v):
        self.setValue(self.minimum() if v is None else v)


class HardwareDialog(QDialog):
    def __init__(self, state: AppState, library: HardwareLibrary, parent=None):
        super().__init__(parent)
        self.state = state
        self.lib = library
        self._loading = False
        self.setWindowTitle("Hardware library")
        self.resize(760, 520)
        root = QHBoxLayout(self)

        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_row)
        left.addWidget(self.list, 1)
        brow = QHBoxLayout()
        for text, fn in [("+ Projector", lambda: self._add("projector")), ("+ Camera", lambda: self._add("camera")),
                         ("+ Depth cam", lambda: self._add("depth_camera")), ("Delete", self._delete)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            brow.addWidget(b)
        left.addLayout(brow)
        root.addLayout(left, 1)

        right = QVBoxLayout()
        self.form_box = QGroupBox("Device")
        form = QFormLayout(self.form_box)
        self.name = QLineEdit()
        self.kind = QComboBox()
        self.kind.addItems(list(KINDS))
        self.make = QLineEdit()
        self.model = QLineEdit()
        self.width = QSpinBox()
        self.width.setRange(1, 16384)
        self.height = QSpinBox()
        self.height.setRange(1, 16384)
        self.throw = _OptDouble(0.0, 20.0, 0.05)
        self.throw.setToolTip("distance / image width, from the projector spec sheet")
        self.fov_h = _OptDouble(0.0, 179.0, 0.5, 1, " °")
        self.fov_v = _OptDouble(0.0, 179.0, 0.5, 1, " °")
        self.dmin = _OptDouble(0.0, 50.0, 0.1, 2, " m")
        self.dmax = _OptDouble(0.0, 50.0, 0.1, 2, " m")
        self.notes = QPlainTextEdit()
        self.notes.setMaximumHeight(90)
        self.derived = QLabel()
        for label, w in [("Name", self.name), ("Kind", self.kind), ("Make", self.make), ("Model", self.model),
                         ("Width (px)", self.width), ("Height (px)", self.height), ("Throw ratio", self.throw),
                         ("H. FOV", self.fov_h), ("V. FOV", self.fov_v), ("Depth min", self.dmin),
                         ("Depth max", self.dmax), ("Notes", self.notes), ("", self.derived)]:
            form.addRow(label, w)
        for w in (self.name, self.make, self.model):
            w.editingFinished.connect(self._apply)
        self.kind.currentTextChanged.connect(self._apply)
        self.notes.textChanged.connect(self._apply)
        for w in (self.width, self.height, self.throw, self.fov_h, self.fov_v, self.dmin, self.dmax):
            w.valueChanged.connect(self._apply)
        right.addWidget(self.form_box, 1)

        arow = QHBoxLayout()
        self.use_proj = QPushButton("Use as this project's projector")
        self.use_proj.setToolTip("Sets output size and the 3D projector's field of view from this device")
        self.use_proj.clicked.connect(self._use_as_projector)
        self.use_cam = QPushButton("Assign to selected 3D camera")
        self.use_cam.clicked.connect(self._assign_camera)
        arow.addWidget(self.use_proj)
        arow.addWidget(self.use_cam)
        right.addLayout(arow)
        srow = QHBoxLayout()
        self.path_label = QLabel(str(self.lib.path))
        self.path_label.setStyleSheet("color: #888")
        save = QPushButton("Save library")
        save.clicked.connect(self._save)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        srow.addWidget(self.path_label, 1)
        srow.addWidget(save)
        srow.addWidget(close)
        right.addLayout(srow)
        root.addLayout(right, 2)

        self._refresh_list()

    # -- list / form -----------------------------------------------------------
    def _current(self) -> Device | None:
        it = self.list.currentItem()
        return self.lib.by_id(it.data(Qt.UserRole)) if it else None

    def _refresh_list(self, select_id: str | None = None):
        self._loading = True
        self.list.clear()
        for d in self.lib.devices:
            it = QListWidgetItem(f"[{d.kind}] {d.label}")
            it.setData(Qt.UserRole, d.id)
            self.list.addItem(it)
            if d.id == select_id:
                self.list.setCurrentItem(it)
        self._loading = False
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._on_row(self.list.currentRow())

    def _on_row(self, row: int):
        if self._loading:
            return
        d = self._current()
        self.form_box.setEnabled(d is not None)
        self.use_proj.setEnabled(d is not None and d.kind == "projector")
        self.use_cam.setEnabled(d is not None and d.kind != "projector")
        if d is None:
            return
        self._loading = True
        self.name.setText(d.name)
        self.kind.setCurrentText(d.kind)
        self.make.setText(d.make)
        self.model.setText(d.model)
        self.width.setValue(d.width)
        self.height.setValue(d.height)
        self.throw.put(d.throw_ratio)
        self.fov_h.put(d.fov_h)
        self.fov_v.put(d.fov_v)
        self.dmin.put(d.depth_min)
        self.dmax.put(d.depth_max)
        self.notes.setPlainText(d.notes)
        self._loading = False
        self._derived(d)

    def _derived(self, d: Device):
        fov = d.horizontal_fov
        self.derived.setText(f"horizontal FOV {fov:.1f}°, aspect {d.width / max(1, d.height):.3f}" if fov else "")

    def _apply(self):
        d = self._current()
        if d is None or self._loading:
            return
        d.name = self.name.text() or d.name
        d.kind = self.kind.currentText()
        d.make, d.model = self.make.text(), self.model.text()
        d.width, d.height = self.width.value(), self.height.value()
        d.throw_ratio, d.fov_h, d.fov_v = self.throw.get(), self.fov_h.get(), self.fov_v.get()
        d.depth_min, d.depth_max = self.dmin.get(), self.dmax.get()
        d.notes = self.notes.toPlainText()
        self._derived(d)
        it = self.list.currentItem()
        if it:
            it.setText(f"[{d.kind}] {d.label}")

    def _add(self, kind: str):
        d = Device(name=f"New {kind.replace('_', ' ')}", kind=kind)
        if kind != "projector":
            d.throw_ratio, d.fov_h = None, 60.0
        self.lib.add(d)
        self._refresh_list(d.id)

    def _delete(self):
        d = self._current()
        if d and QMessageBox.question(self, "Delete", f"Delete {d.label}?") == QMessageBox.Yes:
            self.lib.remove(d.id)
            self._refresh_list()

    def _save(self):
        p = self.lib.save()
        self.path_label.setText(f"saved to {p}")

    # -- apply to project -------------------------------------------------------------
    def _use_as_projector(self):
        d = self._current()
        if d is None:
            return
        proj: Projector = self.state.project.scene.projector
        proj.device_id = d.id
        if d.horizontal_fov:
            proj.fov_h = d.horizontal_fov
        self.state.project.output_width, self.state.project.output_height = d.width, d.height
        self.state.surface_edited(None, geometry_only=True)
        self.state.scene_edited()
        self.state.select_scene("projector")

    def _assign_camera(self):
        d = self._current()
        n = self.state.selected_scene_node
        if d is None or n is None or n.kind != "camera":
            QMessageBox.information(self, "Assign", "Select a camera in the 3D scene panel first.")
            return
        n.device_id = d.id
        n.name = d.name
        if d.horizontal_fov:
            n.fov_h = d.horizontal_fov
        self.state.scene_edited()
