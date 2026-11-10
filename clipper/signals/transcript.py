"""Transcript signals: speech density (words/sec) and a hand-tuned hype keyword lexicon."""
from __future__ import annotations

import re

import numpy as np

from .common import bin_events

# phrase -> weight. Matched on normalized lowercase text (punctuation stripped).
KEYWORDS: dict[str, float] = {
    "no way": 2.0, "oh my god": 2.0, "oh my gosh": 1.5, "lets go": 2.0, "let's go": 2.0, "what the": 1.5,
    "are you kidding": 2.0, "are you serious": 1.8, "you're kidding": 1.5, "no no no": 2.0, "yes yes": 1.5,
    "holy": 1.5, "insane": 1.5, "clip that": 3.0, "clip it": 3.0, "bro": 0.8, "dude": 0.8, "what": 0.6,
    "wait": 0.8, "oh no": 1.5, "oh shit": 2.0, "omg": 1.5, "wtf": 1.8, "nah": 0.8, "crazy": 1.0,
    "unbelievable": 1.8, "i can't": 1.0, "dead": 0.8, "run": 0.8, "help": 1.0, "behind you": 1.2,
    "gg": 1.0, "clutch": 1.5, "one tap": 1.5, "headshot": 1.2, "lmao": 1.0, "dying": 0.8,
    "fuck": 1.2, "fucking": 1.2, "shit": 1.0, "damn": 0.8, "goddamn": 1.2, "jesus": 1.0, "yo": 0.6,
    "sheesh": 1.2, "ez": 0.8, "noo": 1.5, "nooo": 2.0, "yesss": 1.5, "aaah": 1.5, "ahh": 1.0,
}
_NORM = re.compile(r"[^\w' ]+")


def norm(text: str) -> str:
    return _NORM.sub(" ", text.lower()).replace("  ", " ").strip()


def keyword_score(text: str) -> float:
    t = f" {norm(text)} "
    s = 0.0
    for k, w in KEYWORDS.items():
        if f" {k} " in t:
            s += w
    # elongated words ("nooooo", "yesssss") are hype markers
    s += 0.8 * len(re.findall(r"(\w)\1{3,}", t))
    return s


def transcript_signals(words: list[dict], n: int, covered_mask: np.ndarray) -> dict[str, np.ndarray]:
    if not words:
        return {}
    mids = [(w["s"] + w["e"]) / 2 for w in words]
    speech = bin_events(mids, n)
    # keywords: score a sliding 4-word window and attribute to its first word's second
    kw = np.zeros(n)
    toks = [w["w"] for w in words]
    for i in range(len(words)):
        single = keyword_score(toks[i])
        multi = keyword_score(" ".join(toks[i:i + 4])) - keyword_score(" ".join(toks[i + 1:i + 4]))
        sc = max(single, multi)
        if sc > 0:
            sec = int(mids[i])
            if 0 <= sec < n:
                kw[sec] += sc
    return {"speech": speech, "keywords": kw, "_mask": covered_mask}
