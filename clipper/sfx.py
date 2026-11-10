"""Step 8: sparse sound effects.

Rule: always one end sting; at most `max_mid_hits` (≤2) mid-clip hits on the top-scoring peaks;
≥ `min_spacing` seconds between hits; source audio ducked ~5 dB under each hit.

If assets/sfx/ is empty, a small default set is synthesized (boom, whoosh, stings). Drop your own
files in there named sting*.wav|mp3, boom*.wav|mp3, whoosh*.wav|mp3 to override.
"""
from __future__ import annotations

import hashlib
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .config import Config

SR = 48000
EXTS = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac")


@dataclass
class Hit:
    file: str
    t: float          # clip-relative start
    dur: float
    kind: str         # sting | boom | whoosh

    def to_json(self):
        return asdict(self)


# ------------------------------------------------------------------ synthesis
def _write(path: Path, x: np.ndarray) -> None:
    x = np.clip(x / (np.abs(x).max() + 1e-9) * 0.9, -1, 1)
    st = np.stack([x, x], axis=1)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((st * 32767).astype(np.int16).tobytes())


def _env(n, attack, decay):
    t = np.arange(n) / SR
    return np.minimum(1, t / max(attack, 1e-4)) * np.exp(-t / decay)


def _boom():
    n = int(SR * 1.6)
    t = np.arange(n) / SR
    f = 55 + 90 * np.exp(-t * 18)                     # pitch drop
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * _env(n, 0.003, 0.45)
    rng = np.random.default_rng(1)
    click = rng.standard_normal(n) * _env(n, 0.0005, 0.02) * 0.6
    return np.tanh(2.2 * (body + click))


def _whoosh():
    n = int(SR * 0.9)
    rng = np.random.default_rng(2)
    noise = rng.standard_normal(n)
    t = np.linspace(0, 1, n)
    env = np.sin(np.pi * t ** 0.7) ** 2
    # sweeping one-pole lowpass
    out = np.zeros(n)
    y = 0.0
    for i in range(n):
        a = 0.02 + 0.35 * env[i]
        y += a * (noise[i] - y)
        out[i] = y
    return out * env


def _sting(variant: int):
    n = int(SR * 1.4)
    t = np.arange(n) / SR
    notes = [[523.25, 659.25, 783.99, 1046.5], [392.0, 523.25, 659.25, 987.77], [440.0, 554.37, 659.25, 880.0]][variant % 3]
    out = np.zeros(n)
    for k, f in enumerate(notes):
        start = int(k * 0.07 * SR)
        m = n - start
        tt = t[:m]
        tone = (np.sin(2 * np.pi * f * tt) + 0.35 * np.sin(2 * np.pi * 2 * f * tt) + 0.12 * np.sin(2 * np.pi * 3 * f * tt))
        out[start:] += tone * _env(m, 0.004, 0.35 + 0.1 * k)
    return out + 0.6 * _boom()[:n] * 0.5


def ensure_assets(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if any(p.suffix.lower() in EXTS for p in d.iterdir()):
        return
    _write(d / "boom.wav", _boom())
    _write(d / "whoosh.wav", _whoosh())
    for i in range(3):
        _write(d / f"sting_{i + 1}.wav", _sting(i))


def _library(d: Path) -> dict[str, list[Path]]:
    lib: dict[str, list[Path]] = {"sting": [], "boom": [], "whoosh": []}
    for p in sorted(d.iterdir()):
        if p.suffix.lower() not in EXTS:
            continue
        for k in lib:
            if p.stem.lower().startswith(k):
                lib[k].append(p)
    return lib


def _duration(p: Path) -> float:
    if p.suffix.lower() == ".wav":
        with wave.open(str(p), "rb") as wf:
            return wf.getnframes() / wf.getframerate()
    try:  # non-wav: ask ffprobe
        import json
        import subprocess
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(p)],
                           capture_output=True, text=True)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 1.0


def plan_hits(clip_id: str, duration: float, peaks_rel: list[tuple[float, float]], peak_value: float,
              cfg: Config) -> list[Hit]:
    if not cfg.sfx.enabled:
        return []
    d = cfg.path(cfg.sfx.dir)
    ensure_assets(d)
    lib = _library(d)
    seed = int(hashlib.md5(clip_id.encode()).hexdigest(), 16)
    hits: list[Hit] = []
    if lib["sting"]:
        p = lib["sting"][seed % len(lib["sting"])]
        dur = _duration(p)
        hits.append(Hit(str(p), round(max(0.0, duration - dur), 3), round(dur, 3), "sting"))
    sting_t = hits[0].t if hits else duration
    chosen: list[float] = []
    # only major peaks: near the clip's best moment and clearly above baseline
    for t, v in sorted(peaks_rel, key=lambda x: -x[1]):
        if len(chosen) >= min(2, cfg.sfx.max_mid_hits):
            break
        if v < max(1.0, 0.8 * peak_value) or t < 2.0 or t > sting_t - cfg.sfx.min_spacing:
            continue
        if any(abs(t - c) < cfg.sfx.min_spacing for c in chosen):
            continue
        chosen.append(t)
    for i, t in enumerate(sorted(chosen)):
        kind = "boom" if i == 0 or not lib["whoosh"] else "whoosh"
        if not lib[kind]:
            continue
        p = lib[kind][(seed + i) % len(lib[kind])]
        dur = _duration(p)
        start = t - (dur * 0.7 if kind == "whoosh" else 0.05)   # whoosh swells *into* the peak
        hits.append(Hit(str(p), round(max(0.0, start), 3), round(dur, 3), kind))
    return sorted(hits, key=lambda h: h.t)
