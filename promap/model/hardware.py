"""Hardware library: projectors, cameras and depth sensors you own, kept outside any
project so every show can pull the same parameters. Stored as JSON in the user's data
directory (override with PROMAP_HARDWARE=/path/to/file.json).
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from uuid import uuid4

KINDS = ("projector", "camera", "depth_camera", "other")


def default_path() -> Path:
    env = os.environ.get("PROMAP_HARDWARE")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "promap" / "hardware.json"


@dataclass
class Device:
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    name: str = "Device"
    kind: str = "projector"
    make: str = ""
    model: str = ""
    width: int = 1920  # native resolution
    height: int = 1080
    throw_ratio: float | None = 1.5  # projectors: distance / image width
    fov_h: float | None = None  # cameras: horizontal field of view in degrees
    fov_v: float | None = None
    depth_min: float | None = None  # depth cameras: usable range in metres
    depth_max: float | None = None
    notes: str = ""

    @property
    def horizontal_fov(self) -> float | None:
        """Horizontal FOV in degrees whichever way the device was specified."""
        if self.fov_h:
            return self.fov_h
        if self.throw_ratio:
            return math.degrees(2.0 * math.atan(1.0 / (2.0 * self.throw_ratio)))
        return None

    @property
    def label(self) -> str:
        mm = " ".join(x for x in (self.make, self.model) if x)
        return f"{self.name} ({mm})" if mm and mm != self.name else self.name


def seed_devices() -> list[Device]:
    """Known hardware to start from. Specs are manufacturer-published values."""
    return [
        Device(
            id="kinectv2", name="Kinect v2", kind="depth_camera", make="Microsoft", model="Kinect for Xbox One",
            width=512, height=424, throw_ratio=None, fov_h=70.6, fov_v=60.0, depth_min=0.5, depth_max=4.5,
            notes="Time-of-flight depth 512x424 @30 fps; colour camera 1920x1080, 84.1 x 53.8 deg. "
            "Needs USB 3 and the Kinect adapter. Driver: libfreenect2.",
        ),
        Device(
            id="kinectv1", name="Kinect v1", kind="depth_camera", make="Microsoft", model="Kinect for Xbox 360",
            width=640, height=480, throw_ratio=None, fov_h=57.0, fov_v=43.0, depth_min=0.8, depth_max=4.0,
            notes="Structured-light depth; colour 640x480. Driver: libfreenect.",
        ),
        Device(
            id="genericpj", name="Generic 1080p projector", kind="projector", make="", model="",
            width=1920, height=1080, throw_ratio=1.5, notes="Edit throw ratio from the spec sheet.",
        ),
    ]


@dataclass
class HardwareLibrary:
    devices: list[Device] = field(default_factory=list)
    path: Path | None = None

    def by_id(self, did: str | None) -> Device | None:
        return next((d for d in self.devices if d.id == did), None) if did else None

    def of_kind(self, *kinds: str) -> list[Device]:
        return [d for d in self.devices if d.kind in kinds]

    def add(self, device: Device) -> Device:
        self.devices.append(device)
        return device

    def remove(self, did: str):
        self.devices = [d for d in self.devices if d.id != did]

    def to_dict(self) -> dict:
        return {"format": 1, "devices": [asdict(d) for d in self.devices]}

    @classmethod
    def from_dict(cls, d: dict) -> "HardwareLibrary":
        return cls(devices=[Device(**x) for x in d.get("devices", [])])

    def save(self, path: Path | None = None) -> Path:
        p = path or self.path or default_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2))
        self.path = p
        return p

    @classmethod
    def load(cls, path: Path | None = None) -> "HardwareLibrary":
        p = path or default_path()
        if p.exists():
            lib = cls.from_dict(json.loads(p.read_text()))
        else:
            lib = cls(devices=seed_devices())
        lib.path = p
        return lib
