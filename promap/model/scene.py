"""3D scene description: where the projector, any extra cameras and the props sit.

Coordinates follow Panda3D's default: metres, Z up, +Y forward, right-handed.
`hpr` is heading / pitch / roll in degrees (heading about Z, pitch about X, roll about Y).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from uuid import uuid4

PRIMITIVES = ("box", "plane", "sphere")


def _new_id() -> str:
    return uuid4().hex[:8]


@dataclass
class Projector:
    pos: list[float] = field(default_factory=lambda: [0.0, -3.0, 1.2])
    hpr: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    fov_h: float = 40.0  # horizontal field of view, degrees; vertical follows output aspect
    device_id: str | None = None  # entry in the hardware library this was filled from

    @property
    def throw_ratio(self) -> float:
        """distance / image width: the number projector spec sheets quote."""
        return 1.0 / (2.0 * math.tan(math.radians(self.fov_h) / 2.0))

    @staticmethod
    def fov_from_throw_ratio(tr: float) -> float:
        return math.degrees(2.0 * math.atan(1.0 / (2.0 * tr)))


@dataclass
class SceneNode:
    id: str = field(default_factory=_new_id)
    name: str = "Prop"
    kind: str = "prop"  # "prop" (a mesh) or "camera" (an offset viewpoint, e.g. the Kinect)
    model: str = "box"  # a PRIMITIVES name or a model file path (obj/gltf/egg/bam/stl via assimp)
    pos: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.5])
    hpr: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    scale: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    fov_h: float = 58.0  # cameras only; 58 is roughly a Kinect v1 colour camera
    color: list[float] = field(default_factory=lambda: [0.75, 0.75, 0.75])
    device_id: str | None = None  # cameras: hardware library entry
    item_type: str = ""  # props: wall / curved object / decoration ... (ITEM_TYPES in project.py)
    notes: str = ""


@dataclass
class Scene:
    projector: Projector = field(default_factory=Projector)
    nodes: list[SceneNode] = field(default_factory=list)

    def node_by_id(self, nid: str) -> SceneNode | None:
        return next((n for n in self.nodes if n.id == nid), None)

    @property
    def cameras(self) -> list[SceneNode]:
        return [n for n in self.nodes if n.kind == "camera"]

    def to_dict(self) -> dict:
        return {"projector": asdict(self.projector), "nodes": [asdict(n) for n in self.nodes]}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Scene":
        d = d or {}
        return cls(
            projector=Projector(**d.get("projector", {})),
            nodes=[SceneNode(**n) for n in d.get("nodes", [])],
        )
