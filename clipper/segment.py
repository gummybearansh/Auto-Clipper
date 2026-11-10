"""Step 4+5: peak finding → buildup/payoff window → snap edges to pauses / sentence ends → NMS → top N."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import find_peaks

from .config import Config
from .score import Signals

SENT_END = (".", "!", "?", "…")


@dataclass
class Candidate:
    peak: float
    start: float
    end: float
    score: float = 0.0
    peak_value: float = 0.0
    sub_peaks: list[tuple[float, float]] = field(default_factory=list)   # (t, value) inside window

    @property
    def dur(self) -> float:
        return self.end - self.start


def find_candidates(s: Signals, cfg: Config, k: int) -> list[float]:
    """Return up to k peak times (seconds), best first."""
    x = s.combined
    if len(x) < cfg.clip.min_len:
        return [float(np.argmax(x))] if len(x) else []
    peaks, props = find_peaks(x, distance=max(5, int(cfg.clip.min_len)), prominence=0.05)
    if len(peaks) == 0:
        peaks = np.array([int(np.argmax(x))])
    order = np.argsort(-x[peaks])
    return [float(p) for p in peaks[order][:k]]


def _boundaries(words: list[dict], min_gap: float) -> tuple[list[float], list[float]]:
    """Good start points (just before an utterance begins) and end points (just after one ends)."""
    starts, ends = [], []
    for i, w in enumerate(words):
        prev = words[i - 1] if i else None
        nxt = words[i + 1] if i + 1 < len(words) else None
        if prev is None or w["s"] - prev["e"] >= min_gap or prev["w"].endswith(SENT_END):
            gap_before = (w["s"] - prev["e"]) if prev else 1.0
            starts.append(w["s"] - min(0.25, max(0.0, gap_before / 2)))
        if nxt is None or nxt["s"] - w["e"] >= min_gap or w["w"].endswith(SENT_END):
            gap_after = (nxt["s"] - w["e"]) if nxt else 1.0
            ends.append(w["e"] + min(0.4, max(0.05, gap_after / 2)))
    return starts, ends


def _inside_word(t: float, words: list[dict]) -> dict | None:
    for w in words:
        if w["s"] < t < w["e"]:
            return w
        if w["s"] > t:
            break
    return None


def _snap(t: float, points: list[float], window: float, prefer: str) -> float | None:
    cands = [p for p in points if abs(p - t) <= window]
    if not cands:
        return None
    # prefer earlier starts (keep buildup) / later ends (keep payoff) when equally close
    bias = -0.15 if prefer == "earlier" else 0.15
    return min(cands, key=lambda p: abs(p - t) + (bias * np.sign(p - t)))


def _quiet_point(t: float, loud: np.ndarray | None, window: float) -> float:
    if loud is None or not len(loud):
        return t
    a, b = max(0, int(t - window)), min(len(loud), int(t + window) + 1)
    if b <= a:
        return t
    return float(a + int(np.argmin(loud[a:b])) + 0.5)


def make_window(peak: float, s: Signals, words: list[dict], cfg: Config, duration: float) -> Candidate:
    c = cfg.clip
    x = s.combined
    n = len(x)
    p = int(peak)
    pv = float(x[min(p, n - 1)])
    floor = float(np.median(x))
    thr = floor + 0.35 * (pv - floor)
    # walk back to where the moment starts building, forward to where it settles
    i = p
    while i > 0 and x[i - 1] >= thr and p - i < c.pre_peak[1]:
        i -= 1
    j = p
    while j < n - 1 and x[j + 1] >= thr and j - p < c.post_peak[1]:
        j += 1
    start = float(np.clip(i - 4, peak - c.pre_peak[1], peak - c.pre_peak[0]))
    end = float(np.clip(j + 2, peak + c.post_peak[0], peak + c.post_peak[1]))

    # meet min length: extend buildup first, then payoff
    if end - start < c.min_len:
        need = c.min_len - (end - start)
        start -= need * 0.7
        end += need * 0.3

    # snap to natural boundaries
    loud = s.raw.get("loud_db")
    near = [w for w in words if start - 10 <= w["s"] <= end + 10]
    st_pts, en_pts = _boundaries(near, c.min_gap)
    ss = _snap(start, st_pts, c.snap_window, "earlier")
    if ss is None:
        w = _inside_word(start, near)
        ss = (w["s"] - 0.1) if w else (_quiet_point(start, loud, 1.5) if not near else start)
    ee = _snap(end, en_pts, c.snap_window, "later")
    if ee is None:
        w = _inside_word(end, near)
        ee = (w["e"] + 0.2) if w else (_quiet_point(end, loud, 1.5) if not near else end)
    start, end = ss, ee

    # hard bounds
    if end - start > c.max_len:
        start = end - c.max_len
        w = _inside_word(start, near)
        if w:
            start = w["e"] + 0.05
    if end - start < c.min_len:
        end = start + c.min_len
    start = max(0.0, start)
    end = min(duration, end)
    if end - start < c.min_len:
        start = max(0.0, end - c.min_len)

    a, b = int(start), max(int(start) + 1, int(np.ceil(end)))
    seg = x[a:b]
    score = 0.55 * pv + 0.45 * float(np.mean(np.sort(seg)[-max(1, len(seg) // 3):]))
    sp, _ = find_peaks(seg, distance=5, prominence=0.2)
    sub = sorted([(float(a + q), float(seg[q])) for q in sp], key=lambda t: -t[1])
    return Candidate(peak=peak, start=round(start, 2), end=round(end, 2), score=round(score, 4),
                     peak_value=round(pv, 4), sub_peaks=sub)


def nms(cands: list[Candidate], top: int, gap: float = 1.0) -> list[Candidate]:
    picked: list[Candidate] = []
    for cnd in sorted(cands, key=lambda c: -c.score):
        if all(cnd.end + gap <= p.start or cnd.start >= p.end + gap for p in picked):
            picked.append(cnd)
        if len(picked) >= top:
            break
    return picked
