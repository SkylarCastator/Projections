"""Build the source image for generation from the reference photo and a surface's quad."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from promap.model.geometry import homography_unit_square_to_quad


def _unit_to_px(w: float, h: float) -> np.ndarray:
    return np.array([[w, 0, 0], [0, h, 0], [0, 0, 1.0]])


def load_photo(path: str | Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        return np.ascontiguousarray(np.asarray(im.convert("RGB")))


def suggested_size(corners, multiple: int = 16, max_side: int = 832, min_side: int = 256) -> tuple[int, int]:
    """Pixel size for a flat rendition of the quad: mean edge lengths, longest side capped."""
    c = np.array(corners, dtype=float)
    w = (np.linalg.norm(c[1] - c[0]) + np.linalg.norm(c[2] - c[3])) / 2
    h = (np.linalg.norm(c[2] - c[1]) + np.linalg.norm(c[3] - c[0])) / 2
    w, h = max(w, 1.0), max(h, 1.0)
    scale = min(max_side / max(w, h), 1.0) if max(w, h) > max_side else 1.0
    if min(w, h) * scale < min_side:
        scale = min_side / min(w, h)
    w, h = w * scale, h * scale
    w = max(multiple, int(round(w / multiple)) * multiple)
    h = max(multiple, int(round(h / multiple)) * multiple)
    return w, h


def rectify(photo: np.ndarray, photo_corners, surface_corners, size: tuple[int, int]) -> np.ndarray:
    """Flat image of what the surface quad covers in the reference photo.

    `photo_corners` is the reference surface's quad in output space (where the photo was
    dragged to), `surface_corners` the target surface's quad. Chain: target px -> unit
    square -> output px (H_s) -> photo unit uv (H_ref^-1) -> photo px.
    """
    ph, pw = photo.shape[:2]
    H_s = homography_unit_square_to_quad(surface_corners)
    H_ref = homography_unit_square_to_quad(photo_corners)
    M = _unit_to_px(pw, ph) @ np.linalg.inv(H_ref) @ H_s @ np.linalg.inv(_unit_to_px(*size))
    return cv2.warpPerspective(photo, M, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REFLECT)


def full_photo(photo: np.ndarray, photo_corners, out_size: tuple[int, int], size: tuple[int, int] | None = None) -> np.ndarray:
    """The reference photo as it sits in output space, optionally resized."""
    ph, pw = photo.shape[:2]
    H_ref = homography_unit_square_to_quad(photo_corners)
    M = H_ref @ np.linalg.inv(_unit_to_px(pw, ph))
    img = cv2.warpPerspective(photo, M, out_size, flags=cv2.INTER_LINEAR)
    if size and size != out_size:
        img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    return img


def quad_mask(corners, out_size: tuple[int, int], size: tuple[int, int] | None = None) -> np.ndarray:
    mask = np.zeros((out_size[1], out_size[0]), np.uint8)
    cv2.fillPoly(mask, [np.array(corners, dtype=np.int32)], 255)
    if size and size != out_size:
        mask = cv2.resize(mask, size, interpolation=cv2.INTER_AREA)
    return mask


def save_png(img: np.ndarray, path: str | Path) -> Path:
    from PIL import Image

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(p)
    return p
