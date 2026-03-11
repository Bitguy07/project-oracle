"""
core/image_generator.py
Zero-cost image generation with professional fallback chain.

Chain (in order):
  1. Pollinations.ai — reduced resolution (768px), short prompt, no model specified
  2. Pollinations.ai — retry with different seed
  3. Pollinations.ai — third attempt
  4. Local gradient background (NEVER fails — pure FFmpeg)
"""

import asyncio
import hashlib
import logging
import os
import random
import subprocess
import urllib.parse
from pathlib import Path

import httpx

log = logging.getLogger("oracle.image")

ASSETS_DIR = Path("assets/images")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

# Dimensions per post type
DIMENSIONS = {
    "reel": (1080, 1920),
    "feed": (1080, 1350),
}

# Reduced dimensions for Pollinations (avoids 500 errors on high-res)
POLLINATIONS_DIMENSIONS = {
    "reel": (768, 1344),
    "feed": (768, 960),
}


class ImageGenerator:
    def __init__(self):
        self.hf_token = os.environ.get("HF_TOKEN")

    async def generate(self, prompt: str, post_type: str = "reel") -> Path:
        """
        Generate image with fallback chain.
        Never raises — worst case returns local gradient background.
        """
        width, height = DIMENSIONS.get(post_type, (1080, 1920))
        poll_w, poll_h = POLLINATIONS_DIMENSIONS.get(post_type, (768, 1344))

        slug = hashlib.md5(f"{prompt}{post_type}".encode()).hexdigest()[:12]
        output_path = ASSETS_DIR / f"{slug}.png"

        if output_path.exists():
            log.info(f"Image cache hit: {output_path}")
            return output_path

        # Shorten prompt to max 80 chars for URL safety
        short_prompt = self._shorten_prompt(prompt, max_chars=80)

        # ── Try Pollinations 3 times with different seeds ──────────────────
        for attempt in range(3):
            try:
                path = await self._pollinations(
                    short_prompt, poll_w, poll_h, output_path
                )
                log.info(f"Pollinations succeeded on attempt {attempt+1}: {path}")
                return path
            except Exception as e:
                log.warning(f"Pollinations attempt {attempt+1} failed: {e}")
                await asyncio.sleep(3)

        # ── All APIs failed → local gradient (never fails) ─────────────────
        log.warning("All image APIs failed. Generating local gradient background.")
        return self._generate_gradient(width, height, output_path, prompt)

    def _shorten_prompt(self, prompt: str, max_chars: int = 80) -> str:
        """Shorten prompt to avoid Pollinations URL length issues."""
        if len(prompt) <= max_chars:
            return prompt.strip()
        return prompt[:max_chars].rsplit(" ", 1)[0].strip()

    async def _pollinations(
        self,
        prompt: str,
        width: int,
        height: int,
        output_path: Path,
    ) -> Path:
        """
        Fetch image from Pollinations.ai.
        Uses random seed each call, no model specified (uses their default stable model).
        """
        seed = random.randint(1, 999999)
        encoded = urllib.parse.quote(prompt)

        # No &model= parameter — let Pollinations use its most stable default
        url = (
            f"https://image.pollinations.ai/prompt/{encoded}"
            f"?width={width}&height={height}"
            f"&seed={seed}&nologo=true"
        )

        log.info(f"Pollinations: prompt='{prompt[:50]}...' seed={seed} size={width}x{height}")

        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
            r = await client.get(url)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            if len(r.content) < 5000:
                raise RuntimeError(f"Response too small ({len(r.content)} bytes) — likely error page")

        output_path.write_bytes(r.content)
        log.info(f"Image saved: {output_path} ({len(r.content)/1024:.0f} KB)")
        return output_path

    def _generate_gradient(
        self,
        width: int,
        height: int,
        output_path: Path,
        prompt: str,
    ) -> Path:
        """
        Generate a solid color background using FFmpeg.
        Uses topic keywords to pick a fitting dark color.
        Never fails — pure local generation, no internet needed.
        """
        color = self._pick_color(prompt)

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=0x{color}:s={width}x{height}",
            "-frames:v", "1",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.warning(f"FFmpeg gradient failed: {result.stderr[:200]}")
            # Absolute last resort: black image
            cmd2 = [
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", f"color=c=black:s={width}x{height}",
                "-frames:v", "1",
                str(output_path),
            ]
            subprocess.run(cmd2, capture_output=True)

        log.info(f"Gradient image saved: {output_path}")
        return output_path

    @staticmethod
    def _pick_color(prompt: str) -> str:
        """Pick a dark background color based on topic mood."""
        prompt_lower = prompt.lower()
        color_map = {
            "stoic": "0D1B2A",
            "philosoph": "1A1A2E",
            "motivat": "1A0A2E",
            "mindful": "0D2818",
            "success": "0A1628",
            "nature": "0D2010",
            "space": "030418",
            "tech": "0D1B2A",
            "fitness": "1A0808",
            "love": "1A0818",
            "wisdom": "1A1408",
            "life": "0D1020",
        }
        for keyword, color in color_map.items():
            if keyword in prompt_lower:
                return color
        return "0D1B2A"  # Default: deep navy