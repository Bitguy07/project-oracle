"""
Image generation via Gemini 2.0 Flash (native image output).

Model:  gemini-2.0-flash-preview-image-generation
        — free tier, works in India, ~500 images/day
API:    client.models.generate_content() with responseModalities=["IMAGE", "TEXT"]
        DO NOT use generate_images() — that's Imagen 3 (paid, 404s on free tier)

Aspect ratio hint is baked into the prompt text since GenerateContentConfig
does not accept image_config for this model.

Fallback: FFmpeg solid-color gradient.
"""

import asyncio
import hashlib
import logging
import os
import subprocess
from pathlib import Path

from google import genai
from google.genai import types

log = logging.getLogger("oracle.image")

ASSETS_DIR = Path("assets/images")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

DIMENSIONS = {"reel": (1080, 1920), "feed": (1080, 1350)}
# Aspect ratio hint injected into the prompt — the model respects these phrases
ASPECT_HINTS = {"reel": "portrait 9:16 vertical", "feed": "portrait 4:5 vertical"}

# Correct model name (as of early 2026, free tier)
MODEL = "gemini-2.5-flash-image"

TOPIC_COLORS = {
    "stoic": "0D1B2A", "philosoph": "1A1A2E", "motivat": "1A0A2E",
    "mindful": "0D2818", "success": "0A1628", "nature": "0D2010",
    "space": "030418",  "tech": "0D1B2A",    "fitness": "1A0808",
    "wisdom": "1A1408", "life": "0D1020",     "love": "1A0818",
}


class ImageGenerator:
    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY not set.")
        self.client = genai.Client(api_key=api_key)

    async def generate(self, prompt: str, post_type: str = "reel") -> Path:
        w, h = DIMENSIONS.get(post_type, (1080, 1920))
        slug = hashlib.md5(f"{prompt}{post_type}".encode()).hexdigest()[:12]
        out  = ASSETS_DIR / f"{slug}.png"

        if out.exists():
            log.info(f"Image cache hit: {out}")
            return out

        try:
            return await self._call(prompt, post_type, out)
        except Exception as e:
            log.warning(f"Gemini image generation failed: {e}")

        log.warning("Image API failed — using gradient fallback.")
        return self._gradient(w, h, out, prompt)

    async def _call(self, prompt: str, post_type: str, out: Path) -> Path:
        aspect_hint = ASPECT_HINTS.get(post_type, "portrait 9:16 vertical")
        enhanced = (
            f"{prompt}. "
            f"Composition: {aspect_hint}. "
            "Cinematic dramatic lighting, dark moody atmosphere, "
            "ultra high quality, professional photography, Instagram-ready. "
            "No text or watermarks in the image."
        )
        log.info(f"Calling {MODEL} for post_type={post_type}")

        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=MODEL,
            contents=enhanced,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )

        # Response.candidates[0].content.parts may hold the image
        candidates = getattr(response, "candidates", None)
        parts = []
        if candidates:
            parts = candidates[0].content.parts
        elif hasattr(response, "parts"):
            parts = response.parts

        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None and inline.data:
                out.write_bytes(inline.data)
                log.info(f"Image saved: {out} ({len(inline.data) // 1024} KB)")
                return out

        raise RuntimeError(
            f"No image returned by {MODEL}. Parts: {[type(p).__name__ for p in parts]}"
        )

    def _gradient(self, w: int, h: int, out: Path, prompt: str) -> Path:
        color = next(
            (c for kw, c in TOPIC_COLORS.items() if kw in prompt.lower()), "0D1B2A"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"color=c=0x{color}:s={w}x{h}",
                "-frames:v", "1", str(out),
            ],
            capture_output=True,
        )
        log.info(f"Gradient fallback saved: {out}")
        return out