<div align="center">

# 🍬 Auto-Clipper

### Paste a Twitch or YouTube link. Get ranked, captioned, ready-to-post 9:16 shorts.

**Fully local · no LLM · no cloud · built for stream clippers**

![Python](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)
![ffmpeg](https://img.shields.io/badge/ffmpeg-H.264%20%2B%20AAC-007808?logo=ffmpeg&logoColor=white)
![Whisper](https://img.shields.io/badge/faster--whisper-local%20ASR-412991)
![Runs on](https://img.shields.io/badge/runs%20on-Apple%20Silicon-000000?logo=apple&logoColor=white)
![LLM](https://img.shields.io/badge/LLMs%20used-0-FF2E4D)

<img src="docs/dashboard.jpg" alt="Auto-Clipper dashboard" width="100%"/>

</div>

---

## ✨ What it does

Drop in a **Twitch VOD, a live Twitch channel, or a YouTube video** and Auto-Clipper:

1. **Downloads** it in 30-minute chunks (parallel HLS, ~10 MB/s) along with the **chat replay** and YouTube's *most-replayed* heatmap
2. **Transcribes** it locally with word-level timestamps (faster-whisper + Silero VAD)
3. **Scores every second** of the stream from chat velocity, emote spam, "CLIP IT" mentions, loudness, yelling, hype keywords, speech density and on-screen action
4. **Cuts the best moments** with build-up before the peak and payoff after, snapped to natural pauses
5. **Re-frames to 9:16** automatically: facecam-on-top split for gaming, face-tracked crop for IRL, smart fallback when there's no cam
6. **Burns in captions**: a tilted *sticker* hook title (🔥 or 😭) plus word-by-word running captions with the current word highlighted
7. **Adds sparse SFX**: an outro sting plus a hit on the biggest peak, with the stream audio ducked underneath
8. **Exports** every clip to a Desktop folder with a ready-to-paste **caption + hashtags** for Instagram and YouTube Shorts
9. Optionally **publishes to Instagram** as a Reel through the official Graph API

<div align="center">
<img src="docs/clips.jpg" alt="Example output clips" width="100%"/>
<sub>Real output: one pipeline, four layouts, picked automatically per clip.</sub>
</div>

### 🎬 Live examples
Posted straight from the pipeline to [@gummybearclips](https://www.instagram.com/gummybearclips/):
[gacha pull 🔥](https://www.instagram.com/reel/DfjsVefCHa_/) ·
[grocery run 😭](https://www.instagram.com/reel/DfjshQaCEk7/) ·
[chat lost it 😭](https://www.instagram.com/reel/DfjrfZxiISP/)

---

## 🧠 How it works

```mermaid
flowchart LR
    A[🔗 link] --> B[⬇️ chunked download<br/>video · chat · heatmap]
    B --> C[🎙️ local Whisper<br/>word timestamps]
    B --> D[📈 per-second signals]
    C --> D
    D --> E[🎯 peaks → windows<br/>snap to pauses · NMS]
    E --> F[🖼️ 9:16 layout<br/>per clip]
    F --> G[💬 sticker title +<br/>running captions]
    G --> H[🔊 sparse SFX +<br/>ducking]
    H --> I[📁 Desktop export<br/>clip + caption + tags]
    I -.-> J[📸 Instagram Reel]
```

### Scoring, with no LLM

Every signal becomes a per-second series. Each one is robust-z-normalised, smoothed, and combined with tunable weights:

| Signal | Source | Why it works |
|---|---|---|
| **Chat velocity** | Twitch chat replay (shifted for chat lag) | the strongest hype detector on Twitch |
| **Emote density** | KEKW · LUL · Pog · W · `?????` · caps ratio | *how* chat reacted, not just how much |
| **"Clip it" mentions** | chat | close to a human-labelled highlight |
| **Loudness jumps + spectral flux** | audio | shouting, sudden noise |
| **Yelling / laughing** | voice-band energy × brightness | reactions |
| **Hype keywords** | transcript ("no way", "let's go", …) | spoken excitement |
| **Speech density** | transcript | fast talking = hype |
| **Most-replayed** | YouTube heatmap | the audience literally voted |
| **Visual activity** | frame-diff | action on screen |

Moments where the streamer's cam disappears (starting soon / BRB screens) are down-weighted automatically.

### Smart 9:16 layouts

Face detection (YuNet) runs on frames sampled from **each clip**, because streams change scenes constantly:

- **Facecam overlay found** → the overlay's exact border is detected (edges that stay put across frames), the cam goes on top and the gameplay below, cropped to avoid the cam
- **On-camera / IRL** → a 9:16 crop that follows the face
- **Small faces in content, or none** → 4:3 frame on a blurred background, so nothing important gets cropped away

### Titles that hook

Built from heuristics only: the dominant chat reaction (`KEKW` spam → *"chat lost it 😭"*), a shouted line from the transcript, or a varied template. Titles never repeat within a chunk and always end in 🔥 (hype) or 😭 (funny / chaos).

---

## 🖥️ The dashboard

`clipper ui` opens a local web app at `http://localhost:8765`:

- **Paste links** (several at once), set clips per 30 min, pick a layout, follow live streams
- **Job cards** with per-chunk progress bars, a LIVE badge, logs, cancel (stops within seconds), retry and *open folder*
- **Clip grid** with thumbnails, an inline player, scores, a ▶ deep link to the exact moment in the stream, and click-to-edit titles that re-render on Enter
- **Connect Instagram** with one click (OAuth, permanent Page token)

---

## 🚀 Quickstart

```bash
git clone https://github.com/gummybearansh/Auto-Clipper.git && cd Auto-Clipper
uv venv --python 3.11 .venv
uv pip install --python .venv -e .
brew install ffmpeg          # any ffmpeg works; captions fall back to Pillow if it lacks libass

.venv/bin/clipper ui         # → http://localhost:8765
```

Or straight from the terminal:

```bash
.venv/bin/clipper run https://www.twitch.tv/videos/123456789          # a VOD
.venv/bin/clipper run https://www.twitch.tv/somechannel --follow      # live: from stream start, every new 30 min
.venv/bin/clipper run "https://www.youtube.com/watch?v=…" -n 10       # 10 clips per 30 minutes
.venv/bin/clipper run ./my_vod.mp4                                    # local file (+ optional my_vod.chat.jsonl)
```

| Command | What it does |
|---|---|
| `clipper ui` | dashboard |
| `clipper run <link>` | clip a VOD / live channel / YouTube video / local file |
| `clipper review [out/<id>]` | terminal review: approve, reject, retitle, nudge bounds |
| `clipper rerender <clip.json>` | re-render after editing a clip's json |
| `clipper post <clip.json>` | publish one clip to Instagram as a Reel |
| `clipper export` | (re)build the Desktop export folders |
| `clipper cleanup` | delete heavy chunk files that are already clipped |

---

## 📁 Output

Every clip lands on your Desktop, one folder per link, sorted best-first within each 30-minute part:

```
~/Desktop/gummybear clips/
└── 2026-11-10 jasontheween - RICE AND CRACKERS (twitch v2924380274)/
    ├── p01-01 29m32s - WAIT FAKER LOST CHAT.mp4      1080×1920 · H.264/AAC · 15–60 s
    ├── p01-01 29m32s - WAIT FAKER LOST CHAT.txt      ← caption + hashtags, ready to paste
    ├── p01-02 27m25s - chat went FERAL.mp4
    └── captions.txt                                  ← every clip, with score + link to the moment
```

```
━━━━━━━━ INSTAGRAM REEL ━━━━━━━━
WAIT FAKER LOST CHAT 😭

🎥 jasontheween on twitch.tv/jasontheween
Follow @gummybearclips for more 🍬

#jasontheween #justchatting #twitchclips #funny #streamerclips
```

Hashtags are capped at five and layered from niche to broad: **streamer → game/category (pulled from Twitch) → clips community → mood**. That follows current Instagram and Shorts guidance, which favours a few relevant tags over walls of #fyp.

**Disk stays small:** once a chunk is clipped, its ~2 GB source video is deleted and each clip keeps a ~15 MB excerpt so titles can still be edited. Exports are hard links, so they cost no extra space.

---

## 📸 Instagram publishing (optional)

Uses the official **Instagram API with Facebook Login**, publishing via resumable upload with no public video URL needed.

1. Make your Instagram a **Creator** (or Business) account
2. Create a **Facebook Page** *from the same Facebook account that owns your Meta app*, and link the Instagram account to it (Page → Settings → Linked accounts)
3. Create a Meta app, then put its id and secret in `~/.config/auto-clipper/instagram.json`:
   ```json
   { "app_id": "…", "app_secret": "…" }
   ```
4. In the dashboard click **Connect Instagram** and tick your Page and Instagram account. You get a Page token that **never expires**; re-connect roughly every 90 days when Facebook asks.
5. `clipper post out/<id>/chunk_01/clip_01.json`

> Credentials live in `~/.config/auto-clipper/` (chmod 600), never in the repo.

---

## ⚙️ Configuration

Everything is in [`config.yaml`](config.yaml): signal weights per platform, clip length, chat offset, Whisper model, fonts and colours, SFX levels, cleanup, export folder, streamer handles and extra hashtags.

```yaml
weights:
  twitch:  { chat: 0.35, audio: 0.25, emote: 0.15, keywords: 0.10, speech: 0.10, visual: 0.05, clip_mentions: 0.10 }
  youtube: { heatmap: 0.35, audio: 0.30, keywords: 0.15, speech: 0.10, visual: 0.10 }
asr:   { backend: whisper, model: small }        # or --asr deepgram with DEEPGRAM_API_KEY
clip:  { min_len: 15, max_len: 60 }
```

## ⚡ Performance

On an Apple M5 MacBook (16 GB), each 30-minute chunk takes about **8–9 minutes end to end**:

| Step | Time |
|---|---|
| Download (1080p60, parallel HLS) | ~3 min at ~10 MB/s |
| Chat replay (~3k messages) | ~8 s |
| Whisper `small` int8 | ~3 min |
| Scoring + per-clip layout | < 1 min |
| Rendering 10 clips | ~1 min |

---

## 🗂️ Project layout

```
clipper/
  cli.py · web.py · web/index.html     CLI + dashboard
  jobs.py                              queue, 30-min chunking, live follow, resume, cancel
  download.py · hls.py · twitch_chat.py  resolve links, parallel HLS ranges, chat replay
  transcribe.py                        faster-whisper / Deepgram, incremental region cache
  signals/                             chat · audio · transcript · visual · heatmap
  score.py · segment.py                combine signals → peaks → windows → NMS
  layout.py                            YuNet faces, overlay-box detection, per-clip 9:16 plan
  titles.py · captions.py · tags.py    hooks + emoji, ASS / Pillow captions, captions & hashtags
  sfx.py · render.py                   sparse SFX + ducking, ffmpeg assembly
  export.py · cleanup.py · instagram.py  Desktop export, disk cleanup, Reels publishing
CONTEXT.md                             the original design doc
```

## 🛣️ Roadmap

- [ ] YouTube Shorts upload (Data API, once quota is approved)
- [ ] Scheduled posting with an account warm-up ramp
- [ ] Zoom punches, emoji pops, progress bar
- [ ] Learn the signal weights from real view counts

---

<div align="center">

**Clip responsibly.** Only post streams you have permission to clip, and always credit the creator. Auto-Clipper adds the credit line for you.

Fonts: [Montserrat](https://github.com/JulietaUla/Montserrat) & [Anton](https://github.com/googlefonts/AntonFont) (SIL OFL) · Face detection: [YuNet](https://github.com/opencv/opencv_zoo) (OpenCV Zoo)

Made with 🍬 by [@gummybearclips](https://www.instagram.com/gummybearclips/)

</div>
