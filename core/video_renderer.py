"""
core/video_renderer.py

AI-Guided editing: video_style from IntelligenceEngine selects FFmpeg filter preset.
  slow_zoom   — Ken Burns slow zoom (default)
  static      — No movement, clean static image
  pulse       — Subtle zoom pulse in/out rhythm
  fade_drift  — Slow horizontal drift + fade

Fade-in: First 1.5 seconds always fade from black. Always on.

Instagram spec:
  - MP4, H264, yuv420p, closed GOP
  - AAC audio, 48kHz, 128kbps stereo
  - moov atom at front (faststart)
  - 9:16 for Reels (1080x1920)
"""

import hashlib
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("oracle.renderer")

OUTPUT_DIR = Path("assets/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

FONT_BOLD    = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

DIMENSIONS = {"reel": (1080, 1920), "feed": (1080, 1350)}
VIDEO_DURATION = 30
FRAMERATE      = 30
AUDIO_FADE     = 2
FADE_IN_DUR    = 1.5   # seconds — black to image


class VideoRenderer:

    def render(
        self,
        image_path: Path,
        audio_path: Path,
        text_layers: list[dict],
        post_type: str = "reel",
        video_style: str = "slow_zoom",
    ) -> Path:
        width, height = DIMENSIONS.get(post_type, (1080, 1920))
        img_hash = hashlib.md5(str(image_path).encode()).hexdigest()[:10]
        output_path = OUTPUT_DIR / f"{img_hash}_{post_type}.mp4"

        if not self._is_valid_audio(audio_path):
            log.warning("Invalid audio — using silent fallback.")
            audio_path = self._make_silent()

        fc = self._build_filter_complex(width, height, text_layers, video_style)
        cmd = self._build_cmd(image_path, audio_path, output_path, fc)

        log.info(f"Rendering [{video_style}]: {output_path.name}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.error(f"FFmpeg stderr:\n{result.stderr}")
            raise RuntimeError(f"FFmpeg failed (exit {result.returncode})")

        size_mb = output_path.stat().st_size / (1024 * 1024)
        log.info(f"Render complete: {output_path} ({size_mb:.1f} MB)")
        return output_path

    def _build_filter_complex(
        self, width: int, height: int, text_layers: list[dict], video_style: str
    ) -> str:
        filters = []
        total_frames = VIDEO_DURATION * FRAMERATE
        fade_frames  = int(FADE_IN_DUR * FRAMERATE)

        # ── Scale + crop ──────────────────────────────────────────────────────
        filters.append(
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1[scaled]"
        )

        # ── Motion style ──────────────────────────────────────────────────────
        motion = self._motion_filter(video_style, width, height, total_frames)
        filters.append(f"[scaled]{motion}[moved]")

        # ── Fade in from black (always on) ────────────────────────────────────
        filters.append(
            f"[moved]fade=t=in:st=0:d={FADE_IN_DUR}[faded]"
        )

        # ── Text overlays ─────────────────────────────────────────────────────
        prev = "faded"
        for i, layer in enumerate(text_layers):
            next_lbl = f"t{i}" if i < len(text_layers) - 1 else "vout"
            font  = FONT_BOLD if layer.get("bold") else FONT_REGULAR
            text  = self._escape(layer["text"])
            color = layer.get("color", "#FFFFFF").lstrip("#")
            shadow= layer.get("shadow_color", "#000000").lstrip("#")
            size  = layer.get("font_size", 72)
            y_pct = layer.get("y_position", 0.5)
            appear= layer.get("appear_at", 0.6)
            filters.append(
                f"[{prev}]drawtext="
                f"fontfile='{font}':"
                f"text='{text}':"
                f"fontsize={size}:"
                f"fontcolor=0x{color}FF:"
                f"x=(w-text_w)/2:"
                f"y=(h*{y_pct:.3f})-text_h/2:"
                f"shadowcolor=0x{shadow}CC:"
                f"shadowx=3:shadowy=3:"
                f"enable='gte(t,{appear})'[{next_lbl}]"
            )
            prev = next_lbl

        if not text_layers:
            filters.append("[faded]copy[vout]")

        # ── Audio ─────────────────────────────────────────────────────────────
        filters.append(
            f"[1:a]atrim=0:{VIDEO_DURATION},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={AUDIO_FADE},"
            f"afade=t=out:st={VIDEO_DURATION-AUDIO_FADE}:d={AUDIO_FADE},"
            f"volume=0.4[aout]"
        )

        return ";".join(filters)

    def _motion_filter(
        self, style: str, width: int, height: int, total_frames: int
    ) -> str:
        if style == "static":
            return f"trim=0:{VIDEO_DURATION},fps={FRAMERATE}"

        elif style == "pulse":
            # Zooms in then back out rhythmically
            zoom_expr = f"1+0.03*sin(2*PI*t/4)"
            return (
                f"zoompan=z='{zoom_expr}':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={width}x{height}:fps={FRAMERATE}"
            )

        elif style == "fade_drift":
            # Slow horizontal drift left to right
            drift = f"iw/2-(iw/zoom/2)+t*0.5"
            return (
                f"zoompan=z='1.05':"
                f"x='{drift}':y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={width}x{height}:fps={FRAMERATE}"
            )

        else:
            # slow_zoom (default) — classic Ken Burns
            zoom_inc = 0.04 / total_frames
            return (
                f"zoompan="
                f"z='min(zoom+{zoom_inc:.6f},1.04)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={width}x{height}:fps={FRAMERATE}"
            )

    def _build_cmd(
        self,
        image_path: Path,
        audio_path: Path,
        output_path: Path,
        filter_complex: str,
    ) -> list[str]:
        return [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", str(FRAMERATE), "-i", str(image_path),
            "-stream_loop", "-1", "-i", str(audio_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-g", str(FRAMERATE * 2),
            "-bf", "2",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            "-t", str(VIDEO_DURATION),
            "-movflags", "+faststart",
            str(output_path),
        ]

    def _is_valid_audio(self, audio_path: Path) -> bool:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_type",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
                capture_output=True, text=True, timeout=10,
            )
            return "audio" in r.stdout
        except Exception:
            return False

    def _make_silent(self) -> Path:
        out = Path("assets/audio/silent_30s.mp3")
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t", str(VIDEO_DURATION), "-q:a", "9",
                "-acodec", "libmp3lame", str(out),
            ], capture_output=True)
        return out

    @staticmethod
    def _escape(text: str) -> str:
        return (
            text
            .replace("'", "\u2019")
            .replace(":", "\\:")
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("[", "\\[")
            .replace("]", "\\]")
        )