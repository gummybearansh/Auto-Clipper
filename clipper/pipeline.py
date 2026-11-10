"""Steps 2–9 for one ≤30-min chunk: score → segment → layout → title → captions → sfx → render."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import sfx as sfx_mod
from .config import Config
from .download import Source, load_chat
import numpy as np

from .layout import Layout, cam_absent_mask, plan, plan_clips
from .render import render_clip
from .score import Signals, base_signals, combine
from .segment import Candidate, find_candidates, make_window, nms
from .export import export_clip
from .titles import make_title
from .transcribe import covered, ensure_transcript, words_in
from .utils import Cancelled, check_cancel, fmt_ts, log, read_json, write_json  # noqa: F401

CHUNK_SECONDS = 1800


def clips_for(duration: float, per_30: int) -> int:
    return max(1, math.ceil(per_30 * duration / CHUNK_SECONDS - 1e-6))


def _transcribe(src: Source, cfg: Config, regions=None) -> list[dict]:
    if not src.has_audio:
        return []
    return ensure_transcript(src.audio_path, src.work_dir, cfg, src.duration, regions)


def _apply_penalty(sig: Signals, absent) -> Signals:
    if absent is not None:
        n = min(len(absent), len(sig.combined))
        x = sig.combined
        x[:n] = np.where(absent[:n] & (x[:n] > 0), x[:n] * 0.15, x[:n])
    return sig


def _campaign_filter(cands: list[Candidate], camp, src: Source, words: list[dict], cfg: Config,
                     whole: bool = False) -> list[Candidate]:
    """Apply a campaign brief: trim around excluded zones, drop banned/off-message windows, boost priorities, flag."""
    from .campaign import fit_window, priority_window
    out = []
    total = src.total_duration or (src.offset + src.duration)
    for c in cands:
        a, b = src.offset + c.start, src.offset + c.end
        if c.meta.get("forced_priority") is not None:      # key moment from the brief: build its window directly
            w = priority_window(camp, c.meta["forced_priority"], cfg.clip.min_len, cfg.clip.max_len, total)
            if w:
                a, b = w
                c.start, c.end = a - src.offset, b - src.offset
                c.peak = c.meta["forced_priority"] - src.offset
        fit = fit_window(camp, a, b, min(cfg.clip.min_len, b - a) if whole else cfg.clip.min_len)
        if not fit:
            continue
        a, b = fit
        c.start, c.end = round(a - src.offset, 2), round(b - src.offset, 2)
        text = " ".join(w["w"] for w in words_in(words, c.start, c.end))
        if camp.has_banned(text):
            continue                                   # e.g. footage mentions lab-grown → never clip it
        pri = camp.priority_in(a, b)
        hits = camp.mentions(text)
        if camp.require_keywords and not pri and not camp.strong_mentions(text) and not whole:
            continue                                   # doesn't connect back to the campaign
        flag = camp.flagged(a, b)
        c.meta = {"priority": pri["hook"] if pri else None, "keywords": hits, "flag": flag.reason if flag else None}
        if pri:
            c.score = round(c.score + 3.0, 4)          # brief's key moments go first
        out.append(c)
    return out


def _whole_clip(src: Source, words: list[dict], sig: Signals) -> Candidate:
    """Short inputs (official cutdowns): one clip = the whole video, trimmed to the speech."""
    a = max(0.0, words[0]["s"] - 0.4) if words else 0.0
    b = min(src.duration, words[-1]["e"] + 0.7) if words else src.duration
    if b - a < 5:
        a, b = 0.0, src.duration
    seg = sig.combined[int(a):max(int(a) + 1, int(b))]
    return Candidate(peak=(a + b) / 2, start=round(a, 2), end=round(b, 2), score=round(2.5 + float(seg.mean()), 4),
                     peak_value=float(seg.max()))


def select(src: Source, cfg: Config, top: int, chat, stage: Callable[[str], None],
           absent=None, camp=None) -> tuple[list[Candidate], Signals, list[dict]]:
    stage("analyzing audio / video / chat")
    raw = base_signals(src, cfg, chat)
    if not raw:
        raise RuntimeError("No usable signals (no audio, no chat, unreadable video).")
    stage(f"transcribing {fmt_ts(src.duration)} of audio")
    if src.duration <= cfg.asr.full_transcript_max_minutes * 60:
        words = _transcribe(src, cfg)
    else:
        pre = _apply_penalty(combine(raw, [], [], src.platform, cfg), absent)
        k = max(3 * top, 10)
        cands = nms([make_window(p, pre, [], cfg, src.duration) for p in find_candidates(pre, cfg, k * 3)], k)
        m = cfg.asr.candidate_margin
        words = _transcribe(src, cfg, [(c.start - m, c.end + m) for c in cands])

    stage("scoring + picking moments")
    if src.duration <= cfg.clip.max_len + 5:          # short input → the whole thing is the clip
        sig = combine(raw, words, covered(src.work_dir), src.platform, cfg)
        cands = [_whole_clip(src, words, sig)]
        if camp:
            cands = _campaign_filter(cands, camp, src, words, cfg, whole=True)
        for c in cands:
            c.meta["whole"] = True
        return cands, sig, words
    for _ in range(2):
        sig = _apply_penalty(combine(raw, words, covered(src.work_dir), src.platform, cfg), absent)
        peaks = find_candidates(sig, cfg, top * 8)
        pri_peaks = [p["t"] - src.offset for p in camp.priorities if 0 <= p["t"] - src.offset < src.duration] if camp else []
        cands = [make_window(p, sig, words, cfg, src.duration) for p in pri_peaks + peaks]
        for c, t in zip(cands, pri_peaks):     # the brief's key moments are always candidates
            c.meta["forced_priority"] = t + src.offset
        if camp:
            cands = _campaign_filter(cands, camp, src, words, cfg)
        if absent is not None:   # drop windows that show the intro/BRB screen instead of the cam
            cands = [c for c in cands if absent[int(c.start):int(np.ceil(c.end))].mean() <= 0.15]
        picked = nms(cands, top)
        need = [(c.start - cfg.clip.snap_window - 5, c.end + cfg.clip.snap_window + 5) for c in picked]
        before = len(words)
        words = _transcribe(src, cfg, need) if src.has_audio else words
        if len(words) == before:
            break
    return picked, sig, words


def _deeplink(platform: str, url: str, t: float) -> str:
    sep = "&" if "?" in url else "?"
    if platform == "youtube":
        return f"{url}{sep}t={int(t)}"
    if platform == "twitch":
        h, r = divmod(int(t), 3600)
        m, s = divmod(r, 60)
        return f"{url}{sep}t={h}h{m}m{s}s"
    return url


def build_clip_record(i: int, c: Candidate, src: Source, sig: Signals, words: list[dict], chat,
                      lay: Layout, cfg: Config, used_titles: set[str] | None = None, camp=None) -> dict:
    cid = f"clip_{i:02d}"
    bd = sig.breakdown(c.start, c.end)
    cw = words_in(words, c.start - 1, c.end + 1)
    if camp:
        cw0 = words_in(words, c.start - 0.2, c.end + 0.2)
        title = camp.title(c.meta, cw0, src.title, bool(c.meta.get("whole")), used_titles)
        strategy, mood = ("campaign:priority" if c.meta.get("priority") else "campaign"), camp.emoji
    else:
        title, strategy, mood = make_title(f"{src.video_id}:{src.chunk}:{c.start}", c.start, c.end, cw, chat,
                                           cfg.signals.chat_offset, sig.raw.get("audio_jump"), bd, used_titles)
    if used_titles is not None:
        used_titles.add(title)
    dur = c.end - c.start
    hits = [] if camp else sfx_mod.plan_hits(f"{src.video_id}:{src.chunk}:{cid}", dur,
                                             [(t - c.start, v) for t, v in c.sub_peaks], c.peak_value, cfg)
    abs_start = src.offset + c.start
    return {
        "id": cid, "rank": i, "video_id": src.video_id, "chunk": src.chunk,
        "key": f"{src.video_id}/chunk_{src.chunk:02d}/{cid}",
        "source": {"url": src.url, "title": src.title, "uploader": src.uploader, "platform": src.platform,
                   "game": getattr(src, "game", ""),
                   "video_path": str(src.video_path), "duration": src.duration, "fps": src.fps,
                   "width": src.width, "height": src.height, "has_audio": src.has_audio,
                   "work_dir": str(src.work_dir), "offset": src.offset},
        "start": c.start, "end": c.end, "duration": round(dur, 2), "peak": c.peak,
        "abs_start": round(abs_start, 2), "abs_end": round(src.offset + c.end, 2),
        "timestamp": fmt_ts(abs_start), "source_link": _deeplink(src.platform, src.url, abs_start),
        "score": c.score, "peak_value": c.peak_value, "breakdown": bd,
        "weights": {k: round(v, 3) for k, v in sig.weights.items()},
        "sub_peaks": [[round(t, 2), round(v, 3)] for t, v in c.sub_peaks],
        "title": title, "title_strategy": strategy, "mood": mood,
        "layout": lay.to_json(),
        "sfx": [h.to_json() for h in hits],
        "words": cw,
        "status": "pending",
        **({"campaign": {**camp.to_json(), **c.meta, "creator": camp.creator}, "music": False if not camp.music else None}
           if camp else {}),
        "output": f"{cid}.mp4",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def process_source(src: Source, cfg: Config, top: int, layout_mode: str | None = None,
                   stage: Callable[[str], None] | None = None,
                   checkpoint: Callable[[], None] | None = None,
                   on_clip: Callable[[dict], None] | None = None, campaign: str | None = None) -> list[dict]:
    """Score, pick and render the top clips of one chunk. Skips clips that were already rendered."""
    from .campaign import load as load_campaign
    camp = load_campaign(campaign)
    if camp:
        total = src.total_duration or (src.offset + src.duration)
        if camp.times_apply(total):
            log(f"campaign: {camp.name} — {len(camp.priorities)} priority moments, {len(camp.exclude)} excluded zones")
        else:
            camp = camp.without_times()
            log(f"campaign: {camp.name} — short/other source: keyword + hashtag rules only (brief timestamps are for the main video)")
        if not src.uploader:
            src.uploader = camp.creator
    stage = stage or (lambda s: log(s))
    checkpoint = checkpoint or (lambda: None)
    chat = load_chat(src.chat_path)
    if src.duration < cfg.clip.min_len:
        log(f"chunk {src.chunk} is shorter than the minimum clip length; skipping")
        return []
    existing = sorted(src.out_dir.glob("clip_*.json"))
    if existing and all((p.with_suffix(".mp4")).exists() for p in existing) and len(existing) >= top:
        log(f"chunk {src.chunk}: {len(existing)} clips already rendered")
        return [read_json(p) for p in existing]

    stage("detecting layout")
    lay = plan(src, cfg, layout_mode if layout_mode and layout_mode != "auto" else None)
    log(f"layout: {lay.mode} ({lay.reason})")
    absent = cam_absent_mask(lay.face, int(np.ceil(src.duration))) if lay.mode == "split" else None
    if absent is not None and absent.any():
        log(f"facecam off screen for ~{fmt_ts(float(absent.sum()))} (intro/BRB?) — down-weighting those parts")
    checkpoint()
    picked, sig, words = select(src, cfg, top, chat, stage, absent, camp)
    checkpoint()
    if not picked:
        return []
    log("signals: " + ", ".join(f"{k}={v:.2f}" for k, v in sig.weights.items()))

    clips = []
    used_titles: set[str] = set()
    ordered = sorted(picked, key=lambda c: -c.score)
    stage("choosing layout per clip")
    lays = plan_clips(src, cfg, lay, [(c.start, c.end) for c in ordered], layout_mode)
    for i, (c, clay) in enumerate(zip(ordered, lays), 1):
        if clay is not lay:
            log(f"clip {i}: {clay.mode} ({clay.reason})")
    for i, (c, clay) in enumerate(zip(ordered, lays), 1):
        checkpoint()
        rec = build_clip_record(i, c, src, sig, words, chat, clay, cfg, used_titles, camp)
        jp, out = src.out_dir / f"{rec['id']}.json", src.out_dir / rec["output"]
        if jp.exists() and out.exists():
            old = read_json(jp)
            used_titles.add(old.get("title", ""))
            clips.append(old)
            continue
        stage(f"rendering clip {i}/{len(ordered)}")
        log(f"{rec['id']}  {fmt_ts(rec['abs_start'])} ({rec['duration']:.0f}s)  score {c.score:.2f}  “{rec['title']}”")
        render_clip(rec, src.work_dir / "clips" / rec["id"], out, cfg)
        write_json(jp, rec)
        try:
            export_clip(cfg, jp)
            rec = read_json(jp)
        except Exception as e:
            log(f"export failed for {rec['id']}: {e}")
        clips.append(rec)
        if on_clip:
            on_clip(rec)
    write_json(src.out_dir / "index.json", {
        "video_id": src.video_id, "chunk": src.chunk, "offset": src.offset, "title": src.title,
        "clips": [{"id": c["id"], "score": c["score"], "title": c["title"], "abs_start": c["abs_start"]} for c in clips]})
    return clips


def rerender(clip_json: Path, cfg: Config) -> Path:
    rec = read_json(clip_json)
    src = rec["source"]
    work = Path(src["work_dir"])
    start, end = max(0.0, float(rec["start"])), min(float(src["duration"]), float(rec["end"]))
    off = float(src.get("offset", 0))
    rec.update(start=start, end=end, duration=round(end - start, 2), abs_start=round(off + start, 2),
               abs_end=round(off + end, 2), timestamp=fmt_ts(off + start))
    audio = work / "audio16k.wav"
    if src.get("has_audio", True) and audio.exists():
        words = ensure_transcript(audio, work, cfg, float(src["duration"]), [(start - 2, end + 2)])
        rec["words"] = words_in(words, start - 1, end + 1)
    peaks_rel = [(t - start, v) for t, v in rec.get("sub_peaks", []) if start <= t <= end]
    rec["sfx"] = [] if rec.get("campaign") else [h.to_json() for h in sfx_mod.plan_hits(
        rec.get("key", rec["id"]), end - start, peaks_rel, rec.get("peak_value", 1.0), cfg)]
    out = clip_json.parent / rec["output"]
    log(f"re-rendering {rec['id']} “{rec['title']}”")
    render_clip(rec, work / "clips" / rec["id"], out, cfg)
    rec.pop("rendering", None)
    write_json(clip_json, rec)
    try:
        export_clip(cfg, clip_json)
    except Exception as e:
        log(f"export failed for {rec['id']}: {e}")
    return out
