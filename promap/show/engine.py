"""Show engine: turns the project + the current cue into a list of things to draw.

Two modes:
  * preview (cue_index == -1): each surface shows its own `media` field. This is
    what you look at while aligning corners in the editor.
  * cue (cue_index >= 0): the cue is a sequence. A cue clock runs from 0 when the cue
    is fired (and can be paused, scrubbed and looped); at every tick each surface
    plays whichever clip covers the clock, with the clip's own fade-in / fade-out.
    Firing a cue fades the previous cue's layers out over the new cue's `fade`.

A Layer is one piece of media on one surface with its own clock. During a
crossfade a surface briefly has two layers, one fading out and one fading in.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from promap.media.sources import FrameSource, SourceCache, Frame, VIDEO_EXTS

from promap.model.project import Project, Surface, Cue

log = logging.getLogger(__name__)


@dataclass
class Layer:
    surface_id: str
    media: str | None  # None => surface is dark (used for masks / blackout)
    source: FrameSource | None
    started: float
    fade_in: float = 0.0
    fade_out_at: float | None = None
    fade_out: float = 0.0
    id: str = field(default_factory=lambda: f"L{int(time.monotonic()*1e6):x}")
    voice: object = None  # audio Voice when the layer plays its video's sound
    clip_key: tuple | None = None  # (clip id, loop cycle) when the layer plays a cue clip
    end_at: float | None = None  # wall time the clip ends; its fade_out runs up to it
    end_fade: float = 0.0

    def opacity(self, now: float) -> float:
        o = 1.0
        if self.fade_in > 0:
            o = min(1.0, (now - self.started) / self.fade_in)
        if self.end_at is not None and self.end_fade > 0:
            o = min(o, max(0.0, (self.end_at - now) / self.end_fade))
        if self.fade_out_at is not None:
            if self.fade_out <= 0:
                return 0.0
            o = min(o, max(0.0, 1.0 - (now - self.fade_out_at) / self.fade_out))
        return max(0.0, o)

    def dead(self, now: float) -> bool:
        if self.fade_out_at is not None and now >= self.fade_out_at + self.fade_out:
            return True
        return self.end_at is not None and now >= self.end_at

    def shift(self, dt: float):
        """Move the layer's clocks by dt (the engine was paused that long)."""
        self.started += dt
        if self.fade_out_at is not None:
            self.fade_out_at += dt
        if self.end_at is not None:
            self.end_at += dt

    def frame(self, now: float, surface: Surface) -> tuple[Frame, object] | None:
        if self.source is None:
            return None
        src = self.source
        if src.is_still:
            return src.frame_at(0.0)
        t = (now - self.started) * max(0.01, surface.speed)
        if surface.loop:
            t = t % src.duration
        else:
            t = min(t, src.duration - 1e-3)
        return src.frame_at(t)


@dataclass
class RenderItem:
    layer_id: str
    surface: Surface
    frame: Frame | None  # None => solid black (mask or blackout)
    frame_key: object
    opacity: float
    corners: list | None = None  # defaults to the surface's corners; reactive scale overrides
    gain: float = 1.0  # brightness multiplier
    mesh: object = None  # Mesh when the surface is a triangle mesh (reactive scale overrides)

    def __post_init__(self):
        if self.corners is None:
            self.corners = self.surface.corners
        if self.mesh is None:
            self.mesh = self.surface.mesh


class ShowEngine:
    def __init__(self, project: Project, cache: SourceCache | None = None, audio=None):
        self.project = project
        self.cache = cache or SourceCache()
        self.audio = audio  # promap.audio.engine.AudioEngine or None
        self._cue_voice = None
        self._cue_audio_path: str | None = None
        self.layers: list[Layer] = []
        self.cue_index: int = -1
        self.blackout: bool = False
        self._cue_entered_at: float | None = None  # wall time of cue clock zero
        self.paused: bool = False
        self._paused_t: float = 0.0  # cue time while paused
        self._load_errors: dict[str, str] = {}
        self.preview_mode()

    # -- mode switching ------------------------------------------------------
    def set_project(self, project: Project):
        self.project = project
        if self.audio is not None:
            self.audio.stop_all()
        self._cue_voice, self._cue_audio_path = None, None
        self.layers.clear()
        self.cache.clear()
        self._load_errors.clear()
        self.cue_index = -1
        self.preview_mode()

    def preview_mode(self):
        self.cue_index = -1
        self._cue_entered_at = None
        self.paused = False
        self._apply({s.id: s.media for s in self.project.surfaces}, fade=0.0)
        self._set_cue_audio(None, 0.0, 1.0, True)

    @property
    def current_cue(self) -> Cue | None:
        if 0 <= self.cue_index < len(self.project.cues):
            return self.project.cues[self.cue_index]
        return None

    def goto(self, index: int, at: float = 0.0, paused: bool = False):
        """Fire a cue: its clock starts at `at` seconds; the previous cue's layers fade out over
        the new cue's `fade`."""
        cues = self.project.cues
        if not cues:
            return
        index = max(0, min(index, len(cues) - 1))
        cue = cues[index]
        now = time.monotonic()
        self.cue_index = index
        self.paused = paused
        self._paused_t = at
        self._cue_entered_at = now - at
        for l in self.layers:
            if l.fade_out_at is None:
                surf = self.project.surface_by_id(l.surface_id)
                if surf is not None and surf.kind == "reference":
                    continue
                l.fade_out_at, l.fade_out = now, cue.fade
                if l.voice is not None:
                    self.audio.stop(l.voice, cue.fade)
        self._sync_clips(now)
        fade = cue.fade if cue.audio_fade is None else cue.audio_fade
        self._set_cue_audio(cue.audio, fade, cue.audio_gain, cue.audio_loop)

    # -- cue clock ---------------------------------------------------------------
    def cue_time(self, now: float | None = None) -> float:
        """Seconds into the current cue's sequence (wrapped when the cue loops)."""
        cue = self.current_cue
        if cue is None or self._cue_entered_at is None:
            return 0.0
        if self.paused:
            return self._paused_t
        now = time.monotonic() if now is None else now
        t = now - self._cue_entered_at
        total = cue.total
        if cue.loop and cue.hold is None:
            if t >= total:
                cycles = int(t // total)
                self._cue_entered_at += cycles * total  # keep the clock small; layers carry their cycle
                t -= cycles * total
        else:
            t = min(t, total)  # a one-shot sequence parks at its end
        return t

    def _cycle(self, now: float) -> int:
        """Which pass through a looping cue a layer belongs to (so a loop restarts its clips)."""
        cue = self.current_cue
        if cue is None or self._cue_entered_at is None or not (cue.loop and cue.hold is None):
            return 0
        return int((now - self._cue_entered_at) // cue.total) if not self.paused else 0

    def seek(self, t: float):
        """Move the cue clock (scrubbing). Clips are re-evaluated with no fades."""
        cue = self.current_cue
        if cue is None:
            return
        now = time.monotonic()
        t = max(0.0, min(float(t), max(0.0, cue.total - 1e-3)))
        self._paused_t = t
        self._cue_entered_at = now - t
        for l in self.layers:
            if l.clip_key is not None and l.fade_out_at is None:
                l.fade_out_at, l.fade_out = now, 0.0
                if l.voice is not None:
                    self.audio.stop(l.voice, 0.0)
        self._sync_clips(now, restart=True)

    def pause(self):
        if self.current_cue is None or self.paused:
            return
        now = time.monotonic()
        self._paused_t = self.cue_time(now)
        self.paused = True
        if self.audio is not None and self._cue_voice is not None:
            self.audio.stop(self._cue_voice, 0.0)
            self._cue_voice, self._cue_audio_path = None, None

    def play(self):
        cue = self.current_cue
        if cue is None:
            if self.project.cues:
                self.goto(0)
            return
        if not self.paused:
            return
        now = time.monotonic()
        dt = now - (self._cue_entered_at + self._paused_t)
        for l in self.layers:
            l.shift(dt)
        self._cue_entered_at = now - self._paused_t
        self.paused = False
        self._set_cue_audio(cue.audio, 0.0, cue.audio_gain, cue.audio_loop)

    def toggle_play(self):
        if self.paused or self.current_cue is None:
            self.play()
        else:
            self.pause()

    def _sync_clips(self, now: float, restart: bool = False):
        """Make the live layers match the clips under the cue clock."""
        cue = self.current_cue
        if cue is None:
            return
        t = self.cue_time(now)
        cycle = self._cycle(now)
        live = {l.surface_id: l for l in self.layers if l.fade_out_at is None and l.clip_key is not None}
        for s in self.project.surfaces:
            if s.kind != "media":
                continue
            clip = cue.clip_at(s.id, t)
            cur = live.get(s.id)
            key = (clip.id, cycle) if clip is not None else None
            if cur is not None and cur.clip_key == key:
                continue
            if cur is not None:
                cur.fade_out_at = now
                cur.fade_out = 0.0 if restart else (0.0 if clip is not None else cur.end_fade)
                if cur.voice is not None:
                    self.audio.stop(cur.voice, cur.fade_out)
            if clip is None or clip.media is None:
                continue
            started = now - (t - clip.start)
            layer = Layer(surface_id=s.id, media=clip.media, source=self._open(clip.media), started=started,
                          fade_in=clip.fade_in, clip_key=key, end_at=started + clip.duration, end_fade=clip.fade_out)
            if (self.audio is not None and s.video_audio and self._is_video(clip.media) and layer.source is not None):
                layer.voice = self.audio.play(self.project.resolve_media(clip.media), loop=s.loop, fade_in=clip.fade_in)
            self.layers.append(layer)

    def _set_cue_audio(self, path: str | None, fade: float, gain: float, loop: bool):
        if self.audio is None:
            return
        cur = self._cue_voice
        if path and path == self._cue_audio_path and cur is not None and not cur.done:
            cur.gain = gain
            return
        if cur is not None:
            self.audio.stop(cur, fade)
            self._cue_voice = None
        self._cue_audio_path = path
        if path:
            self._cue_voice = self.audio.play(self.project.resolve_media(path), gain=gain, loop=loop, fade_in=fade)

    def go(self):
        if self.cue_index + 1 < len(self.project.cues):
            self.goto(self.cue_index + 1)

    def back(self):
        if self.cue_index > 0:
            self.goto(self.cue_index - 1)

    def refresh(self):
        """Preview mode: re-read surface.media. Cue mode: re-evaluate the clips (after an edit)."""
        if self.cue_index == -1:
            self.preview_mode()
        else:
            now = time.monotonic()
            for l in self.layers:  # a clip under the playhead may have changed media or timing
                if l.clip_key is not None and l.fade_out_at is None:
                    cue = self.current_cue
                    clip = cue.clip_by_id(l.clip_key[0])
                    if clip is None or clip.media != l.media or abs((l.end_at - l.started) - clip.duration) > 1e-6 \
                            or clip.fade_out != l.end_fade or not clip.covers(self.cue_time(now)):
                        l.fade_out_at, l.fade_out = now, 0.0
            self._sync_clips(now)

    # -- layer bookkeeping ---------------------------------------------------
    def _open(self, media: str | None) -> FrameSource | None:
        if not media:
            return None
        path = self.project.resolve_media(media)
        try:
            src = self.cache.open(path)
            self._load_errors.pop(media, None)
            return src
        except Exception as exc:  # unreadable file: surface goes dark, UI shows why
            if self._load_errors.get(media) != str(exc):
                log.warning("cannot open %s: %s", path, exc)
            self._load_errors[media] = str(exc)
            return None

    def _apply(self, assign: dict[str, str | None], fade: float):
        now = time.monotonic()
        assign = dict(assign)
        for s in self.project.surfaces:
            if s.kind == "reference":
                assign[s.id] = s.media  # cue-independent
        live = {l.surface_id: l for l in self.layers if l.fade_out_at is None}
        for sid, media in assign.items():
            cur = live.get(sid)
            if cur is not None and cur.media == media:
                continue
            if cur is not None:
                cur.fade_out_at = now
                cur.fade_out = fade
                if cur.voice is not None:
                    self.audio.stop(cur.voice, fade)
            layer = Layer(surface_id=sid, media=media, source=self._open(media), started=now, fade_in=fade)
            surf = self.project.surface_by_id(sid)
            if (media and self.audio is not None and surf is not None and surf.video_audio
                    and self._is_video(media) and layer.source is not None):
                layer.voice = self.audio.play(self.project.resolve_media(media), loop=surf.loop, fade_in=fade)
            self.layers.append(layer)
        wanted = set(assign)
        for l in self.layers:
            if l.surface_id not in wanted and l.fade_out_at is None:
                l.fade_out_at, l.fade_out = now, 0.0
                if l.voice is not None:
                    self.audio.stop(l.voice, 0.0)

    @property
    def load_errors(self) -> dict[str, str]:
        return dict(self._load_errors)

    # -- per-frame -----------------------------------------------------------
    def tick(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        dead = [l for l in self.layers if l.dead(now)]
        for l in dead:
            self.layers.remove(l)
            if l.voice is not None and self.audio is not None:
                self.audio.stop(l.voice, 0.0)
            if l.source is not None and l.media and self._is_video(l.media):
                l.source.close()
        cue = self.current_cue
        if cue is not None:
            if cue.hold is not None and not self.paused and self._cue_entered_at is not None \
                    and now - self._cue_entered_at >= cue.total and self.cue_index + 1 < len(self.project.cues):
                self.go()
                return
            self._sync_clips(now)

    @staticmethod
    def _is_video(media: str) -> bool:
        from pathlib import Path

        if media.startswith("gen:"):
            return False
        return Path(media).suffix.lower() in VIDEO_EXTS

    def _react(self, s: Surface, op: float):
        """Apply the surface's audio-reactive mapping; returns (opacity, corners, gain, mesh)."""
        r = s.react
        if not r or self.audio is None:
            return op, s.corners, 1.0, s.mesh
        lvl = self.audio.analysis.level(r.get("band", "rms"))
        amt = float(r.get("amount", 0.5))
        param = r.get("param", "opacity")
        if param == "opacity":
            return op * ((1.0 - amt) + amt * lvl), s.corners, 1.0, s.mesh
        if param == "scale":
            f = 1.0 + 0.5 * amt * lvl
            cx, cy = s.centroid()
            if s.mesh is not None:
                return op, s.corners, 1.0, s.mesh.scaled_about(cx, cy, f)
            return op, [[cx + (x - cx) * f, cy + (y - cy) * f] for x, y in s.corners], 1.0, None
        if param == "gain":
            return op, s.corners, 1.0 + amt * lvl, s.mesh
        return op, s.corners, 1.0, s.mesh

    def render_items(self, now: float | None = None) -> list[RenderItem]:
        now = time.monotonic() if now is None else now
        if self.blackout:
            return []
        by_surface: dict[str, list[Layer]] = {}
        for l in self.layers:
            by_surface.setdefault(l.surface_id, []).append(l)
        items: list[RenderItem] = []
        for s in self.project.surfaces:  # list order == z order, first drawn first
            if not s.visible:
                continue
            if s.kind == "mask":
                items.append(RenderItem(f"mask:{s.id}", s, None, 0, s.opacity))
                continue
            for l in by_surface.get(s.id, []):
                op = l.opacity(now) * s.opacity
                if op <= 0.0:
                    continue
                fr = l.frame(now, s)
                if fr is None:
                    continue
                frame, key = fr
                op, corners, gain, mesh = self._react(s, op)
                items.append(RenderItem(l.id, s, frame, key, op, corners=corners, gain=gain, mesh=mesh))
        return items
