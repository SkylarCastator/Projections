"""Skeleton tracking on the camera image with MediaPipe's pose landmarker (33 joints per person).

Works on any RGB frame; with a depth camera whose depth is registered to the RGB, each joint
also gets a metric 3D position (median depth in a small window around the joint). The
model file is fetched once into ~/.local/share/promap/models/ (see MODEL_URL).
"""
from __future__ import annotations

import logging
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

MODEL_DIR = Path("~/.local/share/promap/models").expanduser()
MODEL_FILE = MODEL_DIR / "pose_landmarker_lite.task"
MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task"

# MediaPipe pose landmark indices
JOINTS = ["nose", "l_eye_in", "l_eye", "l_eye_out", "r_eye_in", "r_eye", "r_eye_out", "l_ear", "r_ear", "mouth_l", "mouth_r",
          "l_shoulder", "r_shoulder", "l_elbow", "r_elbow", "l_wrist", "r_wrist", "l_pinky", "r_pinky", "l_index", "r_index",
          "l_thumb", "r_thumb", "l_hip", "r_hip", "l_knee", "r_knee", "l_ankle", "r_ankle", "l_heel", "r_heel", "l_foot", "r_foot"]
BONES = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24), (23, 24), (23, 25), (25, 27), (24, 26), (26, 28),
         (27, 29), (29, 31), (28, 30), (30, 32), (0, 11), (0, 12), (15, 19), (16, 20)]


@dataclass
class Skeleton:
    px: np.ndarray  # (33, 2) image pixels
    visibility: np.ndarray  # (33,) 0..1
    xyz: np.ndarray | None = None  # (33, 3) metres in the sensor frame, NaN where no depth
    t: float = field(default_factory=time.monotonic)

    def visible(self, i: int, thresh: float = 0.5) -> bool:
        return bool(self.visibility[i] >= thresh)


def model_available() -> tuple[bool, str]:
    try:
        import mediapipe  # noqa: F401
    except Exception as exc:
        return False, f"mediapipe not installed ({exc}); uv pip install mediapipe"
    if not MODEL_FILE.exists():
        return False, f"pose model missing: {MODEL_FILE} (it downloads on first use)"
    return True, ""


def ensure_model():
    if MODEL_FILE.exists():
        return
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    log.info("downloading pose model to %s", MODEL_FILE)
    urllib.request.urlretrieve(MODEL_URL, MODEL_FILE)


class PoseTracker:
    """Call `track(rgb, depth_mm=None)` per frame; returns a list of Skeleton (one per person)."""

    def __init__(self, max_people: int = 2):
        ensure_model()
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self._mp = mp
        opts = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(MODEL_FILE)),
            running_mode=vision.RunningMode.VIDEO, num_poses=max_people,
            min_pose_detection_confidence=0.5, min_tracking_confidence=0.5)
        self._lm = vision.PoseLandmarker.create_from_options(opts)
        self._t0 = time.monotonic()
        self._last_ms = -1

    def track(self, rgb: np.ndarray, depth_mm: np.ndarray | None = None, intrinsics=None) -> list[Skeleton]:
        mp = self._mp
        h, w = rgb.shape[:2]
        ms = int((time.monotonic() - self._t0) * 1000)
        if ms <= self._last_ms:
            ms = self._last_ms + 1
        self._last_ms = ms
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        res = self._lm.detect_for_video(img, ms)
        out: list[Skeleton] = []
        for lms in res.pose_landmarks or []:
            px = np.array([[l.x * w, l.y * h] for l in lms], np.float32)
            vis = np.array([getattr(l, "visibility", 1.0) or 0.0 for l in lms], np.float32)
            sk = Skeleton(px, vis)
            if depth_mm is not None:
                sk.xyz = joints_3d(px, depth_mm, intrinsics)
            out.append(sk)
        return out

    def close(self):
        try:
            self._lm.close()
        except Exception:  # pragma: no cover
            pass


def joints_3d(px: np.ndarray, depth_mm: np.ndarray, intrinsics=None, win: int = 4) -> np.ndarray:
    """Metric joint positions from registered depth: median of the valid depths in a window."""
    if intrinsics is None:
        from promap.camera.kinect1 import INTRINSICS as intrinsics
    fx, fy, cx, cy = intrinsics
    H, W = depth_mm.shape
    out = np.full((len(px), 3), np.nan, np.float32)
    for i, (x, y) in enumerate(px):
        xi, yi = int(round(x)), int(round(y))
        if not (0 <= xi < W and 0 <= yi < H):
            continue
        patch = depth_mm[max(0, yi - win):yi + win + 1, max(0, xi - win):xi + win + 1]
        valid = patch[patch > 0]
        if valid.size < 3:
            continue
        z = float(np.median(valid)) / 1000.0
        out[i] = ((x - cx) * z / fx, -(y - cy) * z / fy, z)
    return out


def draw_skeletons(img: np.ndarray, skeletons: list[Skeleton], color=(255, 200, 0)) -> np.ndarray:
    import cv2

    out = img.copy()
    for sk in skeletons:
        for a, b in BONES:
            if sk.visible(a) and sk.visible(b):
                pa, pb = sk.px[a], sk.px[b]
                cv2.line(out, (int(pa[0]), int(pa[1])), (int(pb[0]), int(pb[1])), color, 2, cv2.LINE_AA)
        for i, p in enumerate(sk.px):
            if sk.visible(i):
                cv2.circle(out, (int(p[0]), int(p[1])), 3, (255, 255, 255), -1, cv2.LINE_AA)
    return out
