"""Local web UI: paste links, watch jobs/chunks progress, review clips (approve / reject / retitle)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import Config
from .jobs import JobManager
from .review import clip_jsons, set_status
from .utils import read_json, write_json

STATIC = Path(__file__).parent / "web"
SKIP_FIELDS = {"words", "sfx", "layout", "sub_peaks", "weights"}


class AddJobs(BaseModel):
    urls: str
    per_30: int = 10
    layout: str = "auto"
    follow: bool = True


class Campaign(BaseModel):
    video_id: str
    count: int = 50
    interval: float = 5.0


class ClipAction(BaseModel):
    key: str
    action: str                    # approve | reject | pending | title | reveal
    title: str | None = None


def create_app(cfg: Config) -> FastAPI:
    out_root = cfg.path(cfg.out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    jm = JobManager(cfg)
    jm.start()
    from .publisher import Publisher
    pub = Publisher(cfg)
    pub.start()
    app = FastAPI(title="auto-clipper")
    app.mount("/media", StaticFiles(directory=out_root), name="media")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def clip_path(key: str) -> Path:
        p = (out_root / f"{key}.json").resolve()
        if out_root.resolve() not in p.parents or not p.exists():
            raise HTTPException(404, "clip not found")
        return p

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    # ---------------- Instagram connect
    def _redirect_uri(request: Request) -> str:
        # Facebook allows http://localhost redirects for apps in development mode
        return f"http://localhost:{request.url.port or 8765}/auth/instagram/callback"

    def _page(title: str, body: str, ok: bool) -> HTMLResponse:
        color = "#3ddc84" if ok else "#ff2e4d"
        return HTMLResponse(f"""<html><body style="background:#0c0c0d;color:#ececef;font:15px -apple-system,sans-serif;
            display:grid;place-items:center;height:100vh;margin:0"><div style="max-width:560px">
            <h2 style="color:{color}">{title}</h2><p style="line-height:1.5">{body}</p>
            <p><a style="color:#ececef" href="/">← back to auto-clipper</a></p></div></body></html>""")

    @app.get("/auth/instagram/start")
    def ig_start(request: Request):
        from . import instagram as ig
        try:
            return RedirectResponse(ig.login_url(_redirect_uri(request)))
        except ig.IGError as e:
            return _page("Can't start Instagram login", str(e), False)

    @app.get("/auth/instagram/callback")
    def ig_callback(request: Request, code: str = "", state: str = "", error_description: str = "",
                    error_message: str = ""):
        from . import instagram as ig
        if not code:
            return _page("Instagram login cancelled", error_description or error_message or "No code returned.", False)
        try:
            st = ig.finish_login(code, state, _redirect_uri(request))
        except ig.IGError as e:
            return _page("Instagram connect failed", str(e), False)
        return _page("Instagram connected ✓",
                     f"Posting as <b>@{st['username']}</b> via the Page <b>{st['page']}</b>. "
                     "The Page token doesn't expire.", True)

    @app.get("/api/instagram/status")
    def ig_status():
        from . import instagram as ig
        st = ig.status()
        if st["connected"]:
            try:
                st["limit"] = ig.publishing_limit()
            except Exception as e:
                st["limit_error"] = str(e)
        return st

    @app.get("/api/publish")
    def publish_state():
        return pub.snapshot()

    @app.post("/api/publish/start")
    def publish_start(body: Campaign):
        pub.start_campaign(body.video_id, max(1, min(100, body.count)), max(1.0, body.interval))
        return pub.snapshot()

    @app.post("/api/publish/{op}")
    def publish_op(op: str):
        if op == "pause":
            pub.set_active(False)
        elif op == "resume":
            pub.set_active(True)
        elif op == "stop":
            pub.stop()
        else:
            raise HTTPException(400, "unknown op")
        return pub.snapshot()

    @app.get("/api/state")
    def state():
        return {"jobs": jm.snapshot(), "current": jm.current, "rerender_queue": jm.rerender_q.qsize()}

    @app.post("/api/jobs")
    def add_jobs(body: AddJobs):
        urls = [u.strip() for u in body.urls.replace(",", "\n").split() if u.strip()]
        if not urls:
            raise HTTPException(400, "no links")
        per = max(1, min(40, body.per_30))
        lay = body.layout if body.layout in ("auto", "split", "crop", "blur") else "auto"
        return {"added": [jm.add(u, per, lay, body.follow)["id"] for u in urls]}

    @app.post("/api/jobs/{jid}/{op}")
    def job_op(jid: str, op: str):
        if op == "cancel":
            jm.cancel_job(jid)
        elif op == "retry":
            jm.retry(jid)
        elif op == "remove":
            jm.remove(jid)
        elif op == "open":
            j = next((x for x in jm.snapshot() if x["id"] == jid), None)
            marker = out_root / (j or {}).get("video_id", "") / "export.json" if j and j.get("video_id") else None
            if not marker or not marker.exists():
                raise HTTPException(404, "no clips exported yet")
            d = read_json(marker)["dir"]
            if sys.platform == "darwin":
                subprocess.Popen(["open", d])
        else:
            raise HTTPException(400, "unknown op")
        return {"ok": True}

    @app.get("/api/clips")
    def clips():
        out = []
        for p in clip_jsons(out_root):
            if p.parts[len(out_root.parts)].startswith("_"):
                continue
            try:
                c = read_json(p)
            except Exception:
                continue
            key = str(p.relative_to(out_root).with_suffix(""))
            mp4 = p.parent / c.get("output", p.stem + ".mp4")
            d = {k: v for k, v in c.items() if k not in SKIP_FIELDS}
            d["key"] = key
            d["layout_mode"] = (c.get("layout") or {}).get("mode")
            d["src_title"] = (c.get("source") or {}).get("title")
            d["platform"] = (c.get("source") or {}).get("platform")
            d.pop("source", None)
            d["media"] = f"/media/{mp4.relative_to(out_root)}?v={int(mp4.stat().st_mtime) if mp4.exists() else 0}"
            thumb = mp4.with_suffix(".jpg")
            if mp4.exists() and not thumb.exists():
                from .render import make_thumb
                make_thumb(mp4, c)
            d["poster"] = f"/media/{thumb.relative_to(out_root)}?v={int(thumb.stat().st_mtime)}" if thumb.exists() else ""
            d["ready"] = mp4.exists()
            d["mtime"] = mp4.stat().st_mtime if mp4.exists() else 0
            out.append(d)
        out.sort(key=lambda c: (-c["mtime"]))
        return out

    @app.post("/api/clip")
    def clip_action(body: ClipAction):
        p = clip_path(body.key)
        if body.action in ("approve", "reject", "pending"):
            c = set_status(p, {"approve": "approved", "reject": "rejected", "pending": "pending"}[body.action])
            return {"status": c["status"]}
        if body.action == "title":
            t = (body.title or "").strip()
            if not t:
                raise HTTPException(400, "empty title")
            c = read_json(p)
            c.update(title=t, title_strategy="manual", rendering=True)
            c.pop("render_error", None)
            write_json(p, c)
            if c.get("status") == "approved":   # approved copy gets refreshed after the re-render
                c["status"] = "pending"
                write_json(p, c)
            jm.queue_rerender(p)
            return {"rendering": True}
        if body.action == "post":
            from . import instagram as ig
            if not ig.status()["connected"]:
                raise HTTPException(400, "Instagram isn't connected — click Connect Instagram first")
            pub.enqueue(body.key)
            return {"queued": True}
        if body.action == "reveal":
            c = read_json(p)
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p.parent / c["output"])])
            return {"ok": True}
        raise HTTPException(400, "unknown action")

    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import threading
    import uvicorn
    import webbrowser
    app = create_app(cfg)
    url = f"http://{host}:{port}"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"auto-clipper UI → {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
