"""Brand campaign briefs (campaigns/<slug>.yaml).

A brief turns client requirements into hard rules for the clipper:
  priorities  → forced candidate moments with brief-accurate hooks
  exclude     → time ranges no clip may touch (windows are trimmed around them, or dropped)
  review      → clips touching these are flagged and never auto-posted
  keywords    → every clip must mention the campaign in its dialogue (or be a priority moment)
  emoji / hashtags / disclosure / creator / music → captions + render settings

Times in a brief refer to the full source video; chunks convert them with their offset.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import ROOT

CAMPAIGN_DIR = ROOT / "campaigns"


def ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    parts = [float(p) for p in str(v).strip().split(":")]
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


@dataclass
class Zone:
    start: float
    end: float
    reason: str


@dataclass
class Campaign:
    slug: str
    name: str
    creator: str = ""
    emoji: str = "💎"
    disclosure: str = ""
    hashtags: dict = field(default_factory=dict)
    banned: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=lambda: ["instagram", "tiktok"])
    submit_within_min: int = 0
    weak_keywords: list[str] = field(default_factory=lambda: ["botswana", "future"])
    music: bool = False
    review_before_post: bool = False
    require_keywords: bool = True
    keywords: list[str] = field(default_factory=list)
    priorities: list[dict] = field(default_factory=list)     # {t, hook}
    exclude: list[Zone] = field(default_factory=list)
    review: list[Zone] = field(default_factory=list)

    def times_apply(self, total_duration: float) -> bool:
        """Brief timestamps describe one specific (long) video. Shorter sources — e.g. official cutdowns — only get
        the keyword / banned-word / hashtag rules, not the time-based ones."""
        stamps = [p["t"] for p in self.priorities] + [z.end for z in self.exclude + self.review]
        return bool(stamps) and total_duration >= 0.9 * max(stamps)

    def without_times(self) -> "Campaign":
        import copy
        c = copy.copy(self)
        c.priorities, c.exclude, c.review = [], [], []
        return c

    # ---------------------------------------------------------------- helpers (absolute times)
    def blocked(self, a: float, b: float) -> Zone | None:
        return next((z for z in self.exclude if z.start < b and z.end > a), None)

    def flagged(self, a: float, b: float) -> Zone | None:
        return next((z for z in self.review if z.start < b and z.end > a), None)

    def priority_in(self, a: float, b: float) -> dict | None:
        return next((p for p in self.priorities if a - 2 <= p["t"] <= b + 2), None)

    def mentions(self, text: str) -> list[str]:
        t = f" {re.sub(r'[^a-z0-9 ]+', ' ', text.lower())} "
        return [k for k in self.keywords if f" {k.lower()} " in t]

    def strong_mentions(self, text: str) -> list[str]:
        """Keywords that actually carry the message (a bare 'Botswana' doesn't)."""
        return [k for k in self.mentions(text) if k not in self.weak_keywords]

    def has_banned(self, text: str) -> str | None:
        t = text.lower()
        return next((b for b in self.banned if b in t), None)

    def to_json(self) -> dict:
        return {"slug": self.slug, "name": self.name}

    # ---------------------------------------------------------------- hooks
    def title(self, meta: dict, words: list[dict], source_title: str, whole: bool,
              used: set[str] | None = None) -> str:
        """Accurate hook: the brief's own wording for priority moments, the source title for official cutdowns,
        else an exact quote containing campaign keywords, else a plain descriptive line naming Marlon."""
        if meta.get("priority"):
            return f"{meta['priority']} {self.emoji}"
        if whole:
            t = _clean_source_title(source_title)
            if len(t.split()) >= 4 and not self.has_banned(t):
                return f"{t} {self.emoji}"
        used = used or set()
        q = self.quote(words)
        if q and f"“{q}” {self.emoji}" not in used:
            return f"“{q}” {self.emoji}"
        hits = set(meta.get("keywords") or [])
        name = self.creator or "Marlon"
        pools = []
        if hits & {"diamond", "diamonds", "mine", "mines", "mining", "debswana"}:
            pools.append([f"{name} learns where natural diamonds come from", f"{name} on Botswana's natural diamond story",
                          f"The natural diamond story, through {name}'s trip", f"{name} in Botswana, where natural diamonds come from"])
        if "university" in hits:
            pools.append([f"{name} visits a university in Botswana", f"Inside a Botswana university with {name}"])
        if hits & {"education", "school", "schools", "student", "students", "scholarship", "scholarships", "tuition", "educated"}:
            pools.append([f"{name} meets students in Botswana", f"Education in Botswana, through {name}'s eyes",
                          f"{name} talks to students in Botswana"])
        if hits & {"wildlife", "safari", "elephant", "elephants", "okavango", "conservation"}:
            pools.append([f"{name} on safari in Botswana", f"Botswana's wildlife with {name}", f"{name}'s Botswana safari",
                          f"Out on safari with {name} in Botswana", f"{name} meets Botswana's wildlife"])
        if hits & {"community", "communities", "development", "develop", "developed", "economy", "economic", "jobs",
                   "opportunity", "opportunities", "infrastructure", "road", "roads", "hospital", "hospitals"}:
            pools.append([f"{name} sees Botswana's development up close", f"{name} on what's changing in Botswana"])
        pools.append([f"{name}'s trip to Botswana", f"{name}'s Botswana travel diary", f"Botswana, through {name}'s eyes"])
        for pool in pools:
            for base in pool:
                if f"{base} {self.emoji}" not in used:
                    return f"{base} {self.emoji}"
        return f"{pools[0][0]} {self.emoji}"

    def quote(self, words: list[dict]) -> str | None:
        """Best verbatim line (4–11 words) that mentions the campaign, preferring 'diamond'."""
        phrases, cur = [], []
        for w in words:
            if cur and (w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"].endswith((".", "!", "?"))):
                phrases.append(cur)
                cur = []
            cur.append(w)
        if cur:
            phrases.append(cur)
        best, best_sc = None, 0.0
        for ph in phrases:
            text = " ".join(x["w"] for x in ph).strip()
            n = len(text.split())
            if not 4 <= n <= 11 or self.has_banned(text):
                continue
            if sum(x.get("p", 1) for x in ph) / len(ph) < 0.6:      # don't quote shaky transcription
                continue
            hits = self.mentions(text)
            if not hits:
                continue
            sc = len(hits) + (3 if any(h.startswith("diamond") for h in hits) else 0) + (1 if 6 <= n <= 9 else 0)
            if sc > best_sc:
                best, best_sc = text, sc
        if not best:
            return None
        best = re.sub(r"[,;:]+$", "", best).rstrip(".")
        return best[0].upper() + best[1:]


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\U0001F1E6-\U0001F1FF]+")


def _clean_source_title(t: str) -> str:
    t = _EMOJI.sub(" ", t or "")
    if "__" in t:                         # "Short title __ Longer descriptive title" → the descriptive part
        t = t.split("__")[-1]
    t = re.sub(r"#\w+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -|_.")


def _zones(items: list[dict]) -> list[Zone]:
    out = []
    for z in items or []:
        pad = float(z.get("pad", 0))
        if "at" in z:
            a = b = ts(z["at"])
        else:
            a, b = ts(z["from"]), ts(z["to"])
        out.append(Zone(max(0.0, a - pad), b + pad, z.get("reason", "")))
    return out


def load(slug: str | None) -> Campaign | None:
    if not slug:
        return None
    p = CAMPAIGN_DIR / f"{slug}.yaml"
    if not p.exists():
        return None
    d = yaml.safe_load(p.read_text()) or {}
    return Campaign(
        slug=slug, name=d.get("name", slug), creator=d.get("creator", ""), emoji=d.get("emoji", "💎"),
        disclosure=d.get("disclosure", ""), hashtags=dict(d.get("hashtags") or {}), music=bool(d.get("music", False)),
        banned=[b.lower() for b in d.get("banned", [])], platforms=list(d.get("platforms", ["instagram", "tiktok"])),
        submit_within_min=int(d.get("submit_within_min", 0)),
        weak_keywords=[k.lower() for k in d.get("weak_keywords", ["botswana", "future"])],
        review_before_post=bool(d.get("review_before_post", False)),
        require_keywords=bool(d.get("require_keywords", True)), keywords=[k.lower() for k in d.get("keywords", [])],
        priorities=[{"t": ts(p["at"]), "hook": p["hook"]} for p in d.get("priorities", [])],
        exclude=_zones(d.get("exclude")), review=_zones(d.get("review")),
    )


def available() -> list[dict]:
    out = []
    for p in sorted(CAMPAIGN_DIR.glob("*.yaml")):
        try:
            d = yaml.safe_load(p.read_text()) or {}
            out.append({"slug": p.stem, "name": d.get("name", p.stem)})
        except Exception:
            pass
    return out


def priority_window(c: Campaign, t: float, min_len: float, max_len: float, total: float) -> tuple[float, float] | None:
    """Window for a key moment: start just before it, run forward ~25s, staying inside the gap between
    excluded zones (e.g. licence plates either side)."""
    a = max(0.0, t - 1.5)
    z = c.blocked(a, a + 0.01)
    if z:
        a = z.end + 0.2
    nxt = min((z.start for z in c.exclude if z.start > a), default=total)
    b = min(a + 25.0, nxt - 0.2, total, a + max_len)
    if b - a < min_len:                     # gap too small going forward → try ending at the moment instead
        prev = max((z.end for z in c.exclude if z.end <= t), default=0.0)
        a2 = max(prev + 0.2, t + 8 - 25.0)
        b2 = min(t + 8, nxt - 0.2, total)
        if b2 - a2 >= min_len and not c.blocked(a2, b2):
            return a2, b2
        return None
    return a, b


def fit_window(c: Campaign, a: float, b: float, min_len: float) -> tuple[float, float] | None:
    """Trim [a, b] (absolute seconds) so it doesn't touch an excluded zone; None if it can't stay ≥ min_len."""
    for _ in range(6):
        z = c.blocked(a, b)
        if not z:
            return a, b
        left = (a, min(b, z.start - 0.2))       # keep the part before the zone
        right = (max(a, z.end + 0.2), b)        # or after it
        best = max((left, right), key=lambda w: w[1] - w[0])
        if best[1] - best[0] < min_len:
            return None
        a, b = best
    return None


# ====================================================================== UI editing
# The dashboard edits campaigns through a simple form. Timestamp lists are plain text, one item per line, so a
# brief can be pasted straight in:
#   priorities:  "01:30 – How diamonds helped transform Botswana"
#   exclude:     "00:47-00:53 must not be included" · "Licence plates: 03:13, 03:58" · "29:39 alcohol ±20s"
_T = r"\d{1,2}:\d{2}(?::\d{2})?"
_DEFAULT_PAD = [("plate", 4), ("logo", 4), ("brand", 4), ("music", 20), ("song", 20), ("war", 15), ("sensitive", 20),
                ("alcohol", 20), ("drink", 20), ("meat", 20), ("cultur", 20), ("review", 15), ("flag", 15)]


def _fmt(sec: float) -> str:
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _guess_pad(reason: str) -> float:
    r = reason.lower()
    return next((p for k, p in _DEFAULT_PAD if k in r), 5)


def parse_priorities(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        m = re.match(rf"\s*({_T})\s*[-–—:|]*\s*(.*)$", line)
        if m and m.group(2).strip():
            out.append({"at": m.group(1), "hook": m.group(2).strip()})
    return out


def parse_zones(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        pad_m = re.search(r"±\s*(\d+(?:\.\d+)?)\s*s", line)
        line_np = re.sub(r"±\s*\d+(?:\.\d+)?\s*s", "", line).strip()
        rng = re.match(rf"({_T})\s*[-–—]\s*({_T})\s*(.*)$", line_np)
        if rng:
            reason = rng.group(3).strip(" -–—:()") or "excluded"
            out.append({"from": rng.group(1), "to": rng.group(2), "reason": reason,
                        "pad": float(pad_m.group(1)) if pad_m else 2})
            continue
        times = re.findall(_T, line_np)
        if not times:
            continue
        label = re.split(rf"{_T}", line_np)[0].strip(" -–—:,(") if re.match(r"\s*[A-Za-z]", line_np) else ""
        rest = re.sub(rf"({_T}|[,()])", " ", line_np)
        reason = (label or re.sub(r"\s+", " ", rest).strip(" -–—:")) or "excluded"
        for t in times:
            out.append({"at": t, "reason": reason, "pad": float(pad_m.group(1)) if pad_m else _guess_pad(reason)})
    return out


def zones_text(items: list[dict]) -> str:
    lines = []
    for z in items or []:
        pad = f" ±{z['pad']:g}s" if z.get("pad") is not None else ""
        if "at" in z:
            lines.append(f"{z['at']} {z.get('reason', '')}{pad}".strip())
        else:
            lines.append(f"{z['from']}-{z['to']} {z.get('reason', '')}{pad}".strip())
    return "\n".join(lines)


def _csv(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip().lstrip("#") for x in v if str(x).strip()]
    return [x.strip().lstrip("#") for x in re.split(r"[,\s]+", v or "") if x.strip()]


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "campaign").lower()).strip("-")[:40] or "campaign"


def read_raw(slug: str) -> dict:
    p = CAMPAIGN_DIR / f"{slug}.yaml"
    return yaml.safe_load(p.read_text()) or {} if p.exists() else {}


def to_form(slug: str) -> dict:
    d = read_raw(slug)
    h = d.get("hashtags") or {}
    if isinstance(h, list):
        h = {"required": h}
    return {
        "slug": slug, "name": d.get("name", slug), "creator": d.get("creator", ""), "emoji": d.get("emoji", "💎"),
        "disclosure": d.get("disclosure", ""), "music": bool(d.get("music", False)),
        "require_keywords": bool(d.get("require_keywords", True)),
        "review_before_post": bool(d.get("review_before_post", False)),
        "submit_within_min": int(d.get("submit_within_min", 0) or 0),
        "platforms": ", ".join(d.get("platforms", ["instagram", "tiktok"])),
        "required": ", ".join("#" + t for t in h.get("required", [])),
        "owned": ", ".join("#" + t for t in h.get("owned", [])),
        "discovery": ", ".join("#" + t for t in h.get("discovery", [])),
        "owned_per_post": int(h.get("owned_per_post", 2)), "discovery_per_post": int(h.get("discovery_per_post", 1)),
        "keywords": ", ".join(d.get("keywords", [])), "banned": ", ".join(d.get("banned", [])),
        "priorities": "\n".join(f"{p['at']} – {p['hook']}" for p in d.get("priorities", [])),
        "exclude": zones_text(d.get("exclude", [])), "review": zones_text(d.get("review", [])),
    }


def save_form(f: dict) -> str:
    slug = f.get("slug") or slugify(f.get("name", ""))
    d = {
        "name": f.get("name") or slug, "creator": f.get("creator", ""), "emoji": f.get("emoji") or "💎",
        "disclosure": (f.get("disclosure") or "").strip(),
        "platforms": [p.strip().lower() for p in re.split(r"[,\s]+", f.get("platforms") or "instagram, tiktok") if p.strip()],
        "submit_within_min": int(f.get("submit_within_min") or 0),
        "music": bool(f.get("music")), "require_keywords": bool(f.get("require_keywords")),
        "review_before_post": bool(f.get("review_before_post")),
        "hashtags": {"required": _csv(f.get("required")), "owned": _csv(f.get("owned")),
                     "discovery": _csv(f.get("discovery")),
                     "owned_per_post": int(f.get("owned_per_post") or 2),
                     "discovery_per_post": int(f.get("discovery_per_post") or 1)},
        "keywords": [k.strip().lower() for k in re.split(r",", f.get("keywords") or "") if k.strip()],
        "banned": [k.strip().lower() for k in re.split(r",", f.get("banned") or "") if k.strip()],
        "priorities": parse_priorities(f.get("priorities", "")),
        "exclude": parse_zones(f.get("exclude", "")), "review": parse_zones(f.get("review", "")),
    }
    CAMPAIGN_DIR.mkdir(parents=True, exist_ok=True)
    (CAMPAIGN_DIR / f"{slug}.yaml").write_text(
        f"# Edited in the auto-clipper dashboard\n" + yaml.safe_dump(d, sort_keys=False, allow_unicode=True, width=120))
    return slug
