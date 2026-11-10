# auto-clipper — Project Context

Automatic Twitch/YouTube → short-form clip pipeline. Give it a link and it outputs ranked, captioned, 9:16 clips into a
review folder. You approve clips before anything is posted.

## Non-negotiables
1. **Short-form resolution:** 1080x1920 (9:16), H.264 + AAC, 30/60fps, 15–60s per clip.
2. **Closed captions** are burned in wherever there is speech. No speech means no captions.
3. **Decent graphics.** v1: a title caption at the top and running captions. v2: richer graphics (B-roll, zooms, emoji pops, progress bar).

## Decisions (locked)
- **Runs locally** on the user's Mac. No cloud is required.
- **No LLM.** Clip choice, scoring and titles are all done with heuristics and signal processing.
- **Primary content:** gaming streams (Twitch VODs + YouTube gaming).
- **Interface:** CLI first. Run `clipper <url> --top 5`.
- **Clip recommendation is fully automatic**, but **posting requires review** (approve/reject step).
- **Sound effects are sparse:** one at the end of the clip (outro sting) plus at most 1–2 mid-clip on major peaks. Never spammy.
- **Captions have two layers:**
  - **Top title caption:** static hook text for the whole clip (upper ~15% of the frame).
  - **Running captions:** word-by-word, 2–3 words at a time, current word highlighted (lower-middle of the frame, clear of the facecam).

## Pipeline
```
url
 → 1. download        yt-dlp (video + Twitch chat replay + YT "most replayed" heatmap if present)
 → 2. transcribe      faster-whisper / WhisperX (local, word-level timestamps, VAD)
 → 3. signals         per-second time series (see Scoring)
 → 4. segment         find peaks → expand to natural boundaries (pauses / sentence ends) → 15–60s windows
 → 5. score + rank    weighted sum, non-max suppression so clips don't overlap
 → 6. reframe 9:16    gaming layout: facecam top / gameplay bottom; fallback: center/face-tracked crop
 → 7. captions        ASS subtitles (top title + running word-highlight), burned with ffmpeg
 → 8. sfx             sparse: end sting + optional peak hit(s), ducked mix via ffmpeg
 → 9. export          out/<video_id>/clip_XX.mp4 + clip_XX.json (score breakdown, title, timestamps)
 → 10. review         `clipper review` → approve/reject → approved/ folder (posting = later phase)
```

## Transcription
- **Default:** **faster-whisper** (or WhisperX for better word alignment). It runs locally and is free. On Apple Silicon, use the `small`/`medium` models, or `large-v3` if speed is OK. `mlx-whisper` is an option for speed on M-series Macs.
- **Deepgram works too and is faster and more accurate on noisy streams, but it is a paid cloud API.** Keep it as an optional
  backend behind an interface: `--asr deepgram` with a `DEEPGRAM_API_KEY` set. The default stays local.
- VAD (Silero, built into faster-whisper) gates the captions so there are no captions during silence or game audio.

## Scoring (no LLM)
Every signal is a per-second series, normalized (z-score), then smoothed. The clip score combines the signals in the window:

| Signal | Source | Notes |
|---|---|---|
| Chat velocity | Twitch chat replay (`chat-downloader` / TwitchDownloaderCLI) | messages/sec vs. a rolling baseline. **Strongest signal for Twitch.** |
| Emote/hype density | chat | KEKW, LUL, OMEGALUL, PogChamp, Pog, W, L, "?????", "CLIP IT", caps ratio |
| "Clip" mentions | chat | literal "clip"/"clip that" spikes are a near-perfect label |
| Audio energy | librosa RMS + spectral flux | loud spikes, sudden jumps relative to the local baseline |
| Laughter / yelling | pitch + energy heuristics (optional: YAMNet/PANNs audio classifier, which runs locally and is not an LLM) | |
| Speech density | transcript | words/sec. Hype moments usually have fast talking. |
| Keyword lexicon | transcript | hand-tuned list: "no way", "oh my god", "let's go", "what", "bro", swear words, etc. |
| YT most-replayed | yt-dlp `heatmap` | when available, weight it heavily |
| Visual activity | frame-diff / scene cuts (PySceneDetect) | action moments |

The starting weights are tunable in `config.yaml`. Twitch: chat 0.35, audio 0.25, emote 0.15, keywords 0.1, speech 0.1, visual 0.05.
YouTube without chat: heatmap 0.35, audio 0.3, keywords 0.15, speech 0.1, visual 0.1.

**Segmentation:**
1. Find the peaks of the combined score.
2. Start the clip about 8–20s before the peak (buildup matters) and end it about 3–8s after.
3. Snap both edges to the nearest pause or sentence end.
4. Use non-max suppression to remove overlapping clips.
5. Take the top N.

Chat lags video by about 3–8s, so **shift the chat signal back** (make the offset configurable).

## Top title caption (no LLM)
Pick the first strategy that applies:
1. The dominant chat reaction in the window, mapped to a template. For example, KEKW spam becomes "chat lost it 💀", "?????" becomes "what just happened", and "W" spam becomes "HUGE W".
2. The highest-energy transcript phrase (3–7 words, a shouted line), uppercased.
3. A generic template pool by signal type (e.g. "wait for it…", "he did NOT expect that").

The title is editable in the review step.

## Layout (gaming)
- Canvas: 1080x1920.
- Facecam on top (~35%), gameplay on the bottom (~65%).
- The facecam region is found by auto-detection (face detection with MediaPipe on sampled frames → stable bbox) or a per-streamer preset in config.
- No face found → the full gameplay frame gets a center crop, or a blurred-background letterbox.
- Title caption in the top safe zone. Running captions at the facecam/gameplay seam or the lower third.
- Keep clear of the TikTok/IG UI zones: bottom ~20% and right ~12%.

## SFX
- An `assets/sfx/` folder holds the sound effects: end stings, a boom, and a whoosh.
- **Rule:**
  - Always add 1 end sting.
  - Add at most 2 mid-clip hits, and only on the top-scoring peaks.
  - Keep at least 10s between hits.
  - Duck the source audio by about 4–6 dB under each hit.

## CLI
```
clipper run <url> [--top 5] [--min 15 --max 60] [--asr whisper|deepgram] [--layout auto|split|crop]
clipper review [out/<id>]      # list clips w/ scores, open/preview, approve/reject, edit title
clipper rerender <clip.json>   # after editing title/bounds
```

## Stack
Python 3.11+, yt-dlp, ffmpeg, faster-whisper/WhisperX, librosa, numpy/scipy, MediaPipe, PySceneDetect,
chat-downloader, typer (CLI), pydantic + YAML config. Optional: deepgram-sdk.

## Repo layout (proposed)
```
auto-clipper/
  clipper/
    cli.py
    download.py      # yt-dlp, chat, heatmap
    transcribe.py    # whisper / deepgram backends
    signals/         # chat.py, audio.py, transcript.py, visual.py, heatmap.py
    segment.py       # peak finding, boundary snapping, NMS
    score.py
    layout.py        # facecam detect, 9:16 composition
    captions.py      # ASS generation (title + running)
    titles.py        # heuristic title generation
    sfx.py
    render.py        # ffmpeg assembly
    review.py
  assets/sfx/ assets/fonts/
  config.yaml
  out/
```

## Roadmap
- **v1:** steps 1–10 above, with CLI review.
- **v2:** graphics: zoom punches on peaks, emoji/reaction pops, progress bar, animated title, B-roll/memes.
- **v3:** posting via the YouTube Shorts API (easy), the Instagram Graph API (Business account), and the TikTok Content Posting API (needs app approval; drafts until then). Also a scheduler and a tiny web review UI.
- **Later:** learn the weights from performance data, comparing views per clip to its signal breakdown.

## Open questions
- Which Mac chip and how much RAM? This decides the Whisper model size.
- Target streamers/channels? Facecam presets can be added per streamer.
- Font/style preference for captions (default: Montserrat/Komika-style bold, white with black stroke, yellow highlight).
