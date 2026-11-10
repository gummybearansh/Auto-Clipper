"""Step 1: resolve an input (URL / Twitch channel / local file) and fetch it in ≤30-minute chunks.

* Twitch VOD URL            → that VOD
* Twitch channel URL, live  → the in-progress VOD of the current broadcast (so we get the stream from its start)
* Twitch channel, offline   → the channel's most recent past broadcast
* YouTube video             → the video (+ "most replayed" heatmap, + live-chat replay if it was a stream)
* local file                → the file (+ optional <file>.chat.jsonl sidecar)

Each chunk gets its own work dir with the chunk video, 16 kHz audio and chunk-relative chat/heatmap.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Config
from .utils import check_cancel, log, media_info, read_json, run, write_json

ProgressFn = Callable[[str], None]


# ------------------------------------------------------------------ data
@dataclass
class Remote:
    """A resolved input. `duration` is what's available *now* (grows while a stream is live)."""
    kind: str                 # remote | local
    platform: str             # twitch | youtube | local | other
    video_id: str
    title: str
    uploader: str
    url: str                  # canonical url to download from (Twitch live → in-progress VOD url)
    input_url: str            # what the user gave us
    duration: float
    is_live: bool = False     # still recording/growing
    heatmap: list[dict] = field(default_factory=list)
    vod_id: str | None = None          # twitch numeric VOD id
    youtube_chat: bool = False         # youtube live-chat replay exists
    local_path: str | None = None
    sidecar_chat: str | None = None
    game: str = ""                     # Twitch category ("Genshin Impact", "Just Chatting")

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Source:
    """One downloaded chunk, ready for the pipeline. Times inside are chunk-relative; `offset` maps to the full video."""
    video_id: str
    platform: str
    title: str
    uploader: str
    url: str
    video_path: Path
    audio_path: Path
    work_dir: Path
    out_dir: Path
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    heatmap: list[dict] = field(default_factory=list)
    chat_path: Path | None = None
    offset: float = 0.0
    chunk: int = 1
    game: str = ""
    total_duration: float = 0.0        # length of the whole source video (all chunks)

    def to_json(self) -> dict:
        d = self.__dict__.copy()
        for k, v in d.items():
            if isinstance(v, Path):
                d[k] = str(v)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Source":
        d = dict(d)
        for k in ("video_path", "audio_path", "work_dir", "out_dir"):
            d[k] = Path(d[k])
        d["chat_path"] = Path(d["chat_path"]) if d.get("chat_path") else None
        return cls(**d)


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_")[:60] or "video"


def _platform(extractor: str) -> str:
    e = (extractor or "").lower()
    return "twitch" if "twitch" in e else "youtube" if "youtube" in e else "other"


def _ydl(extra: dict | None = None):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


# ------------------------------------------------------------------ resolve
_TW_CHANNEL = re.compile(r"twitch\.tv/(?!videos/)(?P<name>[A-Za-z0-9_]+)/?(?:\?.*)?$")


def resolve(url_or_path: str) -> Remote:
    p = Path(url_or_path).expanduser()
    if p.exists():
        mi = media_info(p)
        h = hashlib.sha1(str(p.resolve()).encode()).hexdigest()[:6]
        side = p.with_suffix(".chat.jsonl")
        return Remote(kind="local", platform="local", video_id=f"{_safe(p.stem)}_{h}", title=p.stem, uploader="",
                      url=str(p.resolve()), input_url=url_or_path, duration=mi["duration"],
                      local_path=str(p.resolve()), sidecar_chat=str(side) if side.exists() else None)

    m = _TW_CHANNEL.search(url_or_path)
    if m and "twitch.tv" in url_or_path:
        return _resolve_twitch_channel(m.group("name"), url_or_path)

    with _ydl({"skip_download": True}) as y:
        info = y.extract_info(url_or_path, download=False)
    return _from_info(info, url_or_path)


def _from_info(info: dict, input_url: str) -> Remote:
    platform = _platform(info.get("extractor_key", ""))
    live_status = info.get("live_status")
    if platform == "youtube" and live_status in ("is_live", "is_upcoming", "post_live"):
        raise RuntimeError("This YouTube stream is still live/processing. YouTube live isn't supported yet — "
                           "add it again once the stream has ended (Twitch live streams are supported).")
    vid = str(info.get("id"))
    return Remote(
        kind="remote", platform=platform, video_id=_safe(f"{platform}_{vid}"),
        title=info.get("title") or vid, uploader=info.get("uploader") or info.get("channel") or "",
        url=info.get("webpage_url") or input_url, input_url=input_url,
        duration=float(info.get("duration") or 0), is_live=live_status == "is_live",
        heatmap=info.get("heatmap") or [],
        vod_id=vid.lstrip("v") if platform == "twitch" else None,
        youtube_chat=platform == "youtube" and live_status == "was_live",
        game=_twitch_game(vid.lstrip("v")) if platform == "twitch" else "",
    )


def _twitch_game(vod_id: str) -> str:
    try:
        import urllib.request
        from .twitch_chat import CLIENT_ID, GQL
        q = {"query": "query($id:ID!){video(id:$id){game{name}}}", "variables": {"id": vod_id}}
        req = urllib.request.Request(GQL, data=json.dumps(q).encode(),
                                     headers={"Client-Id": CLIENT_ID, "Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=15) as r:
            g = ((json.loads(r.read()).get("data") or {}).get("video") or {}).get("game") or {}
        return g.get("name") or ""
    except Exception:
        return ""


def _resolve_twitch_channel(name: str, input_url: str) -> Remote:
    import yt_dlp
    stream_ts = None
    try:
        with _ydl({"skip_download": True}) as y:
            live = y.extract_info(f"https://www.twitch.tv/{name}", download=False)
        stream_ts = live.get("timestamp")
        log(f"{name} is live (started {time.strftime('%H:%M', time.localtime(stream_ts or 0))}) — looking for the in-progress VOD")
    except yt_dlp.utils.DownloadError as e:
        if "not currently live" not in str(e).lower() and "offline" not in str(e).lower():
            raise
        log(f"{name} is offline — using the most recent past broadcast")
    with _ydl({"skip_download": True, "extract_flat": True, "playlistend": 3}) as y:
        lst = y.extract_info(f"https://www.twitch.tv/{name}/videos?filter=archives&sort=time", download=False)
    entries = [e for e in (lst.get("entries") or []) if e]
    if not entries:
        raise RuntimeError(f"{name}: no past broadcasts found (VODs may be disabled), so the stream can't be fetched "
                           "from its start.")
    with _ydl({"skip_download": True}) as y:
        info = y.extract_info(entries[0].get("url") or f"https://www.twitch.tv/videos/{entries[0]['id'].lstrip('v')}",
                              download=False)
    if stream_ts and abs((info.get("timestamp") or 0) - stream_ts) > 1800:
        raise RuntimeError(f"{name} is live but the broadcast isn't being saved as a VOD (VODs disabled?).")
    r = _from_info(info, input_url)
    if stream_ts:
        r.is_live = True
    return r


def refresh(remote: Remote) -> Remote:
    """Re-resolve a growing stream to get its current length / whether it ended."""
    if remote.kind == "local":
        return remote
    with _ydl({"skip_download": True}) as y:
        info = y.extract_info(remote.url, download=False)
    r = _from_info(info, remote.input_url)
    return r


# ------------------------------------------------------------------ chunk fetch
def chunk_dir(out_root: Path, video_id: str, idx: int) -> Path:
    return out_root / video_id / f"chunk_{idx:02d}"


def extract_audio(video: Path, wav: Path) -> None:
    if wav.exists() and wav.stat().st_size > 0:
        return
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)])


def fetch_chunk(remote: Remote, idx: int, start: float, end: float, cfg: Config,
                progress: ProgressFn | None = None) -> Source:
    progress = progress or (lambda s: None)
    out_dir = chunk_dir(cfg.path(cfg.out_dir), remote.video_id, idx)
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    meta_p = work / "source.json"
    if meta_p.exists():
        src = Source.from_json(read_json(meta_p))
        if src.video_path.exists():
            return src

    video = work / "source.mp4"
    if not video.exists():
        progress("downloading video")
        if remote.kind == "local":
            if start <= 0 and end >= remote.duration - 0.5:
                video.symlink_to(remote.local_path)
            else:
                _cut(remote.local_path, start, end, video)
        else:
            _download_range(remote, start, end, work, cfg, progress)

    mi = media_info(video)
    audio = work / "audio16k.wav"
    if mi["has_audio"]:
        progress("extracting audio")
        extract_audio(video, audio)

    progress("fetching chat")
    chat_path = _chunk_chat(remote, start, end, work, cfg)

    heat = []
    for seg in remote.heatmap:
        a, b = float(seg.get("start_time", 0)), float(seg.get("end_time", 0))
        if b > start and a < end:
            heat.append({"start_time": max(0.0, a - start), "end_time": min(end, b) - start, "value": seg.get("value", 0)})

    src = Source(video_id=remote.video_id, platform=remote.platform, title=remote.title, uploader=remote.uploader,
                 url=remote.url, video_path=video, audio_path=audio, work_dir=work, out_dir=out_dir,
                 duration=mi["duration"], width=mi["width"], height=mi["height"], fps=mi["fps"],
                 has_audio=mi["has_audio"], heatmap=heat, chat_path=chat_path, offset=start, chunk=idx,
                 game=remote.game, total_duration=remote.duration)
    write_json(meta_p, src.to_json())
    return src


def _download_range(remote: Remote, start: float, end: float, work: Path, cfg: Config, progress: ProgressFn) -> None:
    dest = work / "source.mp4"
    if remote.platform == "twitch":
        # parallel HLS segment fetch for just this range (works on growing in-progress VODs too)
        from .hls import download_range
        download_range(_twitch_playlist(remote.url, cfg.download.max_height), start, end, dest, progress)
        return
    # YouTube / others: fetch the whole video once at full speed, then cut chunks locally
    full = _full_download(remote, work.parent.parent.parent, cfg, progress)
    progress("cutting chunk")
    _cut(full, start, end, dest)


def _cut(src: Path | str, start: float, end: float, dest: Path) -> None:
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src),
         "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", str(dest)])


def _twitch_playlist(url: str, max_h: int) -> str:
    with _ydl({"skip_download": True}) as y:
        info = y.extract_info(url, download=False)
    fmts = [f for f in info.get("formats") or [] if (f.get("protocol") or "").startswith("m3u8")
            and f.get("vcodec") not in (None, "none") and (f.get("height") or 0) <= max_h]
    if not fmts:
        raise RuntimeError("no HLS formats found for this Twitch VOD")
    best = max(fmts, key=lambda f: ((f.get("height") or 0), f.get("fps") or 0, f.get("tbr") or 0))
    return best["url"]


def _full_download(remote: Remote, out_root: Path, cfg: Config, progress: ProgressFn) -> Path:
    d = out_root / remote.video_id / "_full"
    d.mkdir(parents=True, exist_ok=True)
    final = d / "source.mp4"
    if final.exists():
        return final
    h = cfg.download.max_height
    last = [0.0]

    def hook(x):
        check_cancel()                      # raising inside a yt-dlp hook aborts the download
        if x.get("status") == "downloading" and time.time() - last[0] > 1.0:
            last[0] = time.time()
            done = x.get("downloaded_bytes") or 0
            tot = x.get("total_bytes") or x.get("total_bytes_estimate")
            pct = f"{100 * done / tot:.0f}% " if tot else ""
            speed = x.get("speed") or 0
            progress(f"downloading full video {pct}({done / 1e6:.0f} MB, {speed / 1e6:.0f} MB/s)")

    opts = {
        "format": f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
        "merge_output_format": "mp4", "outtmpl": str(d / "dl.%(ext)s"),
        "concurrent_fragment_downloads": 8, "progress_hooks": [hook],
    }
    with _ydl(opts) as y:
        y.download([remote.url])
    got = next((p for p in d.glob("dl.*") if p.suffix in (".mp4", ".mkv", ".webm", ".mov")), None)
    if got is None:
        raise RuntimeError("download finished but no video file was produced")
    if got.suffix != ".mp4":
        run(["ffmpeg", "-y", "-v", "error", "-i", str(got), "-c", "copy", str(final)])
        got.unlink()
    else:
        shutil.move(got, final)
    return final


def _chunk_chat(remote: Remote, start: float, end: float, work: Path, cfg: Config) -> Path | None:
    dest = work / "chat.jsonl"
    if dest.exists():
        return dest if dest.stat().st_size > 0 else None
    if not cfg.download.chat:
        return None
    msgs: list[tuple[float, str]] = []
    try:
        if remote.platform == "twitch" and remote.vod_id:
            from .twitch_chat import fetch_chat
            msgs = fetch_chat(remote.vod_id, start, end)
        elif remote.platform == "youtube" and remote.youtube_chat:
            from chat_downloader import ChatDownloader
            for m in ChatDownloader().get_chat(remote.url, start_time=start, end_time=end,
                                               message_types=["text_message"], max_attempts=2):
                t = m.get("time_in_seconds")
                if t is not None:
                    msgs.append((float(t), m.get("message", "")))
        elif remote.sidecar_chat:
            for line in Path(remote.sidecar_chat).read_text().splitlines():
                d = json.loads(line)
                if start <= float(d["t"]) < end:
                    msgs.append((float(d["t"]), str(d.get("msg", ""))))
    except Exception as e:  # chat is best-effort
        log(f"chat fetch failed: {type(e).__name__}: {e}")
    with dest.open("w") as f:
        for t, m in msgs:
            f.write(json.dumps({"t": round(t - start, 3), "msg": m}, ensure_ascii=False) + "\n")
    if msgs:
        log(f"chat: {len(msgs)} messages")
    return dest if msgs else None


def load_chat(path: Path | None) -> list[tuple[float, str]]:
    if not path or not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text().splitlines():
        try:
            d = json.loads(line)
            out.append((float(d["t"]), str(d.get("msg", ""))))
        except (ValueError, KeyError):
            continue
    return out
