"""Export finished clips to a browsable folder (default: ~/Desktop/gummybear clips/<run>/).

One subfolder per link/video, named like
    2026-11-10 jasontheween - RICE AND CRACKERS EPICNESS (twitch v2924380274)
containing every rendered clip, named so they sort by part then rank:
    p01-01 08m52s - chat went crazy for this.mp4
plus captions.txt with a ready-to-paste caption, the stream timestamp, score and source link for each clip.

Files are hard links when possible (same disk → no extra space); deleting one copy doesn't affect the other.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

from .config import Config
from .utils import read_json, write_json

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]")


def _clean(s: str, n: int = 60) -> str:
    s = _EMOJI.sub(" ", s or "")
    s = _BAD.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return s[:n].rstrip(" .-_") or "untitled"


def export_root(cfg: Config) -> Path:
    return Path(os.path.expanduser(cfg.export.dir))


def video_dir(cfg: Config, rec: dict) -> Path:
    """Stable per-video export folder (name fixed the first time it's created)."""
    vid = rec["video_id"]
    marker = cfg.path(cfg.out_dir) / vid / "export.json"
    if marker.exists():
        return Path(read_json(marker)["dir"])
    src = rec.get("source", {})
    who = _clean(src.get("uploader") or "", 30)
    title = _clean(src.get("title") or vid, 50)
    plat, _, ident = vid.partition("_")
    name = f"{time.strftime('%Y-%m-%d')} {who + ' - ' if who else ''}{title} ({plat} {ident})"
    d = export_root(cfg) / name
    write_json(marker, {"dir": str(d)})
    return d


def _ts(sec: float) -> str:
    sec = int(max(0, sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m:02d}m{s:02d}s"


def clip_filename(rec: dict) -> str:
    return f"p{int(rec.get('chunk', 1)):02d}-{int(rec['rank']):02d} {_ts(rec.get('abs_start', rec['start']))} - {_clean(rec['title'], 70)}.mp4"


def _place(src: Path, dst: Path) -> None:
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def export_clip(cfg: Config, json_path: Path) -> Path | None:
    if not cfg.export.enabled:
        return None
    rec = read_json(json_path)
    mp4 = json_path.parent / rec["output"]
    if not mp4.exists():
        return None
    d = video_dir(cfg, rec)
    d.mkdir(parents=True, exist_ok=True)
    dst = d / clip_filename(rec)
    old = rec.get("export_path")
    if old and Path(old) != dst:
        Path(old).unlink(missing_ok=True)          # title changed → drop the old-named copy
    _place(mp4, dst)
    from .tags import post_sheet
    if old and Path(old) != dst:
        Path(old).with_suffix(".txt").unlink(missing_ok=True)
    dst.with_suffix(".txt").write_text(post_sheet(rec, cfg), encoding="utf-8")
    if rec.get("export_path") != str(dst):
        rec["export_path"] = str(dst)
        from .utils import write_json as _w
        _w(json_path, rec)
    write_captions(cfg, rec["video_id"])
    return dst


def caption_for(rec: dict, cfg: Config | None = None) -> str:
    from .tags import instagram_caption
    return instagram_caption(rec, cfg)


def write_captions(cfg: Config, video_id: str) -> None:
    root = cfg.path(cfg.out_dir) / video_id
    recs = []
    for jp in sorted(root.glob("chunk_*/clip_[0-9][0-9].json")) + sorted(root.glob("clip_[0-9][0-9].json")):
        try:
            r = read_json(jp)
        except Exception:
            continue
        if r.get("export_path") and Path(r["export_path"]).exists():
            recs.append(r)
    if not recs:
        return
    recs.sort(key=lambda r: (int(r.get("chunk", 1)), int(r["rank"])))
    d = Path(recs[0]["export_path"]).parent
    lines = [f"{recs[0]['source'].get('title', '')}", f"{recs[0]['source'].get('url', '')}", "=" * 60, ""]
    for r in recs:
        lines += [f"▶ {Path(r['export_path']).name}",
                  f"   stream time {r.get('timestamp')}  ·  {r['duration']:.0f}s  ·  score {r['score']:.2f}",
                  f"   source: {r.get('source_link', '')}",
                  "", caption_for(r, cfg), "", "-" * 60, ""]
    (d / "captions.txt").write_text("\n".join(lines), encoding="utf-8")


def backfill(cfg: Config) -> int:
    n = 0
    for jp in sorted(cfg.path(cfg.out_dir).glob("*/chunk_*/clip_[0-9][0-9].json")):
        if export_clip(cfg, jp):
            n += 1
    return n
