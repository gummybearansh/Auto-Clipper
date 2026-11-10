"""clipper CLI.

  clipper ui                                   # local web UI (paste links, monitor, review)
  clipper run <url|channel|file> [-n 10] [--min 15 --max 60] [--asr whisper|deepgram] [--layout auto|split|crop|blur]
  clipper review [out/<id>]
  clipper rerender <clip.json>
  clipper post <clip.json>                     # publish to Instagram as a Reel
  clipper export | cleanup
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .config import load_config
from .utils import console, require_bin

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Auto-clip Twitch/YouTube streams into 9:16 shorts.")


@app.command()
def run(
    url: str = typer.Argument(..., help="Twitch VOD / Twitch channel (live) / YouTube URL, or a local video file"),
    per_30: int = typer.Option(10, "--clips-per-30", "-n", help="clips per 30 minutes of footage"),
    follow: bool = typer.Option(False, "--follow", help="live stream: keep going as new 30-min blocks are recorded"),
    min_len: Optional[float] = typer.Option(None, "--min", help="min clip length (s)"),
    max_len: Optional[float] = typer.Option(None, "--max", help="max clip length (s)"),
    asr: Optional[str] = typer.Option(None, "--asr", help="whisper | deepgram"),
    model: Optional[str] = typer.Option(None, "--model", help="whisper model (tiny/base/small/medium/large-v3)"),
    layout: str = typer.Option("auto", "--layout", help="auto | split | crop | blur"),
    chat_offset: Optional[float] = typer.Option(None, "--chat-offset", help="seconds chat lags video"),
    no_sfx: bool = typer.Option(False, "--no-sfx", help="disable sound effects"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="path to config.yaml"),
):
    """Download, score, cut, caption and render the top clips."""
    require_bin("ffmpeg")
    require_bin("ffprobe")
    cfg = load_config(config)
    if min_len is not None:
        cfg.clip.min_len = min_len
    if max_len is not None:
        cfg.clip.max_len = max_len
    if cfg.clip.min_len > cfg.clip.max_len:
        raise typer.BadParameter("--min must be <= --max")
    if asr:
        if asr not in ("whisper", "deepgram"):
            raise typer.BadParameter("--asr must be whisper or deepgram")
        cfg.asr.backend = asr  # type: ignore[assignment]
    if model:
        cfg.asr.model = model
    if layout not in ("auto", "split", "crop", "blur"):
        raise typer.BadParameter("--layout must be auto, split, crop or blur")
    if chat_offset is not None:
        cfg.signals.chat_offset = chat_offset
    if no_sfx:
        cfg.sfx.enabled = False
    if per_30 < 1:
        raise typer.BadParameter("--clips-per-30 must be >= 1")
    import time
    from .jobs import JobManager
    jm = JobManager(cfg)
    job = jm.add(url, per_30, layout, follow)
    while True:
        jm.run_job(job)
        if job["status"] != "waiting":
            break
        console.print(f"[yellow]{job['stage']}[/yellow] (checking again in 2 min, Ctrl-C to stop)")
        time.sleep(max(5, job["next_check"] - time.time()))
    if job["status"] != "done":
        console.print(f"[red]{job['status']}: {job.get('error') or ''}[/red]")
        raise typer.Exit(1)
    root = cfg.path(cfg.out_dir) / job["video_id"]
    console.print(f"\n[green]✓[/green] {job['clips']} clips in [bold]{root}[/bold]. "
                  f"Review: [cyan]clipper review {root}[/cyan] or [cyan]clipper ui[/cyan]")


@app.command()
def ui(
    port: int = typer.Option(8765, "--port"),
    no_open: bool = typer.Option(False, "--no-open", help="don't open the browser"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Local web UI: paste links, watch progress, review/approve clips."""
    require_bin("ffmpeg")
    from .web import serve
    serve(load_config(config), port=port, open_browser=not no_open)


@app.command()
def review(
    out_dir: Optional[Path] = typer.Argument(None, help="out/<video_id> (default: most recent)"),
    pending: bool = typer.Option(False, "--pending", help="only step through pending clips"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """List clips with scores; preview, approve/reject, edit title or bounds."""
    from .review import latest_out, review as do_review
    cfg = load_config(config)
    d = out_dir or latest_out(cfg)
    if not d or not Path(d).exists():
        console.print("[red]No output folder found. Run `clipper run <url>` first.[/red]")
        raise typer.Exit(1)
    do_review(Path(d), cfg, only_pending=pending)


PLIST = Path("~/Library/LaunchAgents/com.gummybear.autoclipper.plist").expanduser()


@app.command()
def service(action: str = typer.Argument("status", help="install | uninstall | status | restart")):
    """Run the dashboard as a background service: starts at login, restarts if it crashes (macOS)."""
    import os
    import subprocess
    from .config import ROOT
    label, uid = "com.gummybear.autoclipper", os.getuid()
    exe = ROOT / ".venv" / "bin" / "clipper"
    logs = ROOT / "out" / "_logs"
    if action == "install":
        logs.mkdir(parents=True, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>ui</string><string>--no-open</string></array>
  <key>WorkingDirectory</key><string>{ROOT}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{logs}/ui.log</string>
  <key>StandardErrorPath</key><string>{logs}/ui.log</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
        if r.returncode:
            raise typer.Exit(console.print(f"[red]launchctl failed:[/red] {r.stderr}") or 1)
        console.print(f"[green]✓[/green] service installed — dashboard at http://localhost:8765, logs in {logs}/ui.log")
    elif action == "uninstall":
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        PLIST.unlink(missing_ok=True)
        console.print("[green]✓[/green] service removed")
    elif action == "restart":
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
        console.print("[green]✓[/green] restarted")
    else:
        r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, text=True)
        state = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith("state =")), "not installed")
        console.print(f"service: {state}")


@app.command()
def music(config: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Download the curated background-music tracks (Kevin MacLeod, CC BY 4.0) into assets/music/."""
    from .music import download
    n = download(load_config(config), log=lambda m: console.print(f"[dim]› {m}[/dim]"))
    console.print(f"[green]✓[/green] {n} tracks ready")


@app.command()
def post(
    clip_json: Path = typer.Argument(..., exists=True, help="out/<id>/chunk_XX/clip_YY.json"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Publish one clip to Instagram as a Reel (caption + hashtags generated). Connect first via `clipper ui`."""
    import time
    from . import instagram as ig
    from .tags import instagram_caption
    from .utils import read_json, write_json
    cfg = load_config(config)
    rec = read_json(clip_json)
    if rec.get("posts", {}).get("instagram"):
        console.print(f"[yellow]already posted:[/yellow] {rec['posts']['instagram']['permalink']}")
        raise typer.Exit()
    cap = instagram_caption(rec, cfg)
    console.print(cap + "\n")
    res = ig.publish_reel(clip_json.parent / rec["output"], cap, progress=lambda s: console.print(f"[dim]› {s}[/dim]"))
    rec.setdefault("posts", {})["instagram"] = {**res, "at": time.strftime("%Y-%m-%d %H:%M")}
    write_json(clip_json, rec)
    console.print(f"[green]✓ posted[/green] {res['permalink']}")


@app.command()
def export(config: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Copy every rendered clip into the export folder (one subfolder per link) + captions.txt."""
    from .export import backfill, export_root
    cfg = load_config(config)
    n = backfill(cfg)
    console.print(f"[green]✓[/green] {n} clips in {export_root(cfg)}")


@app.command()
def cleanup(config: Optional[Path] = typer.Option(None, "--config", "-c")):
    """Delete chunk videos/audio of already-clipped chunks (keeps clips + small per-clip excerpts)."""
    from .cleanup import fmt_bytes, sweep
    freed = sweep(load_config(config))
    console.print(f"[green]✓[/green] freed {fmt_bytes(freed)}")


@app.command()
def rerender(
    clip_json: Path = typer.Argument(..., exists=True, help="out/<id>/clip_XX.json (after editing title/bounds)"),
    config: Optional[Path] = typer.Option(None, "--config", "-c"),
):
    """Re-render one clip from its json (title, start/end, layout are read from the file)."""
    from .pipeline import rerender as do_rerender
    out = do_rerender(clip_json, load_config(config))
    console.print(f"[green]✓[/green] {out}")


if __name__ == "__main__":
    app()
