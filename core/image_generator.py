"""
Image generation via Hugging Face Inference Router.
Model: black-forest-labs/FLUX.1-dev  (free tier, high quality)

Endpoint: https://router.huggingface.co  ← NOT api-inference.huggingface.co (deprecated)
Auth:     HF_TOKEN env var (huggingface.co → Settings → Tokens → read + inference)

Aspect ratio: prompt-injected (9:16 for reels, 4:5 for feed)
Fallback: FFmpeg solid-color gradient.
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

HF_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-dev/v1/text-to-image"

DIMENSIONS   = {"reel": (1080, 1920), "feed": (1080, 1350)}
ASPECT_HINTS = {"reel": "portrait 9:16 vertical composition", "feed": "portrait 4:5 vertical composition"}

TOPIC_COLORS = {
    "stoic": "0D1B2A", "philosoph": "1A1A2E", "motivat": "1A0A2E",
    "mindful": "0D2818", "success": "0A1628", "nature": "0D2010",
    "space": "030418",  "tech": "0D1B2A",    "fitness": "1A0808",
    "wisdom": "1A1408", "life": "0D1020",     "love": "1A0818",
}


class ImageGenerator:
    def __init__(self):
        self.hf_token = os.environ.get("HF_TOKEN")
        if not self.hf_token:
            raise EnvironmentError("HF_TOKEN not set.")

    async def generate(self, prompt: str, post_type: str = "reel") -> Path:
        w, h = DIMENSIONS.get(post_type, (1080, 1920))
        slug = hashlib.md5(f"{prompt}{post_type}".encode()).hexdigest()[:12]
        out  = ASSETS_DIR / f"{slug}.png"

        if out.exists():
            log.info(f"Image cache hit: {out}")
            return out

        try:
            return await self._call_with_retry(prompt, post_type, out, w, h)
        except Exception as e:
            log.warning(f"Image generation failed: {e}")

        log.warning("Falling back to gradient image.")
        return self._gradient(w, h, out, prompt)

    async def _call_with_retry(self, prompt: str, post_type: str, out: Path, w: int, h: int) -> Path:
        last_exc = None
        for attempt in range(3):
            try:
                return await self._call(prompt, post_type, out, w, h)
            except Exception as e:
                last_exc = e
                err_str = str(e)
                if "503" in err_str or "loading" in err_str.lower():
                    # Model is cold-starting — wait and retry
                    log.warning(f"Model loading, waiting 20s (attempt {attempt + 1}/3)")
                    await asyncio.sleep(20)
                elif "429" in err_str:
                    log.warning(f"Rate limited, waiting 60s (attempt {attempt + 1}/3)")
                    await asyncio.sleep(60)
                else:
                    log.warning(f"Attempt {attempt + 1}/3 failed: {e}")
                    await asyncio.sleep(3)
        raise last_exc

    async def _call(self, prompt: str, post_type: str, out: Path, w: int, h: int) -> Path:
        aspect_hint = ASPECT_HINTS.get(post_type, "portrait 9:16 vertical composition")
        enhanced = (
            f"{prompt}. {aspect_hint}. "
            "Cinematic dramatic lighting, dark moody atmosphere, "
            "ultra high quality, professional photography, Instagram-ready. "
            "No text or watermarks."
        )
        log.info(f"Calling FLUX.1-dev via HF router | post_type={post_type}")

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                HF_URL,
                headers={
                    "Authorization": f"Bearer {self.hf_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "inputs": enhanced,
                    "parameters": {
                        "width": w,
                        "height": h,
                    }
                },
            )

        if response.status_code != 200:
            raise RuntimeError(f"HF {response.status_code}: {response.text[:200]}")

        content_type = response.headers.get("content-type", "")
        if "image" not in content_type:
            raise RuntimeError(f"Expected image, got {content_type}: {response.text[:200]}")

        out.write_bytes(response.content)
        log.info(f"Image saved: {out} ({len(response.content) // 1024} KB)")
        return out

    def _gradient(self, w: int, h: int, out: Path, prompt: str) -> Path:
        color = next(
            (c for kw, c in TOPIC_COLORS.items() if kw in prompt.lower()), "0D1B2A"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi",
             "-i", f"color=c=0x{color}:s={w}x{h}",
             "-frames:v", "1", str(out)],
            capture_output=True,
        )
        log.info(f"Gradient saved: {out}")
        return out