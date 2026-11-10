"""Shared helpers: ffmpeg/ffprobe wrappers, json io, logging."""
from __future__ import annotations

import functools
import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()
_local = threading.local()


class Cancelled(Exception):
    """Raised inside a running job when the user hits Cancel."""


def set_cancel_check(fn) -> None:
    """fn() -> bool; when it returns True, check_cancel() raises Cancelled in this thread."""
    _local.cancel = fn


def check_cancel() -> None:
    fn = getattr(_local, "cancel", None)
    if fn and fn():
        raise Cancelled()


def set_log_hook(fn) -> None:
    """Route log() lines from the current thread to fn(msg) as well (used by the UI job runner)."""
    _local.hook = fn


def log(msg: str) -> None:
    console.print(f"[dim]›[/dim] {msg}")
    hook = getattr(_local, "hook", None)
    if hook:
        try:
            hook(msg)
        except Exception:
            pass


def require_bin(name: str) -> str:
    p = shutil.which(name)
    if not p:
        raise SystemExit(f"'{name}' not found on PATH. Install it (e.g. `brew install {name}`).")
    return p


def run(cmd: list[str], *, quiet: bool = True, check: bool = True, **kw) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=quiet, text=True, **kw)
    if check and res.returncode != 0:
        err = (res.stderr or "")[-3000:] if quiet else ""
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(map(str, cmd[:6]))} ...\n{err}")
    return res


def ffprobe(path: str | Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)])
    return json.loads(out.stdout)


def media_info(path: str | Path) -> dict:
    """width, height, fps, duration, has_audio."""
    info = ffprobe(path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if v is None:
        raise RuntimeError(f"No video stream in {path}")
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    return {"width": int(v["width"]), "height": int(v["height"]), "fps": fps,
            "duration": dur, "has_audio": a is not None}


@functools.cache
def ffmpeg_filters() -> set[str]:
    out = run(["ffmpeg", "-hide_banner", "-filters"]).stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return names


def has_ffmpeg_filter(name: str) -> bool:
    return name in ffmpeg_filters()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def fmt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
