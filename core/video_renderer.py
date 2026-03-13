"""
FFmpeg-powered video renderer.
 
Instagram API video requirements:
  - Container: MP4, moov atom at front (faststart)
  - Video codec: H264, progressive scan, closed GOP, yuv420p (4:2:0)
  - Audio codec: AAC, max 48kHz sample rate, max 128kbps
  - Frame rate: 23-60 FPS
  - Aspect ratio: 9:16 for Reels (1080x1920)
  - Min duration: 3 seconds
 
Features:
  - Ken Burns slow zoom effect
  - Text overlays (Hook / Body / CTA) with drop shadows
  - Audio merge with fade in/out
  - Handles both real audio and silent fallback
"""
 
import hashlib
import logging
import subprocess
from pathlib import Path
 
log = logging.getLogger("oracle.renderer")
 
OUTPUT_DIR = Path("assets/output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
 
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
 
DIMENSIONS = {
    "reel": (1080, 1920),
    "feed": (1080, 1350),
}
 
VIDEO_DURATION = 30
FRAMERATE = 30
AUDIO_FADE = 2
KEN_BURNS_ZOOM = 0.04
 
 
class VideoRenderer:
    def render(
        self,
        image_path: Path,
        audio_path: Path,
        text_layers: list[dict],
        post_type: str = "reel",
    ) -> Path:
        width, height = DIMENSIONS.get(post_type, (1080, 1920))
        img_hash = hashlib.md5(str(image_path).encode()).hexdigest()[:10]
        output_path = OUTPUT_DIR / f"{img_hash}_{post_type}.mp4"
 
        # Check if audio file is a valid audio file
        audio_valid = self._is_valid_audio(audio_path)
        if not audio_valid:
            log.warning(f"Invalid audio file detected: {audio_path} — using silent fallback")
            audio_path = self._make_silent_audio()
 
        filter_complex = self._build_filter_complex(width, height, text_layers)
        cmd = self._build_cmd(image_path, audio_path, output_path, filter_complex, width, height)
 
        log.info(f"Rendering video: {output_path.name}")
        result = subprocess.run(cmd, capture_output=True, text=True)
 
        if result.returncode != 0:
            log.error(f"FFmpeg stderr:\n{result.stderr}")
            raise RuntimeError(f"FFmpeg rendering failed (exit {result.returncode})")
 
        size_mb = output_path.stat().st_size / (1024 * 1024)
        log.info(f"Render complete: {output_path} ({size_mb:.1f} MB)")
        return output_path
 
    def _is_valid_audio(self, audio_path: Path) -> bool:
        """Check if the file is actually an audio file using ffprobe."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "a:0",
                    "-show_entries", "stream=codec_type",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(audio_path),
                ],
                capture_output=True, text=True, timeout=10,
            )
            return "audio" in result.stdout.strip()
        except Exception:
            return False
 
    def _make_silent_audio(self) -> Path:
        """Generate a silent audio track as fallback."""
        silent_path = Path("assets/audio/silent_30s.mp3")
        silent_path.parent.mkdir(parents=True, exist_ok=True)
        if not silent_path.exists():
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t", str(VIDEO_DURATION),
                "-q:a", "9", "-acodec", "libmp3lame",
                str(silent_path),
            ], capture_output=True)
        return silent_path
 
    def _build_filter_complex(
        self, width: int, height: int, text_layers: list[dict]
    ) -> str:
        filters = []
        total_frames = VIDEO_DURATION * FRAMERATE
        zoom_inc = KEN_BURNS_ZOOM / total_frames
 
        # Scale + crop
        filters.append(
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1[scaled]"
        )
 
        # Ken Burns zoom
        filters.append(
            f"[scaled]zoompan="
            f"z='min(zoom+{zoom_inc:.6f},1+{KEN_BURNS_ZOOM})':"
            f"x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':"
            f"d={total_frames}:s={width}x{height}:fps={FRAMERATE}[zoomed]"
        )
 
        # Text overlays
        prev = "zoomed"
        for i, layer in enumerate(text_layers):
            next_lbl = f"text{i}" if i < len(text_layers) - 1 else "vout"
            font = FONT_BOLD if layer.get("bold") else FONT_REGULAR
            text = self._escape(layer["text"])
            color = layer.get("color", "#FFFFFF").lstrip("#")
            shadow = layer.get("shadow_color", "#464646").lstrip("#")
            size = layer.get("font_size", 60)
            y_pct = layer.get("y_position", 0.5)
            appear = layer.get("appear_at", 0)
 
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
            filters.append("[zoomed]copy[vout]")
 
        # Audio: trim + fade + volume
        filters.append(
            f"[1:a]atrim=0:{VIDEO_DURATION},"
            f"asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={AUDIO_FADE},"
            f"afade=t=out:st={VIDEO_DURATION - AUDIO_FADE}:d={AUDIO_FADE},"
            f"volume=0.4[aout]"
        )
 
        return ";".join(filters)
 
    def _build_cmd(
        self,
        image_path: Path,
        audio_path: Path,
        output_path: Path,
        filter_complex: str,
        width: int,
        height: int,
    ) -> list[str]:
        return [
            "ffmpeg", "-y",
            "-loop", "1",
            "-framerate", str(FRAMERATE),
            "-i", str(image_path),
            "-i", str(audio_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            # Video — Instagram spec
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",      # 4:2:0 chroma subsampling required
            "-g", str(FRAMERATE * 2),   # Closed GOP = keyframe every 2s
            "-bf", "2",                 # B-frames for H264
            # Audio — Instagram spec: AAC, 48kHz, max 128kbps
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "48000",             # 48kHz (Instagram max)
            "-ac", "2",                 # Stereo
            # Output
            "-t", str(VIDEO_DURATION),
            "-movflags", "+faststart",  # moov atom at front (required)
            str(output_path),
        ]
 
    @staticmethod
    def _escape(text: str) -> str:
        """Escape characters for FFmpeg drawtext."""
        return (
            text
            .replace("'", "\u2019")
            .replace(":", "\\:")
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("[", "\\[")
            .replace("]", "\\]")
        )
 