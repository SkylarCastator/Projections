"""USB camera discovery and a background frame grabber.

`list_cameras()` reads /sys/class/video4linux for names and keeps only the nodes that
OpenCV can actually pull a frame from (UVC webcams expose a second metadata node that
opens but never delivers video). `Camera` keeps the newest frame in a thread so the UI
and the calibration runner just ask for `latest()`.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class CameraInfo:
    index: int
    name: str
    path: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.path})"


def _sys_name(index: int) -> str:
    p = Path(f"/sys/class/video4linux/video{index}/name")
    try:
        return p.read_text().strip()
    except OSError:
        return f"video{index}"


NO_SCAN_ENV = "PROMAP_NO_CAMERA_SCAN"  # set (to anything) to skip probing: tests, headless runs


def list_cameras(probe: bool = True, max_index: int = 16) -> list[CameraInfo]:
    """Video devices that deliver frames, best (external) first.

    Probing opens each node and reads a frame, which lights a laptop's webcam LED for a
    moment; with PROMAP_NO_CAMERA_SCAN in the environment nothing is opened and the list is
    empty."""
    import os

    if os.environ.get(NO_SCAN_ENV):
        return []
    import cv2

    found: list[CameraInfo] = []
    try:  # UVC metadata nodes open but never deliver video; OpenCV warns loudly about each one
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
    except Exception:  # pragma: no cover
        pass
    nodes = sorted(Path("/dev").glob("video*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 999)
    for node in nodes:
        if not node.name[5:].isdigit():
            continue
        idx = int(node.name[5:])
        if idx > max_index:
            continue
        name = _sys_name(idx)
        if "gspca" in name.lower() or "kinect" in name.lower():
            # the kernel's gspca_kinect driver exposes the Kinect v1 camera as a webcam; opening it
            # here would lock libfreenect out, and it is listed as a Kinect device anyway
            continue
        if probe:
            cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
            ok = cap.isOpened()
            if ok:
                ok, frame = cap.read()
                ok = ok and frame is not None
            cap.release()
            if not ok:
                continue
        found.append(CameraInfo(idx, name, str(node)))
    # external USB webcams usually enumerate after the built-in one; put them first
    found.sort(key=lambda c: (0 if idx_is_external(c) else 1, c.index))
    return found


def idx_is_external(info: CameraInfo) -> bool:
    n = info.name.lower()
    return not any(k in n for k in ("integrated", "bisoncam", "built-in", "builtin", "internal", "facetime"))


class Camera:
    """Threaded grabber: open, then `latest()` returns (rgb frame, sequence number)."""

    def __init__(self, index: int, size: tuple[int, int] = (1280, 720), fps: float = 30.0):
        import cv2

        self.index = index
        self.cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise RuntimeError(f"cannot open camera {index}")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.size = (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        self._lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"camera{index}", daemon=True)
        self._thread.start()

    def _loop(self):
        import cv2

        while not self._stop.is_set():
            ok, bgr = self.cap.read()
            if not ok or bgr is None:
                time.sleep(0.02)
                continue
            rgb = np.ascontiguousarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            with self._lock:
                self._frame = rgb
                self._seq += 1

    def latest(self) -> tuple[np.ndarray | None, int]:
        with self._lock:
            return self._frame, self._seq

    def wait_fresh(self, after_seq: int, count: int = 2, timeout: float = 2.0) -> np.ndarray | None:
        """Block until `count` new frames have arrived since `after_seq`; returns the newest."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            frame, seq = self.latest()
            if frame is not None and seq >= after_seq + count:
                return frame
            time.sleep(0.01)
        return self.latest()[0]

    def set_control(self, prop: int, value: float):
        self.cap.set(prop, value)

    def lock_exposure(self, on: bool):
        """Fix exposure/white balance so calibration frames are comparable (best effort)."""
        import cv2

        try:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1 if on else 3)  # V4L2: 1 = manual, 3 = aperture priority
            self.cap.set(cv2.CAP_PROP_AUTO_WB, 0 if on else 1)
        except Exception:  # pragma: no cover - driver dependent
            pass

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1.0)
        self.cap.release()
