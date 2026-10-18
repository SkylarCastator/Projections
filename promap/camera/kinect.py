"""Kinect v2 (Xbox One) input through libfreenect2 and the `freenect2` cffi binding.

What it gives, per grab:
  depth       (424, 512) float32 millimetres, 0 = no return (time-of-flight)
  ir          (424, 512) float32 infrared brightness
  color       (1080, 1920, 3) uint8 RGB from the colour camera
  registered  (424, 512, 3) uint8: the colour image resampled onto the depth pixels
  points      (424, 512, 3) float32 metres in the depth camera's frame (x right, y up, z forward)

The sensor needs a USB 3 SuperSpeed link; at USB 2 it only exposes its microphone array
and `open()` fails with a clear message. Build the driver with scripts/setup_kinect.sh.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

DEPTH_SIZE = (512, 424)
COLOR_SIZE = (1920, 1080)


def binding_available() -> tuple[bool, str]:
    """(ok, reason). Importing the binding loads libfreenect2.so, so this also checks the driver."""
    try:
        import freenect2  # noqa: F401
    except Exception as exc:  # ImportError or OSError from the missing shared library
        return False, f"freenect2 binding not usable: {exc} (run scripts/setup_kinect.sh)"
    return True, ""


def usb_link_speed() -> int | None:
    """Link speed in Mbit/s of the first Kinect v2 on the bus, from sysfs; None if not plugged in."""
    for d in sorted(Path("/sys/bus/usb/devices").glob("[0-9]-*")):
        try:
            if (d / "idVendor").read_text().strip() == "045e" and (d / "idProduct").read_text().strip() in ("02c4", "02d8", "02d9"):
                return int((d / "speed").read_text().strip())
        except (OSError, ValueError):
            continue
    return None


def status() -> str:
    """One line for the UI: plugged in? USB 3? driver built?"""
    speed = usb_link_speed()
    if speed is None:
        return "Kinect v2 not detected on USB"
    ok, why = binding_available()
    if not ok:
        return why
    if speed < 5000:
        return f"Kinect v2 on a USB {'2' if speed <= 480 else '?'} link ({speed} Mbit/s): needs USB 3 SuperSpeed (5000). Replug."
    return f"Kinect v2 ready (USB 3, {speed} Mbit/s)"


@dataclass
class KinectFrame:
    depth: np.ndarray  # (424, 512) float32 mm
    ir: np.ndarray | None
    color: np.ndarray | None  # (1080, 1920, 3) uint8 RGB
    registered: np.ndarray | None  # (424, 512, 3) uint8 RGB on depth pixels
    seq: int = 0
    t: float = field(default_factory=time.monotonic)

    def points(self, kinect: "Kinect") -> np.ndarray:
        """(424, 512, 3) float32 metres; NaN where there is no depth."""
        return kinect.points_from_depth(self.depth)


class Kinect:
    """Threaded grabber like capture.Camera: open, then `latest()` returns the newest KinectFrame."""

    def __init__(self, want_color: bool = True, pipeline: str | None = None):
        ok, why = binding_available()
        if not ok:
            raise RuntimeError(why)
        speed = usb_link_speed()
        if speed is not None and speed < 5000:
            raise RuntimeError(f"Kinect v2 is on a USB 2 link ({speed} Mbit/s); it only streams over USB 3 SuperSpeed")
        import freenect2
        from freenect2 import FrameType

        self._ft = FrameType
        try:
            self.device = freenect2.Device()
        except Exception as exc:
            raise RuntimeError(f"cannot open Kinect v2: {exc}") from exc
        self.want_color = want_color
        self._lock = threading.Lock()
        self._frame: KinectFrame | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._undistorted = None
        self._registered = None
        self.device.start()
        self.registration = self.device.registration
        self._thread = threading.Thread(target=self._loop, name="kinect", daemon=True)
        self._thread.start()

    def _loop(self):
        FT = self._ft
        depth = ir = color = None
        while not self._stop.is_set():
            try:
                ftype, frame = self.device.get_next_frame(timeout=1.0)
            except Exception as exc:  # timeout or device error: keep trying
                log.debug("kinect frame wait: %s", exc)
                continue
            if ftype is FT.Depth:
                depth = frame
            elif ftype is FT.Ir:
                ir = frame
            elif ftype is FT.Color:
                color = frame
            if depth is None or (self.want_color and color is None):
                continue
            registered = None
            try:
                if color is not None:
                    undistorted, registered_f = self.registration.apply(color, depth)
                    registered = np.ascontiguousarray(registered_f.to_array()[:, :, :3][:, :, ::-1])  # BGRX -> RGB
            except Exception as exc:
                log.debug("registration: %s", exc)
            kf = KinectFrame(
                depth=np.ascontiguousarray(depth.to_array().astype(np.float32)),
                ir=np.ascontiguousarray(ir.to_array().astype(np.float32)) if ir is not None else None,
                color=np.ascontiguousarray(color.to_array()[:, :, :3][:, :, ::-1]) if color is not None else None,
                registered=registered,
                seq=self._seq + 1,
            )
            with self._lock:
                self._frame = kf
                self._seq = kf.seq
            depth = ir = color = None

    def latest(self) -> tuple[KinectFrame | None, int]:
        with self._lock:
            return self._frame, self._seq

    def wait_fresh(self, after_seq: int, count: int = 2, timeout: float = 3.0) -> KinectFrame | None:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            f, seq = self.latest()
            if f is not None and seq >= after_seq + count:
                return f
            time.sleep(0.01)
        return self.latest()[0]

    # -- geometry ------------------------------------------------------------------------
    def intrinsics(self):
        """Depth camera intrinsics (fx, fy, cx, cy) from the device's factory calibration."""
        p = self.device.ir_camera_params
        return float(p.fx), float(p.fy), float(p.cx), float(p.cy)

    def points_from_depth(self, depth_mm: np.ndarray) -> np.ndarray:
        """Back-project a depth image to metres using the pinhole model; NaN where depth is 0."""
        fx, fy, cx, cy = self.intrinsics()
        h, w = depth_mm.shape
        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
        z = depth_mm / 1000.0
        z = np.where(z > 0, z, np.nan)
        x = (xs - cx) * z / fx
        y = -(ys - cy) * z / fy  # image y down -> world y up
        return np.stack([x, y, z], axis=-1).astype(np.float32)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            self.device.stop()
            self.device.close()
        except Exception:  # pragma: no cover
            pass


# -- depth -> geometry helpers (pure numpy, usable without the sensor) ----------------------
def depth_to_image(depth_mm: np.ndarray, near: float = 500.0, far: float = 4500.0) -> np.ndarray:
    """Depth as an (H, W, 3) uint8 image: near = warm, far = cool, no return = black."""
    d = depth_mm.astype(np.float32)
    t = np.clip((d - near) / max(far - near, 1.0), 0, 1)
    img = np.zeros((*d.shape, 3), np.uint8)
    img[..., 0] = ((1 - t) * 255).astype(np.uint8)
    img[..., 2] = (t * 255).astype(np.uint8)
    img[..., 1] = (np.minimum(t, 1 - t) * 2 * 160).astype(np.uint8)
    img[d <= 0] = 0
    return img


def foreground_mask(depth_mm: np.ndarray, background_mm: np.ndarray | None = None, max_mm: float = 4500.0,
                    min_mm: float = 400.0, step_mm: float = 60.0) -> np.ndarray:
    """Pixels that are closer than the background (or than max_mm when no background): props.

    With a background depth image of the empty scene, a pixel is foreground when it is
    `step_mm` nearer than the background was. Without one, anything inside [min, max] counts.
    """
    d = depth_mm
    valid = d > 0
    if background_mm is None:
        return valid & (d >= min_mm) & (d <= max_mm)
    bg_valid = background_mm > 0
    return valid & ((~bg_valid) | (d < background_mm - step_mm)) & (d >= min_mm) & (d <= max_mm)


def plane_background(depth_mm: np.ndarray, roi: np.ndarray | None = None, passes: int = 2) -> np.ndarray | None:
    """Fit the dominant plane (the wall) through the valid depth and return it as a background image.

    Least squares z = a*x + b*y + c over valid pixels (inside roi when given), then one more
    pass keeping only pixels within 150 mm of the first fit so props do not tilt the plane.
    Lets depth detection work with no stored background: foreground = nearer than this plane.
    """
    d = depth_mm.astype(np.float32)
    valid = d > 0
    if roi is not None:
        valid &= roi
    ys, xs = np.nonzero(valid)
    if len(xs) < 500:
        return None
    step = max(1, len(xs) // 20000)
    xs, ys = xs[::step].astype(np.float32), ys[::step].astype(np.float32)
    z = d[ys.astype(int), xs.astype(int)]
    A = np.stack([xs, ys, np.ones_like(xs)], axis=1)
    coef = None
    for _ in range(passes):
        coef, *_ = np.linalg.lstsq(A, z, rcond=None)
        res = np.abs(A @ coef - z)
        keep = res < 150.0
        if keep.sum() < 100:
            break
        A, z = A[keep], z[keep]
    if coef is None:
        return None
    H, W = d.shape
    gy, gx = np.mgrid[0:H, 0:W].astype(np.float32)
    plane = coef[0] * gx + coef[1] * gy + coef[2]
    return np.clip(plane, 1.0, None).astype(np.float32)


def mask_to_contours(mask: np.ndarray, min_area: float = 300.0, epsilon: float = 0.01):
    """Blob outlines in depth-image pixels (Detection list), largest first."""
    import cv2

    from promap.camera.detect import Detection

    m = (mask.astype(np.uint8) * 255)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        area = float(cv2.contourArea(c))
        if area < min_area:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, max(1.0, epsilon * peri), True).reshape(-1, 2).astype(np.float32)
        if len(approx) < 3:
            continue
        x, y, w, h = cv2.boundingRect(c)
        out.append(Detection(approx, area, (int(x), int(y), int(w), int(h))))
    out.sort(key=lambda d: -d.area)
    return out


class KinectCameraAdapter:
    """Presents the Kinect like capture.Camera so the Camera dock can calibrate and scan through it.

    `latest()` returns the colour image *registered onto the depth pixels* (512x424), so a
    Gray-code calibration done through this adapter maps depth pixels straight to projector
    pixels, and depth-based detections land in projector space with no extra step.
    """

    def __init__(self, kinect: "Kinect | None" = None):
        self.kinect = kinect or Kinect(want_color=True)
        self.size = DEPTH_SIZE
        self.index = -2
        self.depth_background: np.ndarray | None = None

    def latest(self) -> tuple[np.ndarray | None, int]:
        f, seq = self.kinect.latest()
        if f is None or f.registered is None:
            return None, seq
        return f.registered, seq

    def latest_depth(self) -> np.ndarray | None:
        f, _ = self.kinect.latest()
        return None if f is None else f.depth

    def wait_fresh(self, after_seq: int, count: int = 2, timeout: float = 3.0):
        f = self.kinect.wait_fresh(after_seq, count, timeout)
        return None if f is None else f.registered

    def lock_exposure(self, on: bool):  # the Kinect's colour camera exposure is not adjustable here
        pass

    @property
    def alive(self) -> bool:
        return self.kinect.alive

    def close(self):
        self.kinect.close()


def _v2_adapter_points(self, stride: int = 2):
    f, _ = self.kinect.latest()
    if f is None:
        return None, None
    from promap.camera.kinect1 import depth_to_points

    return depth_to_points(f.depth, f.registered, stride, intrinsics=self.kinect.intrinsics())


KinectCameraAdapter.latest_points = _v2_adapter_points
