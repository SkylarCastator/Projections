"""Surface list + property editor."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QFormLayout,
    QLineEdit, QSlider, QCheckBox, QDoubleSpinBox, QGroupBox, QFileDialog, QLabel, QGridLayout, QComboBox,
    QStackedWidget, QSpinBox, QColorDialog,
)

from promap.model.geometry import rect_corners, centroid
from promap.model.project import ITEM_TYPES
from promap.ui.state import AppState

MEDIA_FILTER = "Media (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.mp4 *.mov *.avi *.mkv *.webm *.m4v);;All files (*)"


class SurfacePanel(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self._loading = False
        lay = QVBoxLayout(self)

        self.list = QListWidget()
        self.list.setMinimumHeight(180)
        self.list.currentRowChanged.connect(self._on_row)
        lay.addWidget(self.list, 1)

        row = QHBoxLayout()
        for text, fn, tip in [
            ("+ Media", lambda: state.add_surface("media"), "A four-corner quad with a perspective warp (flat rectangular faces)"),
            ("+ Tri", lambda: state.add_surface("media", mesh="triangle"), "One triangle; A / double-click an edge grows more, drag vertices together to join them"),
            ("+ Mask", lambda: state.add_surface("mask"), "A black quad to blank spill"),
            ("+ Ref image", self._add_reference, "A photo of the space, editor-only"),
            ("Dup", self._dup, ""), ("Del", self._del, ""), ("▲", lambda: self._move(-1), ""), ("▼", lambda: self._move(1), ""),
        ]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            if tip:
                b.setToolTip(tip)
            row.addWidget(b)
        lay.addLayout(row)

        self.props = QGroupBox("Properties")
        form = QFormLayout(self.props)
        self.name = QLineEdit()
        self.name.editingFinished.connect(self._apply)
        form.addRow("Name", self.name)

        self.item_type = QComboBox()
        self.item_type.setEditable(True)
        self.item_type.addItems([""] + list(ITEM_TYPES))
        self.item_type.setToolTip("What this surface lands on in the room.")
        self.item_type.currentTextChanged.connect(self._apply)
        form.addRow("Lands on", self.item_type)

        self.notes = QLineEdit()
        self.notes.setPlaceholderText("e.g. left of porch, 1.2 m pumpkin, slightly curved")
        self.notes.editingFinished.connect(self._apply)
        form.addRow("Notes", self.notes)

        mrow = QHBoxLayout()
        self.media = QLineEdit()
        self.media.setReadOnly(True)
        browse = QPushButton("…")
        browse.setFixedWidth(28)
        browse.clicked.connect(self._browse)
        clear = QPushButton("×")
        clear.setFixedWidth(28)
        clear.clicked.connect(self._clear_media)
        mrow.addWidget(self.media, 1)
        mrow.addWidget(browse)
        mrow.addWidget(clear)
        self.media_row = QWidget()
        self.media_row.setLayout(mrow)
        form.addRow("Media", self.media_row)

        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.valueChanged.connect(self._apply)
        form.addRow("Opacity", self.opacity)

        trow = QHBoxLayout()
        self.tint_btn = QPushButton("tint")
        self.tint_btn.setToolTip("Multiply the media's colour (white = unchanged). Quick way to recolour a clip per surface.")
        self.tint_btn.clicked.connect(self._pick_tint)
        tint_reset = QPushButton("×")
        tint_reset.setFixedWidth(28)
        tint_reset.clicked.connect(self._reset_tint)
        trow.addWidget(self.tint_btn, 1)
        trow.addWidget(tint_reset)
        self.key_black = QCheckBox("Black = transparent")
        self.key_black.setToolTip("Luma key: pixels darker than the threshold let the layers underneath show through")
        self.key_black.toggled.connect(self._apply)
        self.key_threshold = QDoubleSpinBox()
        self.key_threshold.setRange(0.0, 1.0)
        self.key_threshold.setSingleStep(0.02)
        self.key_threshold.setDecimals(2)
        self.key_threshold.setToolTip("Luma (0 black .. 1 white) below which a pixel is transparent; soft edge just above it")
        self.key_threshold.valueChanged.connect(self._apply)
        trow.addWidget(self.key_black)
        trow.addWidget(self.key_threshold)
        tw = QWidget()
        tw.setLayout(trow)
        form.addRow("Tint", tw)

        flags = QHBoxLayout()
        self.visible = QCheckBox("Visible")
        self.loop = QCheckBox("Loop")
        self.flip_h = QCheckBox("Flip H")
        self.flip_v = QCheckBox("Flip V")
        self.locked = QCheckBox("Lock")
        self.locked.setToolTip("Locked surfaces ignore the mouse in the canvas (good for reference photos once aligned)")
        self.video_audio = QCheckBox("Video audio")
        self.video_audio.setToolTip("Play this video's own audio track while it shows")
        for cb in (self.visible, self.loop, self.flip_h, self.flip_v, self.locked, self.video_audio):
            cb.toggled.connect(self._apply)
            flags.addWidget(cb)
        fw = QWidget()
        fw.setLayout(flags)
        form.addRow("", fw)

        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.1, 10.0)
        self.speed.setSingleStep(0.1)
        self.speed.valueChanged.connect(self._apply)
        form.addRow("Speed", self.speed)

        rrow = QHBoxLayout()
        self.react_param = QComboBox()
        self.react_param.addItems(["none", "opacity", "scale", "gain"])
        self.react_param.setToolTip("Which property follows the music")
        self.react_band = QComboBox()
        self.react_band.addItems(["rms", "bass", "mid", "high"])
        self.react_amount = QSlider(Qt.Horizontal)
        self.react_amount.setRange(0, 100)
        self.react_amount.setValue(50)
        for w in (self.react_param, self.react_band):
            w.currentTextChanged.connect(self._apply)
        self.react_amount.valueChanged.connect(self._apply)
        rrow.addWidget(self.react_param)
        rrow.addWidget(self.react_band)
        rrow.addWidget(self.react_amount, 1)
        rw = QWidget()
        rw.setLayout(rrow)
        form.addRow("React", rw)

        # -- geometry: one page for quads, one for meshes ---------------------
        self.geom = QStackedWidget()
        self.geom.addWidget(self._build_quad_page())
        self.geom.addWidget(self._build_mesh_page())
        form.addRow("Geometry", self.geom)
        lay.addWidget(self.props)

        state.surfacesChanged.connect(self._refresh_list)
        state.selectionChanged.connect(self._refresh_props)
        state.projectReplaced.connect(self._refresh_list)
        self._refresh_list()
        self._refresh_props(None)

    def _build_quad_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        self.corner_spins: list[tuple[QDoubleSpinBox, QDoubleSpinBox]] = []
        for i, label in enumerate(["TL", "TR", "BR", "BL"]):
            sx, sy = QDoubleSpinBox(), QDoubleSpinBox()
            for sp in (sx, sy):
                sp.setRange(-20000, 20000)
                sp.setDecimals(1)
                sp.valueChanged.connect(self._apply_corners)
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(sx, i, 1)
            grid.addWidget(sy, i, 2)
            self.corner_spins.append((sx, sy))
        v.addLayout(grid)
        row = QHBoxLayout()
        reset = QPushButton("Reset to rectangle")
        reset.clicked.connect(self._reset_rect)
        row.addWidget(reset)
        self.grid_n = QSpinBox()
        self.grid_n.setRange(1, 32)
        self.grid_n.setValue(4)
        self.grid_n.setToolTip("Cells per side when converting to a mesh. 1 = two triangles (seam on keystoned quads); 4+ keeps the perspective.")
        conv = QPushButton("To mesh")
        conv.setToolTip("Turn this quad into an n x n triangle grid you can then cut, extend and weld")
        conv.clicked.connect(lambda: self.state.convert_to_mesh(n=self.grid_n.value()))
        row.addWidget(QLabel("n"))
        row.addWidget(self.grid_n)
        row.addWidget(conv)
        v.addLayout(row)
        return page

    def _build_mesh_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        self.mesh_info = QLabel("")
        v.addWidget(self.mesh_info)
        grid = QGridLayout()
        self.vert_spins: list[QDoubleSpinBox] = []
        for i, (label, rng, dec) in enumerate([("X", (-20000, 20000), 1), ("Y", (-20000, 20000), 1), ("U", (-10, 10), 3), ("V", (-10, 10), 3)]):
            sp = QDoubleSpinBox()
            sp.setRange(*rng)
            sp.setDecimals(dec)
            sp.setSingleStep(1.0 if dec == 1 else 0.01)
            sp.valueChanged.connect(self._apply_vertex)
            grid.addWidget(QLabel(label), 0, 2 * i)
            grid.addWidget(sp, 0, 2 * i + 1)
            self.vert_spins.append(sp)
        self.vert_box = QWidget()
        self.vert_box.setLayout(grid)
        self.vert_box.setToolTip("The active vertex (Tab cycles): position in output pixels, texture coordinate 0..1")
        v.addWidget(self.vert_box)
        b1 = QHBoxLayout()
        for text, fn, tip in [
            ("Add tri (A)", self.state.extrude, "Grow a triangle off the highlighted edge (click an edge first), or drop a loose one"),
            ("Del tri", self.state.delete_triangle, "Remove the highlighted triangle (click inside it first)"),
            ("Detach", self.state.detach_triangle, "Split the highlighted triangle off into its own surface"),
            ("Subdivide", self.state.subdivide, "Split every triangle into four (for curved props)"),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            b1.addWidget(b)
        v.addLayout(b1)
        b2 = QHBoxLayout()
        self.uv_btn = QPushButton("UV view (U)")
        self.uv_btn.setCheckable(True)
        self.uv_btn.setToolTip("Show the media flat and drag vertices in texture space instead")
        self.uv_btn.toggled.connect(self.state.set_uv_mode)
        for text, fn, tip in [
            ("Fit UVs", self.state.fit_uvs, "Stretch the media over the mesh's bounding box"),
            ("Weld", self.state.weld, "Merge vertices that sit on top of each other"),
            ("Merge ▲", self.state.merge_with_above, "Fold this mesh into the mesh above it in the list (one media for both)"),
            ("To quad", self.state.convert_to_quad, "Back to a four-corner quad on the mesh's bounding box"),
        ]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            b2.addWidget(b)
        b2.addWidget(self.uv_btn)
        v.addLayout(b2)
        return page

    # -- list ------------------------------------------------------------------
    def _refresh_list(self):
        self._loading = True
        self.list.clear()
        for s in self.state.project.surfaces:
            glyph = {"mask": "▣", "reference": "▦"}.get(s.kind, "△" if s.mesh is not None else "▢")
            it = QListWidgetItem(f"{glyph} {s.name}" + (f"  [{s.item_type}]" if s.item_type else ""))
            it.setData(Qt.UserRole, s.id)
            self.list.addItem(it)
            if s.id == self.state.selected:
                self.list.setCurrentItem(it)
        self._loading = False
        if self.state.selected:
            self._refresh_props(self.state.selected)

    def _on_row(self, row: int):
        if self._loading:
            return
        it = self.list.item(row)
        self.state.select(it.data(Qt.UserRole) if it else None)

    def _add_reference(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a photo of the space", str(self.state.project.base_dir),
                                              "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All files (*)")
        if path:
            self.state.add_reference_image(path)

    def _dup(self):
        if self.state.selected:
            self.state.duplicate_surface(self.state.selected)

    def _del(self):
        if self.state.selected:
            self.state.remove_surface(self.state.selected)

    def _move(self, d: int):
        if self.state.selected:
            self.state.move_surface(self.state.selected, d)

    # -- properties --------------------------------------------------------------
    def _refresh_props(self, sid):
        s = self.state.selected_surface
        self.props.setEnabled(s is not None)
        if s is None:
            return
        self._loading = True
        self.name.setText(s.name)
        self.item_type.setCurrentText(s.item_type)
        self.notes.setText(s.notes)
        self.locked.setChecked(s.locked)
        self.video_audio.setChecked(s.video_audio)
        r = s.react or {}
        self.react_param.setCurrentText(r.get("param", "none") if r else "none")
        self.react_band.setCurrentText(r.get("band", "rms"))
        self.react_amount.setValue(int(round(float(r.get("amount", 0.5)) * 100)))
        self.media.setText(self._media_text(s.media))
        self.media_row.setEnabled(s.kind != "mask")
        self.opacity.setValue(int(round(s.opacity * 100)))
        self._show_tint(s.tint)
        self.key_black.setChecked(s.key_black)
        self.key_threshold.setValue(s.key_threshold)
        self.visible.setChecked(s.visible)
        self.loop.setChecked(s.loop)
        self.flip_h.setChecked(s.flip_h)
        self.flip_v.setChecked(s.flip_v)
        self.speed.setValue(s.speed)
        if s.mesh is not None:
            self.geom.setCurrentIndex(1)
            m = s.mesh
            self.mesh_info.setText(f"{len(m.verts)} vertices, {len(m.tris)} triangles"
                                   + (f"   active △{self.state.active_tri}" if self.state.active_tri is not None else "")
                                   + ("   edge picked: A extrudes" if self.state.active_edge is not None else ""))
            i = self.state.active_corner
            ok = i is not None and i < len(m.verts)
            self.vert_box.setEnabled(ok)
            if ok:
                for sp, val in zip(self.vert_spins, m.verts[i]):
                    sp.setValue(val)
            self.uv_btn.setChecked(self.state.uv_mode)
        else:
            self.geom.setCurrentIndex(0)
            for (sx, sy), c in zip(self.corner_spins, s.corners):
                sx.setValue(c[0])
                sy.setValue(c[1])
        for it in [self.list.item(i) for i in range(self.list.count())]:
            if it.data(Qt.UserRole) == sid:
                self.list.setCurrentItem(it)
        self._loading = False

    @staticmethod
    def _media_text(media) -> str:
        if not media:
            return ""
        from promap.media.generative import is_generative, media_label

        return f"animation: {media_label(media)}" if is_generative(media) else media

    def _show_tint(self, tint):
        c = QColor.fromRgbF(*[max(0.0, min(1.0, v)) for v in tint])
        self.tint_btn.setStyleSheet(f"background-color: {c.name()}; color: {'black' if c.lightness() > 110 else 'white'}")
        self.tint_btn.setText(c.name() if tint != [1.0, 1.0, 1.0] else "none")

    def _pick_tint(self):
        s = self.state.selected_surface
        if s is None:
            return
        c = QColorDialog.getColor(QColor.fromRgbF(*s.tint), self, "Tint colour")
        if not c.isValid():
            return
        s.tint = [c.redF(), c.greenF(), c.blueF()]
        self._show_tint(s.tint)
        self.state.surface_edited(s.id, geometry_only=True)

    def _reset_tint(self):
        s = self.state.selected_surface
        if s is None:
            return
        s.tint = [1.0, 1.0, 1.0]
        self._show_tint(s.tint)
        self.state.surface_edited(s.id, geometry_only=True)

    def _apply(self):
        s = self.state.selected_surface
        if s is None or self._loading:
            return
        s.name = self.name.text() or s.name
        s.item_type = self.item_type.currentText().strip()
        s.notes = self.notes.text()
        s.locked = self.locked.isChecked()
        s.video_audio = self.video_audio.isChecked()
        param = self.react_param.currentText()
        s.react = None if param == "none" else {"param": param, "band": self.react_band.currentText(),
                                                 "amount": self.react_amount.value() / 100.0}
        s.opacity = self.opacity.value() / 100.0
        s.key_black = self.key_black.isChecked()
        s.key_threshold = self.key_threshold.value()
        s.visible = self.visible.isChecked()
        s.loop = self.loop.isChecked()
        s.flip_h = self.flip_h.isChecked()
        s.flip_v = self.flip_v.isChecked()
        s.speed = self.speed.value()
        self.state.surface_edited(s.id, geometry_only=True)

    def _apply_corners(self):
        s = self.state.selected_surface
        if s is None or self._loading or s.mesh is not None:
            return
        s.corners = [[sx.value(), sy.value()] for sx, sy in self.corner_spins]
        self.state.surface_edited(s.id, geometry_only=True)

    def _apply_vertex(self):
        s = self.state.selected_surface
        i = self.state.active_corner
        if s is None or self._loading or s.mesh is None or i is None or i >= len(s.mesh.verts):
            return
        s.mesh.verts[i] = [sp.value() for sp in self.vert_spins]
        self.state.surface_edited(s.id, geometry_only=True)

    def _reset_rect(self):
        s = self.state.selected_surface
        if s is None or s.mesh is not None:
            return
        cx, cy = centroid(s.corners)
        xs = [c[0] for c in s.corners]
        ys = [c[1] for c in s.corners]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        s.corners = rect_corners(cx - w / 2, cy - h / 2, w, h)
        self.state.surface_edited(s.id, geometry_only=True)
        self._refresh_props(s.id)

    def _browse(self):
        s = self.state.selected_surface
        if s is None:
            return
        start = str(self.state.project.base_dir)
        path, _ = QFileDialog.getOpenFileName(self, "Choose media", start, MEDIA_FILTER)
        if not path:
            return
        s.media = self.state.project.relativize_media(path)
        self.media.setText(s.media)
        self.state.surface_edited(s.id)

    def _clear_media(self):
        s = self.state.selected_surface
        if s is None:
            return
        s.media = None
        self.media.setText("")
        self.state.surface_edited(s.id)
