import time
import wave

import numpy as np
import pytest

from promap.audio.engine import AudioEngine, AudioAnalysis, BLOCK, RATE
from promap.model.project import Project, Surface, Cue
from promap.show.engine import ShowEngine


def sine_wav(path, freq=60.0, seconds=1.0, rate=48000, amp=0.5):
    t = np.arange(int(seconds * rate)) / rate
    pcm = (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


def wait_decoded(voice, timeout=3.0):
    t0 = time.monotonic()
    while voice._chunks.qsize() < 4 and not voice._ended and time.monotonic() - t0 < timeout:
        time.sleep(0.01)


def test_offline_voice_plays_and_ends(tmp_path):
    wav = sine_wav(tmp_path / "a.wav", seconds=0.25)
    eng = AudioEngine(device=None)
    v = eng.play(wav, loop=False)
    wait_decoded(v)
    blocks = []
    for _ in range(40):
        blocks.append(eng.render(BLOCK))
        if v.done:
            break
        time.sleep(0.002)
    energy = [float(np.abs(b).mean()) for b in blocks]
    assert energy[0] > 0.05, "first block should carry the sine"
    assert v.done and v not in eng.voices
    assert float(np.abs(eng.render(BLOCK)).mean()) == 0.0, "silence after the file ends"


def test_fade_in_and_out(tmp_path):
    wav = sine_wav(tmp_path / "a.wav", seconds=2.0)
    eng = AudioEngine(device=None)
    v = eng.play(wav, loop=True, fade_in=0.5)
    wait_decoded(v)
    first = float(np.abs(eng.render(BLOCK)).mean())
    for _ in range(int(0.6 * RATE / BLOCK)):
        last = float(np.abs(eng.render(BLOCK)).mean())
        time.sleep(0.001)
    assert first < last * 0.2, "fade-in starts quiet"
    eng.stop(v, fade=0.1)
    for _ in range(int(0.2 * RATE / BLOCK) + 2):
        tail = float(np.abs(eng.render(BLOCK)).mean())
    assert tail == 0.0 and v.done


def test_analysis_bands():
    an = AudioAnalysis()
    t = np.arange(BLOCK * 20) / RATE
    bass = np.sin(2 * np.pi * 60 * t).astype(np.float32)
    for i in range(20):
        blk = bass[i * BLOCK:(i + 1) * BLOCK]
        an.update(np.stack([blk, blk], axis=1))
    assert an.level("bass") > 0.8 and an.level("high") < 0.2


def test_cue_audio_follows_cues(tmp_path):
    a = sine_wav(tmp_path / "a.wav", seconds=3.0)
    b = sine_wav(tmp_path / "b.wav", freq=440, seconds=3.0)
    p = Project(path=tmp_path / "s.promap")
    s = Surface(name="S")
    p.surfaces.append(s)
    p.cues += [
        Cue.from_assignments("1", {s.id: None}, audio="a.wav", fade=0.0),
        Cue.from_assignments("2", {s.id: None}, audio="a.wav", audio_gain=0.5, fade=0.0),
        Cue.from_assignments("3", {s.id: None}, audio="b.wav", fade=0.0),
        Cue.from_assignments("4", {s.id: None}, audio=None, fade=0.0),
    ]
    audio = AudioEngine(device=None)
    eng = ShowEngine(p, audio=audio)
    eng.goto(0)
    v1 = eng._cue_voice
    assert v1 is not None and v1.path == a
    eng.goto(1)
    assert eng._cue_voice is v1 and v1.gain == 0.5, "same file keeps playing, gain updates"
    eng.goto(2)
    assert eng._cue_voice is not v1 and eng._cue_voice.path == b
    eng.goto(3)
    assert eng._cue_voice is None
    audio.render(BLOCK)
    assert all(v.path != a for v in audio.voices)


def test_react_scales_corners(tmp_path):
    from PIL import Image

    Image.new("RGB", (4, 4), (255, 255, 255)).save(tmp_path / "w.png")
    p = Project(path=tmp_path / "s.promap")
    s = Surface(name="S", media="w.png", corners=[[0, 0], [100, 0], [100, 100], [0, 100]],
                react={"param": "scale", "band": "bass", "amount": 1.0})
    p.surfaces.append(s)
    audio = AudioEngine(device=None)
    audio.analysis.smooth["bass"] = 1.0
    eng = ShowEngine(p, audio=audio)
    item = eng.render_items()[0]
    assert item.corners[0] == pytest.approx([-25.0, -25.0]) and item.corners[2] == pytest.approx([125.0, 125.0])
    q = Project.load(p.save())
    assert q.surfaces[0].react["band"] == "bass" and q.cues == []
