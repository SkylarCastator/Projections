"""Entry point: `promap [project.promap] [--output SCREEN] [--cue N] [--smoke SECONDS]`."""
from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="promap", description="Projection mapping editor and show runner")
    ap.add_argument("project", nargs="?", help="project file to open")
    ap.add_argument("--output", metavar="SCREEN", help="open the output window on this screen name (e.g. HDMI-1)")
    ap.add_argument("--view3d", action="store_true", help="open the Panda3D scene view at start")
    ap.add_argument("--cue", type=int, metavar="N", help="jump straight to cue N (1-based)")
    ap.add_argument("--smoke", type=float, metavar="SECONDS", help="run headless-ish for N seconds then exit (self-test)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("promap")

    from promap.ui.main_window import MainWindow
    from promap.ui.state import AppState

    state = AppState()
    win = MainWindow(state)
    if args.project:
        win.load_project(args.project)
    win.show()
    if args.output:
        win.open_output(args.output)
    if args.cue is not None:
        state.goto(args.cue - 1)
    if args.view3d:
        win.open_view3d()

    if args.smoke:
        def finish():
            frames = win.canvas.frames
            out = win.output.frames if win.output else 0
            errs = state.engine.load_errors
            v3d = win.view3d.base.clock.getFrameCount() if win.view3d and win.view3d.alive else 0
            ok = frames > 0 and not errs and (v3d > 0 or not args.view3d)
            print(f"smoke: canvas frames={frames} output frames={out} 3d frames={v3d} load_errors={errs}")
            app.exit(0 if ok else 1)
        QTimer.singleShot(int(args.smoke * 1000), finish)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
