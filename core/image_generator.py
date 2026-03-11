"""
core/image_generator.py
Zero-cost image generation via Pollinations.ai.
No API key required. Uses their public inference endpoint.
Fallback: Hugging Face Inference API (free tier, requires HF_TOKEN).
"""

import asyncio
import hashlib
import logging
import os
import time
import urllib.parse
from pathlib import Path

import httpx

log = logging.getLogger("oracle.image")

ASSETS_DIR = Path("assets/images")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

# Pollinations.ai endpoint (no auth required)
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
POLLINATIONS_PARAMS = {
    "width": None,    # Set per post_type
    "height": None,
    "model": "flux",  # flux / turbo / dreamshaper
    "seed": None,     # Dynamic seed per post
    "nologo": "true",
    "enhance": "true",
}

# Hugging Face fallback
HF_API_URL = "https://api-inference.huggingface.co/models/stabilityai/stable-diffusion-xl-base-1.0"

# Dimensions per post type
DIMENSIONS = {
    "reel": (1080, 1920),
    "feed": (1080, 1350),
}


class ImageGenerator:
    def __init__(self):
        self.hf_token = os.environ.get("HF_TOKEN")

    async def generate(self, prompt: str, post_type: str = "reel") -> Path:
        """
        Generate an image for the given prompt and post type.
        Tries Pollinations.ai first, falls back to HuggingFace.
        Returns the local path of the saved image.
        """
        width, height = DIMENSIONS.get(post_type, (1080, 1920))

        # Use prompt hash as filename for deduplication
        slug = hashlib.md5(f"{prompt}{post_type}".encode()).hexdigest()[:12]
        output_path = ASSETS_DIR / f"{slug}.png"

        if output_path.exists():
            log.info(f"Image cache hit: {output_path}")
            return output_path

        # ── Try Pollinations.ai ──────────────────────────────────────────────
        try:
            path = await self._pollinations(prompt, width, height, output_path)
            log.info(f"Pollinations image saved: {path}")
            return path
        except Exception as e:
            log.warning(f"Pollinations failed ({e}). Trying HuggingFace...")

        # ── Try HuggingFace fallback ─────────────────────────────────────────
        if self.hf_token:
            try:
                path = await self._huggingface(prompt, width, height, output_path)
                log.info(f"HuggingFace image saved: {path}")
                return path
            except Exception as e:
                log.error(f"HuggingFace also failed: {e}")

        raise RuntimeError("All image generation backends failed.")

    async def _pollinations(
        self, prompt: str, width: int, height: int, output_path: Path
    ) -> Path:
        seed = int(time.time()) % 999999
        encoded = urllib.parse.quote(prompt)
        url = (
            f"{POLLINATIONS_URL.format(prompt=encoded)}"
            f"?width={width}&height={height}"
            f"&model=flux&seed={seed}&nologo=true&enhance=true"
        )
        log.debug(f"Pollinations URL: {url[:120]}...")

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            for attempt in range(3):
                r = await client.get(url)
                if r.status_code == 200:
                    break
                log.warning(f"Pollinations attempt {attempt+1} failed: HTTP {r.status_code}")
                await asyncio.sleep(5)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            if len(r.content) < 5000:
                raise RuntimeError("Response too small — likely an error page.")

        output_path.write_bytes(r.content)
        return output_path

    async def _huggingface(
        self, prompt: str, width: int, height: int, output_path: Path
    ) -> Path:
        headers = {"Authorization": f"Bearer {self.hf_token}"}
        payload = {
            "inputs": prompt,
            "parameters": {
                "width": min(width, 1024),   # HF free tier caps at 1024
                "height": min(height, 1024),
                "num_inference_steps": 4,    # Fast / schnell model
            },
        }
        async with httpx.AsyncClient(timeout=180.0) as client:
            # HF models may be cold-starting; retry up to 3 times
            for attempt in range(3):
                r = await client.post(HF_API_URL, headers=headers, json=payload)
                if r.status_code == 503:
                    log.info(f"HF model loading (attempt {attempt+1}/3)... waiting 20s")
                    await asyncio.sleep(20)
                    continue
                r.raise_for_status()
                break
            else:
                raise RuntimeError("HuggingFace model never warmed up.")

        output_path.write_bytes(r.content)
        return output_path
