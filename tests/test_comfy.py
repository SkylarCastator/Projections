import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np
import pytest

from promap.comfy import prepare
from promap.comfy.client import ComfyClient, ComfyError
from promap.comfy.templates import bind, bindings, validate, TemplateStore, EXAMPLES_DIR


# ---------------------------------------------------------------- templates
def test_examples_bind_all_names():
    for path in EXAMPLES_DIR.glob("*.json"):
        wf = json.loads(path.read_text())
        names = {b.name for b in bindings(wf)}
        assert {"prompt", "negative", "seed", "frames", "fps", "prefix"} <= names, path.name
        out = bind(wf, {"prompt": "P", "seed": 7, "frames": 33, "width": 640, "height": 352, "image": "promap/x.png"})
        assert out["5"]["inputs"]["text"] == "P"
        assert out["9"]["inputs"]["seed"] == 7
        assert out["8"]["inputs"]["length"] == 33  # explicit key via promap_frames:length
        assert out["8"]["inputs"]["width"] == 832, "width is not bound in the example (no promap_width)"
        assert wf["9"]["inputs"]["seed"] == 0, "bind must not mutate the template"


def test_bind_key_candidates_and_explicit_key():
    wf = {
        "1": {"class_type": "X", "_meta": {"title": "promap_seed"}, "inputs": {"noise_seed": 1}},
        "2": {"class_type": "Y", "_meta": {"title": "promap_frames:custom_len"}, "inputs": {"custom_len": 5}},
        "3": {"class_type": "Z", "_meta": {"title": "untouched"}, "inputs": {"seed": 1}},
    }
    out = bind(wf, {"seed": 99, "frames": 12, "prompt": "ignored"})
    assert out["1"]["inputs"]["noise_seed"] == 99
    assert out["2"]["inputs"]["custom_len"] == 12
    assert out["3"]["inputs"]["seed"] == 1
    assert any("promap_prompt" in p for p in validate(wf))


def test_validate_detects_ui_format():
    assert validate({"nodes": [], "links": []})[0].startswith("This is the UI-format")


def test_store_installs_examples(tmp_path):
    store = TemplateStore(tmp_path / "wf")
    store.install_examples()
    assert "i2v_example" in store.names() and "t2v_example" in store.names()
    assert "binds:" in store.describe("i2v_example")


# ---------------------------------------------------------------- prepare
def test_rectify_pulls_quad_region_flat():
    photo = np.zeros((300, 400, 3), np.uint8)
    photo[:] = (20, 20, 20)
    # a red trapezoid in the photo; the reference surface covers the whole 800x600 output
    import cv2
    quad_photo = np.array([[100, 60], [300, 80], [280, 240], [120, 220]], np.int32)
    cv2.fillPoly(photo, [quad_photo], (255, 0, 0))
    out_size = (800, 600)
    ref_corners = [[0, 0], [800, 0], [800, 600], [0, 600]]
    surface_corners = [[c[0] * 2, c[1] * 2] for c in quad_photo.tolist()]  # photo px -> output px
    img = prepare.rectify(photo, ref_corners, surface_corners, (128, 96))
    inner = img[16:-16, 16:-16]
    assert inner[..., 0].mean() > 240 and inner[..., 1].mean() < 15, "crop should be solid red"
    mask = prepare.quad_mask(surface_corners, out_size, (80, 60))
    assert mask[30, 40] == 255 and mask[2, 2] == 0


def test_suggested_size_rounds_and_caps():
    w, h = prepare.suggested_size([[0, 0], [2000, 0], [2000, 1000], [0, 1000]], max_side=832)
    assert w == 832 and h == 416
    w, h = prepare.suggested_size([[0, 0], [100, 0], [100, 50], [0, 50]])
    assert w % 16 == 0 and h % 16 == 0 and min(w, h) >= 256


# ---------------------------------------------------------------- client vs fake server
class FakeComfy(BaseHTTPRequestHandler):
    polls = {}

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/history/"):
            pid = self.path.rsplit("/", 1)[1]
            n = FakeComfy.polls.get(pid, 0) + 1
            FakeComfy.polls[pid] = n
            if n < 3:
                return self._json({})
            return self._json({pid: {"status": {"status_str": "success"},
                                     "outputs": {"12": {"images": [{"filename": "gen_00001.mp4", "subfolder": "promap", "type": "output"}]}}}})
        if self.path.startswith("/view?"):
            body = b"MP4DATA"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/system_stats":
            return self._json({"devices": [{"name": "fake", "vram_total": 2**34}]})
        self._json({}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path == "/prompt":
            data = json.loads(body)
            assert "prompt" in data and "client_id" in data and "_promap" not in data["prompt"]
            return self._json({"prompt_id": "abc123", "number": 1})
        if self.path == "/upload/image":
            assert b'name="image"; filename="src.png"' in body and b"PNGBYTES" in body
            return self._json({"name": "src.png", "subfolder": "promap", "type": "input"})
        self._json({}, 404)


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), FakeComfy)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_client_end_to_end(server, tmp_path):
    src = tmp_path / "src.png"
    src.write_bytes(b"PNGBYTES")
    c = ComfyClient(server, timeout=5)
    assert c.system_stats()["devices"][0]["name"] == "fake"
    assert c.upload_image(src) == "promap/src.png"
    wf = json.loads((EXAMPLES_DIR / "i2v_example.json").read_text())
    pid = c.queue(wf)
    msgs = []
    outputs = c.wait(pid, on_progress=lambda f, m: msgs.append(m), poll=0.01)
    files = c.output_files(outputs)
    assert files[0]["filename"] == "gen_00001.mp4"
    out = c.download(files[0], tmp_path / "gen", stem="Pumpkin_7")
    assert out.name == "Pumpkin_7.mp4" and out.read_bytes() == b"MP4DATA"
    out2 = c.download(files[0], tmp_path / "gen", stem="Pumpkin_7")
    assert out2.name == "Pumpkin_7_1.mp4", "never overwrite a previous result"


def test_client_reports_unreachable():
    c = ComfyClient("http://127.0.0.1:9", timeout=1)
    with pytest.raises(ComfyError):
        c.system_stats()
