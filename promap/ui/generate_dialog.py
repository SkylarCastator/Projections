"""Generate media for a surface with ComfyUI: prepare source image, bind template, run, collect."""
from __future__ import annotations

import logging
import random
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QComboBox, QPlainTextEdit, QLineEdit, QSpinBox, QCheckBox, QPushButton,
    QHBoxLayout, QVBoxLayout, QLabel, QProgressBar, QListWidget, QListWidgetItem, QMessageBox, QWidget,
)

from promap.comfy import prepare
from promap.comfy.client import ComfyClient, ComfyError
from promap.comfy.templates import TemplateStore, bind, validate
from promap.model.project import Cue
from promap.model.settings import Settings
from promap.ui.state import AppState

log = logging.getLogger(__name__)
SOURCE_MODES = ["rectified crop of the surface", "full reference photo", "none (text to video)"]


class _Worker(QThread):
    progress = Signal(object, str)
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, url: str, workflow: dict, source_png: Path | None, dest_dir: Path, stem: str):
        super().__init__()
        self.url, self.workflow, self.source_png, self.dest_dir, self.stem = url, workflow, source_png, dest_dir, stem
        self.cancel = False

    def run(self):
        try:
            client = ComfyClient(self.url)
            wf = self.workflow
            if self.source_png is not None:
                self.progress.emit(None, "uploading source image")
                name = client.upload_image(self.source_png)
                wf = bind(wf, {"image": name})
            self.progress.emit(None, "queueing")
            pid = client.queue(wf)
            outputs = client.wait(pid, on_progress=lambda f, m: self.progress.emit(f, m), should_cancel=lambda: self.cancel)
            files = client.output_files(outputs)
            if not files:
                raise ComfyError("workflow produced no files (does it end in a Save node?)")
            paths = []
            for i, f in enumerate(files):
                self.progress.emit(None, f"downloading {f['filename']}")
                paths.append(client.download(f, self.dest_dir, stem=f"{self.stem}" if i == 0 else f"{self.stem}_{i}"))
            self.done.emit([str(p) for p in paths])
        except ComfyError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # network / parsing surprises
            log.exception("generation failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class GenerateDialog(QDialog):
    def __init__(self, state: AppState, settings: Settings, parent=None):
        super().__init__(parent)
        self.state = state
        self.settings = settings
        self.store = TemplateStore()
        self.store.install_examples()
        self.worker: _Worker | None = None
        self.setWindowTitle("Generate media with ComfyUI")
        self.resize(900, 640)
        root = QHBoxLayout(self)
        left = QVBoxLayout()
        form = QFormLayout()

        self.surface = QComboBox()
        for s in state.project.surfaces:
            if s.kind == "media":
                self.surface.addItem(s.name, s.id)
        if state.selected:
            idx = self.surface.findData(state.selected)
            if idx >= 0:
                self.surface.setCurrentIndex(idx)
        self.surface.currentIndexChanged.connect(self._auto_size)
        form.addRow("Surface", self.surface)

        self.photo = QComboBox()
        for s in state.project.surfaces:
            if s.kind == "reference" and s.media:
                self.photo.addItem(s.name, s.id)
        form.addRow("Reference photo", self.photo)

        self.mode = QComboBox()
        self.mode.addItems(SOURCE_MODES)
        if self.photo.count() == 0:
            self.mode.setCurrentIndex(2)
        self.mode.currentIndexChanged.connect(self._preview)
        form.addRow("Source", self.mode)

        trow = QHBoxLayout()
        self.template_info = QLabel()
        self.template_info.setWordWrap(True)
        self.template_info.setStyleSheet("color:#888")
        self.template = QComboBox()
        self._fill_templates()
        self.template.currentTextChanged.connect(self._describe)
        reload = QPushButton("⟳")
        reload.setFixedWidth(28)
        reload.clicked.connect(self._fill_templates)
        trow.addWidget(self.template, 1)
        trow.addWidget(reload)
        tw = QWidget()
        tw.setLayout(trow)
        form.addRow("Template", tw)
        form.addRow("", self.template_info)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("a jack-o'-lantern grin flickers with inner flame, embers drift upward, eerie fog, halloween night, cinematic")
        self.prompt.setMaximumHeight(90)
        form.addRow("Prompt", self.prompt)
        self.negative = QLineEdit(settings.negative_prompt)
        form.addRow("Negative", self.negative)

        srow = QHBoxLayout()
        self.seed = QSpinBox()
        self.seed.setRange(0, 2**31 - 1)
        self.seed.setValue(random.randint(0, 2**31 - 1))
        self.random_seed = QCheckBox("random each run")
        self.random_seed.setChecked(True)
        srow.addWidget(self.seed, 1)
        srow.addWidget(self.random_seed)
        sw = QWidget()
        sw.setLayout(srow)
        form.addRow("Seed", sw)

        frow = QHBoxLayout()
        self.frames = QSpinBox()
        self.frames.setRange(1, 2000)
        self.frames.setValue(settings.default_frames)
        self.fps = QSpinBox()
        self.fps.setRange(1, 120)
        self.fps.setValue(settings.default_fps)
        self.width = QSpinBox()
        self.width.setRange(16, 4096)
        self.width.setSingleStep(16)
        self.height = QSpinBox()
        self.height.setRange(16, 4096)
        self.height.setSingleStep(16)
        auto = QPushButton("auto size")
        auto.clicked.connect(self._auto_size)
        for lbl, w in (("frames", self.frames), ("fps", self.fps), ("w", self.width), ("h", self.height)):
            frow.addWidget(QLabel(lbl))
            frow.addWidget(w)
        frow.addWidget(auto)
        fw = QWidget()
        fw.setLayout(frow)
        form.addRow("Clip", fw)
        left.addLayout(form)

        brow = QHBoxLayout()
        self.go = QPushButton("Generate")
        self.go.setStyleSheet("font-weight:bold")
        self.go.clicked.connect(self._generate)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel)
        brow.addWidget(self.go)
        brow.addWidget(self.cancel_btn)
        brow.addStretch(1)
        left.addLayout(brow)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        left.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        left.addWidget(self.log)
        root.addLayout(left, 3)

        right = QVBoxLayout()
        right.addWidget(QLabel("Source image"))
        self.preview = QLabel()
        self.preview.setFixedSize(320, 200)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background:#222;border:1px solid #444")
        right.addWidget(self.preview)
        right.addWidget(QLabel("Results"))
        self.results = QListWidget()
        right.addWidget(self.results, 1)
        rrow = QHBoxLayout()
        assign = QPushButton("Assign to surface")
        assign.clicked.connect(self._assign)
        as_cue = QPushButton("Add as new cue")
        as_cue.clicked.connect(self._add_cue)
        rrow.addWidget(assign)
        rrow.addWidget(as_cue)
        right.addLayout(rrow)
        root.addLayout(right, 2)

        self._auto_size()
        self._describe()

    # -- helpers ---------------------------------------------------------------------
    def _logln(self, text: str):
        self.log.appendPlainText(text)

    def _fill_templates(self):
        cur = self.template.currentText()
        self.template.blockSignals(True)
        self.template.clear()
        self.template.addItems(self.store.names())
        pick = cur or self.settings.default_template
        if pick and self.template.findText(pick) >= 0:
            self.template.setCurrentText(pick)
        self.template.blockSignals(False)
        self._describe()

    def _describe(self):
        name = self.template.currentText()
        if not name:
            self.template_info.setText(f"No templates in {self.store.dir}")
            return
        try:
            self.template_info.setText(self.store.describe(name))
        except Exception as exc:
            self.template_info.setText(f"unreadable: {exc}")

    def _surface(self):
        return self.state.project.surface_by_id(self.surface.currentData())

    def _photo_surface(self):
        return self.state.project.surface_by_id(self.photo.currentData()) if self.photo.count() else None

    def _auto_size(self):
        s = self._surface()
        if s is None:
            return
        w, h = prepare.suggested_size(s.outline_corners, max_side=self.settings.max_side)
        self.width.setValue(w)
        self.height.setValue(h)
        self._preview()

    def _source_image(self) -> np.ndarray | None:
        mode = self.mode.currentIndex()
        s, ref = self._surface(), self._photo_surface()
        if mode == 2 or s is None:
            return None
        if ref is None or not ref.media:
            raise ValueError("no reference photo in the project (add one with + Ref image)")
        photo = prepare.load_photo(self.state.project.resolve_media(ref.media))
        size = (self.width.value(), self.height.value())
        if mode == 0:
            return prepare.rectify(photo, ref.corners, s.outline_corners, size)
        return prepare.full_photo(photo, ref.corners, self.state.project.output_size, size)

    def _preview(self):
        try:
            img = self._source_image()
        except Exception as exc:
            self.preview.setText(str(exc))
            return
        if img is None:
            self.preview.setText("no source image\n(text to video)")
            self.preview.setPixmap(QPixmap())
            return
        h, w = img.shape[:2]
        qimg = QImage(np.ascontiguousarray(img).data, w, h, 3 * w, QImage.Format_RGB888).copy()
        self.preview.setPixmap(QPixmap.fromImage(qimg).scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # -- run -------------------------------------------------------------------------------
    def _generate(self):
        s = self._surface()
        name = self.template.currentText()
        if s is None or not name:
            QMessageBox.warning(self, "Generate", "Pick a surface and a template first.")
            return
        try:
            wf = self.store.load(name)
        except Exception as exc:
            QMessageBox.critical(self, "Template", f"Cannot read template: {exc}")
            return
        for problem in validate(wf):
            self._logln(f"warning: {problem}")
        if self.random_seed.isChecked():
            self.seed.setValue(random.randint(0, 2**31 - 1))
        gen_dir = self.state.project.base_dir / self.settings.generated_dir
        stem = f"{s.name.replace(' ', '_')}_{self.seed.value()}"
        source_png = None
        try:
            img = self._source_image()
        except Exception as exc:
            QMessageBox.warning(self, "Source image", str(exc))
            return
        if img is not None:
            source_png = prepare.save_png(img, gen_dir / "sources" / f"{stem}.png")
        values = {
            "prompt": self.prompt.toPlainText().strip() or None,
            "negative": self.negative.text().strip() or None,
            "seed": self.seed.value(),
            "frames": self.frames.value(),
            "width": self.width.value(),
            "height": self.height.value(),
            "fps": self.fps.value(),
            "prefix": f"promap/{stem}",
        }
        wf = bind(wf, values)
        self.worker = _Worker(self.settings.comfy_url, wf, source_png, gen_dir, stem)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.go.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.bar.setValue(0)
        self._logln(f"→ {self.settings.comfy_url}  template {name}  seed {self.seed.value()}  {self.width.value()}x{self.height.value()}x{self.frames.value()}")
        self.worker.start()

    def _cancel(self):
        if self.worker:
            self.worker.cancel = True
            self._logln("cancelling…")

    def _on_progress(self, frac, msg):
        if frac is not None:
            self.bar.setValue(int(frac * 100))
        self._logln(msg)

    def _on_done(self, paths: list):
        self.bar.setValue(100)
        self.go.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        for p in paths:
            rel = self.state.project.relativize_media(p)
            it = QListWidgetItem(rel)
            it.setData(Qt.UserRole, rel)
            self.results.addItem(it)
            self.results.setCurrentItem(it)
        self._logln(f"done: {len(paths)} file(s)")

    def _on_failed(self, msg: str):
        self.go.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self._logln(f"FAILED: {msg}")

    # -- use results -----------------------------------------------------------------------
    def _picked(self) -> str | None:
        it = self.results.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _assign(self):
        s, rel = self._surface(), self._picked()
        if s is None or rel is None:
            return
        s.media = rel
        self.state.surface_edited(s.id)
        self._logln(f"assigned {rel} to {s.name}")

    def _add_cue(self):
        s, rel = self._surface(), self._picked()
        if s is None or rel is None:
            return
        media = self.state.editor_assignments()
        media[s.id] = rel
        cue = Cue(name=f"Generated: {s.name}", media=media)
        self.state.project.cues.append(cue)
        self.state.cues_edited()
        self._logln(f"added cue '{cue.name}'")
