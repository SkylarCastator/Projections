"""Minimal ComfyUI HTTP client: upload, queue, wait, download. Standard library HTTP;
websocket progress when `websocket-client` is importable, polling otherwise."""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from urllib import request, parse, error

log = logging.getLogger(__name__)


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.client_id = uuid.uuid4().hex

    # -- http helpers ---------------------------------------------------------------
    def _get(self, path: str, raw: bool = False):
        try:
            with request.urlopen(f"{self.base}{path}", timeout=self.timeout) as r:
                data = r.read()
        except error.URLError as exc:
            raise ComfyError(f"GET {path}: {exc}") from exc
        return data if raw else json.loads(data or b"{}")

    def _post_json(self, path: str, payload: dict) -> dict:
        req = request.Request(f"{self.base}{path}", data=json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:2000]
            raise ComfyError(f"POST {path}: {exc.code} {body}") from exc
        except error.URLError as exc:
            raise ComfyError(f"POST {path}: {exc}") from exc

    # -- api ----------------------------------------------------------------------------
    def system_stats(self) -> dict:
        return self._get("/system_stats")

    def upload_image(self, path: str | Path, subfolder: str = "promap", overwrite: bool = True) -> str:
        """Upload to ComfyUI's input folder; returns the name LoadImage expects."""
        p = Path(path)
        boundary = f"----promap{uuid.uuid4().hex}"
        parts = []
        for name, value in (("subfolder", subfolder), ("overwrite", "true" if overwrite else "false"), ("type", "input")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{p.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n".encode() + p.read_bytes() + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        req = request.Request(f"{self.base}/upload/image", data=b"".join(parts),
                              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as r:
                info = json.loads(r.read() or b"{}")
        except error.URLError as exc:
            raise ComfyError(f"upload: {exc}") from exc
        sub = info.get("subfolder") or ""
        name = info.get("name", p.name)
        return f"{sub}/{name}" if sub else name

    def queue(self, workflow: dict) -> str:
        wf = {k: v for k, v in workflow.items() if not k.startswith("_")}
        res = self._post_json("/prompt", {"prompt": wf, "client_id": self.client_id})
        if "prompt_id" not in res:
            raise ComfyError(f"queue failed: {res}")
        return res["prompt_id"]

    def history(self, prompt_id: str) -> dict | None:
        h = self._get(f"/history/{prompt_id}")
        return h.get(prompt_id)

    def wait(self, prompt_id: str, on_progress=None, poll: float = 1.0, timeout: float = 3600.0,
             should_cancel=None) -> dict:
        """Block until the prompt finishes; returns its `outputs`. Progress callback gets
        (fraction 0..1, message)."""
        deadline = time.monotonic() + timeout
        ws = self._open_ws()
        try:
            while time.monotonic() < deadline:
                if should_cancel and should_cancel():
                    self.interrupt()
                    raise ComfyError("cancelled")
                entry = self.history(prompt_id)
                if entry is not None:
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        msgs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                        detail = msgs[0][1].get("exception_message", "") if msgs else ""
                        raise ComfyError(f"workflow failed: {detail or status}")
                    return entry.get("outputs", {})
                if ws is not None:
                    self._pump_ws(ws, on_progress, prompt_id, poll)
                else:
                    if on_progress:
                        on_progress(None, "waiting for ComfyUI…")
                    time.sleep(poll)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
        raise ComfyError("timed out waiting for ComfyUI")

    def _open_ws(self):
        try:
            import websocket  # websocket-client

            url = self.base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={self.client_id}"
            ws = websocket.create_connection(url, timeout=1.0)
            return ws
        except Exception as exc:
            log.debug("no websocket progress (%s); polling", exc)
            return None

    def _pump_ws(self, ws, on_progress, prompt_id: str, poll: float):
        end = time.monotonic() + poll
        while time.monotonic() < end:
            try:
                msg = ws.recv()
            except Exception:
                return
            if not isinstance(msg, str):
                continue
            try:
                m = json.loads(msg)
            except ValueError:
                continue
            data = m.get("data", {})
            if data.get("prompt_id") not in (None, prompt_id):
                continue
            t = m.get("type")
            if t == "progress" and on_progress:
                on_progress(data.get("value", 0) / max(1, data.get("max", 1)), f"sampling {data.get('value')}/{data.get('max')}")
            elif t == "executing" and on_progress:
                on_progress(None, "finished" if data.get("node") is None else f"node {data.get('node')}")
            elif t == "execution_error":
                raise ComfyError(f"workflow failed: {data.get('exception_message', data)}")

    def interrupt(self):
        try:
            self._post_json("/interrupt", {})
        except ComfyError:
            pass

    @staticmethod
    def output_files(outputs: dict) -> list[dict]:
        """Flatten history outputs into file descriptors ({filename, subfolder, type})."""
        files = []
        for node_out in outputs.values():
            if not isinstance(node_out, dict):
                continue
            for key, val in node_out.items():
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, dict) and "filename" in item:
                            files.append({**item, "key": key})
        return files

    def download(self, file: dict, dest_dir: str | Path, stem: str | None = None) -> Path:
        q = parse.urlencode({"filename": file["filename"], "subfolder": file.get("subfolder", ""),
                             "type": file.get("type", "output")})
        data = self._get(f"/view?{q}", raw=True)
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file["filename"]).suffix or ".bin"
        name = (stem or Path(file["filename"]).stem) + suffix
        out = dest_dir / name
        i = 1
        while out.exists():
            out = dest_dir / f"{Path(name).stem}_{i}{suffix}"
            i += 1
        out.write_bytes(data)
        return out
