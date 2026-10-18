"""Audio: a small mixer of file-backed voices, output through PortAudio, with level analysis.

Voices are decoded by PyAV on their own thread (any audio file, or the audio track of a
video), resampled to the engine rate, and pulled by the output callback. Fades are linear
ramps evaluated per block. `AudioEngine(device=None)` is an offline engine: no stream, you
call `render(n)` yourself (tests, or an unattended headless run).
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

RATE = 48000
CHANNELS = 2
BLOCK = 1024
BANDS = {"bass": (20.0, 150.0), "mid": (150.0, 2000.0), "high": (2000.0, 16000.0)}


class _Decoder(threading.Thread):
    """Decode a file into float32 stereo chunks at RATE; loops or sends None at EOF."""

    def __init__(self, path: Path, loop: bool, chunks: queue.Queue, rate: int = RATE):
        super().__init__(daemon=True, name=f"decode:{path.name}")
        self.path, self.loop, self.chunks, self.rate = path, loop, chunks, rate
        self.stop_flag = threading.Event()
        self.error: str | None = None

    def run(self):
        import av

        try:
            while not self.stop_flag.is_set():
                with av.open(str(self.path)) as container:
                    stream = container.streams.audio[0]
                    resampler = av.AudioResampler(format="flt", layout="stereo", rate=self.rate)
                    for frame in container.decode(stream):
                        for rf in resampler.resample(frame):
                            data = rf.to_ndarray().reshape(-1, CHANNELS).astype(np.float32, copy=False)
                            self._put(data)
                        if self.stop_flag.is_set():
                            return
                    for rf in resampler.resample(None):  # flush
                        self._put(rf.to_ndarray().reshape(-1, CHANNELS).astype(np.float32, copy=False))
                if not self.loop:
                    break
        except Exception as exc:
            self.error = str(exc)
            log.warning("audio decode failed for %s: %s", self.path, exc)
        self._put(None)

    def _put(self, item):
        while not self.stop_flag.is_set():
            try:
                self.chunks.put(item, timeout=0.1)
                return
            except queue.Full:
                continue


@dataclass
class Voice:
    path: Path
    gain: float = 1.0
    loop: bool = True
    fade_in: float = 0.0
    fade_out: float = 0.0
    id: int = 0
    _chunks: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=96), repr=False)
    _decoder: _Decoder | None = field(default=None, repr=False)
    _buf: np.ndarray | None = field(default=None, repr=False)
    _pos: int = 0
    _played: int = 0  # samples emitted
    _fade_out_from: int | None = None  # sample index at which fade-out began
    _ended: bool = False

    def start(self, rate: int):
        self._decoder = _Decoder(self.path, self.loop, self._chunks, rate)
        self._decoder.start()

    def stop(self):
        if self._decoder:
            self._decoder.stop_flag.set()

    @property
    def done(self) -> bool:
        return self._ended

    def pull(self, n: int, rate: int) -> np.ndarray:
        """Next n stereo samples with gain and fades applied."""
        out = np.zeros((n, CHANNELS), dtype=np.float32)
        filled = 0
        while filled < n and not self._ended:
            if self._buf is None or self._pos >= len(self._buf):
                try:
                    chunk = self._chunks.get_nowait()
                except queue.Empty:
                    break  # underrun: pad with silence rather than block the callback
                if chunk is None:
                    self._ended = True
                    break
                self._buf, self._pos = chunk, 0
            take = min(n - filled, len(self._buf) - self._pos)
            out[filled:filled + take] = self._buf[self._pos:self._pos + take]
            self._pos += take
            filled += take
        env = self._envelope(n, rate)
        self._played += n
        if self._fade_out_from is not None and self._played >= self._fade_out_from + int(self.fade_out * rate):
            self._ended = True
        return out * env[:, None] * self.gain

    def _envelope(self, n: int, rate: int) -> np.ndarray:
        idx = np.arange(self._played, self._played + n, dtype=np.float64)
        env = np.ones(n, dtype=np.float32)
        if self.fade_in > 0:
            env *= np.clip(idx / (self.fade_in * rate), 0.0, 1.0).astype(np.float32)
        if self._fade_out_from is not None:
            length = max(1.0, self.fade_out * rate)
            env *= np.clip(1.0 - (idx - self._fade_out_from) / length, 0.0, 1.0).astype(np.float32)
        return env

    def begin_fade_out(self, seconds: float):
        if self._fade_out_from is None:
            self.fade_out = max(0.0, seconds)
            self._fade_out_from = self._played


class AudioAnalysis:
    """Levels from the last output block: rms and per-band energies, auto-normalised to 0..1."""

    def __init__(self, rate: int = RATE, attack: float = 0.5, release: float = 0.12):
        self.rate = rate
        self.attack, self.release = attack, release
        self.raw: dict[str, float] = {k: 0.0 for k in ("rms", *BANDS)}
        self.smooth: dict[str, float] = dict(self.raw)
        self.peak: dict[str, float] = {k: 1e-6 for k in self.raw}
        self._history: dict[str, list[float]] = {k: [] for k in self.raw}
        self.onset: dict[str, bool] = {k: False for k in self.raw}
        self.peak_all = 1e-6  # loudest band seen lately; bands under 5% of it read as silence
        self._window: np.ndarray | None = None
        self._freqs: np.ndarray | None = None

    def update(self, block: np.ndarray):
        n = len(block)
        if n == 0:
            return
        mono = block.mean(axis=1)
        if self._window is None or len(self._window) != n:
            self._window = np.hanning(n).astype(np.float32)
            self._freqs = np.fft.rfftfreq(n, 1.0 / self.rate)
        spec = np.abs(np.fft.rfft(mono * self._window))
        vals = {"rms": float(np.sqrt(np.mean(mono ** 2)))}
        for name, (lo, hi) in BANDS.items():
            mask = (self._freqs >= lo) & (self._freqs < hi)
            vals[name] = float(spec[mask].mean()) if mask.any() else 0.0
        self.peak_all = max(max(vals[b] for b in BANDS), self.peak_all * 0.999)
        for k, v in vals.items():
            self.raw[k] = v
            floor = 0.05 * self.peak_all if k in BANDS else 1e-6
            self.peak[k] = max(v, self.peak[k] * 0.999, floor)  # slow decay so quiet passages re-scale
            target = v / self.peak[k] if self.peak[k] > 1e-6 else 0.0
            coef = self.attack if target > self.smooth[k] else self.release
            self.smooth[k] += (target - self.smooth[k]) * coef
            hist = self._history[k]
            hist.append(v)
            if len(hist) > 40:
                hist.pop(0)
            avg = sum(hist) / len(hist)
            self.onset[k] = len(hist) > 8 and v > 1.5 * avg and v > 1e-4

    def level(self, band: str = "rms") -> float:
        return float(min(1.0, max(0.0, self.smooth.get(band, 0.0))))


class AudioEngine:
    def __init__(self, device="default", rate: int = RATE, block: int = BLOCK):
        self.rate, self.block = rate, block
        self.voices: list[Voice] = []
        self.lock = threading.Lock()
        self.master = 1.0
        self.muted = False
        self.analysis = AudioAnalysis(rate)
        self.stream = None
        self.error: str | None = None
        self._next_id = 1
        if device is not None:
            try:
                import sounddevice as sd

                self.stream = sd.OutputStream(
                    samplerate=rate, channels=CHANNELS, dtype="float32", blocksize=block,
                    device=None if device == "default" else device, callback=self._callback,
                )
                self.stream.start()
            except Exception as exc:
                self.error = str(exc)
                self.stream = None
                log.warning("audio output unavailable: %s", exc)

    @property
    def available(self) -> bool:
        return self.stream is not None

    # -- control ----------------------------------------------------------------
    def play(self, path: str | Path, gain: float = 1.0, loop: bool = True, fade_in: float = 0.0) -> Voice:
        v = Voice(Path(path), gain=gain, loop=loop, fade_in=fade_in, id=self._next_id)
        self._next_id += 1
        v.start(self.rate)
        with self.lock:
            self.voices.append(v)
        return v

    def stop(self, voice: Voice | None, fade: float = 0.0):
        if voice is None:
            return
        if fade <= 0:
            with self.lock:
                if voice in self.voices:
                    self.voices.remove(voice)
            voice.stop()
        else:
            voice.begin_fade_out(fade)

    def stop_all(self, fade: float = 0.0):
        with self.lock:
            voices = list(self.voices)
        for v in voices:
            self.stop(v, fade)

    def close(self):
        self.stop_all()
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    # -- mixing ---------------------------------------------------------------------
    def render(self, n: int) -> np.ndarray:
        mix = np.zeros((n, CHANNELS), dtype=np.float32)
        with self.lock:
            voices = list(self.voices)
        done = []
        for v in voices:
            mix += v.pull(n, self.rate)
            if v.done:
                done.append(v)
        if done:
            with self.lock:
                for v in done:
                    if v in self.voices:
                        self.voices.remove(v)
                    v.stop()
        mix *= 0.0 if self.muted else self.master
        np.tanh(mix, out=mix)  # soft clip
        if voices:  # the FFT analysis only matters while something plays; silence costs nothing
            self.analysis.update(mix)
        return mix

    def _callback(self, outdata, frames, time_info, status):
        try:
            outdata[:] = self.render(frames)
        except Exception:
            log.exception("audio callback")
            outdata.fill(0)
