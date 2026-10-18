"""Camera dock: live webcam preview, projector<->camera calibration, object detection.

Calibration projects a Gray-code sequence through the open output window while the
camera watches (see promap.camera.graycode). The result is stored in the project
(`camera` section + a .calib.npz next to the file). Once calibrated:
  * the projector's footprint is outlined on the preview,
  * "Reference from camera" warps a camera frame into projector space and adds it as a
    reference image that is already aligned,
  * "Detect objects" finds blobs in the camera image inside the footprint and "Create
    surfaces" turns each into a triangle-mesh surface in projector pixels.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QComboBox, QLabel, QGroupBox, QFormLayout, QSlider,
    QSpinBox, QCheckBox, QProgressBar, QMessageBox, QListWidget,
)

from promap.camera import graycode, detect, scan, kinect, kinect1, planes, people, pose
from promap.camera.capture import list_cameras, Camera, CameraInfo
from promap.ui.state import AppState

log = logging.getLogger(__name__)

RESOLUTIONS = [(640, 480), (1280, 720), (1920, 1080)]


class CameraPanel(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.camera: Camera | None = None
        self.cameras: list[CameraInfo] = []
        self.background: np.ndarray | None = None
        self.detections: list[detect.Detection] = []
        self._calib: _PatternRun | None = None
        self._last_frame: np.ndarray | None = None

        lay = QVBoxLayout(self)

        dev = QHBoxLayout()
        self.device = QComboBox()
        self.device.setToolTip("USB / built-in cameras that deliver frames")
        self.res = QComboBox()
        for w, h in RESOLUTIONS:
            self.res.addItem(f"{w}x{h}", (w, h))
        self.res.setCurrentIndex(1)
        self.start_btn = QPushButton("Start")
        self.start_btn.setCheckable(True)
        self.start_btn.toggled.connect(self._toggle)
        rescan = QPushButton("⟳")
        rescan.setFixedWidth(28)
        rescan.setToolTip("Rescan cameras")
        rescan.clicked.connect(self._scan_cameras)
        dev.addWidget(self.device, 1)
        dev.addWidget(self.res)
        dev.addWidget(rescan)
        dev.addWidget(self.start_btn)
        lay.addLayout(dev)

        self.preview = QLabel("no camera")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320, 240)
        self.preview.setStyleSheet("background: #111; color: #888")
        self.preview.setToolTip("Ctrl+wheel zooms the preview (the dock scrolls); Ctrl+0 on it resets")
        self.preview_zoom = 1.0
        self.preview.installEventFilter(self)
        lay.addWidget(self.preview, 0)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        cal = QGroupBox("Calibration (projector ↔ camera)")
        cf = QVBoxLayout(cal)
        row = QHBoxLayout()
        self.cal_btn = QPushButton("Calibrate")
        self.cal_btn.setToolTip("Projects Gray-code stripes through the output window and decodes where each camera pixel lands "
                                "in the projector. Open the output on the projector first; dim the room.")
        self.cal_btn.clicked.connect(self._calibrate)
        self.cal_cancel = QPushButton("Cancel")
        self.cal_cancel.setEnabled(False)
        self.cal_cancel.clicked.connect(self._cancel_calibration)
        self.settle = QSpinBox()
        self.settle.setRange(100, 2000)
        self.settle.setValue(400)
        self.settle.setSuffix(" ms")
        self.settle.setToolTip("Wait after each pattern before grabbing (camera exposure + projector latency)")
        row.addWidget(self.cal_btn)
        row.addWidget(self.cal_cancel)
        row.addWidget(QLabel("settle"))
        row.addWidget(self.settle)
        cf.addLayout(row)
        self.cal_progress = QProgressBar()
        self.cal_progress.setVisible(False)
        cf.addWidget(self.cal_progress)
        self.cal_info = QLabel("not calibrated")
        self.cal_info.setWordWrap(True)
        cf.addWidget(self.cal_info)
        row2 = QHBoxLayout()
        self.ref_btn = QPushButton("Reference from camera")
        self.ref_btn.setToolTip("Add the camera's view to the editor as a reference image: aligned to the projector when calibrated, "
                                "otherwise raw, for you to drag into place")
        self.ref_btn.clicked.connect(self._reference_from_camera)
        self.show_pattern = QCheckBox("Show white")
        self.show_pattern.setToolTip("Project plain white (lights the props evenly for 'bright' detection)")
        self.show_pattern.toggled.connect(self._white)
        row2.addWidget(self.ref_btn)
        row2.addWidget(self.show_pattern)
        cf.addLayout(row2)
        lay.addWidget(cal)

        det = QGroupBox("Objects → geometry")
        df = QFormLayout(det)
        self.mode = QComboBox()
        self.mode.addItems(list(detect.MODES))
        self.mode.setToolTip("bright: lighter blobs (props under white light)  dark: darker blobs  "
                             "diff: changed since the background frame  edges: outlines closed into blobs  "
                             "depth: nearer than the depth background (Kinect only)")
        self.mode.setToolTip("bright: lighter blobs (props under white light)  dark: darker blobs  "
                             "diff: changed since the background frame  edges: outlines closed into blobs")
        df.addRow("Mode", self.mode)
        self.mode_help = QLabel("")
        self.mode_help.setWordWrap(True)
        self.mode_help.setStyleSheet("color: #aaa")
        df.addRow("", self.mode_help)
        self.mode.currentTextChanged.connect(self._mode_changed)
        drow = QHBoxLayout()
        self.near_step = QSpinBox()
        self.near_step.setRange(10, 1000)
        self.near_step.setValue(60)
        self.near_step.setSuffix(" mm")
        self.near_step.setToolTip("Depth mode: a pixel is an object when it is at least this much nearer than the background")
        self.max_range = QSpinBox()
        self.max_range.setRange(500, 9000)
        self.max_range.setValue(4500)
        self.max_range.setSuffix(" mm")
        self.max_range.setToolTip("Depth mode: ignore anything farther than this (and, without a background, take everything nearer)")
        drow.addWidget(QLabel("nearer by"))
        drow.addWidget(self.near_step)
        drow.addWidget(QLabel("max range"))
        drow.addWidget(self.max_range)
        self.depth_row = QWidget()
        self.depth_row.setLayout(drow)
        df.addRow("Depth", self.depth_row)
        self.thresh = QSlider(Qt.Horizontal)
        self.thresh.setRange(0, 255)
        self.thresh.setToolTip("0 = automatic (Otsu); for diff mode the per-pixel difference")
        df.addRow("Threshold", self.thresh)
        self.min_area = QSpinBox()
        self.min_area.setRange(50, 500000)
        self.min_area.setValue(1500)
        self.min_area.setToolTip("Ignore blobs smaller than this many camera pixels")
        df.addRow("Min area", self.min_area)
        self.simplify = QSlider(Qt.Horizontal)
        self.simplify.setRange(1, 60)
        self.simplify.setValue(12)
        self.simplify.setToolTip("How much to simplify outlines (fewer vertices to the right)")
        df.addRow("Simplify", self.simplify)
        self.inside_only = QCheckBox("Only inside projector footprint")
        self.inside_only.setChecked(True)
        df.addRow("", self.inside_only)
        brow = QHBoxLayout()
        bg_btn = QPushButton("Set background (diff)")
        bg_btn.setToolTip("Remember the empty scene for diff detection: store it, place the prop, Detect. Adds nothing to the editor; "
                          "'Reference from camera' does that.")
        bg_btn.clicked.connect(self._set_background)
        self.detect_btn = QPushButton("Detect objects")
        self.detect_btn.clicked.connect(self._detect)
        self.live = QCheckBox("Live")
        self.live.setToolTip("Re-run detection twice a second on the preview")
        self.create_btn = QPushButton("Create surfaces")
        self.create_btn.setToolTip("One triangle-mesh surface per detected object, in projector pixels")
        self.create_btn.clicked.connect(self._create_surfaces)
        self.show3d = QCheckBox("Show in 3D")
        self.show3d.setToolTip("Stream the depth camera's point cloud into the 3D view, hung on the scene's first camera node")
        self.show3d.toggled.connect(self._toggle_3d)
        for b in (bg_btn, self.detect_btn, self.live, self.show3d, self.create_btn):
            brow.addWidget(b)
        df.addRow(brow)
        lay.addWidget(det)

        sc = QGroupBox("Scene scan (projector as the light)")
        sf = QFormLayout(sc)
        srow = QHBoxLayout()
        self.scan_btn = QPushButton("Scan scene")
        self.scan_btn.setToolTip("Projects black, white, red, green, blue and a fine stripe pair, and turns the camera's "
                                 "response into per-pixel cues: albedo, colour, direct vs bounced light, depth off the wall, shadows. "
                                 "Needs a calibration; takes a few seconds; the scene must hold still.")
        self.scan_btn.clicked.connect(self._scan)
        self.view = QComboBox()
        self.view.addItems(["camera", "live depth", "albedo", "color", "direct", "depth", "shadow", "segments"])
        self.view.setToolTip("What the preview shows: the live camera or one of the scan's cue maps")
        srow.addWidget(self.scan_btn)
        srow.addWidget(QLabel("view"))
        srow.addWidget(self.view, 1)
        sf.addRow(srow)
        wrow = QHBoxLayout()
        self.k = QSpinBox()
        self.k.setRange(2, 8)
        self.k.setValue(4)
        self.k.setToolTip("Number of material/depth classes to split the scene into")
        wrow.addWidget(QLabel("classes"))
        wrow.addWidget(self.k)
        self.weights: dict[str, QSlider] = {}
        for name, default in (("color", 100), ("depth", 100), ("albedo", 100), ("direct", 50)):
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 200)
            sl.setValue(default)
            sl.setToolTip(f"Weight of the {name} cue in the clustering (0 ignores it)")
            self.weights[name] = sl
            wrow.addWidget(QLabel(name))
            wrow.addWidget(sl, 1)
        sf.addRow(wrow)
        xrow = QHBoxLayout()
        self.seg_btn = QPushButton("Segment")
        self.seg_btn.setToolTip("Cluster the cues and outline every region; 'Create surfaces' then turns them into meshes")
        self.seg_btn.clicked.connect(self._segment)
        self.keep_wall = QCheckBox("Keep largest region")
        self.keep_wall.setToolTip("Normally the biggest region is the wall and is dropped")
        xrow.addWidget(self.seg_btn)
        xrow.addWidget(self.keep_wall)
        sf.addRow(xrow)
        self.scan_info = QLabel("no scan. After a scan: 'classes' = how many kinds of surface to split the footprint into; "
                                "the sliders say how much each cue counts (colour of the object, distance off the wall, "
                                "brightness, direct vs bounced light). Start with depth high and the rest low for props on a wall.")
        self.scan_info.setWordWrap(True)
        sf.addRow(self.scan_info)
        lay.addWidget(sc)
        self.scan: scan.SceneScan | None = None

        # -- depth camera extras: planes, people, skeletons ------------------------------
        dg = QGroupBox("Depth camera: planes, people, skeletons")
        dgl = QVBoxLayout(dg)
        prow = QHBoxLayout()
        self.planes_btn = QPushButton("Find planes")
        self.planes_btn.setToolTip("Fit the flat surfaces in the depth image (walls, floor, table tops, box faces)")
        self.planes_btn.clicked.connect(self._find_planes)
        self.planes_props_btn = QPushButton("→ 3D props")
        self.planes_props_btn.setToolTip("Add each found plane as a 'plane' prop in the 3D scene, placed relative to the Kinect node")
        self.planes_props_btn.clicked.connect(self._planes_to_props)
        self.planes_surf_btn = QPushButton("→ surfaces")
        self.planes_surf_btn.setToolTip("Add each found plane's outline as a mesh surface in projector pixels (needs a calibration through this camera)")
        self.planes_surf_btn.clicked.connect(self._planes_to_surfaces)
        for b in (self.planes_btn, self.planes_props_btn, self.planes_surf_btn):
            prow.addWidget(b)
        dgl.addLayout(prow)
        self.planes_list = QListWidget()
        self.planes_list.setMaximumHeight(90)
        dgl.addWidget(self.planes_list)
        self.found_planes: list = []

        hrow = QHBoxLayout()
        hrow.addWidget(QLabel("People:"))
        self.people_mode = QComboBox()
        self.people_mode.addItems(["off", "mask (keep light off them)", "spotlight (put media on them)"])
        self.people_mode.setToolTip("Live surface that follows anyone who was not there when 'Set background' was pressed. "
                                    "Press Set background with the scene empty of people first.")
        self.people_mode.currentIndexChanged.connect(self._people_mode_changed)
        self.people_margin = QSpinBox()
        self.people_margin.setRange(0, 80)
        self.people_margin.setValue(12)
        self.people_margin.setSuffix(" px")
        self.people_margin.setToolTip("Grow the person outline by this many camera pixels (safety border)")
        hrow.addWidget(self.people_mode, 1)
        hrow.addWidget(QLabel("margin"))
        hrow.addWidget(self.people_margin)
        dgl.addLayout(hrow)
        srow = QHBoxLayout()
        self.skeleton = QCheckBox("Skeleton tracking")
        self.skeleton.setToolTip("MediaPipe pose on the colour image (33 joints per person), drawn on the preview and, with depth, in the 3D view")
        self.skeleton.toggled.connect(self._skeleton_toggled)
        self.people_info = QLabel("")
        self.people_info.setWordWrap(True)
        srow.addWidget(self.skeleton)
        srow.addWidget(self.people_info, 1)
        dgl.addLayout(srow)
        lay.addWidget(dg)
        self._tracker = None
        self._skeletons: list = []
        self._last_people = 0.0

        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)
        self._last_live = 0.0
        self._last_cloud = 0.0
        self._last_mask: np.ndarray | None = None
        self._mode_changed(self.mode.currentText())
        state.projectReplaced.connect(self._refresh_cal_info)
        state.cancelRequested.connect(self._cancel_all)
        self._scan_cameras()
        self._refresh_cal_info()

    # -- camera lifecycle ------------------------------------------------------------
    def _scan_cameras(self):
        self.device.clear()
        try:
            self.cameras = list_cameras()
        except Exception as exc:  # pragma: no cover
            log.warning("camera scan failed: %s", exc)
            self.cameras = []
        for c in self.cameras:
            self.device.addItem(c.label, c.index)
        if kinect.usb_link_speed() is not None:
            self.device.addItem("Kinect v2 (colour on depth pixels, 512x424)", -2)
        if kinect1.plugged_in() or kinect1.motor_only():
            self.device.addItem("Kinect v1 (depth on colour pixels, 640x480)", -3)
        if self.device.count() == 0:
            self.device.addItem("no camera found", -1)

    def _toggle(self, on: bool):
        if on:
            idx = self.device.currentData()
            if idx is None or idx == -1:
                self.start_btn.setChecked(False)
                return
            try:
                if idx == -2:
                    self.status.setText(kinect.status())
                    self.camera = kinect.KinectCameraAdapter()
                elif idx == -3:
                    self.status.setText(kinect1.status())
                    self.camera = kinect1.KinectV1CameraAdapter()
                else:
                    self.camera = Camera(idx, self.res.currentData())
            except Exception as exc:
                self.status.setText(f"cannot open camera: {exc}")
                self.start_btn.setChecked(False)
                return
            self.start_btn.setText("Stop")
            self.status.setText(f"{self.device.currentText()}  {self.camera.size[0]}x{self.camera.size[1]}")
            self._refresh_modes()
            self.timer.start()
        else:
            self.timer.stop()
            self._cancel_calibration()
            if self.camera is not None:
                self.camera.close()
                self.camera = None
            self.start_btn.setText("Start")
            self.preview.setText("no camera")
            self.preview.setPixmap(QPixmap())

    def shutdown(self):
        self.start_btn.setChecked(False)
        if self._tracker is not None:
            self._tracker.close()
            self._tracker = None
        self.state.set_skeletons(None, None)

    # -- preview -----------------------------------------------------------------------
    def _tick(self):
        if self.camera is None:
            return
        frame, seq = self.camera.latest()
        if frame is None:
            return
        self._last_frame = frame
        if self._calib is not None:
            run = self._calib
            run.step(frame, seq)
            if self._calib is run and run.stalled(seq):
                self._calib = None
                self._run_done("aborted: the camera stopped delivering frames during the run (restart it and try again)")
            self._show(frame)
            return
        if self.live.isChecked() and time.monotonic() - self._last_live > 0.5:
            self._last_live = time.monotonic()
            self._detect(quiet=True)
        if self.show3d.isChecked() and time.monotonic() - self._last_cloud > 0.2:
            self._last_cloud = time.monotonic()
            self._push_cloud()
        if (self.people_mode.currentIndex() > 0 or self.skeleton.isChecked()) and time.monotonic() - self._last_people > 0.08:
            self._last_people = time.monotonic()
            self._track_people(frame)
        self._show(frame)

    def _show(self, frame: np.ndarray):
        img = frame
        view = self.view.currentText()
        if view == "live depth":
            d = self.camera.latest_depth() if hasattr(self.camera, "latest_depth") else None
            img = kinect.depth_to_image(d) if d is not None else frame
        elif view != "camera" and self.scan is not None and self._calib is None:
            img = self.scan.view(view)
        cal = self.state.calibration
        outline = None
        if cal is not None and cal.cam_size == tuple(img.shape[1::-1]):
            outline = cal.projector_outline_in_camera()
        if self._last_mask is not None and self.mode.currentText() == "depth" and self._last_mask.shape == img.shape[:2]:
            img = img.copy()
            img[self._last_mask] = (img[self._last_mask] * 0.5 + np.array([0, 110, 60])).astype(np.uint8)
        if self._skeletons:
            img = pose.draw_skeletons(img, self._skeletons)
        if outline is not None or self.detections:
            img = detect.draw_detections(img, self.detections, outline)
        h, w = img.shape[:2]
        qimg = QImage(np.ascontiguousarray(img).data, w, h, 3 * w, QImage.Format_RGB888)
        target = self.preview.size() if self.preview_zoom == 1.0 else self.preview.size() * self.preview_zoom
        pix = QPixmap.fromImage(qimg).scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        if self.preview_zoom != 1.0:
            self.preview.setMinimumSize(pix.size())
        self.preview.setPixmap(pix)

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent

        if obj is self.preview and ev.type() == QEvent.Wheel and ev.modifiers() & Qt.ControlModifier:
            self.preview_zoom = max(1.0, min(6.0, self.preview_zoom * (1.2 if ev.angleDelta().y() > 0 else 1 / 1.2)))
            if self.preview_zoom == 1.0:
                self.preview.setMinimumSize(320, 240)
            return True
        return super().eventFilter(obj, ev)

    # -- calibration -----------------------------------------------------------------------
    def _calibrate(self):
        if self.camera is None:
            QMessageBox.information(self, "Calibrate", "Start the camera first.")
            return
        win = self.window()
        if getattr(win, "output", None) is None:
            QMessageBox.information(self, "Calibrate", "Open the output window on the projector first (Output → Open on screen).")
            return
        self.detections = []
        w, h = self.state.project.output_size
        names = graycode.pattern_names(w, h)
        self._start_run(graycode.pattern_count(w, h), lambda i: graycode.pattern_rgb(i, w, h), names,
                        self._finish_calibration, "calibrating")

    def _start_run(self, count: int, pattern_fn, names: list[str], on_done, label: str):
        self._calib = _PatternRun(self, self.settle.value() / 1000.0, count, pattern_fn, names, on_done, label)
        self.cal_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.cal_cancel.setEnabled(True)
        self.cal_progress.setVisible(True)
        self.cal_progress.setRange(0, count)
        self.cal_progress.setValue(0)
        self.camera.lock_exposure(True)
        self._calib.start()

    def _cancel_calibration(self):
        if self._calib is not None:
            self._calib = None
            self._run_done("cancelled")

    def _cancel_all(self):
        """Esc anywhere: stop a run, drop the white wash, show the surfaces again."""
        self._cancel_calibration()
        if self.show_pattern.isChecked():
            self.show_pattern.setChecked(False)
        self.state.set_override_frame(None)

    def _run_done(self, message: str):
        self._calib = None
        self.state.set_override_frame(None)
        if self.camera is not None:
            self.camera.lock_exposure(False)
        self.cal_btn.setEnabled(True)
        self.scan_btn.setEnabled(True)
        self.cal_cancel.setEnabled(False)
        self.cal_progress.setVisible(False)
        self.status.setText(message)

    def _finish_calibration(self, captures: list[np.ndarray]):
        import cv2

        self.last_captures = [cv2.cvtColor(f, cv2.COLOR_RGB2GRAY).astype(np.float32) for f in captures]
        w, h = self.state.project.output_size
        cam_size = (captures[0].shape[1], captures[0].shape[0])
        try:
            px, py, valid = graycode.decode(self.last_captures, w, h)
            cal = graycode.Calibration.from_decoded(px, py, valid, cam_size, (w, h))
        except Exception as exc:
            self._calibration_done(None, f"calibration failed: {exc}")
            return
        self._calibration_done(cal, f"calibrated: {cal.coverage * 100:.0f}% of the camera view is lit, "
                                    f"{cal.inlier_ratio * 100:.0f}% fits one plane")

    def _calibration_done(self, cal, message: str):
        self._run_done(message)
        if cal is not None:
            cal.camera_name = self.device.currentText()
            self.state.set_calibration(cal)
        self._refresh_cal_info()

    # -- scene scan ------------------------------------------------------------------------
    def _scan(self):
        if self.camera is None:
            QMessageBox.information(self, "Scan scene", "Start the camera first.")
            return
        if self.state.calibration is None:
            QMessageBox.information(self, "Scan scene", "Calibrate first: the scan needs the projector footprint and the depth map.")
            return
        if getattr(self.window(), "output", None) is None:
            QMessageBox.information(self, "Scan scene", "Open the output window on the projector first.")
            return
        self.detections = []
        w, h = self.state.project.output_size
        period = scan.stripe_period_for(self.state.calibration)
        self._start_run(len(scan.SCAN_NAMES), lambda i: scan.scan_pattern_rgb(i, w, h, period), scan.SCAN_NAMES,
                        self._finish_scan, "scanning")

    def _finish_scan(self, captures: list[np.ndarray]):
        try:
            self.scan = scan.SceneScan.from_captures(captures, self.state.calibration)
        except Exception as exc:
            self._run_done(f"scan failed: {exc}")
            return
        p = self.state.project.path
        if p is not None:
            self.scan.save(p.with_suffix(".scan.npz"))
        self._run_done("scene scanned: pick a view (albedo / color / direct / depth / shadow), then Segment")
        self.view.setCurrentText("depth")
        self._segment()

    def _segment(self):
        if self.scan is None:
            p = self.state.project.path
            self.scan = scan.SceneScan.load(p.with_suffix(".scan.npz")) if p is not None else None
            if self.scan is None:
                self.status.setText("no scan yet: press Scan scene")
                return
        wt = {k: s.value() / 100.0 for k, s in self.weights.items()}
        self.detections = self.scan.segment(
            k=self.k.value(), w_color=wt["color"], w_depth=wt["depth"], w_albedo=wt["albedo"], w_direct=wt["direct"],
            min_area=self.min_area.value(), epsilon=self.simplify.value() / 1000.0,
            drop_largest=not self.keep_wall.isChecked())
        self.view.setCurrentText("segments")
        self.scan_info.setText(f"{len(self.detections)} region(s) from {self.k.value()} classes; "
                               "adjust weights / classes and Segment again, or Create surfaces")

    def _refresh_cal_info(self):
        cal = self.state.calibration
        if cal is None:
            self.cal_info.setText("not calibrated")
            return
        self.cal_info.setText(
            f"{cal.camera_name or 'camera'} {cal.cam_size[0]}x{cal.cam_size[1]} → projector {cal.proj_size[0]}x{cal.proj_size[1]}   "
            f"lit {cal.coverage * 100:.0f}% of the camera view, plane fit {cal.inlier_ratio * 100:.0f}% inliers"
            + ("" if cal.valid is not None else "   (homography only; dense map not found)"))

    def _white(self, on: bool):
        if on:
            w, h = self.state.project.output_size
            self.state.set_override_frame(np.full((h, w, 3), 255, np.uint8), key="white")
        elif self._calib is None:
            self.state.set_override_frame(None)

    def _reference_from_camera(self):
        """Add the camera's view to the editor as a reference image.

        Calibrated: warped into projector space, so it is already aligned and gets locked.
        Not calibrated: the raw frame, unlocked, to drag into place like a phone photo.
        """
        cal = self.state.calibration
        if self._last_frame is None:
            QMessageBox.information(self, "Reference", "Start the camera first.")
            return
        import cv2

        frame = self._last_frame
        fh, fw = frame.shape[:2]
        # a calibration only applies to the camera (and resolution) it was made with
        aligned = cal is not None and tuple(cal.cam_size) == (fw, fh)
        img = cal.warp_camera_to_projector(frame) if aligned else frame
        out_dir = self.state.project.base_dir / "media"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"camera_ref_{time.strftime('%H%M%S')}.png"
        cv2.imwrite(str(path), cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        s = self.state.add_reference_image(path)
        s.locked = aligned
        if not aligned:
            # keep the camera's aspect: largest rect of the frame's shape centred in the output
            ow, oh = self.state.project.output_size
            k = min(ow / fw, oh / fh)
            w, h = fw * k, fh * k
            x0, y0 = (ow - w) / 2, (oh - h) / 2
            s.corners = [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h]]
        self.state.surface_edited(s.id, geometry_only=True)
        if aligned:
            self.status.setText(f"reference image added, aligned to the projector: {path.name}")
        elif cal is not None:
            self.status.setText(f"reference added unaligned: the calibration is for a {cal.cam_size[0]}x{cal.cam_size[1]} camera, "
                                f"this one is {fw}x{fh}. Calibrate with this camera, or drag the corners. {path.name}")
        else:
            self.status.setText(f"reference image added unaligned (no calibration yet): drag its corners to fit, {path.name}")

    # -- detection -----------------------------------------------------------------------
    def _set_background(self):
        if self._last_frame is not None:
            self.background = self._last_frame.copy()
            self.status.setText("background stored for diff mode")
        if hasattr(self.camera, "latest_depth"):
            d = self.camera.latest_depth()
            if d is not None:
                self.camera.depth_background = d.copy()
                self.status.setText("background stored for diff and depth modes")

    MODE_HELP = {
        "bright": "Lighter blobs. Tick 'Show white' so the projector lights the props evenly; Threshold 0 = auto. Finds the whole lit wall unless something is darker/brighter than it.",
        "dark": "Darker blobs against the lit wall (black props, dark furniture). Tick 'Show white'. Threshold 0 = auto.",
        "diff": "What changed since 'Set background': store the empty scene, place the prop, Detect. Threshold = how different a pixel must be (0 = 25).",
        "edges": "Outlines closed into blobs. Good for high-contrast props, noisy on cluttered scenes.",
        "depth": "Kinect only, ignores light and colour. Objects = at least 'nearer by' in front of the wall: the wall is a plane fitted to the depth, or the stored depth background if you pressed Set background with the scene empty. Farther than 'max range' is ignored. The cut-out is shaded on the preview.",
    }

    def _mode_changed(self, mode: str):
        self.mode_help.setText(self.MODE_HELP.get(mode, ""))
        self.depth_row.setVisible(mode == "depth")
        self.thresh.setEnabled(mode != "depth")
        self._last_mask = None

    def _toggle_3d(self, on: bool):
        if on:
            scene = self.state.project.scene
            if not scene.cameras:
                n = self.state.add_scene_node("camera")
                n.name = "Kinect"
                n.pos = list(scene.projector.pos)
                n.pos[2] += 0.15  # sits on top of the projector
                n.hpr = list(scene.projector.hpr)
                self.state.scene_edited()
            self.state.view3dRequested.emit("open")
        else:
            self.state.set_point_cloud(None, None, None)

    def _push_cloud(self):
        cam = self.camera
        if not hasattr(cam, "latest_points"):
            return
        xyz, rgb = cam.latest_points(stride=3)
        if xyz is None:
            return
        cams = self.state.project.scene.cameras
        self.state.set_point_cloud(xyz, rgb, cams[0].id if cams else None)

    # -- planes ----------------------------------------------------------------------------
    def _depth_grid(self):
        cam = self.camera
        if not hasattr(cam, "latest_depth"):
            return None
        d = cam.latest_depth()
        if d is None:
            return None
        intr = self.camera.kinect.intrinsics() if hasattr(self.camera.kinect, "intrinsics") else kinect1.INTRINSICS
        xyz, _ = kinect1.depth_to_points(d, None, stride=1, intrinsics=intr)
        H, W = d.shape
        grid = np.full((H, W, 3), np.nan, np.float32)
        grid[(d > 0) & (d < 6000.0)] = xyz
        return grid

    def _find_planes(self):
        grid = self._depth_grid()
        if grid is None:
            self.status.setText("Find planes needs a running depth camera (Kinect)")
            return
        self.found_planes = planes.find_planes(grid, max_planes=6, min_pixels=max(800, self.min_area.value()))
        self.planes_list.clear()
        for i, pl in enumerate(self.found_planes):
            self.planes_list.addItem(f"{i + 1}. {pl.label}  ({pl.pixels} px)")
        self.detections = [d for d in (pl.detection() for pl in self.found_planes) if d is not None]
        self.status.setText(f"{len(self.found_planes)} plane(s); '→ 3D props' adds them to the scene, '→ surfaces' to the projector")

    def _camera_node(self):
        cams = self.state.project.scene.cameras
        if cams:
            return cams[0]
        n = self.state.add_scene_node("camera")
        n.name = "Kinect"
        sc = self.state.project.scene
        n.pos = list(sc.projector.pos)
        n.pos[2] += 0.15
        n.hpr = list(sc.projector.hpr)
        self.state.scene_edited()
        return n

    def _planes_to_props(self):
        if not self.found_planes:
            self._find_planes()
        cam = self._camera_node()
        for i, pl in enumerate(self.found_planes):
            node = planes.plane_to_node(pl, cam.pos, cam.hpr, name=f"Plane {i + 1} ({pl.label.split(' ')[0]})")
            self.state.project.scene.nodes.append(node)
        self.state.scene_edited()
        self.state.view3dRequested.emit("open")
        self.status.setText(f"added {len(self.found_planes)} plane prop(s) to the 3D scene")

    def _planes_to_surfaces(self):
        if not self.found_planes:
            self._find_planes()
        cal = self.state.calibration
        if cal is None or self._last_frame is None or tuple(cal.cam_size) != tuple(self._last_frame.shape[1::-1]):
            QMessageBox.information(self, "Planes", "Calibrate through this camera first so plane outlines can be mapped to projector pixels.")
            return
        dets = [d for d in (pl.detection() for pl in self.found_planes) if d is not None]
        n0 = len(self.state.project.surfaces)
        for i, m in enumerate(detect.detections_to_meshes(dets, cal)):
            lbl = self.found_planes[i].label if i < len(self.found_planes) else "plane"
            self.state.add_mesh_surface(m, f"Plane {n0 + i + 1}", item_type="wall" if "wall" in lbl else "flat object", notes=lbl)
        self.status.setText(f"added {len(dets)} plane surface(s)")

    # -- people / skeletons ----------------------------------------------------------------
    def _people_mode_changed(self, idx: int):
        if idx == 0:
            s = self.state.project.surface_by_id(people.LIVE_ID)
            if s is not None:
                s.mesh = __import__("promap.model.mesh", fromlist=["Mesh"]).Mesh()
                self.state.surface_edited(s.id, geometry_only=True)
            self.people_info.setText("")
            return
        if self.camera is None or not hasattr(self.camera, "latest_depth"):
            self.people_info.setText("needs a running Kinect")
            self.people_mode.setCurrentIndex(0)
            return
        if getattr(self.camera, "depth_background", None) is None:
            self.people_info.setText("press 'Set background' with nobody in the scene first")
        cal = self.state.calibration
        if cal is None:
            self.people_info.setText("calibrate through the Kinect first: the live surface needs camera → projector mapping")

    def _skeleton_toggled(self, on: bool):
        if on and self._tracker is None:
            ok, why = pose.model_available()
            try:
                self._tracker = pose.PoseTracker()
            except Exception as exc:
                self.people_info.setText(f"skeleton tracking unavailable: {why or exc}")
                self.skeleton.setChecked(False)
                return
        if not on:
            self._skeletons = []
            self.state.set_skeletons(None, None)

    def _track_people(self, frame: np.ndarray):
        cam = self.camera
        depth = cam.latest_depth() if hasattr(cam, "latest_depth") else None
        if self.skeleton.isChecked() and self._tracker is not None:
            intr = cam.kinect.intrinsics() if hasattr(getattr(cam, "kinect", None), "intrinsics") else None
            try:
                self._skeletons = self._tracker.track(frame, depth, intr)
            except Exception as exc:
                log.debug("pose: %s", exc)
                self._skeletons = []
            if depth is not None:
                node = self._camera_node() if self._skeletons else None
                self.state.set_skeletons([sk.xyz for sk in self._skeletons if sk.xyz is not None] or None,
                                         node.id if node else None)
        mode = self.people_mode.currentIndex()
        if mode == 0 or depth is None:
            return
        bg = getattr(cam, "depth_background", None)
        if bg is None:
            bg = kinect.plane_background(depth)
            if bg is None:
                return
        mask = people.people_mask(depth, bg, step_mm=float(self.near_step.value()), margin_px=self.people_margin.value())
        roi = self._roi(frame)
        if roi is not None:
            mask &= roi > 0
        dets = people.people_outlines(mask, min_area=float(self.min_area.value()))
        cal = self.state.calibration
        if cal is None or tuple(cal.cam_size) != tuple(frame.shape[1::-1]):
            self.people_info.setText(f"{len(dets)} person(s) seen; calibrate through this camera to project onto them")
            self.detections = dets
            return
        mesh = people.outlines_to_mesh(dets, cal)
        s = people.live_surface(self.state.project, "mask" if mode == 1 else "media")
        s.mesh = mesh
        s.corners = mesh.bounds_corners() if mesh.verts else [[0, 0], [1, 0], [1, 1], [0, 1]]
        self.state.surface_edited(s.id, geometry_only=True, undoable=False)
        self.people_info.setText(f"{len(dets)} person(s): live {'mask' if mode == 1 else 'spotlight'} with {len(mesh.tris)} triangles")

    def _refresh_modes(self):
        has_depth = hasattr(self.camera, "latest_depth")
        items = list(detect.MODES) + (["depth"] if has_depth else [])
        cur = self.mode.currentText()
        self.mode.blockSignals(True)
        self.mode.clear()
        self.mode.addItems(items)
        self.mode.setCurrentText(cur if cur in items else items[0])
        self.mode.blockSignals(False)

    def _detect_depth(self) -> list:
        cam = self.camera
        d = cam.latest_depth() if hasattr(cam, "latest_depth") else None
        if d is None:
            raise ValueError("no depth frame yet")
        roi = self._roi(self._last_frame) if self._last_frame is not None else None
        bg = cam.depth_background
        self._depth_bg_kind = "stored background"
        if bg is None:
            bg = kinect.plane_background(d, None if roi is None else roi > 0)
            self._depth_bg_kind = "fitted wall plane" if bg is not None else "max range only"
        fg = kinect.foreground_mask(d, bg, max_mm=float(self.max_range.value()), step_mm=float(self.near_step.value()))
        if roi is not None:
            fg &= roi > 0
        self._last_mask = fg
        return kinect.mask_to_contours(fg, min_area=self.min_area.value(), epsilon=self.simplify.value() / 1000.0)

    def _roi(self, frame) -> np.ndarray | None:
        cal = self.state.calibration
        if not self.inside_only.isChecked() or cal is None or cal.cam_size != tuple(frame.shape[1::-1]):
            return None
        import cv2

        mask = np.zeros(frame.shape[:2], np.uint8)
        cv2.fillPoly(mask, [cal.projector_outline_in_camera().astype(np.int32)], 255)
        return mask

    def _detect(self, quiet: bool = False):
        frame = self._last_frame
        if frame is None:
            if not quiet:
                self.status.setText("start the camera first")
            return
        mode = self.mode.currentText()
        try:
            if mode == "depth":
                self.detections = self._detect_depth()
                if not quiet:
                    self.status.setText(f"{len(self.detections)} object(s) at least {self.near_step.value()} mm in front of the "
                                        f"{getattr(self, '_depth_bg_kind', 'background')}")
                return
            self.detections = detect.segment(
                frame, mode, background=self.background, thresh=self.thresh.value(), min_area=self.min_area.value(),
                epsilon=self.simplify.value() / 1000.0, roi=self._roi(frame))
        except ValueError as exc:
            self.detections = []
            self.status.setText(str(exc))
            return
        if not quiet:
            self.status.setText(f"{len(self.detections)} object(s); 'Create surfaces' adds them as meshes")

    def _create_surfaces(self):
        if not self.detections:
            self._detect()
        if not self.detections:
            return
        cal = self.state.calibration
        if cal is None:
            QMessageBox.information(self, "Create surfaces",
                                    "Calibrate first so camera pixels can be mapped into the projector. "
                                    "Without it the shapes would land in camera coordinates.")
            return
        meshes = detect.detections_to_meshes(self.detections, cal)
        n0 = len(self.state.project.surfaces)
        for i, m in enumerate(meshes):
            self.state.add_mesh_surface(m, f"Object {n0 + i + 1}", item_type="flat object",
                                        notes=f"detected by camera ({self.mode.currentText()}), {len(m.tris)} triangles")
        self.status.setText(f"added {len(meshes)} mesh surface(s)")
        self.detections = []


class _PatternRun:
    """Step machine driven from the preview timer: show pattern, wait, grab, next, then on_done(captures)."""

    def __init__(self, panel: CameraPanel, settle: float, count: int, pattern_fn, names: list[str], on_done, label: str):
        self.panel = panel
        self.state = panel.state
        self.count = count
        self.pattern_fn = pattern_fn
        self.names = names
        self.on_done = on_done
        self.label = label
        self.settle = settle
        self.index = -1
        self.captures: list[np.ndarray] = []  # RGB frames, one per pattern
        self._shown_at = 0.0
        self._seq_at_show = 0
        self._last_seen_seq = -1
        self._last_seen_at = time.monotonic()

    def start(self):
        self._advance()

    def _advance(self):
        self.index += 1
        if self.index >= self.count:
            self._finish()
            return
        self.state.set_override_frame(self.pattern_fn(self.index), key=(self.label, self.index))
        self._shown_at = time.monotonic()
        self._seq_at_show = self.panel.camera.latest()[1] if self.panel.camera else 0
        self.panel.cal_progress.setValue(self.index)
        name = self.names[self.index] if self.index < len(self.names) else str(self.index)
        self.panel.status.setText(f"{self.label}: pattern {self.index + 1}/{self.count} ({name})")

    STALL_SECONDS = 6.0

    def stalled(self, seq: int) -> bool:
        """True when no fresh camera frame has arrived for STALL_SECONDS after a pattern went up."""
        if self.index < 0 or self.index >= self.count:
            return False
        if seq != self._last_seen_seq:
            self._last_seen_seq, self._last_seen_at = seq, time.monotonic()
        return time.monotonic() - max(self._shown_at, self._last_seen_at) > self.STALL_SECONDS

    def step(self, frame: np.ndarray, seq: int):
        if self.index < 0 or self.index >= self.count:
            return
        # the camera needs the settle time AND at least two frames taken after the pattern went up
        if time.monotonic() - self._shown_at < self.settle or seq < self._seq_at_show + 2:
            return
        self.captures.append(frame.copy())
        self._advance()

    def _finish(self):
        captures = self.captures
        if self.panel._calib is self:
            self.on_done(captures)
