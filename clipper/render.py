"""Step 9: ffmpeg assembly — 9:16 layout + burned captions + sfx mix → H.264/AAC mp4."""
from __future__ import annotations

from pathlib import Path

from . import captions as cap
from .config import Config
from .layout import Layout, caption_anchor, video_filter
from .utils import has_ffmpeg_filter, run


def _duck_expr(hits: list[dict], db: float) -> str:
    if not hits or db <= 0:
        return "1"
    g = 10 ** (-db / 20)
    terms = [f"clip((t-{h['t']:.3f})/0.08,0,1)*clip(({h['t'] + h['dur']:.3f}-t)/0.25,0,1)" for h in hits]
    d = terms[0]
    for tr in terms[1:]:
        d = f"max({d},{tr})"
    return f"1-{1 - g:.4f}*{d}"


def pick_renderer(cfg: Config) -> str:
    r = cfg.captions.renderer
    if r == "auto":
        return "ass" if has_ffmpeg_filter("ass") else "pillow"
    if r == "ass" and not has_ffmpeg_filter("ass"):
        raise SystemExit("captions.renderer=ass but this ffmpeg has no libass. Use 'pillow' or `brew install ffmpeg-full`.")
    return r


def render_clip(clip: dict, clip_dir: Path, out_path: Path, cfg: Config) -> Path:
    src = clip["source"]
    start, end = float(clip["start"]), float(clip["end"])
    dur = round(end - start, 3)
    lay = Layout.from_json(clip["layout"])
    fps = cfg.layout.fps if (cfg.layout.fps <= 30 or src.get("fps", 30) >= 50) else 30
    clip_dir.mkdir(parents=True, exist_ok=True)

    events = cap.build_events(clip.get("words", []), start, end, cfg.captions)
    cap_y = caption_anchor(lay)
    fpath = cap.font_path(cfg)
    ass_path = cap.write_ass(clip_dir / "captions.ass", clip.get("title", ""), events, dur, cap_y, cfg.captions)
    renderer = pick_renderer(cfg)
    clip["captions_renderer"] = renderer

    inputs: list[str] = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src["video_path"]]
    n_in = 1
    fg = [video_filter(lay, fps)]
    vlast = "vbase"

    if renderer == "ass":
        fontsdir = str(Path(fpath).parent)
        fg.append(f"[{vlast}]ass=filename='{ass_path}':fontsdir='{fontsdir}'[vout]")
        vlast = "vout"
    else:
        if clip.get("title"):
            tpng, ty = cap.render_title_png(clip["title"], clip_dir / "title.png", cfg.captions, cap.title_font_path(cfg))
            inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", str(tpng)]
            fg.append(f"[{vlast}][{n_in}:v]overlay=0:{ty}:shortest=1[vt]")
            vlast, n_in = "vt", n_in + 1
        lst = cap.render_caption_sequence(events, dur, clip_dir / "caps", cfg.captions, fpath)
        if lst:
            inputs += ["-f", "concat", "-safe", "0", "-i", str(lst)]
            fg.append(f"[{n_in}:v]format=rgba,setpts=PTS-STARTPTS[capv];"
                      f"[{vlast}][capv]overlay=0:{cap_y - cap.STRIP_H // 2}:eof_action=pass:format=auto[vc]")
            vlast, n_in = "vc", n_in + 1

    # ---- audio
    hits = clip.get("sfx", [])
    if src.get("has_audio", True):
        a0 = "[0:a]"
    else:
        inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        a0 = f"[{n_in}:a]"
        n_in += 1
    chain = "aresample=48000,aformat=channel_layouts=stereo"
    if cfg.render.loudnorm:
        chain += ",loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000"
    chain += f",volume='{_duck_expr(hits, cfg.sfx.duck_db)}':eval=frame"
    chain += f",afade=t=out:st={max(0, dur - 0.4):.3f}:d=0.4"
    from .music import pick, track_path
    mus = pick(cfg, clip)
    clip["music"] = mus if mus else clip.get("music") if clip.get("music") is False else None
    if mus:
        fg.append(f"{a0}{chain},asplit=2[asrc][scin]")
    else:
        fg.append(f"{a0}{chain}[asrc]")
    mix = ["[asrc]"]
    if mus:
        inputs += ["-stream_loop", "-1", "-ss", f"{mus['offset']:.2f}", "-i", str(track_path(cfg, mus["title"]))]
        mchain = (f"[{n_in}:a]atrim=0:{dur:.3f},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                  f"volume={cfg.music.volume_db}dB,afade=t=in:d=0.8,afade=t=out:st={max(0, dur - 1.5):.3f}:d=1.5")
        if cfg.music.duck:
            fg.append(f"{mchain}[mraw];[mraw][scin]sidechaincompress=threshold={cfg.music.duck_threshold}:ratio={cfg.music.duck_ratio}:attack=40:release=450[mbed]")
        else:
            fg.append(f"{mchain}[mbed];[scin]anullsink")
        mix.append("[mbed]")
        n_in += 1
    for i, h in enumerate(hits):
        inputs += ["-i", h["file"]]
        ms = int(h["t"] * 1000)
        fg.append(f"[{n_in}:a]aresample=48000,aformat=channel_layouts=stereo,volume={cfg.sfx.gain_db}dB,"
                  f"adelay={ms}:all=1[sfx{i}]")
        mix.append(f"[sfx{i}]")
        n_in += 1
    if len(mix) > 1:
        fg.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first:dropout_transition=0,"
                  f"alimiter=limit=0.95,atrim=0:{dur:.3f}[aout]")
    else:
        fg.append(f"[asrc]alimiter=limit=0.95,atrim=0:{dur:.3f}[aout]")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-stats", *inputs,
           "-filter_complex", ";".join(fg),
           "-map", f"[{vlast}]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", cfg.render.preset, "-crf", str(cfg.render.crf),
           "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(fps),
           "-c:a", "aac", "-b:a", cfg.render.audio_bitrate, "-ar", "48000",
           "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out_path.with_suffix(".rendering.mp4"))]
    (clip_dir / "ffmpeg_cmd.txt").write_text(" ".join(f"'{c}'" if (" " in c or ";" in c) else c for c in cmd))
    run(cmd)
    import os
    os.replace(out_path.with_suffix(".rendering.mp4"), out_path)
    make_thumb(out_path, clip)
    return out_path


def make_thumb(mp4: Path, clip: dict) -> Path:
    """Poster frame for the UI, taken at the clip's main peak (or 40% in)."""
    dur = float(clip["end"]) - float(clip["start"])
    peaks = [t - float(clip["start"]) for t, _ in clip.get("sub_peaks", []) if clip["start"] <= t <= clip["end"]]
    t = peaks[0] if peaks else dur * 0.4
    t = min(max(0.5, t), max(0.5, dur - 0.5))
    thumb = mp4.with_suffix(".jpg")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1", "-update", "1",
         "-vf", "scale=360:-2", "-q:v", "4", str(thumb)], check=False)
    return thumb
