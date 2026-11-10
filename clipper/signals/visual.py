"""Visual activity: mean absolute frame difference on tiny grayscale frames (cheap scene-cut/action proxy).

Long videos decode keyframes only (-skip_frame nokey) so a 3h VOD takes a minute, not an hour.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

W, H = 64, 36


def analyze(video: Path, n: int, fps: float, duration: float) -> dict[str, np.ndarray]:
    long = duration > 20 * 60
    cmd = ["ffmpeg", "-v", "error", "-hwaccel", "videotoolbox"]
    if long:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", str(video), "-an", "-vf",
            (f"scale={W}:{H}" if long else f"fps={fps},scale={W}:{H}") + ",format=gray",
            "-fps_mode", "passthrough" if long else "cfr",
            "-f", "rawvideo", "-"]
    times = _keyframe_times(video) if long else []  # keyframes are irregular; get their pts
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        # retry without hwaccel
        cmd = [c for c in cmd if c not in ("-hwaccel", "videotoolbox")]
        try:
            proc = subprocess.run(cmd, capture_output=True, check=True)
        except subprocess.CalledProcessError:
            return {}
    frames = np.frombuffer(proc.stdout, dtype=np.uint8)
    k = len(frames) // (W * H)
    if k < 3:
        return {}
    frames = frames[: k * W * H].reshape(k, H, W).astype(np.float32)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    if long:
        t = np.asarray(times[1: k], dtype=np.float64)
        if len(t) != len(diff):
            t = np.linspace(0, duration, len(diff))
    else:
        t = (np.arange(1, k)) / fps
    out = np.zeros(n)
    idx = np.clip(t.astype(int), 0, n - 1)
    np.maximum.at(out, idx, diff)
    if long:  # keyframes are sparse; carry values forward to fill gaps
        last = 0.0
        for i in range(n):
            if out[i] > 0:
                last = out[i]
            else:
                out[i] = last * 0.8
                last = out[i]
    return {"visual": out}


def _keyframe_times(video: Path) -> list[float]:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries",
         "frame=pts_time", "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    out = []
    for line in res.stdout.splitlines():
        try:
            out.append(float(line.strip().strip(",")))
        except ValueError:
            pass
    return out
