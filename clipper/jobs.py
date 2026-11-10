"""Job queue: each URL becomes a job that is processed in ≤30-minute chunks, one chunk at a time.

Live Twitch streams are processed from the start of the broadcast. With `follow` on, the job keeps
watching the stream and processes each new 30-minute block as it becomes available, then the final
partial block when the stream ends.

State is persisted to out/_jobs/<id>.json so a restart resumes where it left off.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid
from pathlib import Path

from .cleanup import cleanup_chunk, cleanup_full, fmt_bytes
from .config import Config
from .download import Remote, chunk_dir, fetch_chunk, refresh, resolve
from .pipeline import CHUNK_SECONDS, clips_for, process_source, rerender
from .utils import Cancelled, fmt_ts, log, read_json, set_cancel_check, set_log_hook, write_json

LIVE_BUFFER = 90          # don't touch the last ~90s of an in-progress VOD (segments may still be landing)
MIN_TAIL = 60             # ignore leftover tails shorter than this
POLL_LIVE = 120           # seconds between checks on a followed live stream


def plan_chunks(duration: float, live: bool, follow: bool) -> list[tuple[float, float]]:
    """Chunk boundaries for what's available now."""
    usable = duration - (LIVE_BUFFER if live else 0)
    out, t = [], 0.0
    while t + CHUNK_SECONDS <= usable:
        out.append((t, t + CHUNK_SECONDS))
        t += CHUNK_SECONDS
    tail = usable - t
    if tail >= MIN_TAIL and not (live and follow):     # following: wait for the full block / the stream to end
        out.append((t, usable))
    return out


class JobManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.path(cfg.out_dir) / "_jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.cancel: set[str] = set()
        self.rerender_q: queue.Queue = queue.Queue()
        self.wake = threading.Event()
        self.current: str | None = None
        for p in sorted(self.dir.glob("*.json")):
            try:
                j = read_json(p)
            except Exception:
                continue
            if j.get("status") in ("running", "resolving", "waiting"):
                j["status"] = "queued"                  # resume after restart
                j["stage"] = "resuming"
            j.pop("cancelling", None)
            for c in j.get("chunks", []):
                if c.get("status") == "running":
                    c["status"] = "pending"
            self.jobs[j["id"]] = j

    # ---------------------------------------------------------------- persistence / api
    def _save(self, j: dict) -> None:
        j["updated"] = time.time()
        write_json(self.dir / f"{j['id']}.json", j)

    def add(self, url: str, per_30: int = 10, layout: str = "auto", follow: bool = True) -> dict:
        j = {"id": uuid.uuid4().hex[:8], "url": url.strip(), "status": "queued", "stage": "queued",
             "opts": {"per_30": int(per_30), "layout": layout, "follow": bool(follow)},
             "title": url.strip(), "platform": "", "video_id": None, "is_live": False, "duration": 0,
             "chunks": [], "clips": 0, "error": None, "log": [], "created": time.time()}
        with self.lock:
            self.jobs[j["id"]] = j
            self._save(j)
        self.wake.set()
        return j

    def snapshot(self) -> list[dict]:
        with self.lock:
            out = []
            for j in sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True):
                d = {k: v for k, v in j.items() if k not in ("log", "remote")}
                d["log"] = j["log"][-40:]
                out.append(d)
            return out

    def cancel_job(self, jid: str) -> None:
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return
            if j["status"] in ("queued", "waiting"):
                j["status"], j["stage"] = "cancelled", "cancelled"
                self._save(j)
            elif j["status"] in ("running", "resolving"):
                self.cancel.add(jid)
                j["cancelling"] = True
        self.wake.set()

    def retry(self, jid: str) -> None:
        with self.lock:
            j = self.jobs.get(jid)
            if j and j["status"] in ("failed", "cancelled", "done"):
                j.update(status="queued", stage="queued", error=None, retry_at=0, auto_retries=0)
                for c in j["chunks"]:
                    if c["status"] in ("failed", "running"):
                        c["status"] = "pending"
                self.cancel.discard(jid)
                self._save(j)
        self.wake.set()

    def remove(self, jid: str) -> None:
        with self.lock:
            j = self.jobs.get(jid)
            if not j or j["status"] in ("running", "resolving"):
                return
            self.jobs.pop(jid)
            (self.dir / f"{jid}.json").unlink(missing_ok=True)

    def queue_rerender(self, clip_json: Path) -> None:
        self.rerender_q.put(clip_json)

    # ---------------------------------------------------------------- worker
    def start(self) -> None:
        threading.Thread(target=self._loop, name="clipper-worker", daemon=True).start()
        threading.Thread(target=self._rerender_loop, name="clipper-rerender", daemon=True).start()

    def _next(self) -> dict | None:
        with self.lock:
            now = time.time()
            ready = [j for j in self.jobs.values()
                     if (j["status"] == "queued" and now >= j.get("retry_at", 0))
                     or (j["status"] == "waiting" and now >= j.get("next_check", 0))]
            return min(ready, key=lambda j: (j["status"] == "waiting", j["created"])) if ready else None

    def _loop(self) -> None:
        while True:
            j = self._next()
            if not j:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.run_job(j)

    def _rerender_loop(self) -> None:
        while True:
            p = self.rerender_q.get()
            try:
                rerender(Path(p), self.cfg)
            except Exception as e:
                log(f"re-render failed for {p}: {e}")
                try:
                    rec = read_json(Path(p))
                    rec.pop("rendering", None)
                    rec["render_error"] = str(e)[:500]
                    write_json(Path(p), rec)
                except Exception:
                    pass

    # ---------------------------------------------------------------- one job
    def run_job(self, j: dict) -> None:
        jid = j["id"]
        self.current = jid

        def logline(msg: str) -> None:
            with self.lock:
                j["log"].append(f"{time.strftime('%H:%M:%S')}  {msg}")
                j["log"] = j["log"][-300:]

        def stage(s: str) -> None:
            with self.lock:
                j["stage"] = s
                # progress ticks (download %) replace the previous tick instead of flooding the log
                if j["log"] and "%" in s and s.split("%")[0].rsplit(" ", 1)[0] in j["log"][-1]:
                    j["log"][-1] = f"{time.strftime('%H:%M:%S')}  {s}"
                    return
            logline(s)

        def checkpoint() -> None:
            if jid in self.cancel:
                raise Cancelled()

        set_log_hook(logline)
        set_cancel_check(lambda: jid in self.cancel)
        try:
            with self.lock:
                j["status"] = "resolving"
            stage("resolving link")
            remote = resolve(j["url"]) if not j.get("remote") else refresh(Remote(**j["remote"]))
            with self.lock:
                j.update(remote=remote.to_json(), title=remote.title, platform=remote.platform,
                         video_id=remote.video_id, is_live=remote.is_live, duration=remote.duration,
                         uploader=remote.uploader, status="running")
                self._merge_chunks(j, plan_chunks(remote.duration, remote.is_live, j["opts"]["follow"]))
                self._save(j)
            if remote.is_live:
                logline(f"live stream — {fmt_ts(remote.duration)} recorded so far")

            for c in j["chunks"]:
                if c["status"] == "done":
                    continue
                checkpoint()
                self._run_chunk(j, remote, c, stage, checkpoint)

            if remote.is_live and j["opts"]["follow"]:
                with self.lock:
                    j.update(status="waiting", next_check=time.time() + POLL_LIVE,
                             stage=f"live — next block when {fmt_ts(len(j['chunks']) * CHUNK_SECONDS + CHUNK_SECONDS)} is recorded")
                    self._save(j)
                return
            if self.cfg.cleanup.enabled:
                freed = cleanup_full(self.cfg.path(self.cfg.out_dir) / remote.video_id)
                if freed:
                    logline(f"freed {fmt_bytes(freed)} (full download deleted)")
            with self.lock:
                j.update(status="done", stage=f"done — {j['clips']} clips", auto_retries=0)
                self._save(j)
        except Cancelled:
            logline("cancelled")
            with self.lock:
                j.update(status="cancelled", stage="cancelled")
                for c in j["chunks"]:
                    if c["status"] == "running":
                        c["status"] = "pending"
                self._save(j)
        except Exception as e:
            if jid in self.cancel:          # e.g. yt-dlp wrapped our Cancelled in its own error
                logline("cancelled")
                with self.lock:
                    j.update(status="cancelled", stage="cancelled")
                    for c in j["chunks"]:
                        if c["status"] == "running":
                            c["status"] = "pending"
                    self._save(j)
                return
            logline(f"ERROR: {e}")
            traceback.print_exc()
            text = f"{type(e).__name__}: {e}".lower()
            netty = any(w in text for w in ("urlopen", "timed out", "timeout", "connection", "network", "nodename",
                                            "temporary failure", "unable to download", "http error 5", "eof"))
            with self.lock:
                tries = j.get("auto_retries", 0)
                if netty and tries < 8:   # network blip (sleep, wifi): retry automatically with backoff
                    wait = min(1800, 60 * 2 ** tries)
                    j.update(status="queued", stage=f"network problem — retrying in {wait // 60} min",
                             auto_retries=tries + 1, retry_at=time.time() + wait, error=str(e)[:300])
                    for c in j["chunks"]:
                        if c["status"] == "running":
                            c["status"] = "pending"
                else:
                    j.update(status="failed", stage="failed", error=str(e)[:800])
                self._save(j)
        finally:
            self.cancel.discard(jid)
            j.pop("cancelling", None)
            self.current = None
            set_log_hook(None)
            set_cancel_check(None)

    def _merge_chunks(self, j: dict, planned: list[tuple[float, float]]) -> None:
        have = {c["idx"]: c for c in j["chunks"]}
        out = []
        for i, (a, b) in enumerate(planned, 1):
            c = have.get(i)
            if c and c["status"] == "done":
                out.append(c)
                continue
            out.append({"idx": i, "start": a, "end": b, "status": "pending", "clips": 0,
                        "target": clips_for(b - a, j["opts"]["per_30"]), "stage": ""})
        j["chunks"] = out

    def _run_chunk(self, j: dict, remote: Remote, c: dict, stage, checkpoint) -> None:
        n = len(j["chunks"])
        done_dir = chunk_dir(self.cfg.path(self.cfg.out_dir), remote.video_id, c["idx"])
        if (done_dir / "index.json").exists():
            have = len(list(done_dir.glob("clip_[0-9][0-9].json")))
            if have >= c["target"] or not (done_dir / "work" / "source.mp4").exists():
                with self.lock:          # already clipped earlier (e.g. job re-added) — don't re-download
                    c.update(status="done", clips=have, stage="done")
                    j["clips"] = sum(x["clips"] for x in j["chunks"])
                    self._save(j)
                stage(f"chunk {c['idx']}/{n}: already clipped ({have} clips)")
                return
        label = f"chunk {c['idx']}/{n} [{fmt_ts(c['start'])}–{fmt_ts(c['end'])}]"
        with self.lock:
            c["status"] = "running"
            c["t0"] = time.time()
            self._save(j)

        def st(s: str) -> None:
            checkpoint()
            with self.lock:
                c["stage"] = s
            stage(f"{label}: {s}")

        def on_clip(rec: dict) -> None:
            with self.lock:
                c["clips"] += 1
                j["clips"] = sum(x["clips"] for x in j["chunks"])
                self._save(j)

        src = fetch_chunk(remote, c["idx"], c["start"], c["end"], self.cfg, st)
        checkpoint()
        clips = process_source(src, self.cfg, c["target"], j["opts"]["layout"], st, checkpoint, on_clip)
        st("cleaning up")
        freed = cleanup_chunk(src.out_dir, self.cfg)
        if freed:
            log(f"freed {fmt_bytes(freed)} (chunk video + audio deleted, per-clip excerpts kept)")
        with self.lock:
            c.update(status="done", clips=len(clips), stage="done", t1=time.time())
            j["clips"] = sum(x["clips"] for x in j["chunks"])
            self._save(j)
