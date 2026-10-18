"""User settings (not per project): ComfyUI endpoint and generation defaults.
Stored at $XDG_CONFIG_HOME/promap/settings.json (override: $PROMAP_SETTINGS)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path


def default_path() -> Path:
    env = os.environ.get("PROMAP_SETTINGS")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "promap" / "settings.json"


@dataclass
class Settings:
    comfy_url: str = "http://127.0.0.1:8188"
    default_template: str = ""
    generated_dir: str = "media/generated"  # relative to the project file
    default_frames: int = 49
    default_fps: int = 16
    negative_prompt: str = "blurry, low quality, watermark, text"
    max_side: int = 832  # longest side of the generated clip
    path: Path | None = None

    def save(self, path: Path | None = None) -> Path:
        p = path or self.path or default_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        d.pop("path")
        p.write_text(json.dumps(d, indent=2))
        self.path = p
        return p

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        p = path or default_path()
        s = cls()
        if p.exists():
            try:
                d = json.loads(p.read_text())
                s = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "path"})
            except Exception:
                s = cls()
        s.path = p
        return s
