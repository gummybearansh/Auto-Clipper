"""Step 2: local ASR with word timestamps (faster-whisper + Silero VAD), optional Deepgram backend.

Transcripts are cached per work dir and can be filled in incrementally by region, so long VODs
only transcribe the areas around candidate peaks (and `rerender` can fill gaps on demand).
"""
from __future__ import annotations

import io
import json
import os
import urllib.request
import wave
from pathlib import Path

import numpy as np

from .config import Config
from .utils import check_cancel, log, read_json, write_json

SR = 16000
Word = dict  # {"w": str, "s": float, "e": float, "p": float}


# ---------------------------------------------------------------- audio io
def read_wav_slice(path: Path, start: float, end: float) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        n = wf.getnframes()
        a = max(0, int(start * sr))
        b = min(n, int(end * sr))
        if b <= a:
            return np.zeros(0, dtype=np.float32)
        wf.setpos(a)
        raw = wf.readframes(b - a)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _wav_bytes(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- backends
_WHISPER = {}


def _whisper_model(cfg: Config):
    key = (cfg.asr.model, cfg.asr.compute_type)
    if key not in _WHISPER:
        from faster_whisper import WhisperModel
        log(f"loading faster-whisper '{cfg.asr.model}' ({cfg.asr.compute_type})")
        _WHISPER[key] = WhisperModel(cfg.asr.model, device="cpu", compute_type=cfg.asr.compute_type,
                                     cpu_threads=os.cpu_count() or 4)
    return _WHISPER[key]


def _whisper(samples: np.ndarray, offset: float, cfg: Config) -> list[Word]:
    model = _whisper_model(cfg)
    segments, _info = model.transcribe(
        samples, language=cfg.asr.language, word_timestamps=True, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
        beam_size=5, condition_on_previous_text=False,
    )
    words: list[Word] = []
    for seg in segments:
        check_cancel()
        for w in seg.words or []:
            text = w.word.strip()
            if not text:
                continue
            words.append({"w": text, "s": round(w.start + offset, 3), "e": round(w.end + offset, 3),
                          "p": round(float(w.probability), 3)})
    return words


def _deepgram(samples: np.ndarray, offset: float, cfg: Config) -> list[Word]:
    key = os.environ.get("DEEPGRAM_API_KEY")
    if not key:
        raise SystemExit("--asr deepgram requires DEEPGRAM_API_KEY to be set")
    params = "model=nova-2&smart_format=true&punctuate=true"
    if cfg.asr.language:
        params += f"&language={cfg.asr.language}"
    else:
        params += "&detect_language=true"
    req = urllib.request.Request(
        f"https://api.deepgram.com/v1/listen?{params}", data=_wav_bytes(samples), method="POST",
        headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.loads(r.read())
    alts = data["results"]["channels"][0]["alternatives"]
    out = []
    for w in (alts[0].get("words") if alts else []) or []:
        out.append({"w": w.get("punctuated_word") or w["word"], "s": round(w["start"] + offset, 3),
                    "e": round(w["end"] + offset, 3), "p": round(float(w.get("confidence", 1)), 3)})
    return out


# ---------------------------------------------------------------- cache + regions
def _merge(regions: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(regions):
        if out and a <= out[-1][1] + 1.0:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _subtract(region: tuple[float, float], covered: list[tuple[float, float]]) -> list[tuple[float, float]]:
    pieces = [region]
    for ca, cb in covered:
        nxt = []
        for a, b in pieces:
            if cb <= a or ca >= b:
                nxt.append((a, b))
                continue
            if a < ca:
                nxt.append((a, ca))
            if cb < b:
                nxt.append((cb, b))
        pieces = nxt
    return [(a, b) for a, b in pieces if b - a > 1.0]


def ensure_transcript(audio_path: Path, work_dir: Path, cfg: Config, duration: float,
                      regions: list[tuple[float, float]] | None = None) -> list[Word]:
    """Return cached words, transcribing any part of `regions` (None = whole file) not yet covered."""
    cache_p = work_dir / "transcript.json"
    cache = read_json(cache_p) if cache_p.exists() else {"backend": cfg.asr.backend, "model": cfg.asr.model,
                                                          "covered": [], "words": []}
    if cache.get("backend") != cfg.asr.backend or cache.get("model") != cfg.asr.model:
        cache = {"backend": cfg.asr.backend, "model": cfg.asr.model, "covered": [], "words": []}
    if not audio_path.exists():
        return []
    want = [(0.0, duration)] if regions is None else [(max(0, a), min(duration, b)) for a, b in regions]
    covered = [tuple(c) for c in cache["covered"]]
    todo = []
    for r in _merge(want):
        todo += _subtract(r, covered)
    if todo:
        total = sum(b - a for a, b in todo)
        log(f"transcribing {total / 60:.1f} min of audio ({cfg.asr.backend}:{cfg.asr.model if cfg.asr.backend == 'whisper' else 'nova-2'})")
        fn = _deepgram if cfg.asr.backend == "deepgram" else _whisper
        pad = 2.0
        for i, (a, b) in enumerate(todo, 1):
            check_cancel()
            pa, pb = max(0.0, a - pad), min(duration, b + pad)
            samples = read_wav_slice(audio_path, pa, pb)
            new = [w for w in fn(samples, pa, cfg) if a <= (w["s"] + w["e"]) / 2 < b]
            cache["words"].extend(new)
            covered.append((a, b))
            if len(todo) > 1:
                log(f"  region {i}/{len(todo)} [{a:.0f}s–{b:.0f}s]: {len(new)} words")
        cache["words"].sort(key=lambda w: w["s"])
        cache["covered"] = [list(c) for c in _merge(covered)]
        write_json(cache_p, cache)
    return cache["words"]


def covered(work_dir: Path) -> list[list[float]]:
    p = work_dir / "transcript.json"
    return read_json(p).get("covered", []) if p.exists() else []


def words_in(words: list[Word], start: float, end: float) -> list[Word]:
    return [w for w in words if w["e"] > start and w["s"] < end]
