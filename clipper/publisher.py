"""Instagram auto-poster.

* Manual: click "Post ▸ IG" on a clip → queued → published as soon as the poster is free.
* Campaigns: "post N clips from folder X, most viral first". Several can run at once; they take turns.
* Global pacing: one post every `interval` seconds (default 2 min) and at most `daily_cap` posts per day
  (default 70, well under Instagram's 100 / 24h API limit). "Sent today" resets at local midnight.
* Brand-campaign clips flagged for review are never auto-posted; campaign posts that must be submitted to the
  brand within N minutes are listed with a countdown.

Robustness: state persists in out/_publish.json; every post has a hard deadline; network errors don't count
against a clip; an interrupted post is checked against the account's recent Reels so nothing is posted twice;
if Instagram pushes back (rate limit / spam block) all campaigns pause themselves.
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from pathlib import Path

from .config import Config
from .utils import log, read_json, write_json

BLOCK_HINTS = ("rate limit", "too many", "spam", "blocked", "action is blocked", "try again later",
               "code 4,", "code 9,", "code 17,", "code 32,", "code 613,", "36003")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


class Publisher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.path(cfg.out_dir)
        self.state_p = self.root / "_publish.json"
        self.lock = threading.RLock()
        self.wake = threading.Event()
        st = read_json(self.state_p) if self.state_p.exists() else {}
        st.setdefault("manual", [])
        st.setdefault("history", [])
        st.setdefault("settings", {"interval": 120, "daily_cap": 70})
        st.setdefault("submitted", [])
        if "campaigns" not in st:                     # migrate the old single-campaign format
            old = st.pop("campaign", None)
            st["campaigns"] = []
            if old:
                old.setdefault("id", uuid.uuid4().hex[:6])
                old.pop("interval", None)
                st["campaigns"].append(old)
        crashed = st.get("current")
        st["current"] = None
        self.st = st
        self.net_down_until = 0.0
        self._save()
        self._recover(crashed)

    # ------------------------------------------------------------------ state helpers
    def _save(self) -> None:
        write_json(self.state_p, self.st)

    def _jp(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def sent_today(self) -> int:
        today = _today()
        return sum(1 for h in self.st["history"] if h.get("ok") and time.strftime("%Y-%m-%d", time.localtime(h["at"])) == today)

    def last_post_at(self) -> float:
        return max((h["at"] for h in self.st["history"] if h.get("ok")), default=0.0)

    def _recover(self, crashed: dict | None) -> None:
        recent = None
        for jp in self.root.glob("*/chunk_*/clip_[0-9][0-9].json"):
            try:
                r = read_json(jp)
            except Exception:
                continue
            if r.get("post_state") not in ("posting", "queued"):
                continue
            key = str(jp.relative_to(self.root).with_suffix(""))
            if r["post_state"] == "posting":
                if recent is None:
                    try:
                        from . import instagram as ig
                        recent = ig.recent_captions(20)
                    except Exception:
                        recent = []
                hit = next((m for m in recent if (m.get("caption") or "").split("\n")[0].strip() == r["title"].strip()), None)
                if hit:
                    r.setdefault("posts", {})["instagram"] = {"media_id": hit["id"], "permalink": hit.get("permalink", ""),
                                                              "at": time.strftime("%Y-%m-%d %H:%M"), "recovered": True}
            r.pop("post_state", None)
            write_json(jp, r)
        for key in list(self.st["manual"]):
            jp = self._jp(key)
            if not jp.exists():
                self.st["manual"].remove(key)
                continue
            r = read_json(jp)
            if r.get("posts", {}).get("instagram"):
                self.st["manual"].remove(key)
                continue
            r["post_state"] = "queued"
            write_json(jp, r)
        self._save()

    # ------------------------------------------------------------------ api
    def snapshot(self) -> dict:
        with self.lock:
            s = self.st["settings"]
            nxt = max(0, int(self.last_post_at() + s["interval"] - time.time()))
            camps = []
            for c in self.st["campaigns"]:
                d = dict(c)
                d["remaining"] = max(0, c["target"] - c["posted"])
                d["queue_size"] = len(self._candidates(c["video_id"]))
                camps.append(d)
            now = time.time()
            subs = []
            for h in reversed(self.st["history"]):
                if not h.get("ok") or not h.get("submit_by"):
                    continue
                if h["key"] in self.st["submitted"] or now - h["at"] > 6 * 3600:
                    continue
                subs.append({"key": h["key"], "title": h["title"], "permalink": h["permalink"],
                             "left": int(h["submit_by"] - now), "campaign": h.get("campaign")})
            return {"manual": list(self.st["manual"]), "current": self.st.get("current"),
                    "history": self.st["history"][-25:], "campaigns": camps, "settings": dict(s),
                    "sent_today": self.sent_today(), "next_in": nxt,
                    "net_down": self.net_down_until > time.time(), "submissions": subs[:20]}

    def enqueue(self, key: str) -> None:
        jp = self._jp(key)
        rec = read_json(jp)
        if rec.get("posts", {}).get("instagram"):
            return
        with self.lock:
            if self.sent_today() >= self.st["settings"]["daily_cap"]:
                raise RuntimeError(f"daily cap reached ({self.st['settings']['daily_cap']} posts today)")
            if key not in self.st["manual"]:
                self.st["manual"].append(key)
                rec["post_state"] = "queued"
                rec.pop("post_error", None)
                write_json(jp, rec)
                self._save()
        self.wake.set()

    def start_campaign(self, video_id: str, target: int = 50) -> dict:
        with self.lock:
            c = next((c for c in self.st["campaigns"] if c["video_id"] == video_id and c["posted"] < c["target"]), None)
            if c:
                c.update(target=int(target), active=True, paused_reason=None)
            else:
                c = {"id": uuid.uuid4().hex[:6], "video_id": video_id, "target": int(target), "posted": 0,
                     "active": True, "started": time.time(), "paused_reason": None, "last_at": 0}
                self.st["campaigns"].append(c)
            self._save()
        self.wake.set()
        return c

    def campaign_op(self, cid: str, op: str) -> None:
        with self.lock:
            c = next((c for c in self.st["campaigns"] if c["id"] == cid), None)
            if not c:
                return
            if op == "pause":
                c.update(active=False, paused_reason="paused by you")
            elif op == "resume":
                c.update(active=True, paused_reason=None)
            elif op == "stop":
                self.st["campaigns"].remove(c)
            self._save()
        self.wake.set()

    def settings(self, interval: float | None = None, daily_cap: int | None = None) -> None:
        with self.lock:
            if interval:
                self.st["settings"]["interval"] = max(60, int(interval))
            if daily_cap:
                self.st["settings"]["daily_cap"] = max(1, min(95, int(daily_cap)))
            self._save()

    def mark_submitted(self, key: str) -> None:
        with self.lock:
            if key not in self.st["submitted"]:
                self.st["submitted"].append(key)
                self.st["submitted"] = self.st["submitted"][-500:]
                self._save()

    # ------------------------------------------------------------------ worker
    def start(self) -> None:
        threading.Thread(target=self._loop, name="ig-publisher", daemon=True).start()

    def _candidates(self, video_id: str) -> list[tuple[float, str]]:
        out = []
        for jp in (self.root / video_id).glob("chunk_*/clip_[0-9][0-9].json"):
            try:
                r = read_json(jp)
            except Exception:
                continue
            if r.get("posts", {}).get("instagram") or r.get("rendering") or r.get("status") == "rejected":
                continue
            if r.get("post_state") in ("posting", "queued") or r.get("post_error_count", 0) >= 2:
                continue
            camp = r.get("campaign") or {}
            if camp.get("flag"):
                continue                                   # brand asked to review these first
            if not (jp.parent / r["output"]).exists():
                continue
            out.append((float(r.get("score", 0)), str(jp.relative_to(self.root).with_suffix(""))))
        return sorted(out, reverse=True)

    def _loop(self) -> None:
        while True:
            try:
                did = self._tick()
            except Exception as e:
                log(f"publisher error: {e}")
                did = False
            if not did:
                self.wake.wait(5)
                self.wake.clear()

    def _tick(self) -> bool:
        if time.time() < self.net_down_until:
            return False
        s = self.st["settings"]
        with self.lock:
            key = self.st["manual"].pop(0) if self.st["manual"] else None
            if key:
                self._save()
        if key:
            self._post(key, manual=True, campaign=None)
            return True
        if self.sent_today() >= s["daily_cap"]:
            return False
        if time.time() < self.last_post_at() + s["interval"]:
            return False
        with self.lock:   # campaigns take turns: the one that posted longest ago goes next
            ready = [c for c in self.st["campaigns"] if c.get("active") and c["posted"] < c["target"]]
            ready.sort(key=lambda c: c.get("last_at", 0))
        for c in ready:
            cands = self._candidates(c["video_id"])
            if not cands:
                continue
            ok = self._post(cands[0][1], manual=False, campaign=c["id"])
            with self.lock:
                cc = next((x for x in self.st["campaigns"] if x["id"] == c["id"]), None)
                if cc:
                    cc["last_at"] = time.time()
                    if ok:
                        cc["posted"] += 1
                        if cc["posted"] >= cc["target"]:
                            cc.update(active=False, paused_reason="done")
                    self._save()
            return True
        return False

    def _post(self, key: str, manual: bool, campaign: str | None) -> bool:
        from . import instagram as ig
        from .tags import instagram_caption
        jp = self._jp(key)
        rec = read_json(jp)
        if rec.get("posts", {}).get("instagram"):
            return True
        rec["post_state"] = "posting"
        write_json(jp, rec)
        with self.lock:
            self.st["current"] = {"key": key, "title": rec["title"], "stage": "starting", "since": time.time()}
            self._save()

        def prog(s: str) -> None:
            with self.lock:
                if self.st.get("current"):
                    self.st["current"]["stage"] = s

        try:
            prev = (rec.get("post_error") or "").lower()
            if "network" in prev or "timed out" in prev:
                hit = next((m for m in ig.recent_captions(20)
                            if (m.get("caption") or "").split("\n")[0].strip() == rec["title"].strip()), None)
                if hit:
                    rec = read_json(jp)
                    rec.setdefault("posts", {})["instagram"] = {"media_id": hit["id"], "permalink": hit.get("permalink", ""),
                                                                "at": time.strftime("%Y-%m-%d %H:%M"), "recovered": True}
                    rec.pop("post_state", None)
                    rec.pop("post_error", None)
                    write_json(jp, rec)
                    return True
            lim = ig.publishing_limit()
            used, total = lim.get("quota_usage", 0), (lim.get("config") or {}).get("quota_total", 100)
            if used >= total - 2:
                raise ig.IGError(f"rate limit: {used}/{total} posts used in the last 24h")
            caption = instagram_caption(rec, self.cfg)
            peaks = [t - rec["start"] for t, _ in rec.get("sub_peaks", []) if rec["start"] <= t <= rec["end"]]
            thumb_ms = int(1000 * (peaks[0] if peaks else (rec["end"] - rec["start"]) * 0.4))
            box: dict = {}

            def work():
                try:
                    box["res"] = ig.publish_reel(jp.parent / rec["output"], caption, progress=prog, thumb_ms=thumb_ms)
                except BaseException as e:  # noqa: BLE001
                    box["err"] = e
            t = threading.Thread(target=work, daemon=True)
            t.start()
            t.join(15 * 60)
            if t.is_alive():
                raise ig.NetError("post timed out (network stalled — laptop asleep?)")
            if "err" in box:
                raise box["err"]
            res = box["res"]
            rec = read_json(jp)
            rec.setdefault("posts", {})["instagram"] = {**res, "at": time.strftime("%Y-%m-%d %H:%M")}
            rec.pop("post_state", None)
            rec.pop("post_error", None)
            write_json(jp, rec)
            log(f"posted to Instagram: {rec['title']} → {res['permalink']}")
            entry = {"key": key, "title": rec["title"], "permalink": res["permalink"], "at": time.time(),
                     "ok": True, "manual": manual, "campaign_run": campaign}
            brand = rec.get("campaign") or {}
            if brand:
                from .campaign import load
                bc = load(brand.get("slug"))
                entry["campaign"] = brand.get("name")
                if bc and bc.submit_within_min:
                    entry["submit_by"] = time.time() + bc.submit_within_min * 60
            with self.lock:
                self.st["history"].append(entry)
            return True
        except Exception as e:
            msg = str(e)[:300]
            rec = read_json(jp)
            if isinstance(e, ig.NetError):
                rec.pop("post_state", None)
                rec["post_error"] = msg
                write_json(jp, rec)
                self.net_down_until = time.time() + 60
                log(f"Instagram unreachable ({msg}); retrying in 60s")
                with self.lock:
                    if manual and key not in self.st["manual"]:
                        self.st["manual"].insert(0, key)
                        rec["post_state"] = "queued"
                        write_json(jp, rec)
                    self.st["history"].append({"key": key, "title": rec["title"], "error": msg, "at": time.time(),
                                               "ok": False, "manual": manual, "net": True})
                return False
            rec["post_state"] = "failed"
            rec["post_error"] = msg
            rec["post_error_count"] = rec.get("post_error_count", 0) + 1
            write_json(jp, rec)
            log(f"Instagram post failed for {key}: {msg}")
            with self.lock:
                self.st["history"].append({"key": key, "title": rec["title"], "error": msg, "at": time.time(),
                                           "ok": False, "manual": manual})
                if any(h in msg.lower() for h in BLOCK_HINTS):
                    for c in self.st["campaigns"]:
                        if c.get("active"):
                            c.update(active=False, paused_reason=f"Instagram pushed back: {msg[:100]}")
            return False
        finally:
            with self.lock:
                self.st["current"] = None
                self.st["history"] = self.st["history"][-1000:]
                self._save()
