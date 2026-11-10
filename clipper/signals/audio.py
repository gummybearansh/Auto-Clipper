"""Audio signals from the 16 kHz mono wav: loudness jumps vs local baseline, spectral flux,
and a cheap 'yelling/laughter' heuristic (loud + bright + voiced-band energy)."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from .common import rolling_baseline

FRAME = 1024
HOP = 512


def analyze(wav_path: Path, n: int, baseline_window: int) -> dict[str, np.ndarray]:
    if not wav_path.exists():
        return {}
    loud_db = np.full(n, -90.0)
    flux = np.zeros(n)
    voice = np.zeros(n)
    centroid = np.zeros(n)
    win = np.hanning(FRAME).astype(np.float32)
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        freqs = np.fft.rfftfreq(FRAME, 1 / sr)
        vband = (freqs >= 250) & (freqs <= 3500)
        chunk_s = 60
        prev_mag = None
        sec0 = 0
        while sec0 < n:
            raw = wf.readframes(sr * chunk_s)
            if not raw:
                break
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            secs = len(x) // sr
            if secs == 0:
                secs = 1
                x = np.pad(x, (0, sr - len(x)))
            x = x[: secs * sr]
            # per-second RMS
            rms = np.sqrt(np.mean(x.reshape(secs, sr) ** 2, axis=1) + 1e-12)
            # STFT
            nfr = 1 + (len(x) - FRAME) // HOP
            idx = np.arange(FRAME)[None, :] + HOP * np.arange(nfr)[:, None]
            mag = np.abs(np.fft.rfft(x[idx] * win, axis=1)).astype(np.float32)
            logmag = np.log1p(mag * 10)
            if prev_mag is None:
                prev_mag = logmag[:1]
            diff = np.diff(np.vstack([prev_mag, logmag]), axis=0)
            fl = np.maximum(diff, 0).sum(axis=1)
            prev_mag = logmag[-1:]
            tot = mag.sum(axis=1) + 1e-9
            cen = (mag * freqs).sum(axis=1) / tot
            vfrac = mag[:, vband].sum(axis=1) / tot
            fsec = (np.arange(nfr) * HOP + FRAME / 2) // sr
            for name, arr, agg in (("flux", fl, np.mean), ("cen", cen, np.mean), ("v", vfrac, np.mean)):
                vals = np.zeros(secs)
                for s in range(secs):
                    m = fsec == s
                    vals[s] = agg(arr[m]) if m.any() else 0
                end = min(n, sec0 + secs)
                k = end - sec0
                if name == "flux":
                    flux[sec0:end] = vals[:k]
                elif name == "cen":
                    centroid[sec0:end] = vals[:k]
                else:
                    voice[sec0:end] = vals[:k]
            end = min(n, sec0 + secs)
            loud_db[sec0:end] = 20 * np.log10(rms[: end - sec0])
            sec0 += secs
    base = rolling_baseline(loud_db, baseline_window)
    jump = loud_db - base                                    # dB above local baseline
    # yelling/laughing: loud vs baseline, energy concentrated in voice band, brighter than usual
    cen_rel = centroid - rolling_baseline(centroid, baseline_window)
    yell = np.maximum(jump, 0) * np.clip(voice, 0, 1) * (1 + np.clip(cen_rel / 1000, -0.5, 1.0))
    return {"audio_jump": jump, "audio_flux": flux, "audio_yell": yell, "loud_db": loud_db}
