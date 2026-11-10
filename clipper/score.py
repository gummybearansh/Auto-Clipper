"""Step 3+5: compute per-second signals, normalize (robust z), smooth, and combine with weights."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import Config
from .download import Source
from .signals import audio as audio_sig
from .signals import visual as visual_sig
from .signals.chat import chat_signals
from .signals.common import robust_z, smooth
from .signals.heatmap import heatmap_signal
from .signals.transcript import transcript_signals
from .utils import log


@dataclass
class Signals:
    n: int
    z: dict[str, np.ndarray]                 # normalized + smoothed, per signal name
    combined: np.ndarray
    weights: dict[str, float]                # effective (renormalized) weights
    raw: dict[str, np.ndarray] = field(default_factory=dict)

    def breakdown(self, start: float, end: float) -> dict[str, float]:
        a, b = int(start), max(int(start) + 1, int(np.ceil(end)))
        return {k: round(float(self.weights[k] * self.z[k][a:b].mean()), 3) for k in self.weights}


def base_signals(src: Source, cfg: Config, chat: list[tuple[float, str]]) -> dict[str, np.ndarray]:
    """Expensive, transcript-independent raw signals. Cached in work/signals.npz."""
    n = int(np.ceil(src.duration)) or 1
    cache = src.work_dir / "signals.npz"
    key = f"{n}|{cfg.signals.chat_offset}|{cfg.signals.baseline_window}|{len(chat)}|{len(src.heatmap)}"
    if cache.exists():
        d = np.load(cache, allow_pickle=False)
        if str(d.get("_key", "")) == key:
            return {k: d[k] for k in d.files if k != "_key"}
    raw: dict[str, np.ndarray] = {}
    if src.has_audio:
        log("audio signals (loudness, flux, yelling)")
        raw |= audio_sig.analyze(src.audio_path, n, cfg.signals.baseline_window)
    log("visual activity (frame diff)")
    raw |= visual_sig.analyze(src.video_path, n, cfg.signals.visual_fps, src.duration)
    if chat:
        log(f"chat signals ({len(chat)} messages, offset {cfg.signals.chat_offset}s)")
        raw |= chat_signals(chat, n, cfg.signals.chat_offset, cfg.signals.baseline_window)
    raw |= heatmap_signal(src.heatmap, n)
    np.savez_compressed(cache, _key=np.array(key), **raw)
    return raw


def combine(raw: dict[str, np.ndarray], words: list[dict], covered: list[list[float]],
            platform: str, cfg: Config) -> Signals:
    n = len(next(iter(raw.values()))) if raw else 1
    sig = cfg.signals.smooth_sigma
    z: dict[str, np.ndarray] = {}

    if "audio_jump" in raw:
        a = 0.5 * robust_z(raw["audio_jump"]) + 0.3 * robust_z(raw["audio_flux"]) + 0.2 * robust_z(raw["audio_yell"])
        z["audio"] = smooth(a, sig)
    if "visual" in raw:
        z["visual"] = smooth(robust_z(raw["visual"]), sig)
    for k in ("chat", "emote", "clip_mentions"):
        if k in raw and raw[k].any():
            z[k] = smooth(robust_z(raw[k]), sig)
    if "heatmap" in raw:
        z["heatmap"] = smooth(robust_z(raw["heatmap"]), sig)

    mask = np.zeros(n, dtype=bool)
    for a, b in covered:
        mask[int(a): int(np.ceil(b))] = True
    ts = transcript_signals(words, n, mask)
    if ts:
        z["speech"] = smooth(robust_z(smooth(ts["speech"], 1.5), mask), sig)
        z["keywords"] = smooth(robust_z(ts["keywords"], mask, lo=0), sig)

    profile = cfg.weights.twitch if platform == "twitch" else cfg.weights.youtube
    w = {k: v for k, v in profile.items() if k in z and v > 0}
    if not w:  # e.g. silent local file with nothing else: use whatever we have equally
        w = {k: 1.0 for k in z}
    tot = sum(w.values()) or 1.0
    w = {k: v / tot for k, v in w.items()}
    combined = np.zeros(n)
    for k, v in w.items():
        combined += v * z[k]
    # Dead air: no streamer speech for a while (starting/BRB screens, AFK, cutscenes). Chat can still be
    # hyped there, but those make bad clips → damp positive scores where the streamer isn't talking.
    if ts and mask.any():
        presence = smooth(ts["speech"], 8.0)                  # words/sec, ~20s neighbourhood
        factor = 0.25 + 0.75 * np.clip(presence / 0.6, 0, 1)
        factor = np.where(mask, factor, 1.0)
        combined = np.where(combined > 0, combined * factor, combined)
    return Signals(n=n, z=z, combined=combined, weights=w, raw=raw)
