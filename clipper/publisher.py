"""Instagram auto-poster.

* Manual: click "Post" on a clip → queued → published as soon as the poster is free.
* Campaign: "post N clips from video X every M minutes, most viral (highest score) first". Each tick picks the
  best clip that isn't posted yet, so strong clips from chunks rendered later still jump the line.

Posted clips get rec["posts"]["instagram"] = {media_id, permalink, at}. State persists in out/_publish.json, so a
restart picks the campaign back up. If Instagram starts refusing (rate limit / spam block), the campaign pauses
itself instead of hammering the API.
"""
from __future__ import annotations

import random
import threading
import time
from pathlib import Path

from .config import Config
from .utils import log, read_json, write_json

BLOCK_HINTS = ("rate limit", "too many", "spam", "blocked", "action is blocked", "try again later",
               "code 4,", "code 9,", "code 17,", "code 32,", "code 613,", "36003")


class Publisher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.path(cfg.out_dir)
        self.state_p = self.root / "_publish.json"
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.st = read_json(self.state_p) if self.state_p.exists() else {}
        self.st.setdefault("manual", [])          # clip keys waiting to be posted
        self.st.setdefault("campaign", None)
        self.st.setdefault("history", [])         # last posts (key, permalink, at, ok/err)
        crashed = self.st.get("current")
        self.st["current"] = None
        self.net_down_until = 0.0
        self._save()
        self._recover(crashed)

    def _recover(self, crashed: dict | None) -> None:
        """After a crash/restart: clips stuck in 'posting'/'queued' are reset. A clip that was mid-post is checked
        against the account's recent Reels first so it's never posted twice."""
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
                    log(f"recovered post after restart: {r['title']} → {hit.get('permalink')}")
            r.pop("post_state", None)
            write_json(jp, r)
            if key in self.st["manual"] and r.get("posts", {}).get("instagram"):
                self.st["manual"].remove(key)
        # manual queue survives restarts: re-flag what's still waiting
        for key in list(self.st["manual"]):
            jp = self._jp(key)
            if jp.exists():
                r = read_json(jp)
                r["post_state"] = "queued"
                write_json(jp, r)
        self._save()

    # ------------------------------------------------------------------ state
    def _save(self) -> None:
        write_json(self.state_p, self.st)

    def _jp(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def snapshot(self) -> dict:
        with self.lock:
            c = self.st.get("campaign")
            out = {"manual": list(self.st["manual"]), "current": self.st.get("current"),
                   "history": self.st["history"][-20:], "campaign": dict(c) if c else None}
            if c:
                out["campaign"]["remaining"] = max(0, c["target"] - c["posted"])
                out["campaign"]["next_in"] = max(0, int(c.get("next_at", 0) - time.time()))
                out["campaign"]["queue_size"] = len(self._candidates(c["video_id"]))
            return out

    # ------------------------------------------------------------------ api
    def enqueue(self, key: str) -> None:
        jp = self._jp(key)
        rec = read_json(jp)
        if rec.get("posts", {}).get("instagram"):
            return
        with self.lock:
            if key not in self.st["manual"]:
                self.st["manual"].append(key)
                rec["post_state"] = "queued"
                rec.pop("post_error", None)
                write_json(jp, rec)
                self._save()
        self.wake.set()

    def start_campaign(self, video_id: str, target: int = 50, interval_min: float = 5.0) -> None:
        with self.lock:
            self.st["campaign"] = {"video_id": video_id, "target": int(target), "interval": float(interval_min) * 60,
                                   "posted": 0, "active": True, "started": time.time(), "next_at": time.time(),
                                   "paused_reason": None}
            self._save()
        self.wake.set()

    def set_active(self, active: bool) -> None:
        with self.lock:
            c = self.st.get("campaign")
            if c:
                c["active"] = active
                c["paused_reason"] = None if active else "paused by you"
                if active:
                    c["next_at"] = min(c.get("next_at", 0), time.time() + 5)
                self._save()
        self.wake.set()

    def stop(self) -> None:
        with self.lock:
            self.st["campaign"] = None
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
            if not (jp.parent / r["output"]).exists():
                continue
            out.append((float(r.get("score", 0)), str(jp.relative_to(self.root).with_suffix(""))))
        return sorted(out, reverse=True)

    def _loop(self) -> None:
        while True:
            try:
                did = self._tick()
            except Exception as e:  # never let the poster thread die
                log(f"publisher error: {e}")
                did = False
            if not did:
                self.wake.wait(5)
                self.wake.clear()

    def _tick(self) -> bool:
        if time.time() < self.net_down_until:
            return False
        with self.lock:
            key = self.st["manual"].pop(0) if self.st["manual"] else None
            camp = self.st.get("campaign")
            if key:
                self._save()
        if key:
            self._post(key, manual=True)
            return True
        if not camp or not camp.get("active") or camp["posted"] >= camp["target"]:
            return False
        if time.time() < camp.get("next_at", 0):
            return False
        cands = self._candidates(camp["video_id"])
        if not cands:
            with self.lock:   # nothing ready yet (chunks still rendering) — look again in a minute
                camp["next_at"] = time.time() + 60
                self._save()
            return False
        ok = self._post(cands[0][1], manual=False)
        with self.lock:
            c = self.st.get("campaign")
            if c:
                if ok:
                    c["posted"] += 1
                    c["next_at"] = time.time() + c["interval"] + random.uniform(0, 20)
                elif time.time() < self.net_down_until:
                    c["next_at"] = self.net_down_until          # offline: retry as soon as we're back
                else:
                    c["next_at"] = time.time() + 30              # this clip failed: move on to the next one soon
                if c["posted"] >= c["target"]:
                    c["active"] = False
                    c["paused_reason"] = "done"
                self._save()
        return True

    def _post(self, key: str, manual: bool) -> bool:
        from . import instagram as ig
        from .tags import instagram_caption
        jp = self._jp(key)
        rec = read_json(jp)
        if rec.get("posts", {}).get("instagram"):
            return True
        rec["post_state"] = "posting"
        rec.pop("post_error", None)
        write_json(jp, rec)
        with self.lock:
            self.st["current"] = {"key": key, "title": rec["title"], "stage": "starting", "since": time.time()}
            self._save()

        def prog(s: str) -> None:
            with self.lock:
                if self.st.get("current"):
                    self.st["current"]["stage"] = s

        try:
            # a previous attempt timed out / lost the network → it may have gone through: check before re-posting
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
                    log(f"already on Instagram (earlier attempt went through): {rec['title']}")
                    return True
            # stay safely under Instagram's 24h publishing limit
            lim = ig.publishing_limit()
            used, total = lim.get("quota_usage", 0), (lim.get("config") or {}).get("quota_total", 100)
            if used >= total - 2:
                raise ig.IGError(f"rate limit: {used}/{total} posts used in the last 24h")
            box: dict = {}

            def work():
                try:
                    box["res"] = ig.publish_reel(jp.parent / rec["output"], instagram_caption(rec, self.cfg), progress=prog)
                except BaseException as e:  # noqa: BLE001
                    box["err"] = e
            t = threading.Thread(target=work, daemon=True)
            t.start()
            t.join(15 * 60)                 # hard deadline: a hung upload (e.g. across sleep) can't freeze the queue
            if t.is_alive():
                raise ig.NetError("post timed out (network stalled — laptop asleep?)")
            if "err" in box:
                raise box["err"]
            res = box["res"]
            rec = read_json(jp)
            rec.setdefault("posts", {})["instagram"] = {**res, "at": time.strftime("%Y-%m-%d %H:%M")}
            rec.pop("post_state", None)
            write_json(jp, rec)
            log(f"posted to Instagram: {rec['title']} → {res['permalink']}")
            with self.lock:
                self.st["history"].append({"key": key, "title": rec["title"], "permalink": res["permalink"],
                                           "at": time.time(), "ok": True, "manual": manual})
            return True
        except Exception as e:
            msg = str(e)[:300]
            rec = read_json(jp)
            if isinstance(e, ig.NetError):
                # not the clip's fault: put it back, wait for the network, and try again
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
                c = self.st.get("campaign")
                if c and any(h in msg.lower() for h in BLOCK_HINTS):
                    c["active"] = False
                    c["paused_reason"] = f"Instagram pushed back: {msg[:120]} — paused, resume later"
            return False
        finally:
            with self.lock:
                self.st["current"] = None
                self.st["history"] = self.st["history"][-200:]
                self._save()
