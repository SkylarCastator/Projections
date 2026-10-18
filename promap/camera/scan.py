"""Scene scan: use the projector as a controlled light to see what is in a static scene.

After the geometric calibration (promap.camera.graycode) we know where the projector
lands in the camera. This second pass projects a few flat colours and a fine stripe pair
and turns the camera's *response* to them into per-pixel cues, independent of room light:

  albedo   white minus black: how much projector light comes back (dark props vs wall)
  color    response under red, green, blue illumination: the surface's own colour
  direct   Nayar et al. (CVPR 2006): under high-frequency stripes, max - min is light that
           came straight from the projector and the minimum is bounce (interreflection,
           subsurface). Edges sharpen and concave corners stop glowing.
  depth    the camera sits beside the lens, so a point in front of the wall maps away from
           the wall's homography: the residual of the dense map against the plane is a
           parallax cue (signed, camera pixels; 0 on the wall)
  shadow   inside the projector footprint but never lit/decoded: cast shadows or a
           surface that swallows the light

`segment()` clusters the weighted cues (k-means) and returns one outline per connected
region, ready for detect.detections_to_meshes. One shot, not real time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from promap.camera.detect import Detection

SCAN_NAMES = ["black", "white", "red", "green", "blue", "stripes", "stripes_inv"]
PALETTE = np.array([[255, 90, 90], [90, 200, 255], [120, 255, 120], [255, 220, 80], [220, 120, 255],
                    [255, 150, 60], [80, 255, 220], [200, 200, 200]], np.uint8)


def scan_pattern_rgb(index: int, w: int, h: int, period: int = 24) -> np.ndarray:
    """Pattern `index` of SCAN_NAMES as an (h, w, 3) uint8 image."""
    name = SCAN_NAMES[index]
    img = np.zeros((h, w, 3), np.uint8)
    if name == "white":
        img[:] = 255
    elif name in ("red", "green", "blue"):
        img[:, :, ("red", "green", "blue").index(name)] = 255
    elif name.startswith("stripes"):
        stripe = ((np.arange(w) // max(1, period // 2)) % 2).astype(np.uint8) * 255
        if name.endswith("_inv"):
            stripe = 255 - stripe
        img[:] = np.repeat(stripe[None, :, None], h, axis=0)
    return np.ascontiguousarray(img)


def stripe_period_for(calibration, default: int = 24) -> int:
    """Stripes about 3 camera pixels wide: fine enough to be 'high frequency', coarse enough to resolve."""
    if calibration is None or calibration.px is None:
        return default
    from promap.camera.graycode import projector_px_per_camera_px

    r = projector_px_per_camera_px(calibration.px, calibration.py, calibration.valid)
    return int(max(16, round(20 * r)))  # stripes ~10 camera px wide


@dataclass
class SceneScan:
    albedo: np.ndarray  # (H, W) 0..1
    color: np.ndarray  # (H, W, 3) 0..1 reflectance chroma
    direct: np.ndarray  # (H, W) 0..1 share of light that is direct
    depth: np.ndarray  # (H, W) signed camera px off the wall plane; 0 unknown
    lit: np.ndarray  # (H, W) bool
    shadow: np.ndarray  # (H, W) bool
    roi: np.ndarray  # (H, W) bool: projector footprint
    labels: np.ndarray | None = field(default=None, repr=False)  # last segmentation
    camera_frame: np.ndarray | None = field(default=None, repr=False)  # the 'white' RGB capture

    # -- building ----------------------------------------------------------------
    @classmethod
    def from_captures(cls, caps_rgb: list[np.ndarray], calibration=None, min_contrast: float = 12.0) -> "SceneScan":
        import cv2

        if len(caps_rgb) != len(SCAN_NAMES):
            raise ValueError(f"expected {len(SCAN_NAMES)} captures, got {len(caps_rgb)}")
        c = {n: np.asarray(im, np.float32) for n, im in zip(SCAN_NAMES, caps_rgb)}
        black, white = c["black"], c["white"]
        resp = np.clip(white - black, 0, None)  # (H, W, 3) projector light that came back
        H, W = resp.shape[:2]
        lum = resp.mean(axis=2)
        scale = max(float(np.percentile(lum, 99)), 1.0)
        albedo = np.clip(lum / scale, 0, 1)
        lit = lum > min_contrast
        # colour: camera channel i under projector light i, ambient removed, normalised to chroma
        diag = np.stack([np.clip(c[n][:, :, i] - black[:, :, i], 0, None) for i, n in enumerate(("red", "green", "blue"))], axis=2)
        color = diag / np.maximum(diag.sum(axis=2, keepdims=True), 1e-3)
        color[~lit] = 1 / 3
        # direct / global from the stripe pair
        s1 = np.clip(c["stripes"] - black, 0, None).mean(axis=2)
        s2 = np.clip(c["stripes_inv"] - black, 0, None).mean(axis=2)
        mx, mn = np.maximum(s1, s2), np.minimum(s1, s2)
        direct_l, global_l = mx - mn, 2 * mn
        direct = np.clip(direct_l / np.maximum(direct_l + global_l, 1e-3), 0, 1)
        direct[~lit] = 0
        # footprint + depth from the calibration
        roi = lit.copy()
        depth = np.zeros((H, W), np.float32)
        shadow = np.zeros((H, W), bool)
        if calibration is not None and calibration.cam_size == (W, H):
            roi = np.zeros((H, W), np.uint8)
            cv2.fillPoly(roi, [calibration.projector_outline_in_camera().astype(np.int32)], 255)
            roi = roi > 0
            if calibration.valid is not None:
                from promap.camera.graycode import projector_px_per_camera_px

                valid = calibration.valid
                # undecoded stripe-boundary pixels form a lattice; only solid patches are real shadows
                shadow = cv2.morphologyEx((roi & ~valid).astype(np.uint8), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)) > 0
                ys, xs = np.nonzero(valid)
                if len(xs) > 16:
                    pts = np.stack([xs, ys], axis=1).astype(np.float32).reshape(-1, 1, 2)
                    plane = cv2.perspectiveTransform(pts, calibration.H).reshape(-1, 2)
                    res = np.stack([calibration.px[ys, xs], calibration.py[ys, xs]], axis=1) - plane
                    res /= projector_px_per_camera_px(calibration.px, calibration.py, valid)
                    # signed distance along the dominant parallax direction, wall at 0
                    mean = res.mean(axis=0)
                    _, _, vt = np.linalg.svd(res - mean, full_matrices=False)
                    axis = vt[0]
                    d = res @ axis
                    d -= np.median(d)
                    if np.mean(d > 0) > 0.5:  # most off-plane pixels are "in front": make front positive
                        pass
                    depth[ys, xs] = d
                    depth = cv2.medianBlur(depth, 5)
                    depth[~valid] = 0
        return cls(albedo, color, direct, depth, lit, shadow, roi, camera_frame=np.clip(white, 0, 255).astype(np.uint8))

    # -- views -------------------------------------------------------------------------
    def view(self, name: str) -> np.ndarray:
        """An (H, W, 3) uint8 image of one cue for the preview."""
        if name == "albedo":
            g = (self.albedo * 255).astype(np.uint8)
            return np.repeat(g[:, :, None], 3, axis=2)
        if name == "color":
            img = (self.color * 255 * np.clip(self.albedo[:, :, None] * 2, 0.25, 1)).astype(np.uint8)
            return img
        if name == "direct":
            g = (self.direct * 255).astype(np.uint8)
            return np.repeat(g[:, :, None], 3, axis=2)
        if name == "depth":
            d = self.depth
            s = max(float(np.percentile(np.abs(d[self.roi]), 98)) if self.roi.any() else 1.0, 0.5)
            img = np.zeros((*d.shape, 3), np.uint8)
            img[:, :, 0] = (np.clip(d / s, 0, 1) * 255).astype(np.uint8)  # in front: red
            img[:, :, 2] = (np.clip(-d / s, 0, 1) * 255).astype(np.uint8)  # behind / away: blue
            img[:, :, 1] = (self.albedo * 80).astype(np.uint8)
            return img
        if name == "shadow":
            img = np.repeat((self.albedo * 120).astype(np.uint8)[:, :, None], 3, axis=2)
            img[self.shadow] = (255, 60, 200)
            return img
        if name == "segments":
            if self.labels is None:
                return self.view("albedo")
            img = PALETTE[self.labels % len(PALETTE)].copy()
            img[~self.roi] = 25
            return img
        return self.camera_frame if self.camera_frame is not None else self.view("albedo")

    # -- segmentation -------------------------------------------------------------------
    def segment(self, k: int = 4, w_color: float = 1.0, w_depth: float = 1.0, w_albedo: float = 1.0,
                w_direct: float = 0.5, min_area: float = 800.0, epsilon: float = 0.01,
                drop_largest: bool = True, max_count: int = 32) -> list[Detection]:
        """Cluster the weighted cues into k classes and outline each connected region."""
        import cv2

        H, W = self.albedo.shape
        f = 2  # cluster at half resolution
        def small(a):  # half resolution, then a median to kill speckle from calibration holes
            s = cv2.resize(a.astype(np.float32), (W // f, H // f), interpolation=cv2.INTER_AREA)
            return cv2.medianBlur(s, 5)

        roi_s = small(self.roi.astype(np.float32)) > 0.5
        d = self.depth
        dscale = max(float(np.percentile(np.abs(d[self.roi]), 95)) if self.roi.any() else 1.0, 0.5)
        feats = [
            w_albedo * small(self.albedo),
            w_color * small(self.color[:, :, 0]) * 2, w_color * small(self.color[:, :, 1]) * 2, w_color * small(self.color[:, :, 2]) * 2,
            w_depth * small(np.clip(d / dscale, -1, 1)),
            w_direct * small(self.direct),
            w_albedo * small(self.shadow.astype(np.float32)),
        ]
        X = np.stack(feats, axis=-1)[roi_s].astype(np.float32)
        if len(X) < k * 10:
            self.labels = None
            return []
        k = max(2, min(int(k), 8))
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.1)
        _, lab, _ = cv2.kmeans(X, k, None, crit, 4, cv2.KMEANS_PP_CENTERS)
        labels_s = np.full(roi_s.shape, -1, np.int32)
        labels_s[roi_s] = lab.ravel()
        labels = cv2.resize(labels_s.astype(np.float32), (W, H), interpolation=cv2.INTER_NEAREST).astype(np.int32)
        self.labels = np.where(self.roi, labels, -1)
        out: list[Detection] = []
        for cls_id in range(k):
            mask = (self.labels == cls_id).astype(np.uint8) * 255
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                area = float(cv2.contourArea(c))
                if area < min_area:
                    continue
                peri = cv2.arcLength(c, True)
                approx = cv2.approxPolyDP(c, max(1.0, epsilon * peri), True).reshape(-1, 2).astype(np.float32)
                if len(approx) < 3:
                    continue
                x, y, bw, bh = cv2.boundingRect(c)
                out.append(Detection(approx, area, (int(x), int(y), int(bw), int(bh)), label=cls_id))
        out.sort(key=lambda d: -d.area)
        if drop_largest and len(out) > 1:
            out = out[1:]  # the biggest region is the wall / background
        return out[:max_count]

    # -- persistence -------------------------------------------------------------------
    def save(self, path: str | Path):
        np.savez_compressed(path, albedo=self.albedo, color=self.color, direct=self.direct, depth=self.depth,
                            lit=self.lit, shadow=self.shadow, roi=self.roi,
                            camera_frame=self.camera_frame if self.camera_frame is not None else np.zeros((1, 1, 3), np.uint8))

    @classmethod
    def load(cls, path: str | Path) -> "SceneScan | None":
        p = Path(path)
        if not p.exists():
            return None
        z = np.load(p)
        cf = z["camera_frame"]
        return cls(z["albedo"], z["color"], z["direct"], z["depth"], z["lit"], z["shadow"], z["roi"],
                   camera_frame=None if cf.shape[0] == 1 else cf)
