"""User settings: ComfyUI endpoint and generation defaults."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QLineEdit, QSpinBox, QPushButton, QHBoxLayout, QVBoxLayout, QLabel,
)

from promap.model.settings import Settings


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.url = QLineEdit(settings.comfy_url)
        self.url.setPlaceholderText("http://gpu-box:8188")
        self.gen_dir = QLineEdit(settings.generated_dir)
        self.frames = QSpinBox()
        self.frames.setRange(1, 2000)
        self.frames.setValue(settings.default_frames)
        self.fps = QSpinBox()
        self.fps.setRange(1, 120)
        self.fps.setValue(settings.default_fps)
        self.max_side = QSpinBox()
        self.max_side.setRange(64, 4096)
        self.max_side.setSingleStep(16)
        self.max_side.setValue(settings.max_side)
        self.negative = QLineEdit(settings.negative_prompt)
        form.addRow("ComfyUI URL", self.url)
        form.addRow("Generated media dir", self.gen_dir)
        form.addRow("Default frames", self.frames)
        form.addRow("Default fps", self.fps)
        form.addRow("Max clip side (px)", self.max_side)
        form.addRow("Default negative", self.negative)
        lay.addLayout(form)
        self.status = QLabel()
        lay.addWidget(self.status)
        row = QHBoxLayout()
        test = QPushButton("Test connection")
        test.clicked.connect(self._test)
        ok = QPushButton("Save")
        ok.clicked.connect(self._save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(test)
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        lay.addLayout(row)

    def _test(self):
        from promap.comfy.client import ComfyClient, ComfyError

        try:
            stats = ComfyClient(self.url.text().strip(), timeout=5).system_stats()
            dev = (stats.get("devices") or [{}])[0]
            vram = dev.get("vram_total", 0) / 2**30
            self.status.setText(f"OK: {dev.get('name', 'device?')}  {vram:.1f} GB VRAM  ComfyUI {stats.get('system', {}).get('comfyui_version', '')}")
        except ComfyError as exc:
            self.status.setText(f"Failed: {exc}")

    def _save(self):
        s = self.settings
        s.comfy_url = self.url.text().strip() or s.comfy_url
        s.generated_dir = self.gen_dir.text().strip() or s.generated_dir
        s.default_frames = self.frames.value()
        s.default_fps = self.fps.value()
        s.max_side = self.max_side.value()
        s.negative_prompt = self.negative.text()
        s.save()
        self.accept()
