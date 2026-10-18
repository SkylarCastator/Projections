"""Workflow templates: any ComfyUI workflow exported in API format, with the nodes promap
should fill titled `promap_<name>` (optionally `promap_<name>:<input key>`).

Names: image, prompt, negative, seed, frames, width, height, fps, prefix.
Binding by title means a template keeps working when models and node types change:
re-export the workflow, keep the titles, done.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

TITLE_PREFIX = "promap_"
KEY_CANDIDATES: dict[str, list[str]] = {
    "image": ["image"],
    "prompt": ["text", "prompt", "positive"],
    "negative": ["text", "prompt", "negative"],
    "seed": ["seed", "noise_seed"],
    "frames": ["length", "num_frames", "frames", "video_length", "frame_count", "video_frames"],
    "width": ["width"],
    "height": ["height"],
    "fps": ["fps", "frame_rate"],
    "prefix": ["filename_prefix"],
}

EXAMPLES_DIR = Path(__file__).parent / "workflows"


@dataclass
class Binding:
    node_id: str
    name: str
    key: str
    class_type: str


def parse_title(title: str) -> tuple[str, str | None] | None:
    if not title.startswith(TITLE_PREFIX):
        return None
    rest = title[len(TITLE_PREFIX):]
    name, _, key = rest.partition(":")
    return name.strip(), (key.strip() or None)


def bindings(workflow: dict) -> list[Binding]:
    out = []
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            continue
        parsed = parse_title(str(node.get("_meta", {}).get("title", "")))
        if parsed is None:
            continue
        name, key = parsed
        inputs = node.get("inputs", {})
        if key is None:
            cands = KEY_CANDIDATES.get(name, [name])
            key = next((k for k in cands if k in inputs), cands[0])
        out.append(Binding(nid, name, key, node.get("class_type", "?")))
    return out


def bind(workflow: dict, values: dict) -> dict:
    """Return a copy of `workflow` with bound inputs set from `values` (None values skipped)."""
    wf = copy.deepcopy(workflow)
    for b in bindings(wf):
        if b.name in values and values[b.name] is not None:
            wf[b.node_id].setdefault("inputs", {})[b.key] = values[b.name]
    return wf


def validate(workflow: dict) -> list[str]:
    """Human-readable problems: not API format, no promap_ titles, etc."""
    problems = []
    if not isinstance(workflow, dict) or "nodes" in workflow and "links" in workflow:
        problems.append("This is the UI-format export. In ComfyUI enable Dev mode and use 'Save (API format)'.")
        return problems
    if not any(isinstance(n, dict) and "class_type" in n for n in workflow.values()):
        problems.append("No nodes with class_type found; is this an API-format workflow?")
    names = {b.name for b in bindings(workflow)}
    if "prompt" not in names:
        problems.append("No node titled promap_prompt; the prompt field will be ignored.")
    return problems


class TemplateStore:
    def __init__(self, directory: Path | None = None):
        env = os.environ.get("PROMAP_WORKFLOWS")
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "promap" / "workflows"
        self.dir = directory or (Path(env).expanduser() if env else base)

    def install_examples(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        for src in EXAMPLES_DIR.glob("*.json"):
            dst = self.dir / src.name
            if not dst.exists():
                shutil.copy(src, dst)

    def names(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def load(self, name: str) -> dict:
        return json.loads(self.path(name).read_text())

    def describe(self, name: str) -> str:
        wf = self.load(name)
        meta = wf.get("_promap", {}) if isinstance(wf.get("_promap"), dict) else {}
        bs = ", ".join(f"{b.name}→{b.class_type}.{b.key}" for b in bindings(wf))
        return (meta.get("description", "") + "\n" if meta else "") + f"binds: {bs or 'nothing'}"
