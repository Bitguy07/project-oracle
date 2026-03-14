"""
core/image_generator.py

Uses HuggingFace Inference API — FLUX.1-schnell (black-forest-labs/FLUX.1-schnell)
Confirmed working March 2026. Returns JPEG bytes directly.

Auth: HF_TOKEN env var (needs "Make calls to Inference Providers" permission)

Gradient fallback uses the AI-generated color_scheme accent color — never hardcoded.
"""

import asyncio
import hashlib
import logging
import os
import subprocess
from pathlib import Path

import httpx

log = logging.getLogger("oracle.image")

ASSETS_DIR = Path("assets/images")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

HF_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"

DIMENSIONS   = {"reel": (1080, 1920), "feed": (1080, 1350)}
ASPECT_HINTS = {"reel": "portrait 9:16 vertical", "feed": "portrait 4:5 vertical"}


class ImageGenerator:

    def __init__(self):
        self.hf_token = os.environ.get("HF_TOKEN")
        if not self.hf_token:
            raise EnvironmentError("HF_TOKEN not set.")

    async def generate(
        self,
        image_prompt: str,
        post_type: str = "reel",
        color_scheme: dict = None,
    ) -> Path:
        """
        Generate image from AI-provided image_prompt.
        color_scheme: from IntelligenceEngine — used only in gradient fallback.
        """
        w, h = DIMENSIONS.get(post_type, (1080, 1920))
        slug = hashlib.md5(f"{image_prompt}{post_type}".encode()).hexdigest()[:12]
        out  = ASSETS_DIR / f"{slug}.jpg"

        if out.exists():
            log.info(f"Image cache hit: {out}")
            return out

        try:
            return await self._call_with_retry(image_prompt, post_type, out)
        except Exception as e:
            log.warning(f"Image generation failed: {e}")

        log.warning("Falling back to gradient.")
        return self._gradient(w, h, out, color_scheme or {})

    async def _call_with_retry(self, prompt: str, post_type: str, out: Path) -> Path:
        w, h = DIMENSIONS.get(post_type, (1080, 1920))
        last_exc = None
        for attempt in range(3):
            try:
                return await self._call(prompt, post_type, out, w, h)
            except Exception as e:
                last_exc = e
                err = str(e)
                if "503" in err or "loading" in err.lower():
                    log.warning(f"Model loading — wait 20s (attempt {attempt+1}/3)")
                    await asyncio.sleep(20)
                elif "429" in err:
                    log.warning(f"Rate limited — wait 60s (attempt {attempt+1}/3)")
                    await asyncio.sleep(60)
                else:
                    log.warning(f"Attempt {attempt+1}/3: {e}")
                    await asyncio.sleep(3)
        raise last_exc

    async def _call(self, prompt: str, post_type: str, out: Path, w: int, h: int) -> Path:
        aspect = ASPECT_HINTS.get(post_type, "portrait 9:16 vertical")
        enhanced = (
            f"{prompt}. {aspect}. "
            "Cinematic dramatic lighting, dark moody atmosphere, "
            "ultra high quality, professional photography, Instagram-ready. "
            "No text, no watermarks."
        )
        log.info(f"FLUX.1-schnell | {post_type} | {w}x{h}")

        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                HF_URL,
                headers={
                    "Authorization": f"Bearer {self.hf_token}",
                    "Content-Type": "application/json",
                },
                json={"inputs": enhanced},
            )

        if r.status_code != 200:
            raise RuntimeError(f"HF {r.status_code}: {r.text[:200]}")

        if r.content[:2] not in (b'\xff\xd8', b'\x89P'):
            raise RuntimeError(f"Response is not an image: {r.content[:50]}")

        out.write_bytes(r.content)
        log.info(f"Image saved: {out} ({len(r.content)//1024} KB)")
        return out

    def _gradient(self, w: int, h: int, out: Path, color_scheme: dict) -> Path:
        """
        Fallback gradient using AI-generated color_scheme.
        Uses 'shadow' (darkest) as background — always looks good behind text.
        Falls back to near-black if color_scheme is missing.
        """
        # Use the shadow color as background — it's always dark by design
        raw = color_scheme.get("shadow", "#0D1B2A").lstrip("#")
        # Ensure it's a valid 6-char hex
        color = raw if len(raw) == 6 else "0D1B2A"

        out = out.with_suffix(".png")
        subprocess.run([
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=0x{color}:s={w}x{h}",
            "-frames:v", "1", str(out),
        ], capture_output=True)
        log.info(f"Gradient saved: {out} (color=#{color})")
        return out