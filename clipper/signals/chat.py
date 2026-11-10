"""Chat signals: velocity vs rolling baseline, emote/hype density, literal 'clip' mentions,
plus a reaction classifier used for the title caption."""
from __future__ import annotations

import re
from collections import Counter

import numpy as np

from .common import bin_events, rolling_baseline

# reaction category -> tokens (matched case-insensitively against whole tokens, emoji by substring)
REACTIONS: dict[str, list[str]] = {
    "laugh": ["kekw", "lul", "lulw", "omegalul", "lmao", "lmfao", "lol", "icant", "kek", "xd", "😂", "🤣", "💀",
              "pepelaugh", "kekl", "dead", "hahaha", "hahahaha", "jajaja"],
    "confused": ["?", "??", "???", "????", "?????", "huh", "what", "wtf", "wut", "hmm", "uhh", "🤨"],
    "hype": ["pog", "pogchamp", "poggers", "pogu", "letsgo", "lets", "hype", "insane", "goated", "clean",
             "🔥", "sheesh", "pagman", "ez", "holy", "omg"],
    "win": ["w", "ww", "www", "dub", "dubs"],
    "loss": ["l", "ll", "lll", "ratio", "f", "ff", "sadge", "rip", "pepehands"],
    "scared": ["monkas", "monkaw", "monkaomega", "😱", "scary", "jumpscare", "aaaa"],
    "clip": ["clip", "clipit", "clipped"],
}
_EMOTE_TOKENS = {t for k, v in REACTIONS.items() if k != "clip" for t in v}
_TOKEN_RE = re.compile(r"[\w']+|\?+|[^\w\s]", re.UNICODE)
_CLIP_RE = re.compile(r"\bclip(\s*(it|that|this))?\b|\bclipped\b", re.IGNORECASE)


def _tokens(msg: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(msg)]


def classify(msg: str) -> set[str]:
    toks = _tokens(msg)
    cats = set()
    if not toks:
        return cats
    ts = set(toks)
    for cat, words in REACTIONS.items():
        if cat == "confused":
            if msg.strip().startswith("?") or ts & {"huh", "wtf", "wut", "🤨"} or (msg.count("?") >= 2):
                cats.add(cat)
            continue
        if cat in ("win", "loss"):
            # single-letter spam only counts when the message is (nearly) just that letter
            if len(toks) <= 3 and ts & set(words):
                cats.add(cat)
            continue
        if ts & set(words) or any(e in msg for e in words if not e.isascii()):
            cats.add(cat)
    if _CLIP_RE.search(msg):
        cats.add("clip")
    return cats


def hype_weight(msg: str) -> float:
    toks = _tokens(msg)
    if not toks:
        return 0.0
    hits = sum(1 for t in toks if t in _EMOTE_TOKENS) + sum(msg.count(e) for e in ("😂", "🤣", "💀", "🔥"))
    letters = [c for c in msg if c.isalpha()]
    caps = (sum(c.isupper() for c in letters) / len(letters)) if len(letters) >= 4 else 0.0
    q = 1.0 if re.search(r"\?{3,}", msg) else 0.0
    return min(3.0, hits * 0.6 + caps * 0.8 + q)


def chat_signals(chat: list[tuple[float, str]], n: int, offset: float, baseline_window: int) -> dict[str, np.ndarray]:
    """Return raw (un-normalized) per-second series, already shifted back by the chat lag."""
    if len(chat) < 30:
        return {}
    times = np.array([t - offset for t, _ in chat])
    count = bin_events(times, n)
    base = rolling_baseline(count, baseline_window)
    velocity = (count - base) / np.sqrt(base + 1.0)          # Poisson-ish surprise vs local baseline
    emote = bin_events(times, n, [hype_weight(m) for _, m in chat])
    clip = bin_events(times, n, [1.0 if _CLIP_RE.search(m) else 0.0 for _, m in chat])
    return {"chat": velocity, "emote": emote, "clip_mentions": clip}


def dominant_reaction(chat: list[tuple[float, str]], start: float, end: float, offset: float,
                      min_count: int = 6, min_share: float = 0.2) -> tuple[str | None, Counter]:
    msgs = [m for t, m in chat if start <= t - offset <= end]
    counts: Counter = Counter()
    for m in msgs:
        for c in classify(m):
            counts[c] += 1
    if not msgs or not counts:
        return None, counts
    # "clip" is a strong label but a weak title — prefer an emotional category when one is close
    ranked = counts.most_common()
    cat, c = ranked[0]
    if cat == "clip" and len(ranked) > 1 and ranked[1][1] >= c * 0.5:
        cat, c = ranked[1]
    if c >= min_count and c / len(msgs) >= min_share:
        return cat, counts
    return None, counts
