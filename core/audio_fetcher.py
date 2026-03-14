"""
core/audio_fetcher.py

Generates background music using MusicGen Small (facebook/musicgen-small).
Receives a descriptive music_prompt from IntelligenceEngine — no keyword mapping needed.
The prompt is already a rich natural language sentence like:
  "dark cinematic ambient piano, slow tempo, no vocals, contemplative mood"

Model: ~300MB, cached after first download.
Generation: ~4-5 min for 15s on GitHub Actions CPU (2 cores). Acceptable for 6hr pipeline.
License: CC-BY-NC 4.0 — free for non-commercial Instagram content.

Fallback: bundled MP3 from assets/bundled_audio/, then silent audio.
"""

import hashlib
import logging
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger("oracle.audio")

ASSETS_DIR  = Path("assets/audio")
BUNDLED_DIR = Path("assets/bundled_audio")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
BUNDLED_DIR.mkdir(parents=True, exist_ok=True)

MODEL_ID         = "facebook/musicgen-small"
DURATION_SECONDS = 15   # 15s on GH Actions (~4min CPU), 30s locally (~10min)


class AudioFetcher:

    def __init__(self):
        self._model     = None
        self._processor = None

    async def fetch(self, music_prompt: str, post_type: str = "reel") -> Path:
        """
        Generate music from descriptive music_prompt.
        music_prompt: full sentence from IntelligenceEngine e.g.
          "melancholic violin strings, slow tempo, emotional, no vocals"
        """
        import asyncio

        slug = hashlib.md5(f"{music_prompt}{DURATION_SECONDS}".encode()).hexdigest()[:12]
        out  = ASSETS_DIR / f"music_{slug}.wav"

        if out.exists() and out.stat().st_size > 10_000:
            log.info(f"Audio cache hit: {out}")
            return out

        log.info(f"Generating music: '{music_prompt[:70]}...'")
        try:
            path = await asyncio.to_thread(self._generate, music_prompt, out)
            return path
        except Exception as e:
            log.error(f"MusicGen failed: {e}")

        # Bundled MP3 fallback
        bundled = self._find_bundled()
        if bundled:
            log.info(f"Using bundled audio: {bundled}")
            return bundled

        # Silent fallback
        log.info("All audio sources failed — silent fallback.")
        return self._make_silent()

    def _generate(self, music_prompt: str, out: Path) -> Path:
        import torch
        import scipy.io.wavfile
        from transformers import AutoProcessor, MusicgenForConditionalGeneration

        self._load_model()

        max_tokens = DURATION_SECONDS * 50  # 50 tokens = 1 second

        inputs = self._processor(
            text=[music_prompt],
            padding=True,
            return_tensors="pt",
        )

        with torch.no_grad():
            audio_values = self._model.generate(
                **inputs,
                do_sample=True,
                guidance_scale=3,
                max_new_tokens=max_tokens,
            )

        sampling_rate = self._model.config.audio_encoder.sampling_rate
        audio_data    = audio_values[0, 0].numpy()

        # Normalize
        max_val = np.abs(audio_data).max()
        if max_val > 0:
            audio_data = (audio_data / max_val * 0.9).astype(np.float32)

        scipy.io.wavfile.write(str(out), rate=sampling_rate, data=audio_data)
        log.info(f"Music saved: {out} ({out.stat().st_size//1024} KB, {DURATION_SECONDS}s)")
        return out

    def _load_model(self):
        if self._model is not None:
            return
        log.info("Loading MusicGen Small (first run downloads ~300MB)...")
        from transformers import AutoProcessor, MusicgenForConditionalGeneration
        self._processor = AutoProcessor.from_pretrained(MODEL_ID)
        self._model     = MusicgenForConditionalGeneration.from_pretrained(MODEL_ID)
        self._model.eval()
        log.info("MusicGen loaded.")

    def _find_bundled(self) -> Path | None:
        for f in BUNDLED_DIR.glob("*.mp3"):
            if f.stat().st_size > 50_000:
                return f
        return None

    def _make_silent(self) -> Path:
        out = ASSETS_DIR / f"silent_{DURATION_SECONDS}s.mp3"
        if not out.exists():
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t", str(DURATION_SECONDS),
                "-q:a", "9", "-acodec", "libmp3lame", str(out),
            ], capture_output=True)
        return out