
Copy

"""
core/image_generator.py
Image generation using Gemini 2.5 Flash Image API.
 
Model: gemini-2.5-flash-image
Free tier: 500 images/day — uses your existing GEMINI_API_KEY, no new signup.
Aspect ratio: 9:16 for reels, 4:5 for feed.
Falls back to local gradient if API fails.
"""
 
import asyncio
import base64
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
 
DIMENSIONS = {
    "reel": (1080, 1920),
    "feed": (1080, 1350),
}
 
ASPECT_RATIOS = {
    "reel": "9:16",
    "feed": "4:5",
}
 
 
class ImageGenerator:
    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY not set.")
        self.client = genai.Client(api_key=api_key)
 
    async def generate(self, prompt: str, post_type: str = "reel") -> Path:
        width, height = DIMENSIONS.get(post_type, (1080, 1920))
        aspect_ratio = ASPECT_RATIOS.get(post_type, "9:16")
 
        slug = hashlib.md5(f"{prompt}{post_type}".encode()).hexdigest()[:12]
        output_path = ASSETS_DIR / f"{slug}.png"
 
        if output_path.exists():
            log.info(f"Image cache hit: {output_path}")
            return output_path
 
        # ── Try Gemini 2.5 Flash Image ─────────────────────────────────────
        try:
            path = await self._gemini_image(prompt, aspect_ratio, output_path)
            log.info(f"Gemini image succeeded: {path}")
            return path
        except Exception as e:
            log.warning(f"Gemini image failed: {e}")
 
        # ── Fallback: local gradient ───────────────────────────────────────
        log.warning("Gemini image failed. Generating local gradient background.")
        return self._generate_gradient(width, height, output_path, prompt)
 
    async def _gemini_image(
        self,
        prompt: str,
        aspect_ratio: str,
        output_path: Path,
    ) -> Path:
        enhanced_prompt = (
            f"{prompt}. "
            f"Cinematic lighting, dramatic atmosphere, "
            f"professional photography, dark moody aesthetic, "
            f"ultra high quality, Instagram-ready visual."
        )
 
        log.info(f"Calling gemini-2.5-flash-image, ratio={aspect_ratio}")
 
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model="gemini-2.5-flash-image",
            contents=enhanced_prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(
                    aspect_ratio=aspect_ratio,
                ),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
 
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                image_data = part.inline_data.data
                if isinstance(image_data, str):
                    image_data = base64.b64decode(image_data)
                output_path.write_bytes(image_data)
                log.info(f"Image saved: {output_path} ({len(image_data)//1024}KB)")
                return output_path
 
        raise RuntimeError("No image data in Gemini response")
 
    def _generate_gradient(
        self,
        width: int,
        height: int,
        output_path: Path,
        prompt: str,
    ) -> Path:
        color = self._pick_color(prompt)
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=0x{color}:s={width}x{height}",
            "-frames:v", "1",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"color=c=black:s={width}x{height}",
                "-frames:v", "1", str(output_path),
            ], capture_output=True)
        log.info(f"Gradient saved: {output_path}")
        return output_path
 
    @staticmethod
    def _pick_color(prompt: str) -> str:
        colors = {
            "stoic": "0D1B2A", "philosoph": "1A1A2E",
            "motivat": "1A0A2E", "mindful": "0D2818",
            "success": "0A1628", "nature": "0D2010",
            "space": "030418", "tech": "0D1B2A",
            "fitness": "1A0808", "love": "1A0818",
            "wisdom": "1A1408", "life": "0D1020",
        }
        p = prompt.lower()
        for kw, c in colors.items():
            if kw in p:
                return c
        return "0D1B2A"
 