"""3D scene panel: projector lens/pose, offset cameras, props."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QFormLayout,
    QLineEdit, QDoubleSpinBox, QGroupBox, QFileDialog, QLabel, QGridLayout, QComboBox,
)

from promap.model.scene import PRIMITIVES, Projector
from promap.model.project import ITEM_TYPES
from promap.ui.state import AppState

MODEL_FILTER = "3D models (*.obj *.gltf *.glb *.stl *.egg *.bam *.ply *.dae *.fbx);;All files (*)"


def _triple(lo, hi, step, decimals=2):
    spins = []
    for _ in range(3):
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setDecimals(decimals)
        spins.append(sp)
    return spins


class ScenePanel(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._loading = False
        lay = QVBoxLayout(self)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_row)
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        for text, fn in [("+ Box", lambda: state.add_scene_node("prop", "box")),
                         ("+ Plane", lambda: state.add_scene_node("prop", "plane")),
                         ("+ Model…", self._add_model),
                         ("+ Camera", lambda: state.add_scene_node("camera")),
                         ("Del", self._del)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)

        self.props = QGroupBox("Properties")
        form = QFormLayout(self.props)
        self.name = QLineEdit()
        self.name.editingFinished.connect(self._apply)
        form.addRow("Name", self.name)
        self.device = QLabel()
        self.device.setStyleSheet("color: #888")
        form.addRow("Device", self.device)
        self.item_type = QComboBox()
        self.item_type.setEditable(True)
        self.item_type.addItems([""] + list(ITEM_TYPES))
        self.item_type.currentTextChanged.connect(self._apply)
        form.addRow("Item type", self.item_type)
        self.notes = QLineEdit()
        self.notes.editingFinished.connect(self._apply)
        form.addRow("Notes", self.notes)

        self.pos = _triple(-100, 100, 0.05)
        self.hpr = _triple(-360, 360, 1.0, 1)
        self.scale = _triple(0.001, 100, 0.05, 3)
        for label, spins in [("Pos x y z (m)", self.pos), ("H P R (deg)", self.hpr), ("Scale", self.scale)]:
            hb = QHBoxLayout()
            for sp in spins:
                sp.valueChanged.connect(self._apply)
                hb.addWidget(sp)
            w = QWidget()
            w.setLayout(hb)
            form.addRow(label, w)
        self.scale_row = form.labelForField(form.itemAt(form.rowCount() - 1, QFormLayout.FieldRole).widget())

        self.fov = QDoubleSpinBox()
        self.fov.setRange(5, 170)
        self.fov.setSuffix(" °")
        self.fov.valueChanged.connect(self._apply)
        self.throw = QLabel()
        fb = QHBoxLayout()
        fb.addWidget(self.fov)
        fb.addWidget(self.throw, 1)
        fw = QWidget()
        fw.setLayout(fb)
        form.addRow("H. FOV", fw)

        mrow = QHBoxLayout()
        self.model = QComboBox()
        self.model.addItems(list(PRIMITIVES))
        self.model.currentTextChanged.connect(self._model_changed)
        browse = QPushButton("…")
        browse.setFixedWidth(28)
        browse.clicked.connect(self._browse_model)
        mrow.addWidget(self.model, 1)
        mrow.addWidget(browse)
        self.model_row = QWidget()
        self.model_row.setLayout(mrow)
        form.addRow("Model", self.model_row)
        lay.addWidget(self.props)

        vrow = QHBoxLayout()
        for text, what in [("Open 3D view", "open"), ("Orbit", "orbit"), ("Projector view", "projector")]:
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, w=what: state.view3dRequested.emit(w))
            vrow.addWidget(b)
        self.cam_view_btn = QPushButton("This camera")
        self.cam_view_btn.clicked.connect(lambda: state.view3dRequested.emit(state.scene_selected))
        vrow.addWidget(self.cam_view_btn)
        lay.addLayout(vrow)
        hint = QLabel("Z up, +Y forward, metres. Projector looks along +Y at H=P=R=0.")
        hint.setStyleSheet("color: #888")
        lay.addWidget(hint)

        state.sceneChanged.connect(self._refresh_list)
        state.sceneSelectionChanged.connect(self._refresh_props)
        state.projectReplaced.connect(self._refresh_list)
        self._refresh_list()
        self._refresh_props(None)

    # -- list ---------------------------------------------------------------------
    def _refresh_list(self):
        self._loading = True
        self.list.clear()
        it = QListWidgetItem("◆ Projector")
        it.setData(Qt.UserRole, "projector")
        self.list.addItem(it)
        if self.state.scene_selected == "projector":
            self.list.setCurrentItem(it)
        for n in self.state.project.scene.nodes:
            it = QListWidgetItem(f"{'📷' if n.kind == 'camera' else '▣'} {n.name}")
            it.setData(Qt.UserRole, n.id)
            self.list.addItem(it)
            if n.id == self.state.scene_selected:
                self.list.setCurrentItem(it)
        self._loading = False
        if self.state.scene_selected and not self.props.isEnabled():
            self._refresh_props(self.state.scene_selected)

    def _on_row(self, row: int):
        if self._loading:
            return
        it = self.list.item(row)
        self.state.select_scene(it.data(Qt.UserRole) if it else None)

    def _del(self):
        if self.state.scene_selected and self.state.scene_selected != "projector":
            self.state.remove_scene_node(self.state.scene_selected)

    def _add_model(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose 3D model", str(self.state.project.base_dir), MODEL_FILTER)
        if path:
            self.state.add_scene_node("prop", self.state.project.relativize_media(path))

    # -- properties -------------------------------------------------------------------
    def _target(self):
        key = self.state.scene_selected
        if key == "projector":
            return self.state.project.scene.projector
        return self.state.selected_scene_node

    def _refresh_props(self, key):
        t = self._target()
        self.props.setEnabled(t is not None)
        if t is None:
            return
        self._loading = True
        is_proj = isinstance(t, Projector)
        is_cam = (not is_proj) and t.kind == "camera"
        self.name.setEnabled(not is_proj)
        self.name.setText("Projector" if is_proj else t.name)
        dev = self.state.hardware.by_id(getattr(t, "device_id", None)) if self.state.hardware else None
        self.device.setText(dev.label if dev else ("none (Hardware → Library…)" if is_proj or is_cam else ""))
        self.item_type.setEnabled(not is_proj and not is_cam)
        self.notes.setEnabled(not is_proj)
        self.item_type.setCurrentText("" if is_proj or is_cam else t.item_type)
        self.notes.setText("" if is_proj else t.notes)
        for sp, v in zip(self.pos, t.pos):
            sp.setValue(v)
        for sp, v in zip(self.hpr, t.hpr):
            sp.setValue(v)
        for sp in self.scale:
            sp.setEnabled(not is_proj and not is_cam)
        if not is_proj and not is_cam:
            for sp, v in zip(self.scale, t.scale):
                sp.setValue(v)
        self.fov.setValue(t.fov_h)
        self.fov.setEnabled(is_proj or is_cam)
        self.throw.setText(f"throw ratio {t.throw_ratio:.2f}" if is_proj else "")
        self.model_row.setEnabled(not is_proj and not is_cam)
        if not is_proj and not is_cam:
            if self.model.findText(t.model) < 0:
                self.model.addItem(t.model)
            self.model.setCurrentText(t.model)
        self.cam_view_btn.setEnabled(is_cam)
        self._loading = False

    def _apply(self):
        t = self._target()
        if t is None or self._loading:
            return
        t.pos = [sp.value() for sp in self.pos]
        t.hpr = [sp.value() for sp in self.hpr]
        t.fov_h = self.fov.value()
        if isinstance(t, Projector):
            self.throw.setText(f"throw ratio {t.throw_ratio:.2f}")
        else:
            t.name = self.name.text() or t.name
            t.notes = self.notes.text()
            if t.kind == "prop":
                t.scale = [sp.value() for sp in self.scale]
                t.item_type = self.item_type.currentText().strip()
        self.state.scene_edited()

    def _model_changed(self, text: str):
        t = self._target()
        if t is None or self._loading or isinstance(t, Projector) or t.kind != "prop" or not text:
            return
        t.model = text
        self.state.scene_edited()

    def _browse_model(self):
        t = self._target()
        if t is None or isinstance(t, Projector) or t.kind != "prop":
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose 3D model", str(self.state.project.base_dir), MODEL_FILTER)
        if not path:
            return
        t.model = self.state.project.relativize_media(path)
        self._refresh_props(self.state.scene_selected)
        self.state.scene_edited()
