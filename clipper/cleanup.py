"""Disk cleanup after clipping.

Once a chunk's clips are rendered, the 30-min chunk video (~2 GB) and its audio are deleted. Each clip keeps a
small re-encoded excerpt (clip ±pad seconds) so titles can still be edited and bounds nudged in review; the
clip's json is rewritten to point at the excerpt. Rejecting a clip deletes its excerpt.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from .config import Config
from .utils import log, media_info, read_json, run, write_json


def make_excerpt(rec: dict, json_path: Path, pad: float, fps: int = 30) -> bool:
    """Cut clip ±pad from the chunk video into clip_XX.src.mp4 and rebase the record onto it."""
    src = rec["source"]
    if src.get("excerpt"):
        return True
    video = Path(src["video_path"])
    if not video.exists():
        return False
    a = max(0.0, float(rec["start"]) - pad)
    b = min(float(src["duration"]), float(rec["end"]) + pad)
    ex = json_path.with_name(json_path.stem + ".src.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(video),
         "-map", "0:v:0", "-map", "0:a:0?", *(["-r", str(fps)] if fps <= 30 else []),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(ex)])
    mi = media_info(ex)

    def sh(t):
        return round(float(t) - a, 3)

    rec["start"], rec["end"], rec["peak"] = sh(rec["start"]), sh(rec["end"]), sh(rec.get("peak", rec["start"]))
    rec["sub_peaks"] = [[sh(t), v] for t, v in rec.get("sub_peaks", [])]
    rec["words"] = [{**w, "s": sh(w["s"]), "e": sh(w["e"])} for w in rec.get("words", [])]
    src.update(video_path=str(ex), duration=mi["duration"], offset=round(float(src.get("offset", 0)) + a, 3),
               excerpt=True, fps=mi["fps"])
    write_json(json_path, rec)
    return True


def cleanup_chunk(out_dir: Path, cfg: Config) -> int:
    """Excerpt every clip of a finished chunk, then delete the heavy files. Returns bytes freed."""
    if not cfg.cleanup.enabled:
        return 0
    work = out_dir / "work"
    ok = True
    for jp in sorted(out_dir.glob("clip_[0-9][0-9].json")):
        rec = read_json(jp)
        if rec.get("status") == "rejected":
            continue
        try:
            ok &= make_excerpt(rec, jp, cfg.cleanup.excerpt_pad, cfg.layout.fps)
        except Exception as e:  # never delete the source if an excerpt failed
            log(f"excerpt failed for {jp.name}: {e}")
            ok = False
    if not ok:
        return 0
    freed = 0
    for p in (work / "source.mp4", work / "audio16k.wav", work / "joined.ts"):
        if p.exists() or p.is_symlink():
            freed += p.stat().st_size if p.exists() and not p.is_symlink() else 0
            p.unlink()
    for d in (work / "clips", work / "segs"):
        if d.exists():
            freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d, ignore_errors=True)
    return freed


def cleanup_full(video_root: Path) -> int:
    """Delete a whole-video download (YouTube) once every chunk has been cut and processed."""
    d = video_root / "_full"
    if not d.exists():
        return 0
    freed = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    shutil.rmtree(d, ignore_errors=True)
    return freed


def drop_excerpt(json_path: Path) -> None:
    """Rejected clip: its excerpt isn't needed anymore."""
    json_path.with_name(json_path.stem + ".src.mp4").unlink(missing_ok=True)


def sweep(cfg: Config) -> int:
    """Clean every finished chunk under out/ (used by `clipper cleanup`)."""
    root = cfg.path(cfg.out_dir)
    freed = 0
    for idx in root.glob("*/chunk_*/index.json"):
        freed += cleanup_chunk(idx.parent, cfg)
    for jp in root.glob("*/chunk_*/clip_[0-9][0-9].json"):
        try:
            if read_json(jp).get("status") == "rejected":
                drop_excerpt(jp)
        except Exception:
            pass
    return freed


def fmt_bytes(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"
