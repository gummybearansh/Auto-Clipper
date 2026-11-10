"""Typed configuration (pydantic) loaded from config.yaml with sane defaults."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


class DownloadCfg(BaseModel):
    max_height: int = 1080
    chat: bool = True


class ClipCfg(BaseModel):
    min_len: float = 15
    max_len: float = 60
    pre_peak: tuple[float, float] = (8, 20)
    post_peak: tuple[float, float] = (3, 8)
    snap_window: float = 4.0
    min_gap: float = 0.45


class AsrCfg(BaseModel):
    backend: Literal["whisper", "deepgram"] = "whisper"
    model: str = "small"
    compute_type: str = "int8"
    language: str | None = None
    full_transcript_max_minutes: float = 30
    candidate_margin: float = 30


class SignalsCfg(BaseModel):
    chat_offset: float = 5.0
    smooth_sigma: float = 2.0
    baseline_window: int = 120
    visual_fps: float = 2


class WeightsCfg(BaseModel):
    twitch: dict[str, float] = Field(default_factory=lambda: {
        "chat": 0.35, "audio": 0.25, "emote": 0.15, "keywords": 0.10,
        "speech": 0.10, "visual": 0.05, "clip_mentions": 0.10})
    youtube: dict[str, float] = Field(default_factory=lambda: {
        "heatmap": 0.35, "audio": 0.30, "keywords": 0.15, "speech": 0.10,
        "visual": 0.10, "chat": 0.15, "emote": 0.05})


class LayoutCfg(BaseModel):
    mode: Literal["auto", "split", "crop", "blur"] = "auto"
    facecam_ratio: float = 0.35
    fallback: Literal["blur", "crop"] = "blur"
    fps: int = 30
    presets: dict[str, dict] = Field(default_factory=dict)


class CaptionCfg(BaseModel):
    renderer: Literal["auto", "ass", "pillow"] = "auto"
    font: str = "assets/fonts/Montserrat.ttf"
    font_weight: str = "ExtraBold"
    size: int = 84
    title_size: int = 76
    title_font: str = "assets/fonts/Anton-Regular.ttf"
    title_bg: str = "#FF2E4D"          # sticker fill
    title_color: str = "#FFFFFF"
    title_border: str = "#FFFFFF"
    title_tilt: float = 3.0            # degrees counter-clockwise
    words_per_group: int = 3
    max_group_chars: int = 18
    color: str = "#FFFFFF"
    highlight: str = "#FFE600"
    stroke: str = "#000000"
    stroke_width: int = 7
    uppercase: bool = True


class SfxCfg(BaseModel):
    enabled: bool = True
    dir: str = "assets/sfx"
    max_mid_hits: int = 2
    min_spacing: float = 10.0
    duck_db: float = 5.0
    gain_db: float = -4.0


class RenderCfg(BaseModel):
    crf: int = 20
    preset: str = "medium"
    audio_bitrate: str = "192k"
    loudnorm: bool = True


class CleanupCfg(BaseModel):
    enabled: bool = True          # delete chunk video/audio once its clips are rendered
    excerpt_pad: float = 8.0      # seconds kept around each clip so it can still be re-rendered


class ExportCfg(BaseModel):
    enabled: bool = True
    dir: str = "~/Desktop/gummybear clips"   # one subfolder per link/video


class PublishCfg(BaseModel):
    instagram_handle: str = "@gummybearclips"
    youtube_channel: str = "gummybearclip"
    # twitch login → {"instagram": "@handle", "youtube": "@handle"} used in the credit line
    streamer_handles: dict[str, dict[str, str]] = Field(default_factory=dict)
    # twitch login → extra hashtags placed right after the streamer tag
    extra_tags: dict[str, list[str]] = Field(default_factory=dict)


class MusicCfg(BaseModel):
    enabled: bool = True
    max_score: float = 2.0         # clips scoring below this get background music
    dir: str = "assets/music"
    volume_db: float = -11.0       # music level (≈ 9–10 dB under the voice after ducking)
    duck: bool = True              # dip the music a little whenever someone talks
    duck_threshold: float = 0.06
    duck_ratio: float = 2.5


class Config(BaseModel):
    out_dir: str = "out"
    download: DownloadCfg = DownloadCfg()
    clip: ClipCfg = ClipCfg()
    asr: AsrCfg = AsrCfg()
    signals: SignalsCfg = SignalsCfg()
    weights: WeightsCfg = WeightsCfg()
    layout: LayoutCfg = LayoutCfg()
    captions: CaptionCfg = CaptionCfg()
    sfx: SfxCfg = SfxCfg()
    render: RenderCfg = RenderCfg()
    cleanup: CleanupCfg = CleanupCfg()
    export: ExportCfg = ExportCfg()
    publish: PublishCfg = PublishCfg()
    music: MusicCfg = MusicCfg()

    def path(self, p: str) -> Path:
        """Resolve a config path relative to the repo root."""
        pp = Path(p).expanduser()
        return pp if pp.is_absolute() else ROOT / pp


def load_config(path: str | Path | None = None) -> Config:
    candidates = [Path(path)] if path else [Path.cwd() / "config.yaml", ROOT / "config.yaml"]
    for c in candidates:
        if c.exists():
            data = yaml.safe_load(c.read_text()) or {}
            return Config.model_validate(data)
    return Config()
