"""YouTube 'most replayed' heatmap (from yt-dlp info) → per-second series."""
from __future__ import annotations

import numpy as np


def heatmap_signal(heatmap: list[dict], n: int) -> dict[str, np.ndarray]:
    if not heatmap:
        return {}
    out = np.zeros(n)
    for seg in heatmap:
        a, b = int(seg.get("start_time", 0)), int(np.ceil(seg.get("end_time", 0)))
        out[max(0, a): min(n, b)] = float(seg.get("value", 0))
    if out.max() <= 0:
        return {}
    return {"heatmap": out}
