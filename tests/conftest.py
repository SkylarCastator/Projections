"""Test-wide setup: never touch cameras (the probe lights the webcam LED), render offscreen."""
import os

os.environ.setdefault("PROMAP_NO_CAMERA_SCAN", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
