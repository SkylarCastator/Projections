"""Kinect v1 (Xbox 360) through libfreenect's synchronous C API via ctypes: nothing to compile.

`sudo apt install freenect libfreenect0.5t64 libfreenect-bin` provides the library and the
udev rule. Per grab: RGB 640x480 and depth 640x480 uint16 millimetres *registered onto the
RGB pixels* (FREENECT_DEPTH_REGISTERED), so colour and depth share a pixel grid and a
Gray-code calibration through the RGB camera maps depth pixels to the projector directly.

Structured light: the sensor needs to see its own infrared dot pattern, so very dark,
glossy or sunlit surfaces and anything closer than ~0.8 m come back as 0 (no depth).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SIZE = (640, 480)
# RGB camera intrinsics for the registered 640x480 image (libfreenect defaults)
INTRINSICS = (594.21, 591.04, 339.5, 242.7)  # fx, fy, cx, cy
DEPTH_REGISTERED = 4  # freenect_depth_format: mm, aligned to the RGB image
DEPTH_MM = 5  # mm, unaligned
VIDEO_RGB = 0
LED_OFF, LED_GREEN, LED_RED, LED_YELLOW, LED_BLINK_GREEN = 0, 1, 2, 3, 4

_lib = None


def _load():
    global _lib
    if _lib is None:
        name = ctypes.util.find_library("freenect_sync") or "libfreenect_sync.so.0.5"
        lib = ctypes.CDLL(name)
        vp, u32 = ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)
        lib.freenect_sync_get_depth.argtypes = [vp, u32, ctypes.c_int, ctypes.c_int]
        lib.freenect_sync_get_video.argtypes = [vp, u32, ctypes.c_int, ctypes.c_int]
        lib.freenect_sync_set_tilt_degs.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.freenect_sync_set_led.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.freenect_sync_stop.argtypes = []
        _lib = lib
    return _lib


def binding_available() -> tuple[bool, str]:
    try:
        _load()
    except OSError as exc:
        return False, f"libfreenect not installed ({exc}); sudo apt install freenect libfreenect0.5t64 libfreenect-bin"
    return True, ""


def plugged_in() -> bool:
    """True when the camera device (045e:02ae) is on the bus; the motor alone means no 12 V."""
    for d in Path("/sys/bus/usb/devices").glob("[0-9]-*"):
        try:
            if (d / "idVendor").read_text().strip() == "045e" and (d / "idProduct").read_text().strip() == "02ae":
                return True
        except OSError:
            continue
    return False


def motor_only() -> bool:
    found = {"02b0": False, "02ae": False}
    for d in Path("/sys/bus/usb/devices").glob("[0-9]-*"):
        try:
            if (d / "idVendor").read_text().strip() == "045e":
                pid = (d / "idProduct").read_text().strip()
                if pid in found:
                    found[pid] = True
        except OSError:
            continue
    return found["02b0"] and not found["02ae"]


def status() -> str:
    if plugged_in():
        ok, why = binding_available()
        return "Kinect v1 ready" if ok else why
    if motor_only():
        return "Kinect v1: only the motor is on the bus; the camera board has no 12 V (power-cycle: brick first, then USB)"
    return "Kinect v1 not detected on USB"


@dataclass
class V1Frame:
    rgb: np.ndarray  # (480, 640, 3) uint8
    depth: np.ndarray  # (480, 640) float32 mm, 0 = no depth, registered to rgb
    seq: int = 0
    t: float = field(default_factory=time.monotonic)


class KinectV1:
    """Threaded grabber: open, then `latest()` returns the newest V1Frame."""

    def __init__(self, index: int = 0):
        ok, why = binding_available()
        if not ok:
            raise RuntimeError(why)
        if not plugged_in():
            raise RuntimeError(status())
        self.lib = _load()
        self.index = index
        self._lock = threading.Lock()
        self._frame: V1Frame | None = None
        self._seq = 0
        self._stop = threading.Event()
        # first call opens the device and starts streaming; fail loudly here, not in the thread
        buf, ts = ctypes.c_void_p(), ctypes.c_uint32()
        if self.lib.freenect_sync_get_depth(ctypes.byref(buf), ctypes.byref(ts), index, DEPTH_REGISTERED) != 0:
            raise RuntimeError("cannot open Kinect v1 (is another program using it?)")
        self.set_led(LED_GREEN)
        self._thread = threading.Thread(target=self._loop, name="kinect1", daemon=True)
        self._thread.start()

    def _loop(self):
        lib = self.lib
        buf, ts = ctypes.c_void_p(), ctypes.c_uint32()
        while not self._stop.is_set():
            if lib.freenect_sync_get_depth(ctypes.byref(buf), ctypes.byref(ts), self.index, DEPTH_REGISTERED) != 0:
                time.sleep(0.05)
                continue
            depth = np.ctypeslib.as_array(ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint16)), shape=(SIZE[1], SIZE[0])).astype(np.float32)
            if lib.freenect_sync_get_video(ctypes.byref(buf), ctypes.byref(ts), self.index, VIDEO_RGB) != 0:
                continue
            rgb = np.ctypeslib.as_array(ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)), shape=(SIZE[1], SIZE[0], 3)).copy()
            with self._lock:
                self._seq += 1
                self._frame = V1Frame(rgb, np.ascontiguousarray(depth), self._seq)

    def latest(self) -> tuple[V1Frame | None, int]:
        with self._lock:
            return self._frame, self._seq

    def set_led(self, mode: int):
        try:
            self.lib.freenect_sync_set_led(mode, self.index)
        except Exception:  # pragma: no cover
            pass

    def set_tilt(self, degrees: int):
        """Motor tilt, -30..30 degrees."""
        self.lib.freenect_sync_set_tilt_degs(int(max(-30, min(30, degrees))), self.index)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.set_led(LED_BLINK_GREEN)
        try:
            self.lib.freenect_sync_stop()
        except Exception:  # pragma: no cover
            pass


class KinectV1CameraAdapter:
    """Looks like capture.Camera to the Camera window, plus `latest_depth()` (mm, same pixels as the RGB)."""

    def __init__(self, kinect: KinectV1 | None = None):
        self.kinect = kinect or KinectV1()
        self.size = SIZE
        self.index = -3
        self.depth_background: np.ndarray | None = None

    def latest(self) -> tuple[np.ndarray | None, int]:
        f, seq = self.kinect.latest()
        return (None, seq) if f is None else (f.rgb, seq)

    _recent: list = None
    _recent_seq: int = -1
    _median: np.ndarray | None = None
    _median_seq: int = -1

    def latest_depth(self, smooth: int = 3) -> np.ndarray | None:
        """Depth with a temporal median over the last `smooth` frames (zeros, i.e. no return, ignored)."""
        f, seq = self.kinect.latest()
        if f is None:
            return None
        if self._recent is None:
            self._recent = []
        if seq != self._recent_seq:
            self._recent_seq = seq
            self._recent.append(f.depth)
            self._recent = self._recent[-max(1, smooth):]
        if smooth <= 1 or len(self._recent) < 2:
            return f.depth
        if self._median_seq == seq and self._median is not None:
            return self._median  # already computed for this frame
        self._median = median_ignoring_zeros(self._recent)
        self._median_seq = seq
        return self._median

    def wait_fresh(self, after_seq: int, count: int = 2, timeout: float = 3.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            f, seq = self.kinect.latest()
            if f is not None and seq >= after_seq + count:
                return f.rgb
            time.sleep(0.01)
        f, _ = self.kinect.latest()
        return None if f is None else f.rgb

    def lock_exposure(self, on: bool):  # no exposure control through the sync API
        pass

    @property
    def alive(self) -> bool:
        return self.kinect.alive

    def close(self):
        self.kinect.close()


def median_ignoring_zeros(frames: list) -> np.ndarray:
    """Per-pixel median of a few depth frames where 0 means 'no depth' and is left out.

    Sorts the stack with zeros pushed to +inf and picks the middle of the valid ones: a few
    milliseconds for 3x480x640, against ~80 ms for np.nanmedian.
    """
    stack = np.stack([np.asarray(f, np.float32) for f in frames])
    valid = (stack > 0).sum(axis=0)
    s = np.sort(np.where(stack > 0, stack, np.inf), axis=0)
    idx = np.clip((valid - 1) // 2, 0, len(frames) - 1)
    med = np.take_along_axis(s, idx[None], axis=0)[0]
    med[valid == 0] = 0.0
    return np.ascontiguousarray(med.astype(np.float32))


def depth_to_points(depth_mm: np.ndarray, rgb: np.ndarray | None = None, stride: int = 2, max_mm: float = 6000.0,
                    intrinsics=INTRINSICS):
    """Back-project a registered depth image to (N, 3) metres (x right, y up, z forward) plus (N, 3) colours."""
    fx, fy, cx, cy = intrinsics
    d = depth_mm[::stride, ::stride].astype(np.float32)
    h, w = d.shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32) * stride
    ok = (d > 0) & (d < max_mm)
    z = d[ok] / 1000.0
    x = (xs[ok] - cx) * z / fx
    y = -(ys[ok] - cy) * z / fy
    xyz = np.stack([x, y, z], axis=1).astype(np.float32)
    if rgb is None:
        col = np.full((len(xyz), 3), 200, np.uint8)
    else:
        col = np.ascontiguousarray(rgb[::stride, ::stride][ok])
    return xyz, col


def _adapter_points(self, stride: int = 2):
    f, _ = self.kinect.latest()
    if f is None:
        return None, None
    depth = self.latest_depth()  # temporal median: steadier points, fewer edge dropouts
    return depth_to_points(depth if depth is not None else f.depth, f.rgb, stride)


KinectV1CameraAdapter.latest_points = _adapter_points
