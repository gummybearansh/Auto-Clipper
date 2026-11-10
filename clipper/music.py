"""Background music for weaker clips (score below `music.max_score`).

Tracks: Kevin MacLeod (incompetech.com), licensed CC BY 4.0. Free to use commercially with attribution, which
is added to every caption automatically. Picked by the clip's mood: 🔥 hype → energetic, 😭 funny → quirky.

The mp3s aren't stored in the repo; `clipper music` downloads the curated set into assets/music/.
"""
from __future__ import annotations

import hashlib
import urllib.parse
import urllib.request
from pathlib import Path

from .config import Config

SOURCE = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/{}.mp3"
TRACKS = {
    "😭": ["Monkeys Spinning Monkeys", "Sneaky Snitch", "Fluffing a Duck", "Scheming Weasel faster",
           "Pixel Peeker Polka - faster", "Run Amok", "Local Forecast - Elevator", "Investigations",
           "Itty Bitty 8 Bit", "Carefree"],
    "🔥": ["Hitman", "Volatile Reaction", "Exhilarate", "Movement Proposition", "Funkorama", "Pamgaea", "Rhinoceros"],
}
ARTIST = "Kevin MacLeod"
LICENSE = "CC BY 4.0"


def music_dir(cfg: Config) -> Path:
    return cfg.path(cfg.music.dir)


def track_path(cfg: Config, title: str) -> Path:
    return music_dir(cfg) / f"{title}.mp3"


def download(cfg: Config, log=print) -> int:
    d = music_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for title in [t for ts in TRACKS.values() for t in ts]:
        p = track_path(cfg, title)
        if p.exists() and p.stat().st_size > 100_000:
            n += 1
            continue
        try:
            req = urllib.request.Request(SOURCE.format(urllib.parse.quote(title)), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) > 100_000:
                p.write_bytes(data)
                n += 1
                log(f"downloaded {title}")
        except Exception as e:
            log(f"couldn't download {title}: {e}")
    return n


def credit(title: str) -> str:
    return f'🎵 "{title}" {ARTIST} (incompetech.com) · {LICENSE}'


def pick(cfg: Config, rec: dict) -> dict | None:
    """Music choice for a clip, or None. Stable per clip (same track on every re-render)."""
    if not cfg.music.enabled:
        return None
    if rec.get("music") is False:              # explicitly disabled for this clip
        return None
    if isinstance(rec.get("music"), dict) and track_path(cfg, rec["music"]["title"]).exists():
        return rec["music"]
    if float(rec.get("score", 0)) >= cfg.music.max_score:
        return None
    mood = rec.get("mood") or ("😭" if "😭" in rec.get("title", "") else "🔥")
    pool = [t for t in TRACKS.get(mood, TRACKS["🔥"]) if track_path(cfg, t).exists()]
    if not pool:
        pool = [t for ts in TRACKS.values() for t in ts if track_path(cfg, t).exists()]
    if not pool:
        return None
    h = int(hashlib.md5(str(rec.get("key", rec["title"])).encode()).hexdigest(), 16)
    title = pool[h % len(pool)]
    offset = float((h >> 8) % 40)               # start somewhere in the first ~40s for variety
    return {"title": title, "artist": ARTIST, "license": LICENSE, "offset": offset, "credit": credit(title)}
