"""Step 6: decide the 9:16 composition.

split : facecam on top (~35%), gameplay on the bottom (~65%)
crop  : single 9:16 crop (centered on the streamer's face for full-cam content, else center)
blur  : whole 16:9 frame letterboxed on a blurred, zoomed copy of itself
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .config import Config
from .download import Source
from .utils import log, read_json, write_json

W_OUT, H_OUT = 1080, 1920


@dataclass
class Layout:
    mode: str                                       # split | crop | blur
    facecam: tuple[int, int, int, int] | None = None   # x, y, w, h in source px
    game: tuple[int, int, int, int] | None = None
    crop: tuple[int, int, int, int] | None = None
    reason: str = ""
    face: dict | None = None                       # detection details (for cam-presence scoring)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("face", None)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Layout":
        d = dict(d)
        d.pop("face", None)
        for k in ("facecam", "game", "crop"):
            if d.get(k) is not None:
                d[k] = tuple(int(v) for v in d[k])
        return cls(**d)


def _even(v: float) -> int:
    return int(v) // 2 * 2


def _fit_rect(cx: float, cy: float, w: float, h: float, W: int, H: int) -> tuple[int, int, int, int]:
    w, h = min(w, W), min(h, H)
    x = float(np.clip(cx - w / 2, 0, W - w))
    y = float(np.clip(cy - h / 2, 0, H - h))
    return _even(x), _even(y), _even(w), _even(h)


# ------------------------------------------------------------------ face detection
YUNET = Path(__file__).resolve().parent.parent / "assets" / "models" / "face_detection_yunet_2023mar.onnx"


def _detector():
    """Best available face detector → (detect(img_bgr) -> [(x, y, w, h)], name)."""
    import cv2
    if YUNET.exists() and hasattr(cv2, "FaceDetectorYN_create"):
        yn = cv2.FaceDetectorYN_create(str(YUNET), "", (960, 540), 0.75, 0.3, 50)

        def detect(img):
            h, w = img.shape[:2]
            s = 960 / w
            small = cv2.resize(img, (960, int(round(h * s))))
            yn.setInputSize((small.shape[1], small.shape[0]))
            _, faces = yn.detect(small)
            out = []
            for f in faces if faces is not None else []:
                x, y, fw, fh = (float(v) / s for v in f[:4])
                if fh >= 0.035 * h:
                    out.append((x, y, fw, fh))
            return out
        return detect, "yunet"
    try:  # MediaPipe if installed (legacy solutions API)
        import mediapipe as mp  # type: ignore
        fd = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.6)

        def detect(img):
            h, w = img.shape[:2]
            res = fd.process(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            out = []
            for d in res.detections or []:
                b = d.location_data.relative_bounding_box
                out.append((b.xmin * w, b.ymin * h, b.width * w, b.height * h))
            return out
        return detect, "mediapipe"
    except Exception:
        pass
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect(img):
        gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        h = gray.shape[0]
        faces = casc.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=7, minSize=(int(h * 0.05),) * 2)
        return [tuple(map(float, f)) for f in faces]
    return detect, "haar"


def detect_face_region(src: Source, samples: int | None = None, t0: float | None = None,
                       t1: float | None = None, _det=None) -> dict | None:
    """Sample frames in [t0, t1] (default: whole chunk) and find the face that keeps showing up in one place."""
    import cv2
    cap = cv2.VideoCapture(str(src.video_path))
    if not cap.isOpened():
        return None
    detect, name = _det or _detector()
    whole = t0 is None
    t0 = src.duration * 0.02 if t0 is None else max(0.0, t0)
    t1 = src.duration * 0.98 if t1 is None else min(src.duration - 0.1, t1)
    samples = samples or int(np.clip((t1 - t0) / 25, 12, 72))      # ~every 25s
    boxes, owners, grays, times, any_face, frames = [], [], [], [], [], 0
    for t in np.linspace(t0, t1, samples):
        cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000)
        ok, img = cap.read()
        if not ok:
            continue
        times.append(round(float(t), 1))
        fs = detect(img)
        any_face.append(bool(fs))
        for b in fs:
            boxes.append(b)
            owners.append(frames)
        grays.append(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        frames += 1
    cap.release()
    if frames == 0 or not boxes:
        return None
    W, H = src.width, src.height
    # cluster face centers; a facecam is a face that keeps showing up in the same place
    centers = np.array([(x + w / 2, y + h / 2) for x, y, w, h in boxes])
    best, best_n = None, 0
    for c in centers:
        m = np.linalg.norm(centers - c, axis=1) < 0.08 * W
        if m.sum() > best_n:
            best, best_n = m, int(m.sum())
    if best_n < (max(4, 0.2 * frames) if whole else max(3, 0.4 * frames)):
        return None
    cl = np.array(boxes)[best]
    x, y, w, h = np.median(cl, axis=0)
    spread = float(np.linalg.norm(centers[best].std(axis=0)) / W)
    cam_frames = sorted({owners[i] for i in np.flatnonzero(best)})
    cam = _overlay_box([grays[i] for i in cam_frames], (x, y, w, h))
    present = _cam_presence(grays, cam, set(cam_frames))
    return {"box": [float(x), float(y), float(w), float(h)], "hits": best_n, "frames": frames,
            "spread": round(spread, 4), "detector": name, "cam": cam,
            "presence": [[t, bool(p), af] for t, p, af in zip(times, present, any_face)]}


def _cam_presence(grays: list, cam: list[int] | None, face_frames: set[int]) -> list[bool]:
    """Per sampled frame: is the facecam on screen? Uses the overlay's inner borders when we know them
    (robust even when the face isn't detected), else 'a face was seen within ±2 samples'."""
    import cv2
    n = len(grays)
    if cam:
        H, W = grays[0].shape
        bx, by, bw, bh = cam
        scores = []
        for g in grays:
            sc, k = 0.0, 0
            if bx + bw < W - 2:                       # right border column
                gx = np.abs(cv2.Sobel(g[by:by + bh, bx + bw - 3: bx + bw + 3], cv2.CV_32F, 1, 0, ksize=3))
                sc += float(gx.max(axis=1).mean()); k += 1
            if by > 2:                                # top border row
                gy = np.abs(cv2.Sobel(g[by - 3: by + 3, bx:bx + bw], cv2.CV_32F, 0, 1, ksize=3))
                sc += float(gy.max(axis=0).mean()); k += 1
            if bx > 2:                                # left border column
                gx = np.abs(cv2.Sobel(g[by:by + bh, bx - 3: bx + 3], cv2.CV_32F, 1, 0, ksize=3))
                sc += float(gx.max(axis=1).mean()); k += 1
            scores.append(sc / max(1, k))
        ref = np.median([scores[i] for i in face_frames]) if face_frames else np.median(scores)
        return [s >= 0.45 * ref for s in scores]
    return [any(j in face_frames for j in range(i - 2, i + 3)) for i in range(n)]


def cam_absent_mask(face: dict | None, n: int) -> "np.ndarray | None":
    """Per-second True where the facecam is (probably) off screen — intro/BRB/'starting soon' screens."""
    if not face or not face.get("presence"):
        return None
    ts = np.array([p[0] for p in face["presence"]])
    # "on screen" = the cam overlay is visible OR some face is visible (full-cam/IRL scenes are content too);
    # only stretches with neither (starting soon / BRB / AFK screens) count as absent
    pr = np.array([p[1] or (len(p) > 2 and p[2]) for p in face["presence"]], dtype=bool)
    if pr.all() or not pr.any():
        return None
    idx = np.clip(np.searchsorted(ts, np.arange(n) + 0.5), 0, len(ts) - 1)
    prev = np.clip(idx - 1, 0, len(ts) - 1)
    # conservative: the gap between an absent and a present sample counts as absent (transition zone)
    return (~pr[idx]) | (~pr[prev])


def _overlay_box(grays: list, face: tuple) -> list[int] | None:
    """Find the facecam overlay rectangle around a face: its borders are edges that stay put in every frame
    while the content inside/around them changes, so they survive a per-pixel median over frames."""
    import cv2
    if len(grays) < 3:
        return None
    H, W = grays[0].shape
    gx = np.median(np.stack([np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) for g in grays]), axis=0)
    gy = np.median(np.stack([np.abs(cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)) for g in grays]), axis=0)
    fx, fy, fw, fh = face
    rows = slice(int(max(0, fy - fh * 0.5)), int(min(H, fy + fh * 1.5)))
    cols = slice(int(max(0, fx - fw * 0.5)), int(min(W, fx + fw * 1.5)))
    colp = gx[rows].mean(axis=0)            # vertical-edge strength per column
    rowp = gy[:, cols].mean(axis=1)         # horizontal-edge strength per row

    def edge(profile, lo, hi, frame_edge):
        lo, hi = int(max(0, lo)), int(min(len(profile), hi))
        if hi - lo < 3:
            return frame_edge
        seg = profile[lo:hi]
        k = int(np.argmax(seg))
        if seg[k] > 4 * (np.median(profile) + 1e-3) and seg[k] > 8:
            return lo + k
        # no persistent border found: if the search reached the frame edge, the cam sits against it
        return frame_edge if (lo == 0 or hi == len(profile)) else None

    left = edge(colp, fx - 3 * fw, fx - 0.2 * fw, 0)
    right = edge(colp, fx + 1.2 * fw, fx + 4 * fw, W)
    top = edge(rowp, fy - 3 * fh, fy - 0.2 * fh, 0)
    bottom = edge(rowp, fy + 1.2 * fh, fy + 4 * fh, H)
    if None in (left, right, top, bottom):
        return None
    if left == 0 and fx - 3 * fw > 0:   # searched range didn't actually reach the edge
        return None
    bw, bh = right - left, bottom - top
    if bw < fw * 1.4 or bh < fh * 1.4 or bw * bh > 0.35 * W * H:
        return None
    return [int(left), int(top), int(bw), int(bh)]


def _detect_ranges(src: Source, ranges: list) -> list:
    det = _detector()
    return [detect_face_region(src, None if r is None else 8, *(r or (None, None)), _det=det) for r in ranges]


def _detect_isolated(src: Source, ranges: list | None = None) -> list | dict | None:
    """Run OpenCV in a child process: cv2 and faster-whisper (PyAV) ship clashing libavdevice dylibs on macOS.
    ranges=None → whole-chunk detection (dict); else one result per (t0, t1) range (list)."""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor
    try:
        with ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as ex:
            res = ex.submit(_detect_ranges, src, ranges or [None]).result(timeout=900)
        return res if ranges else res[0]
    except Exception as e:
        log(f"face detection failed ({type(e).__name__}: {e}); using fallback layout")
        return [None] * len(ranges) if ranges else None


# ------------------------------------------------------------------ planning
def plan(src: Source, cfg: Config, force: str | None = None) -> Layout:
    W, H = src.width, src.height
    mode = force or cfg.layout.mode
    top_h = _even(H_OUT * cfg.layout.facecam_ratio)
    top_aspect = W_OUT / top_h
    bot_aspect = W_OUT / (H_OUT - top_h)

    def game_rect():
        gw = min(W, H * bot_aspect)
        return _fit_rect(W / 2, H / 2, gw, gw / bot_aspect, W, H)

    def center_crop(cx=W / 2):
        cw = min(W, H * 9 / 16)
        return _fit_rect(cx, H / 2, cw, cw * 16 / 9, W, H)

    if mode == "blur":
        return Layout("blur", reason="forced")
    if mode == "crop":
        return Layout("crop", crop=center_crop(), reason="forced")

    # preset by uploader
    preset = cfg.layout.presets.get((src.uploader or "").lower())
    if preset and preset.get("facecam"):
        sx, sy = W / 1920, H / 1080
        fx, fy, fw, fh = preset["facecam"]
        fc = _fit_rect((fx + fw / 2) * sx, (fy + fh / 2) * sy, fw * sx, fw * sx / top_aspect, W, H)
        return Layout("split", facecam=fc, game=game_rect(), reason=f"preset:{src.uploader}")

    cache = src.work_dir / "face.json"
    if cache.exists():
        face = read_json(cache).get("face")
    else:
        log("detecting facecam")
        face = _detect_isolated(src)
        write_json(cache, {"face": face})

    return decide(face, src, cfg, mode)


def plan_clips(src: Source, cfg: Config, chunk: Layout, windows: list[tuple[float, float]],
               force: str | None = None) -> list[Layout]:
    """Per-clip layouts. Streams change scenes (full cam in the room, cam in a corner over a phone/screen,
    no cam at all…), so each clip is decided from frames inside its own window:
      face inside the chunk's facecam box (or overlay visible) → the chunk's facecam split
      face somewhere else                                       → decided from the clip (crop on face / split)
      no face                                                   → fallback (bigger 4:3 letterbox on blur)"""
    if force and force != "auto":
        return [chunk] * len(windows)
    face = chunk.face or {}
    cam = face.get("cam") if chunk.mode == "split" else None
    pres = face.get("presence", [])
    faces = _detect_isolated(src, [(a + 0.5, b - 0.5) for a, b in windows])
    out = []
    for (a, b), f in zip(windows, faces):
        if cam:
            bx, by, bw, bh = cam
            mx, my = bw * 0.1, bh * 0.1
            in_cam = False
            if f:
                x, y, w, h = f["box"]
                cx, cy = x + w / 2, y + h / 2
                in_cam = bx - mx <= cx <= bx + bw + mx and by - my <= cy <= by + bh + my
            win = [p[1] for p in pres if a - 15 <= p[0] <= b + 15]
            overlay_on = bool(win) and sum(win) / len(win) >= 0.5
            if in_cam or overlay_on:
                out.append(chunk)
                continue
        lay = decide(f, src, cfg, "auto")
        lay.reason = "clip: " + lay.reason
        out.append(lay)
    return out


def decide(face: dict | None, src: Source, cfg: Config, mode: str = "auto") -> Layout:
    """Layout from a face-detection result (chunk-level or a single clip's frames)."""
    W, H = src.width, src.height
    top_h = _even(H_OUT * cfg.layout.facecam_ratio)
    top_aspect = W_OUT / top_h
    bot_aspect = W_OUT / (H_OUT - top_h)

    def game_rect():
        gw = min(W, H * bot_aspect)
        return _fit_rect(W / 2, H / 2, gw, gw / bot_aspect, W, H)

    def center_crop(cx=W / 2):
        cw = min(W, H * 9 / 16)
        return _fit_rect(cx, H / 2, cw, cw * 16 / 9, W, H)

    if face is not None and face.get("spread", 1) < 0.0015 and not face.get("cam_ok"):
        # a face that doesn't move at all across the samples is a picture / paused video, not a person
        face = None
    if face is None:
        if mode == "split":
            # forced split without a face: use top-left quadrant as a guess
            fc = _fit_rect(W * 0.15, H * 0.15, W * 0.3, W * 0.3 / top_aspect, W, H)
            return Layout("split", facecam=fc, game=game_rect(), reason="forced split, no face found")
        fb = cfg.layout.fallback
        return Layout(fb, crop=center_crop() if fb == "crop" else None, reason="no face found")

    x, y, w, h = face["box"]
    cx, cy = x + w / 2, y + h / 2
    hit_rate = face["hits"] / max(1, face["frames"])
    # A facecam overlay = small face, pinned in place, away from the frame center.
    off_center = abs(cx / W - 0.5) > 0.2 or abs(cy / H - 0.5) > 0.25
    cam = face.get("cam")
    is_facecam = h < 0.25 * H and off_center and (face["spread"] < 0.04 or cam) and hit_rate >= 0.2
    if not is_facecam and mode != "split":
        if h < 0.12 * H:
            # small face that isn't a corner cam (someone on the screen/phone) → don't zoom into it
            fb = cfg.layout.fallback
            return Layout(fb, crop=center_crop() if fb == "crop" else None,
                          reason=f"small face in content ({face['detector']}), keeping the whole frame")
        # camera content (vlog / just-chatting / full cam) → 9:16 crop centered on where the face usually is
        return Layout("crop", crop=center_crop(cx),
                      reason=f"on-camera face, not a facecam overlay ({face['detector']}, {face['hits']}/{face['frames']} frames)")
    if cam:
        bx, by, bw, bh = cam
    else:  # no visible border: assume a cam ~3.2 face-widths wide around the face
        bw = w * 3.2
        bh = bw * 0.75
        bx, by = cx - bw / 2, cy - bh * 0.45
        bx, by, bw, bh = _fit_rect(bx + bw / 2, by + bh / 2, bw, bh, W, H)
    # top panel height follows the cam's shape (clamped to 28–42% of the canvas)
    top_h = _even(float(np.clip(W_OUT * bh / bw, 0.28 * H_OUT, 0.45 * H_OUT)))
    aspect = W_OUT / top_h
    if bw / bh > aspect:       # cam wider than the panel → trim width around the face
        fw_, fh_ = bh * aspect, bh
    else:                      # taller → trim height around the face (keep a bit more below the face)
        fw_, fh_ = bw, bw / aspect
    fcx = float(np.clip(cx, bx + fw_ / 2, bx + bw - fw_ / 2))
    # vertical: leave headroom above the face for the title sticker (top ~280px of the canvas),
    # but never cut off the chin
    scale = top_h / fh_
    want_top = y - 290 / scale
    chin_top = y + h * 1.08 - fh_
    crop_top = float(np.clip(max(want_top, chin_top), by, by + bh - fh_))
    fc = _fit_rect(fcx, crop_top + fh_ / 2, fw_, fh_, W, H)
    # gameplay: center the crop in the part of the frame not covered by a side-docked cam
    bot_aspect = W_OUT / (H_OUT - top_h)
    gw = min(W, H * bot_aspect)
    gx_lo, gx_hi = 0.0, float(W)
    if bx + bw <= W * 0.45:
        gx_lo = bx + bw
    elif bx >= W * 0.55:
        gx_hi = bx
    gcx = (gx_lo + gx_hi) / 2 if gx_hi - gx_lo >= gw else W / 2
    game = _fit_rect(gcx, H / 2, gw, gw / bot_aspect, W, H)
    return Layout("split", facecam=fc, game=game, face=face,
                  reason=f"facecam ({face['detector']}, {face['hits']}/{face['frames']} frames"
                         f"{', overlay box found' if cam else ''})")


def _top_h(lay: Layout) -> int:
    fw, fh = lay.facecam[2], lay.facecam[3]
    return int(np.clip(_even(W_OUT * fh / fw), 400, 900)) if fw else 672


def video_filter(lay: Layout, fps: int) -> str:
    """ffmpeg filtergraph from [0:v] to [vbase] at 1080x1920."""
    post = f"fps={fps},setsar=1,format=yuv420p"
    if lay.mode == "split" and lay.facecam and lay.game:
        fx, fy, fw, fh = lay.facecam
        gx, gy, gw, gh = lay.game
        top_h = _top_h(lay)
        bot_h = H_OUT - top_h
        return (f"[0:v]split=2[fc][gm];"
                f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={W_OUT}:{top_h}:flags=lanczos[top];"
                f"[gm]crop={gw}:{gh}:{gx}:{gy},scale={W_OUT}:{bot_h}:flags=lanczos[bot];"
                f"[top][bot]vstack=inputs=2,{post}[vbase]")
    if lay.mode == "crop" and lay.crop:
        cx, cy, cw, ch = lay.crop
        return f"[0:v]crop={cw}:{ch}:{cx}:{cy},scale={W_OUT}:{H_OUT}:flags=lanczos,{post}[vbase]"
    # blur letterbox
    return (f"[0:v]split=2[bg][fg];"
            f"[bg]scale={W_OUT}:{H_OUT}:force_original_aspect_ratio=increase,crop={W_OUT}:{H_OUT},"
            f"scale=270:480,gblur=sigma=12,scale={W_OUT}:{H_OUT},eq=brightness=-0.08[bgb];"
            f"[fg]crop='min(iw,ih*4/3)':ih,scale={W_OUT}:-2:flags=lanczos[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,{post}[vbase]")


def caption_anchor(lay: Layout) -> int:
    """Vertical center (px) for running captions."""
    if lay.mode == "split" and lay.facecam:
        return _top_h(lay) + 90          # just below the facecam/gameplay seam, clear of the face
    return int(H_OUT * 0.64)        # lower third, above the bottom 20% UI zone
