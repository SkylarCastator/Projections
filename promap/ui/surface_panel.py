"""Surface list + property editor."""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QColor
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QPushButton, QFormLayout,
    QLineEdit, QSlider, QCheckBox, QDoubleSpinBox, QGroupBox, QFileDialog, QLabel, QGridLayout, QComboBox,
    QStackedWidget, QSpinBox, QColorDialog, QSizePolicy,
)

from promap.media.generative import EFFECTS, is_generative, parse_media, make_media
from promap.media.sources import IMAGE_EXTS, ANIM_EXTS, VIDEO_EXTS

from promap.model.geometry import rect_corners, centroid
from promap.model.project import ITEM_TYPES
from promap.ui.state import AppState
from promap.ui.surface_menu import surface_menu

MEDIA_FILTER = "Media (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.mp4 *.mov *.avi *.mkv *.webm *.m4v);;All files (*)"

# What a surface can show. The picker row and the options below it follow the chosen type.
MEDIA_TYPES = [
    ("None", "none"),
    ("Image", "image"),
    ("Video", "video"),
    ("Animated image (GIF / WebP)", "anim"),
    ("Animation (built-in)", "gen"),
]
MEDIA_FILTERS = {
    "image": "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff);;All files (*)",
    "video": "Video (*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.mpg *.mpeg);;All files (*)",
    "anim": "Animated images (*.gif *.webp *.apng);;All files (*)",
}


def media_kind(media) -> str:
    """Which MEDIA_TYPES key a media string is."""
    if not media:
        return "none"
    if is_generative(media):
        return "gen"
    ext = Path(media).suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in ANIM_EXTS:
        return "anim"
    return "image"


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

        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.list.installEventFilter(self)

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

        # -- media: a type, then a picker that fits the type ------------------------------
        self.form = form
        self.media_type = QComboBox()
        for label, key in MEDIA_TYPES:
            self.media_type.addItem(label, key)
        self.media_type.setToolTip("What this surface shows. The picker and the options below follow the type.")
        self.media_type.currentIndexChanged.connect(self._type_chosen)
        form.addRow("Type", self.media_type)

        self.media = QLineEdit()
        self.media.setReadOnly(True)
        browse = QPushButton("…")
        browse.setFixedWidth(28)
        browse.setToolTip("Choose a file")
        browse.clicked.connect(self._browse)
        frow = QHBoxLayout()
        frow.setContentsMargins(0, 0, 0, 0)
        frow.addWidget(self.media, 1)
        frow.addWidget(browse)
        file_page = QWidget()
        file_page.setLayout(frow)

        self.anim_combo = QComboBox()
        for name, eff in EFFECTS.items():
            self.anim_combo.addItem(f"{eff.title}   ·  {eff.group}", name)
            self.anim_combo.setItemData(self.anim_combo.count() - 1, eff.blurb, Qt.ToolTipRole)
        self.anim_combo.currentIndexChanged.connect(self._anim_chosen)
        anim_edit = QPushButton("Parameters…")
        anim_edit.setToolTip("Open the Animations window on this animation; edits apply live")
        anim_edit.clicked.connect(lambda: self.state.windowRequested.emit("animations"))
        arow = QHBoxLayout()
        arow.setContentsMargins(0, 0, 0, 0)
        arow.addWidget(self.anim_combo, 1)
        arow.addWidget(anim_edit)
        anim_page = QWidget()
        anim_page.setLayout(arow)

        self.media_pick = QStackedWidget()
        self.media_pick.addWidget(file_page)  # 0: image / video / animated image
        self.media_pick.addWidget(anim_page)  # 1: built-in animation
        self.media_pick.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow("Media", self.media_pick)

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
        self.react_row = rw
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

    def add_reference(self):
        """Pick a photo of the space and add it as an editor-only reference layer."""
        path, _ = QFileDialog.getOpenFileName(self, "Choose a photo of the space", str(self.state.project.base_dir),
                                              "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All files (*)")
        if path:
            self.state.add_reference_image(path)

    def _list_menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            return
        self.list.setCurrentItem(it)  # right-click selects, like every other list
        m = surface_menu(self.state, it.data(Qt.UserRole), self)
        if m is not None:
            m.exec(self.list.viewport().mapToGlobal(pos))

    def eventFilter(self, obj, ev):
        if obj is self.list and ev.type() == QEvent.KeyPress and ev.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            if self.state.selected:
                self.state.remove_surface(self.state.selected)
            return True
        return super().eventFilter(obj, ev)

    # -- properties --------------------------------------------------------------
    def _refresh_props(self, sid):
        s = self.state.selected_surface
        self.props.setEnabled(s is not None)
        if s is None:
            for row in (self.media_type, self.media_pick, self.speed, self.react_row):
                self.form.setRowVisible(row, False)
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
        self._layout_media(s)
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

    # -- media type ------------------------------------------------------------------
    def _layout_media(self, s):
        """Show the picker and the options that make sense for what the surface shows."""
        kind = media_kind(s.media)
        is_media = s.kind == "media"
        self.media_type.setCurrentIndex(self.media_type.findData(kind))
        self.form.setRowVisible(self.media_type, is_media)
        self.form.setRowVisible(self.media_pick, s.kind != "mask" and kind != "none")
        self.media_pick.setCurrentIndex(1 if kind == "gen" else 0)
        if kind == "gen":
            try:
                self.anim_combo.setCurrentIndex(self.anim_combo.findData(parse_media(s.media)[0].name))
            except ValueError:
                pass
        else:
            self.media.setText(s.media or "")
        moving = kind in ("video", "anim", "gen")
        self.loop.setVisible(kind in ("video", "anim"))
        self.video_audio.setVisible(kind == "video")
        self.form.setRowVisible(self.speed, moving)
        self.form.setRowVisible(self.react_row, is_media and kind != "none")

    def _set_media(self, s, media):
        s.media = media
        self.state.surface_edited(s.id)
        self._refresh_props(s.id)

    def _type_chosen(self, _idx: int):
        s = self.state.selected_surface
        if self._loading or s is None:
            return
        kind = self.media_type.currentData()
        cur = media_kind(s.media)
        if kind == cur:
            return
        if kind == "none":
            self._set_media(s, None)
        elif kind == "gen":
            self._set_media(s, make_media(self.anim_combo.currentData() or next(iter(EFFECTS))))
        else:
            path = self._pick_file(kind)
            if path:
                self._set_media(s, path)
            else:  # cancelled: the combo goes back to what the surface really shows
                self._loading = True
                self.media_type.setCurrentIndex(self.media_type.findData(cur))
                self._loading = False

    def _anim_chosen(self, _idx: int):
        s = self.state.selected_surface
        if self._loading or s is None:
            return
        name = self.anim_combo.currentData()
        if name and (not is_generative(s.media) or parse_media(s.media)[0].name != name):
            self._set_media(s, make_media(name))

    def _pick_file(self, kind: str) -> str | None:
        start = str(self.state.project.base_dir)
        path, _ = QFileDialog.getOpenFileName(self, f"Choose {dict((k, l) for l, k in MEDIA_TYPES)[kind].lower()}", start,
                                              MEDIA_FILTERS.get(kind, MEDIA_FILTER))
        return self.state.project.relativize_media(path) if path else None

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
        kind = media_kind(s.media)
        path = self._pick_file(kind if kind in MEDIA_FILTERS else "image")
        if path:
            self._set_media(s, path)
