"""Heuristic hook titles (no LLM): chat reaction → template, else shouted transcript phrase, else generic pool."""
from __future__ import annotations

import hashlib
import re

import numpy as np

from .signals.chat import dominant_reaction
from .signals.transcript import keyword_score

REACTION_TEMPLATES = {
    "laugh": ["chat lost it", "I can't breathe", "chat was CRYING", "this broke chat"],
    "confused": ["what just happened", "wait… WHAT?", "nobody saw that coming", "chat was so confused"],
    "hype": ["chat went FERAL", "this was INSANE", "the most hype moment", "absolute cinema"],
    "win": ["HUGE W", "biggest W of the stream", "W W W W W"],
    "loss": ["massive L", "this aged terribly", "pain. just pain"],
    "scared": ["this got scary", "heart attack moment", "he was NOT ready"],
    "clip": ["chat said CLIP IT", "the clip everyone wanted", "instant clip"],
}

GENERIC = {
    "audio": ["he did NOT expect that", "wait for it…", "the reaction", "he lost it", "this went left fast",
              "bro was NOT ready"],
    "chat": ["chat went crazy for this", "the moment chat exploded", "chat couldn't believe it", "chat had a field day",
             "chat will never let this go"],
    "emote": ["chat went crazy for this", "chat couldn't believe it", "chat spammed for this one",
              "the chat reaction", "chat was going OFF"],
    "clip_mentions": ["the clip everyone wanted", "chat said CLIP IT"],
    "heatmap": ["everyone rewatched this part", "the most replayed moment", "you have to see this"],
    "keywords": ["no way this happened", "he did NOT expect that", "wait for it…"],
    "speech": ["he couldn't stop talking about it", "the rant", "wait for it…"],
    "visual": ["watch closely…", "wait for it…", "how did this happen"],
}

# Mood → the emoji that ends every title. 🔥 = hype / skill / win, 😭 = funny / fail / chaos.
FIRE, CRY = "🔥", "😭"
CAT_MOOD = {"hype": FIRE, "win": FIRE, "clip": FIRE, "laugh": CRY, "loss": CRY, "confused": CRY, "scared": CRY}
CRY_WORDS = ("lmao", "lol", "haha", "dead", "why", "i can't", "help", "oh no", "noo", "wtf", "sorry", "bro what",
             "what the", "are you serious", "are you kidding", "stop", "please", "dying", "crying")
FIRE_WORDS = ("let's go", "lets go", "insane", "clutch", "holy", "no way", "oh my god", "crazy", "clean", "gg",
              "sheesh", "yes", "goated", "huge", "one tap", "headshot", "easy", "ez")
_EMOJI_END = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+\s*$")


def mood_for(cat_counts: dict | None, text: str = "", category: str | None = None) -> str:
    if category in CAT_MOOD:
        return CAT_MOOD[category]
    if cat_counts:
        cry = sum(cat_counts.get(k, 0) for k in ("laugh", "loss", "confused", "scared"))
        fire = sum(cat_counts.get(k, 0) for k in ("hype", "win", "clip"))
        if cry or fire:
            return CRY if cry > fire else FIRE
    t = f" {text.lower()} "
    c = sum(w in t for w in CRY_WORDS)
    f = sum(w in t for w in FIRE_WORDS)
    if c or f:
        return CRY if c > f else FIRE
    return FIRE


def with_emoji(title: str, mood: str) -> str:
    return f"{_EMOJI_END.sub('', title).rstrip()} {mood}"


SENT_END = (".", "!", "?", "…")
FILLER = {"yeah", "so", "um", "uh", "like", "and", "but", "okay", "ok", "well", "i mean", "oh", "just"}
BAD_END = {"my", "the", "a", "an", "to", "of", "and", "or", "but", "that", "this", "with", "for", "in",
           "on", "at", "your", "his", "her", "their", "our", "is", "was", "i", "it's", "im", "i'm", "so", "if",
           "because", "like", "gonna", "just", "we", "you", "he", "she", "they", "be", "are", "from"}


def _bare(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def _pick(options: list[str], seed: str, used: set[str] | None = None) -> str:
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    for k in range(len(options)):
        o = options[(h + k) % len(options)]
        if not used or o not in used and with_emoji(o, FIRE) not in used and with_emoji(o, CRY) not in used:
            return o
    return options[h % len(options)]


def _phrases(words: list[dict], min_gap: float = 0.35) -> list[list[dict]]:
    """Split into natural utterances (pause / sentence end / clause comma). No length cap here."""
    out, cur = [], []
    for i, w in enumerate(words):
        if cur and (w["s"] - cur[-1]["e"] > min_gap or cur[-1]["w"].endswith(SENT_END + (",",))):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def shouted_phrase(words: list[dict], loud_jump: np.ndarray | None) -> str | None:
    best, best_sc = None, 0.0
    # speech level baseline in this window: a 'shouted' line must be louder than normal talking
    base = 0.0
    if loud_jump is not None and len(loud_jump) and words:
        secs = sorted({int((w["s"] + w["e"]) / 2) for w in words if 0 <= int(w["s"]) < len(loud_jump)})
        if secs:
            base = float(np.median(loud_jump[secs]))
    for ph in _phrases(words):
        while ph and _bare(ph[0]["w"]) in FILLER:           # drop leading "yeah so um"
            ph = ph[1:]
        while ph and _bare(ph[-1]["w"]) in BAD_END:         # never end on "my" / "the" / "that"
            ph = ph[:-1]
        bare = [_bare(w["w"]) for w in ph]
        if not 3 <= len(ph) <= 7:                            # must be a complete 3–7 word utterance
            continue
        if len(set(bare)) < len(bare) * 0.6:                 # "yeah yeah yeah" stutter
            continue
        text = " ".join(w["w"] for w in ph)
        if np.mean([w.get("p", 1) for w in ph]) < 0.5:   # don't headline low-confidence ASR
            continue
        energy = 0.0
        if loud_jump is not None and len(loud_jump):
            a, b = int(ph[0]["s"]), int(np.ceil(ph[-1]["e"]))
            energy = float(np.mean(loud_jump[max(0, a): max(a + 1, b)])) - base
        kw = keyword_score(text)
        if energy < 3.0 and kw < 1.5:
            continue
        sc = max(0.0, energy) + 2.0 * kw + (2.0 if "!" in text else 0)
        if sc > best_sc:
            best, best_sc = text, sc
    if best is None or best_sc < 6.0:
        return None
    t = re.sub(r"[,;:.]+$", "", best.strip())
    t = re.sub(r",\s*", " ", t)
    return t.upper()


def make_title(clip_id: str, start: float, end: float, words: list[dict], chat: list[tuple[float, str]],
               chat_offset: float, loud_jump: np.ndarray | None, breakdown: dict[str, float],
               used: set[str] | None = None) -> tuple[str, str, str]:
    """Return (title ending in 🔥/😭, strategy, mood). `used` = titles already used in this chunk (no repeats)."""
    used = used if used is not None else set()
    counts: dict = {}
    if chat:
        cat, counts = dominant_reaction(chat, start, end, chat_offset)
        if cat and any(o not in used for o in REACTION_TEMPLATES[cat]):
            mood = mood_for(counts, "", cat)
            return with_emoji(_pick(REACTION_TEMPLATES[cat], clip_id, used), mood), f"chat:{cat}", mood
    spoken = " ".join(w["w"] for w in words)
    ph = shouted_phrase(words, loud_jump)
    if ph and ph not in used and with_emoji(ph, FIRE) not in used and with_emoji(ph, CRY) not in used:
        mood = mood_for(counts, ph + " " + spoken)
        return with_emoji(ph, mood), "transcript", mood
    top = max(breakdown, key=breakdown.get) if breakdown else "audio"
    pool = GENERIC.get(top, GENERIC["audio"])
    if all(o in used for o in pool):
        pool = [o for k, v in GENERIC.items() if k not in ("heatmap", "clip_mentions") for o in v]
    mood = mood_for(counts, spoken)
    return with_emoji(_pick(pool, clip_id, used), mood), f"generic:{top}", mood
