"""Fast time-range download from an HLS media playlist (Twitch VODs, incl. the in-progress VOD of a live stream).

yt-dlp's ranged download goes through ffmpeg and fetches segments one by one (~1 MB/s). Here we pick only
the segments overlapping [start, end], fetch them in parallel, concatenate, and trim with a stream copy.
"""
from __future__ import annotations

import shutil
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

from .utils import check_cancel, run

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _get(url: str, tries: int = 5) -> bytes:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.0 * (i + 1))
    raise RuntimeError("unreachable")


def parse_playlist(url: str) -> tuple[str | None, list[tuple[float, float, str]]]:
    """Returns (init segment url or None, [(seg_start, seg_dur, seg_url), ...])."""
    text = _get(url).decode("utf-8", "replace")
    if "#EXT-X-STREAM-INF" in text:          # master playlist: take the first (highest listed) variant
        lines = [l.strip() for l in text.splitlines()]
        for i, l in enumerate(lines):
            if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                return parse_playlist(urljoin(url, lines[i + 1]))
    init, segs, t, dur = None, [], 0.0, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:"):
            uri = line.split("URI=", 1)[1].split(",")[0].strip('"')
            init = urljoin(url, uri)
        elif line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line and not line.startswith("#") and dur is not None:
            segs.append((t, dur, urljoin(url, line)))
            t += dur
            dur = None
    return init, segs


def download_range(playlist_url: str, start: float, end: float, dest: Path,
                   progress: Callable[[str], None] | None = None, workers: int = 16) -> Path:
    progress = progress or (lambda s: None)
    init, segs = parse_playlist(playlist_url)
    sel = [(s, d, u) for s, d, u in segs if s + d > start and s < end]
    if not sel:
        raise RuntimeError(f"no HLS segments cover {start:.0f}s–{end:.0f}s (playlist has {len(segs)} segments)")
    tmp = dest.parent / "segs"
    tmp.mkdir(parents=True, exist_ok=True)
    done, total_bytes, t0 = 0, 0, time.time()

    fresh = [0]

    def fetch(i: int, url: str) -> int:
        p = tmp / f"{i:05d}.seg"
        if p.exists() and p.stat().st_size > 0:     # already downloaded before a restart/cancel
            return p.stat().st_size
        data = _get(url)
        p.with_suffix(".part").write_bytes(data)
        p.with_suffix(".part").rename(p)
        fresh[0] += len(data)
        return len(data)

    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = [ex.submit(fetch, i, u) for i, (_, _, u) in enumerate(sel)]
        for f in as_completed(futs):
            check_cancel()
            total_bytes += f.result()
            done += 1
            if done % 8 == 0 or done == len(sel):
                rate = fresh[0] / max(0.1, time.time() - t0) / 1e6
                progress(f"downloading video {100 * done // len(sel)}% ({total_bytes / 1e6:.0f} MB, {rate:.0f} MB/s)")
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)

    joined = dest.parent / "joined.ts"
    with joined.open("wb") as out:
        if init:
            out.write(_get(init))
        for i in range(len(sel)):
            with (tmp / f"{i:05d}.seg").open("rb") as f:
                shutil.copyfileobj(f, out)
    offset = max(0.0, start - sel[0][0])
    progress("trimming chunk")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{offset:.3f}", "-i", str(joined), "-t", f"{end - start:.3f}",
         "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", str(dest)])
    joined.unlink(missing_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return dest
