"""Captions + hashtags for publishing (Instagram Reels / YouTube Shorts).

Hashtag strategy — few and relevant beats many and generic:
  * Instagram: max 5 (Instagram caps/down-ranks hashtag walls; Meta recommends 3–5 relevant tags)
  * YouTube Shorts: 5 in the description; the first 3 show above the title; #shorts first
Layered from niche → broad:
  streamer (fans search it) → game/category → clips community → mood (funny / gaming / hype)
"""
from __future__ import annotations

import hashlib
import re

from .config import Config

GAME_TAGS = {
    "just chatting": ["justchatting", "irl"],
    "irl": ["irl", "justchatting"],
    "genshin impact": ["genshinimpact", "genshin"],
    "honkai: star rail": ["honkaistarrail", "hsr"],
    "zenless zone zero": ["zzz", "zenlesszonezero"],
    "fortnite": ["fortnite", "fortniteclips"],
    "valorant": ["valorant", "valorantclips"],
    "league of legends": ["leagueoflegends", "lol"],
    "counter-strike": ["cs2", "counterstrike"],
    "counter-strike 2": ["cs2", "counterstrike"],
    "minecraft": ["minecraft", "minecraftmemes"],
    "grand theft auto v": ["gta5", "gtarp"],
    "call of duty: warzone": ["warzone", "callofduty"],
    "apex legends": ["apexlegends", "apex"],
    "marvel rivals": ["marvelrivals", "marvel"],
    "overwatch 2": ["overwatch2", "overwatch"],
    "rocket league": ["rocketleague", "rl"],
    "roblox": ["roblox", "robloxfunny"],
    "chess": ["chess", "chessclips"],
    "music": ["music", "musician"],
    "sports": ["sports", "sportsclips"],
}
NON_GAME = {"just chatting", "irl", "music", "sports", "talk shows & podcasts", "art", "special events"}

IG_CTAS = ["Follow {me} for daily stream clips 🍬", "Follow {me} for more 🍬", "More clips every day → {me} 🍬",
           "Daily clips at {me} 🍬"]
YT_CTAS = ["Subscribe for daily stream clips 🍬", "Subscribe for more clips every day 🍬",
           "New clips every day — subscribe 🍬"]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (s or "").lower())


def _pick(options: list[str], seed: str) -> str:
    return options[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(options)]


def _cfg_get(cfg: Config | None, *path, default=None):
    cur = getattr(cfg, "publish", None) if cfg else None
    for k in path:
        if cur is None:
            return default
        cur = cur.get(k) if isinstance(cur, dict) else getattr(cur, k, None)
    return default if cur is None else cur


def hashtags(rec: dict, platform: str, cfg: Config | None = None) -> list[str]:
    src = rec.get("source", {})
    who = slug(src.get("uploader"))
    game = (src.get("game") or "").strip()
    g = game.lower()
    mood = rec.get("mood") or ("😭" if "😭" in rec.get("title", "") else "🔥")
    cat = GAME_TAGS.get(g, [slug(game)] if game else [])
    is_game = bool(game) and g not in NON_GAME
    mood_tag = "funny" if mood == "😭" else ("gaming" if is_game else "streamer")
    community = "twitchclips" if src.get("platform") == "twitch" else "streamerclips"
    extra = [slug(t) for t in (_cfg_get(cfg, "extra_tags", src.get("uploader", "").lower(), default=[]) or [])]

    order = ([who] if who else []) + extra + cat[:1] + [community, mood_tag, "streamerclips", "clips"]
    if platform == "youtube":
        order = ["shorts"] + order
    out: list[str] = []
    for t in order:
        if t and t not in out:
            out.append(t)
    return ["#" + t for t in out[:5]]


def credit(rec: dict, platform: str, cfg: Config | None = None) -> str:
    src = rec.get("source", {})
    who = src.get("uploader") or ""
    if not who:
        return ""
    plat = src.get("platform")
    handle = _cfg_get(cfg, "streamer_handles", who.lower(), platform, default=None)
    if plat == "twitch":
        where = f"twitch.tv/{who.lower()}"
    elif plat == "youtube":
        where = "YouTube"
    else:
        where = ""
    name = handle or who
    return f"🎥 {name}" + (f" on {where}" if where else "")


def instagram_caption(rec: dict, cfg: Config | None = None) -> str:
    me = _cfg_get(cfg, "instagram_handle", default="@gummybearclips")
    cta = _pick(IG_CTAS, rec.get("key", rec["title"])).format(me=me)
    parts = [rec["title"], "", credit(rec, "instagram", cfg), cta, "", " ".join(hashtags(rec, "instagram", cfg))]
    return "\n".join(parts).strip()


def youtube_title(rec: dict) -> str:
    t = rec["title"]
    who = rec.get("source", {}).get("uploader")
    full = f"{t} | {who}" if who else t
    return full if len(full) <= 100 else t[:100]


def youtube_description(rec: dict, cfg: Config | None = None) -> str:
    cta = _pick(YT_CTAS, rec.get("key", rec["title"]))
    parts = [rec["title"], "", credit(rec, "youtube", cfg), cta, "", " ".join(hashtags(rec, "youtube", cfg))]
    return "\n".join(parts).strip()


def post_sheet(rec: dict, cfg: Config | None = None) -> str:
    """The per-clip .txt that sits next to the .mp4 in the export folder."""
    src = rec.get("source", {})
    return "\n".join([
        "━━━━━━━━ INSTAGRAM REEL ━━━━━━━━",
        instagram_caption(rec, cfg),
        "",
        "━━━━━━━━ YOUTUBE SHORT ━━━━━━━━",
        "TITLE:",
        youtube_title(rec),
        "",
        "DESCRIPTION:",
        youtube_description(rec, cfg),
        "",
        "━━━━━━━━ INFO ━━━━━━━━",
        f"stream:  {src.get('title', '')}",
        f"moment:  {rec.get('source_link', '')}",
        f"time {rec.get('timestamp')} · {rec.get('duration', 0):.0f}s · score {rec.get('score', 0):.2f}"
        f" · {src.get('game') or 'unknown category'}",
        "",
    ])
