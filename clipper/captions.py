"""Step 7: captions.

Two layers:
  * title   — static hook text for the whole clip, top safe zone (white rounded box, black text)
  * running — 2–3 words at a time, current word highlighted, only where there is speech

Two renderers:
  * ASS (libass) when ffmpeg has the `ass` filter — writes captions.ass and burns it
  * Pillow fallback (ffmpeg without libass): pre-renders caption strips as PNGs and overlays them with
    the concat demuxer (one PNG per caption state, so it's fast). An .ass file is still written for reference.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import CaptionCfg, Config

W, H = 1080, 1920
SAFE_W = 820                        # keeps text clear of the right-side UI rail (~12%) when centered
TITLE_CY = 150                      # title box center (inside top ~15%)
EMOJI_FONT = "/System/Library/Fonts/Apple Color Emoji.ttc"
_EMOJI_RE = re.compile("([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF][\uFE0F\u200D\U0001F3FB-\U0001F3FF"
                       "\U0001F000-\U0001FAFF\u2600-\u27BF]*)")


@dataclass
class CapEvent:
    start: float            # clip-relative seconds
    end: float
    words: list[str]
    hi: int                 # index of highlighted word


# ------------------------------------------------------------------ grouping
def _clean(w: str, upper: bool) -> str:
    w = w.strip()
    w = re.sub(r"^[\"'(\[]+|[\"')\],.;:]+$", "", w)
    w = w.rstrip(",.;:")
    return w.upper() if upper else w


def build_events(words: list[dict], clip_start: float, clip_end: float, cc: CaptionCfg) -> list[CapEvent]:
    ws = []
    for w in words:
        if w["s"] < clip_start - 0.05 or w["s"] >= clip_end - 0.1:
            continue
        t = _clean(w["w"], cc.uppercase)
        if not t:
            continue
        ws.append({"w": t, "raw": w["w"], "s": max(0.0, w["s"] - clip_start), "e": min(clip_end, w["e"]) - clip_start})
    if not ws:
        return []
    # group into 2–3 word chunks, breaking on pauses / sentence ends / length
    groups: list[list[dict]] = [[]]
    for i, w in enumerate(ws):
        g = groups[-1]
        if g:
            prev = g[-1]
            chars = sum(len(x["w"]) + 1 for x in g) + len(w["w"])
            if (len(g) >= cc.words_per_group or chars > cc.max_group_chars or w["s"] - prev["e"] > 0.5
                    or prev["raw"].rstrip().endswith((".", "!", "?", "…", ","))):
                groups.append([])
        groups[-1].append(w)
    groups = [g for g in groups if g]

    events: list[CapEvent] = []
    for gi, g in enumerate(groups):
        nxt_start = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else None
        g_end = g[-1]["e"] + 0.35
        if nxt_start is not None:
            g_end = nxt_start if nxt_start - g[-1]["e"] < 0.6 else min(g_end, nxt_start)
        g_end = min(g_end, clip_end - clip_start)
        texts = [x["w"] for x in g]
        for i, w in enumerate(g):
            s = w["s"] if i else g[0]["s"]
            e = g[i + 1]["s"] if i + 1 < len(g) else g_end
            if e - s < 0.04:
                continue
            events.append(CapEvent(round(s, 3), round(e, 3), texts, i))
    # remove overlaps from ASR timestamp jitter
    for a, b in zip(events, events[1:]):
        if a.end > b.start:
            a.end = b.start
    return [e for e in events if e.end - e.start >= 0.04]


# ------------------------------------------------------------------ ASS
def _ass_color(hex_: str, alpha: int = 0) -> str:
    h = hex_.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(t: str) -> str:
    return t.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def write_ass(path: Path, title: str, events: list[CapEvent], duration: float, cap_y: int, cc: CaptionCfg,
              font_name: str = "Montserrat") -> Path:
    white, hi, black = _ass_color(cc.color), _ass_color(cc.highlight), _ass_color(cc.stroke)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Title,Anton,{cc.title_size},{_ass_color(cc.title_color)},{_ass_color(cc.title_color)},"
        f"{_ass_color(cc.title_bg)},&H80000000,0,0,0,0,100,100,0,{cc.title_tilt},"
        f"3,16,4,8,130,130,{TITLE_CY - cc.title_size},1",
        f"Style: Cap,{font_name},{cc.size},{white},{white},{black},&H80000000,-1,0,0,0,100,100,1,0,1,"
        f"{cc.stroke_width},3,5,130,130,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if title:
        lines.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(duration)},Title,,0,0,0,,{_ass_escape(title.upper())}")
    for e in events:
        parts = []
        for i, w in enumerate(e.words):
            w = _ass_escape(w)
            parts.append(f"{{\\c{hi}\\fscx108\\fscy108}}{w}{{\\c{white}\\fscx100\\fscy100}}" if i == e.hi else w)
        lines.append(f"Dialogue: 0,{_ass_time(e.start)},{_ass_time(e.end)},Cap,,0,0,0,,"
                     f"{{\\an5\\pos({W // 2},{cap_y})}}" + " ".join(parts))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ Pillow renderer
@lru_cache(maxsize=16)
def _font(path: str, size: int, weight: str) -> ImageFont.FreeTypeFont:
    try:
        f = ImageFont.truetype(path, size)
        try:
            f.set_variation_by_name(weight)
        except Exception:
            pass
        return f
    except OSError:
        for fb in ("/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Supplemental/Impact.ttf"):
            if Path(fb).exists():
                return ImageFont.truetype(fb, size)
        return ImageFont.load_default(size)


@lru_cache(maxsize=1)
def _emoji_font():
    try:
        return ImageFont.truetype(EMOJI_FONT, 160)
    except OSError:
        return None


def _runs(text: str) -> list[tuple[str, bool]]:
    out = []
    for part in _EMOJI_RE.split(text):
        if part:
            out.append((part, bool(_EMOJI_RE.fullmatch(part))))
    return out


def _emoji_img(e: str, size: int) -> Image.Image | None:
    ef = _emoji_font()
    if ef is None:
        return None
    im = Image.new("RGBA", (200 * max(1, len(e)), 200), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((0, 0), e, font=ef, embedded_color=True)
    bb = im.getbbox()
    if not bb:
        return None
    im = im.crop(bb)
    return im.resize((max(1, int(im.width * size / 160)), size), Image.LANCZOS)


def _measure_runs(runs, font, size) -> int:
    w = 0
    for t, is_e in runs:
        if is_e:
            w += int(size * 1.05)
        else:
            w += int(font.getlength(t))
    return w


def _wrap_title(text: str, font, size: int, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if _measure_runs(_runs(cand), font, size) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) == 2:   # balance two lines (avoid a lonely last word)
        best, best_w = lines, max(_measure_runs(_runs(l), font, size) for l in lines)
        for k in range(1, len(words)):
            a, b = " ".join(words[:k]), " ".join(words[k:])
            wmax = max(_measure_runs(_runs(a), font, size), _measure_runs(_runs(b), font, size))
            if wmax <= max_w and wmax < best_w:
                best, best_w = [a, b], wmax
        lines = best
    return lines


def _rgba(hex_: str, a: int = 255) -> tuple[int, int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a


def render_title_png(title: str, path: Path, cc: CaptionCfg, font_path: str) -> tuple[Path, int]:
    """Top hook title as a tilted 'sticker': display font (Anton), solid colored label, white border,
    drop shadow. Deliberately unlike the running captions (Montserrat, white/yellow, outlined).
    Returns (png, y offset). Shrinks the font to stay within 2 lines."""
    title = title.upper()
    size = cc.title_size
    pad_x, pad_y, border = 34, 16, 7
    max_text_w = SAFE_W - 2 * (pad_x + border) - 40          # leave room for the tilt
    while True:
        font = _font(font_path, size, "Regular")
        lines = _wrap_title(title, font, size, max_text_w)
        if len(lines) <= 2 or size <= 44:
            break
        size -= 4
    asc, desc = font.getmetrics()
    lh = int((asc + desc) * 1.02)
    text_w = max(_measure_runs(_runs(l), font, size) for l in lines)
    box_w = text_w + 2 * pad_x
    box_h = lh * len(lines) + 2 * pad_y
    cw, ch = box_w + 2 * border + 60, box_h + 2 * border + 60
    sticker = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    ox, oy = 30, 30
    # shadow
    sh = Image.new("RGBA", sticker.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([ox + 6, oy + 12, ox + box_w + 2 * border + 6, oy + box_h + 2 * border + 12],
                                         radius=22, fill=(0, 0, 0, 150))
    sticker.alpha_composite(sh.filter(ImageFilter.GaussianBlur(9)))
    d = ImageDraw.Draw(sticker)
    d.rounded_rectangle([ox, oy, ox + box_w + 2 * border, oy + box_h + 2 * border], radius=22,
                        fill=_rgba(cc.title_border))
    d.rounded_rectangle([ox + border, oy + border, ox + border + box_w, oy + border + box_h], radius=16,
                        fill=_rgba(cc.title_bg))
    y = oy + border + pad_y
    for line in lines:
        runs = _runs(line)
        lw = _measure_runs(runs, font, size)
        x = ox + border + (box_w - lw) // 2
        for t, is_e in runs:
            if is_e:
                em = _emoji_img(t, int(size * 0.9))
                if em is not None:
                    sticker.alpha_composite(em, (x + int(size * 0.08), y + (lh - em.height) // 2))
                x += int(size * 1.05)
            else:
                d.text((x, y), t, font=font, fill=_rgba(cc.title_color))
                x += int(font.getlength(t))
        y += lh
    if cc.title_tilt:
        sticker = sticker.rotate(cc.title_tilt, resample=Image.BICUBIC, expand=True)
    img = Image.new("RGBA", (W, sticker.height), (0, 0, 0, 0))
    img.alpha_composite(sticker, ((W - sticker.width) // 2, 0)) if sticker.width <= W else img.paste(
        sticker.crop(((sticker.width - W) // 2, 0, (sticker.width - W) // 2 + W, sticker.height)))
    img.save(path)
    top = max(36, TITLE_CY + 10 - img.height // 2)
    return path, top


STRIP_H = 260


def render_caption_png(e: CapEvent, path: Path, cc: CaptionCfg, font_path: str) -> None:
    size = cc.size
    font = _font(font_path, size, cc.font_weight)
    space = font.getlength(" ")
    widths = [font.getlength(w) for w in e.words]
    total = sum(widths) + space * (len(widths) - 1)
    if total > SAFE_W:
        size = max(40, int(size * SAFE_W / total))
        font = _font(font_path, size, cc.font_weight)
        space = font.getlength(" ")
        widths = [font.getlength(w) for w in e.words]
        total = sum(widths) + space * (len(widths) - 1)
    img = Image.new("RGBA", (W, STRIP_H), (0, 0, 0, 0))
    # drop shadow layer
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    dsh = ImageDraw.Draw(sh)
    d = ImageDraw.Draw(img)
    x = (W - total) / 2
    cy = STRIP_H // 2
    sw = max(2, int(cc.stroke_width * size / cc.size))
    for i, (w, wd) in enumerate(zip(e.words, widths)):
        fill = cc.highlight if i == e.hi else cc.color
        dsh.text((x + 4, cy + 6), w, font=font, anchor="lm", fill=(0, 0, 0, 160), stroke_width=sw, stroke_fill=(0, 0, 0, 160))
        d.text((x, cy), w, font=font, anchor="lm", fill=fill, stroke_width=sw, stroke_fill=cc.stroke)
        x += wd + space
    out = sh.filter(ImageFilter.GaussianBlur(4))
    out.alpha_composite(img)
    out.save(path, optimize=False, compress_level=1)


def render_caption_sequence(events: list[CapEvent], duration: float, out_dir: Path, cc: CaptionCfg,
                            font_path: str) -> Path | None:
    """PNG strips + concat list covering [0, duration]. Returns the concat list path."""
    if not events:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in out_dir.glob("*.png"):
        p.unlink()
    blank = out_dir / "blank.png"
    Image.new("RGBA", (W, STRIP_H), (0, 0, 0, 0)).save(blank)
    entries: list[tuple[Path, float]] = []
    t = 0.0
    cache: dict[tuple, Path] = {}
    for i, e in enumerate(events):
        if e.start > t + 0.01:
            entries.append((blank, e.start - t))
        key = (tuple(e.words), e.hi)
        if key not in cache:
            p = out_dir / f"c{i:04d}.png"
            render_caption_png(e, p, cc, font_path)
            cache[key] = p
        entries.append((cache[key], e.end - max(e.start, t)))
        t = e.end
    if t < duration:
        entries.append((blank, duration - t))
    lst = out_dir / "captions.txt"
    lines = ["ffconcat version 1.0"]
    for p, d in entries:
        if d <= 0:
            continue
        lines += [f"file '{p.resolve()}'", f"duration {d:.3f}"]
    lines.append(f"file '{blank.resolve()}'")   # concat demuxer needs the last file repeated
    lst.write_text("\n".join(lines) + "\n")
    return lst


def font_path(cfg: Config) -> str:
    return str(cfg.path(cfg.captions.font))


def title_font_path(cfg: Config) -> str:
    return str(cfg.path(cfg.captions.title_font))
