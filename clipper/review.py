"""Step 10: interactive review — preview, approve/reject, edit title/bounds, re-render.

Approved clips are copied to out/<id>/approved/. Nothing is ever posted automatically.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from rich.prompt import Prompt
from rich.table import Table

from .config import Config
from .pipeline import rerender
from .utils import console, fmt_ts, read_json, write_json

STATUS_STYLE = {"pending": "yellow", "approved": "green", "rejected": "red"}


def clip_jsons(root: Path) -> list[Path]:
    """All clip json files under root (video dir or the whole out dir), excluding approved/ copies."""
    return sorted(p for p in root.rglob("clip_*.json") if "approved" not in p.parts and "work" not in p.parts)


def latest_out(cfg: Config) -> Path | None:
    root = cfg.path(cfg.out_dir)
    dirs = [d for d in root.glob("*") if d.is_dir() and not d.name.startswith("_") and clip_jsons(d)]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def load_clips(out_dir: Path) -> list[tuple[Path, dict]]:
    return [(p, read_json(p)) for p in clip_jsons(out_dir)]


def video_root(clip_json: Path) -> Path:
    """out/<video_id> for a clip json in out/<video_id>/chunk_XX/ (or legacy out/<video_id>/)."""
    return clip_json.parent.parent if clip_json.parent.name.startswith("chunk_") else clip_json.parent


def approved_name(clip_json: Path) -> str:
    return (f"{clip_json.parent.name}_{clip_json.stem}" if clip_json.parent.name.startswith("chunk_")
            else clip_json.stem)


def set_status(p: Path, status: str) -> dict:
    c = read_json(p)
    c["status"] = status
    write_json(p, c)
    approved = video_root(p) / "approved"
    name = approved_name(p)
    mp4 = p.parent / c["output"]
    if status == "approved":
        approved.mkdir(exist_ok=True)
        shutil.copy2(mp4, approved / f"{name}.mp4")
        shutil.copy2(p, approved / f"{name}.json")
    else:
        (approved / f"{name}.mp4").unlink(missing_ok=True)
        (approved / f"{name}.json").unlink(missing_ok=True)
    if status == "rejected" and c.get("source", {}).get("excerpt"):
        from .cleanup import drop_excerpt
        drop_excerpt(p)
    return c


def table(clips: list[tuple[Path, dict]]) -> Table:
    t = Table(show_lines=False, header_style="bold")
    for col in ("#", "status", "score", "time", "len", "title", "top signals"):
        t.add_column(col)
    for _, c in clips:
        bd = sorted(c.get("breakdown", {}).items(), key=lambda kv: -kv[1])[:3]
        st = c.get("status", "pending")
        t.add_row(f"{c.get('chunk', 1)}.{c['id'].split('_')[-1]}", f"[{STATUS_STYLE.get(st, 'white')}]{st}[/]", f"{c['score']:.2f}",
                  f"{fmt_ts(c.get('abs_start', c['start']))}", f"{c['duration']:.0f}s", c["title"],
                  ", ".join(f"{k} {v:+.2f}" for k, v in bd))
    return t


def _open(path: Path) -> None:
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _set_status(p: Path, c: dict, status: str) -> None:
    set_status(p, status)


def review(out_dir: Path, cfg: Config, only_pending: bool = False) -> None:
    clips = load_clips(out_dir)
    if not clips:
        console.print(f"[red]No clips in {out_dir}[/red]")
        return
    console.print(table(clips))
    todo = [(p, c) for p, c in clips if not only_pending or c.get("status", "pending") == "pending"]
    i = 0
    while i < len(todo):
        p, c = todo[i]
        c = read_json(p)
        mp4 = p.parent / c["output"]
        console.rule(f"[bold]{c['id']}[/bold]  score {c['score']:.2f}  [{STATUS_STYLE.get(c['status'])}]{c['status']}[/]")
        console.print(f"  title  : [bold]{c['title']}[/bold]  [dim]({c.get('title_strategy')})[/dim]")
        console.print(f"  window : {fmt_ts(c.get('abs_start', c['start']))} ({c['duration']:.1f}s)  "
                      f"layout {c['layout']['mode']}  sfx {len(c.get('sfx', []))}")
        console.print(f"  source : {c.get('source_link', '')}")
        console.print(f"  file   : {mp4}")
        choice = Prompt.ask("  [o]pen  [a]pprove  [r]eject  [t]itle  [b]ounds  [n]ext  [p]rev  [q]uit",
                            choices=list("oartbnpq"), default="o", show_choices=False)
        if choice == "o":
            _open(mp4)
            continue
        if choice == "a":
            _set_status(p, c, "approved")
            console.print("  [green]approved[/green] → approved/")
        elif choice == "r":
            _set_status(p, c, "rejected")
            console.print("  [red]rejected[/red]")
        elif choice == "t":
            new = Prompt.ask("  new title", default=c["title"]).strip()
            if new and new != c["title"]:
                c["title"], c["title_strategy"] = new, "manual"
                write_json(p, c)
                rerender(p, cfg)
                if c.get("status") == "approved":
                    _set_status(p, read_json(p), "approved")
            continue
        elif choice == "b":
            raw = Prompt.ask("  adjust start/end in seconds (e.g. '-3 +2')", default="0 0")
            try:
                ds, de = (float(x) for x in raw.replace(",", " ").split()[:2])
            except ValueError:
                console.print("  [red]expected two numbers[/red]")
                continue
            c["start"], c["end"] = round(c["start"] + ds, 2), round(c["end"] + de, 2)
            if c["end"] - c["start"] < 3:
                console.print("  [red]clip too short[/red]")
                continue
            write_json(p, c)
            rerender(p, cfg)
            continue
        elif choice == "p":
            i = max(0, i - 1)
            continue
        elif choice == "q":
            break
        i += 1
    console.print(table(load_clips(out_dir)))
    n = sum(1 for _, c in load_clips(out_dir) if c.get("status") == "approved")
    console.print(f"[green]{n} approved[/green] → {out_dir}/approved")
