"""Project document: surfaces, cues, output settings. Serialises to JSON."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from uuid import uuid4

from promap.model.geometry import rect_corners, centroid as quad_centroid
from promap.model.mesh import Mesh
from promap.model.scene import Scene

FORMAT_VERSION = 1

ITEM_TYPES = ("wall", "floor", "ceiling", "flat object", "curved object", "decoration", "actor", "other")
SURFACE_KINDS = ("media", "mask", "reference")


def _new_id() -> str:
    return uuid4().hex[:8]


@dataclass
class Surface:
    id: str = field(default_factory=_new_id)
    name: str = "Surface"
    kind: str = "media"  # "media" draws a texture, "mask" solid black, "reference" editor-only backdrop
    corners: list[list[float]] = field(default_factory=lambda: rect_corners(100, 100, 400, 300))
    media: str | None = None  # path relative to the project file (or absolute)
    opacity: float = 1.0
    visible: bool = True
    loop: bool = True
    speed: float = 1.0
    flip_h: bool = False
    flip_v: bool = False
    item_type: str = ""  # what the quad lands on in the room (see ITEM_TYPES); informational for now
    notes: str = ""
    locked: bool = False  # ignore mouse hits in the canvas
    video_audio: bool = False  # play the video file's audio track with the layer
    react: dict | None = None  # {"param": opacity|scale|gain, "band": rms|bass|mid|high, "amount": 0..1}
    tint: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])  # RGB multiplier on the media
    key_black: bool = False  # luma key: pixels darker than key_threshold become transparent
    key_threshold: float = 0.12  # 0..1 luma below which a pixel is fully transparent (soft edge above it)
    mesh: Mesh | None = None  # triangle mesh geometry; None => the surface is the quad in `corners`

    def __post_init__(self):
        if self.mesh is not None and not isinstance(self.mesh, Mesh):
            self.mesh = Mesh.from_dict(self.mesh)
        self.tint = [float(c) for c in (self.tint or [1.0, 1.0, 1.0])][:3]

    @property
    def is_output(self) -> bool:
        """Reference images exist only to help draw; they never reach the projector."""
        return self.kind != "reference"

    @property
    def is_mesh(self) -> bool:
        return self.mesh is not None

    @property
    def outline_corners(self) -> list[list[float]]:
        """A quad standing in for the surface: its corners, or a mesh's bounding box."""
        return self.mesh.bounds_corners() if self.mesh is not None else self.corners

    def handle_points(self) -> list[list[float]]:
        """Draggable points in output space: the quad corners or every mesh vertex."""
        return self.mesh.positions() if self.mesh is not None else self.corners

    def centroid(self) -> tuple[float, float]:
        return self.mesh.centroid() if self.mesh is not None else quad_centroid(self.corners)

    def translate(self, dx: float, dy: float):
        if self.mesh is not None:
            self.mesh.translate(dx, dy)
        else:
            self.corners = [[x + dx, y + dy] for x, y in self.corners]

    def move_point(self, index: int, x: float, y: float):
        if self.mesh is not None:
            self.mesh.verts[index][0], self.mesh.verts[index][1] = x, y
        else:
            self.corners[index] = [x, y]

    def clone(self) -> "Surface":
        d = asdict(self)
        d["id"] = _new_id()
        d["name"] = f"{self.name} copy"
        d["corners"] = [[x + 20, y + 20] for x, y in self.corners]
        s = Surface(**d)
        if s.mesh is not None:
            s.mesh.translate(20, 20)
        return s


@dataclass
class Clip:
    """One piece of media on one surface for a stretch of the cue's timeline."""
    id: str = field(default_factory=_new_id)
    surface_id: str = ""
    media: str | None = None  # file path or gen: string; None = a deliberate dark stretch
    start: float = 0.0  # seconds into the cue
    duration: float = 5.0
    fade_in: float = 0.0  # seconds, inside the clip
    fade_out: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.duration

    def covers(self, t: float) -> bool:
        return self.start <= t < self.end


DEFAULT_CUE_LENGTH = 10.0


@dataclass
class Cue:
    """A sequence: tracks are surfaces, clips sit on them along a time axis. Fired as one
    unit (GO, a pad), it runs from 0 and loops unless it holds then advances."""
    id: str = field(default_factory=_new_id)
    name: str = "Cue"
    clips: list[Clip] = field(default_factory=list)
    length: float | None = None  # seconds; None = hold, else the last clip's end, else DEFAULT_CUE_LENGTH
    loop: bool = True  # run the sequence again when it reaches its length (unless hold advances first)
    fade: float = 1.0  # seconds the previous cue's layers take to fade out when this one starts
    hold: float | None = None  # auto-advance after N seconds; None waits for GO
    audio: str | None = None  # soundtrack file; same file as the previous cue keeps playing
    audio_gain: float = 1.0
    audio_loop: bool = True
    audio_fade: float | None = None  # None -> use `fade`

    # -- timeline queries ---------------------------------------------------------------
    @property
    def total(self) -> float:
        if self.length is not None:
            return max(0.1, self.length)
        if self.hold is not None:
            return max(0.1, self.hold)
        return max((c.end for c in self.clips), default=DEFAULT_CUE_LENGTH)

    def track(self, sid: str) -> list[Clip]:
        return sorted((c for c in self.clips if c.surface_id == sid), key=lambda c: c.start)

    def clip_at(self, sid: str, t: float) -> Clip | None:
        """The clip a surface plays at cue time t: when clips overlap, the one that started last."""
        hits = [c for c in self.clips if c.surface_id == sid and c.covers(t)]
        return max(hits, key=lambda c: c.start) if hits else None

    def media_at(self, sid: str, t: float) -> str | None:
        c = self.clip_at(sid, t)
        return c.media if c else None

    def clip_by_id(self, cid: str) -> Clip | None:
        return next((c for c in self.clips if c.id == cid), None)

    def assignments(self, t: float = 0.0) -> dict[str, str | None]:
        """surface id -> media at time t, for every surface that has a clip in this cue."""
        return {sid: self.media_at(sid, t) for sid in {c.surface_id for c in self.clips}}

    def set_track(self, sid: str, media: str | None, fade_in: float | None = None):
        """Make the surface play `media` for the whole cue (one full-length clip)."""
        old = self.track(sid)
        fi = fade_in if fade_in is not None else (old[0].fade_in if old else self.fade)
        self.clips = [c for c in self.clips if c.surface_id != sid]
        if media is not None:
            self.clips.append(Clip(surface_id=sid, media=media, start=0.0, duration=self.total, fade_in=fi))

    def set_assignments(self, assign: dict[str, str | None]):
        for sid, media in assign.items():
            self.set_track(sid, media)

    @classmethod
    def from_assignments(cls, name: str, assign: dict[str, str | None], **kw) -> "Cue":
        cue = cls(name=name, **kw)
        cue.set_assignments(assign)
        return cue

    # -- serialisation --------------------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        d["clips"] = [asdict(c) for c in self.clips]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Cue":
        d = dict(d)
        legacy = d.pop("media", None)  # files from before clips: one media per surface for the whole cue
        clips = [Clip(**c) for c in d.pop("clips", [])]
        cue = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        cue.clips = clips
        if legacy and not clips:
            cue.set_assignments(legacy)
        return cue


@dataclass
class Project:
    name: str = "Untitled"
    output_width: int = 1920
    output_height: int = 1080
    output_screen: str | None = None  # QScreen name, e.g. "HDMI-1"
    surfaces: list[Surface] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    scene: Scene = field(default_factory=Scene)
    camera: dict | None = None  # projector<->camera calibration (promap.camera.graycode.Calibration.to_dict)
    path: Path | None = None  # where it was loaded from / saved to (not serialised)

    @property
    def dense_map_path(self) -> Path | None:
        """Where the calibration's per-pixel map lives, next to the project file."""
        return self.path.with_suffix(".calib.npz") if self.path else None

    # -- lookups -------------------------------------------------------------
    def surface_by_id(self, sid: str) -> Surface | None:
        return next((s for s in self.surfaces if s.id == sid), None)

    def cue_by_id(self, cid: str) -> Cue | None:
        return next((c for c in self.cues if c.id == cid), None)

    @property
    def output_size(self) -> tuple[int, int]:
        return self.output_width, self.output_height

    # -- media paths ---------------------------------------------------------
    @property
    def base_dir(self) -> Path:
        return self.path.parent if self.path else Path.cwd()

    def resolve_media(self, media: str | None):
        if not media:
            return None
        if media.startswith("gen:"):
            return media  # a scripted animation, not a file
        p = Path(media).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    def relativize_media(self, path: str | Path) -> str:
        p = Path(path).expanduser().resolve()
        try:
            return str(p.relative_to(self.base_dir.resolve()))
        except ValueError:
            return str(p)

    # -- serialisation -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "format": FORMAT_VERSION,
            "name": self.name,
            "output": {
                "width": self.output_width,
                "height": self.output_height,
                "screen": self.output_screen,
            },
            "surfaces": [asdict(s) for s in self.surfaces],
            "cues": [c.to_dict() for c in self.cues],
            "scene": self.scene.to_dict(),
            "camera": self.camera,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        out = d.get("output", {})
        return cls(
            name=d.get("name", "Untitled"),
            output_width=int(out.get("width", 1920)),
            output_height=int(out.get("height", 1080)),
            output_screen=out.get("screen"),
            surfaces=[Surface(**s) for s in d.get("surfaces", [])],
            cues=[Cue.from_dict(c) for c in d.get("cues", [])],
            scene=Scene.from_dict(d.get("scene")),
            camera=d.get("camera"),
        )

    def save(self, path: str | Path | None = None) -> Path:
        if path is not None:
            self.path = Path(path)
        if self.path is None:
            raise ValueError("no path to save to")
        self.path.write_text(json.dumps(self.to_dict(), indent=2))
        return self.path

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        p = Path(path)
        proj = cls.from_dict(json.loads(p.read_text()))
        proj.path = p
        return proj
