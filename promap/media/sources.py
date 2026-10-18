"""Frame sources: anything that can hand back an RGB frame for a time t.

Every source answers `frame_at(t)` with (frame, key). `frame` is an HxWx3 uint8
RGB array; `key` identifies the frame so a renderer can skip re-uploading a
texture that has not changed. Time `t` is local seconds in [0, duration].
"""
from __future__ import annotations

import bisect
from pathlib import Path

import numpy as np

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
ANIM_EXTS = {".gif", ".webp", ".apng"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}

Frame = np.ndarray


class FrameSource:
    size: tuple[int, int] = (0, 0)  # (w, h)
    duration: float | None = None  # None => still image

    def frame_at(self, t: float) -> tuple[Frame, object]:
        raise NotImplementedError

    def close(self) -> None:
        pass

    @property
    def is_still(self) -> bool:
        return self.duration is None


class ImageSource(FrameSource):
    def __init__(self, path: str | Path):
        from PIL import Image

        with Image.open(path) as im:
            self._frame = np.ascontiguousarray(np.asarray(im.convert("RGB")))
        h, w = self._frame.shape[:2]
        self.size = (w, h)

    def frame_at(self, t: float):
        return self._frame, 0


class AnimatedImageSource(FrameSource):
    """GIF / animated WebP / APNG, fully decoded into memory up front."""

    def __init__(self, path: str | Path, default_frame_ms: float = 100.0):
        from PIL import Image, ImageSequence

        frames: list[Frame] = []
        times: list[float] = [0.0]
        with Image.open(path) as im:
            for fr in ImageSequence.Iterator(im):
                frames.append(np.ascontiguousarray(np.asarray(fr.convert("RGB"))))
                ms = fr.info.get("duration", default_frame_ms) or default_frame_ms
                times.append(times[-1] + ms / 1000.0)
        if not frames:
            raise ValueError(f"no frames in {path}")
        self._frames = frames
        self._starts = times[:-1]
        h, w = frames[0].shape[:2]
        self.size = (w, h)
        self.duration = times[-1] if len(frames) > 1 else None

    def frame_at(self, t: float):
        if self.duration is None:
            return self._frames[0], 0
        idx = bisect.bisect_right(self._starts, max(0.0, t)) - 1
        idx = max(0, min(idx, len(self._frames) - 1))
        return self._frames[idx], idx


class VideoSource(FrameSource):
    """Video via OpenCV. Sequential reads are cheap; random seeks less so."""

    def __init__(self, path: str | Path):
        import cv2

        self._cv2 = cv2
        self._cap = cv2.VideoCapture(str(path))
        if not self._cap.isOpened():
            raise ValueError(f"cannot open video {path}")
        self.fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.frame_count = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.size = (w, h)
        self.duration = self.frame_count / self.fps
        self._idx = -1
        self._frame: Frame | None = None

    def frame_at(self, t: float):
        idx = int(max(0.0, t) * self.fps)
        idx = max(0, min(idx, self.frame_count - 1))
        if idx == self._idx and self._frame is not None:
            return self._frame, idx
        if idx != self._idx + 1:
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, idx)
        ok, bgr = self._cap.read()
        if not ok:
            # ran off the end (frame_count is often an over-estimate); rewind
            self._cap.set(self._cv2.CAP_PROP_POS_FRAMES, 0)
            ok, bgr = self._cap.read()
            idx = 0
            if not ok:
                if self._frame is not None:
                    return self._frame, self._idx
                raise ValueError("video yields no frames")
        self._frame = np.ascontiguousarray(self._cv2.cvtColor(bgr, self._cv2.COLOR_BGR2RGB))
        self._idx = idx
        return self._frame, idx

    def close(self):
        self._cap.release()


class ColorSource(FrameSource):
    def __init__(self, rgb=(255, 255, 255), size=(64, 64)):
        self.size = size
        self._frame = np.ascontiguousarray(
            np.full((size[1], size[0], 3), rgb, dtype=np.uint8)
        )

    def frame_at(self, t: float):
        return self._frame, 0


class TestPatternSource(FrameSource):
    """Alignment grid: 100px grid, diagonals, centre crosshair, labelled axes."""

    def __init__(self, size=(1920, 1080), step: int = 100):
        import cv2

        w, h = size
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:] = (16, 16, 16)
        for x in range(0, w, step):
            cv2.line(img, (x, 0), (x, h - 1), (90, 90, 90), 1)
        for y in range(0, h, step):
            cv2.line(img, (0, y), (w - 1, y), (90, 90, 90), 1)
        cv2.line(img, (0, 0), (w - 1, h - 1), (0, 160, 255), 1)
        cv2.line(img, (w - 1, 0), (0, h - 1), (0, 160, 255), 1)
        cv2.line(img, (w // 2, 0), (w // 2, h - 1), (255, 255, 255), 2)
        cv2.line(img, (0, h // 2), (w - 1, h // 2), (255, 255, 255), 2)
        cv2.circle(img, (w // 2, h // 2), min(w, h) // 3, (255, 0, 255), 2)
        cv2.rectangle(img, (0, 0), (w - 1, h - 1), (255, 255, 0), 3)
        for x in range(0, w, step * 2):
            cv2.putText(img, str(x), (x + 4, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        for y in range(0, h, step * 2):
            cv2.putText(img, str(y), (4, y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        self._frame = np.ascontiguousarray(img)
        self.size = (w, h)

    def frame_at(self, t: float):
        return self._frame, 0


def open_source(path: str | Path) -> FrameSource:
    if isinstance(path, str) and path.startswith("gen:"):
        from promap.media.generative import GenerativeSource

        return GenerativeSource(path)
    p = Path(path)
    ext = p.suffix.lower()
    if ext in ANIM_EXTS:
        return AnimatedImageSource(p)
    if ext in IMAGE_EXTS:
        return ImageSource(p)
    if ext in VIDEO_EXTS:
        return VideoSource(p)
    raise ValueError(f"unsupported media type: {p.name}")


class SourceCache:
    """Share decoded stills/animations between layers; videos get a fresh decoder each."""

    def __init__(self):
        self._cache: dict[str, FrameSource] = {}

    def open(self, path: str | Path) -> FrameSource:
        if isinstance(path, str) and path.startswith("gen:"):
            return open_source(path)  # one source per layer: parameters are edited live per surface
        p = Path(path)
        if p.suffix.lower() in VIDEO_EXTS:
            return open_source(p)
        key = str(p.resolve())
        src = self._cache.get(key)
        if src is None:
            src = self._cache[key] = open_source(p)
        return src

    def clear(self):
        for s in self._cache.values():
            s.close()
        self._cache.clear()
