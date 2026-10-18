"""Preferences: one dialog, a tab per concern (hardware library, media generation)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QTabWidget, QDialogButtonBox

from promap.ui.hardware_dialog import HardwareDialog
from promap.ui.settings_dialog import SettingsDialog


class PreferencesDialog(QDialog):
    TABS = ("Hardware", "Generation")

    def __init__(self, state, hardware, settings, parent=None, tab: str = "Hardware"):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.resize(820, 600)
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.hardware_page = HardwareDialog(state, hardware)
        self.generation_page = SettingsDialog(settings)
        for page, title in ((self.hardware_page, "Hardware"), (self.generation_page, "Generation")):
            page.setWindowFlags(Qt.Widget)  # a QDialog hosted as a plain page
            self.tabs.addTab(page, title)
        # the Generation page's own Save / Cancel close the whole dialog
        self.generation_page.accepted.connect(self.accept)
        self.generation_page.rejected.connect(self.reject)
        self.hardware_page.accepted.connect(self.accept)  # its Close button
        lay.addWidget(self.tabs, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.tabs.setCurrentIndex(max(0, self.TABS.index(tab) if tab in self.TABS else 0))
