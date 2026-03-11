"""
core/intelligence.py
Gemini 1.5 Flash Intelligence Engine.
Generates: quote/hook, image prompt, captions, hashtags, text layers.
"""

import asyncio
import json
import logging
import os
import re
import textwrap
from typing import Optional

from google import genai
from google.genai import types

log = logging.getLogger("oracle.intelligence")

# ── Gemini Config ──────────────────────────────────────────────────────────────
MODEL_NAME = "gemini-1.5-flash"
GENERATION_CONFIG = {
    "temperature": 0.9,
    "top_p": 0.95,
    "max_output_tokens": 1024,
}

# ── Prompts ────────────────────────────────────────────────────────────────────

CONTENT_PROMPT = """
You are a viral Instagram content strategist. Generate content for a {post_type} about: "{topic}"

Respond ONLY with valid JSON — no markdown, no explanation. Schema:
{{
  "hook": "A powerful 1-line opening (max 12 words). Create curiosity or shock.",
  "body": "The core insight in 2-3 punchy sentences (max 40 words total).",
  "cta": "A direct call-to-action. E.g. 'Save this.' / 'Tag someone who needs this.'",
  "caption": "Full Instagram caption (hook + body + cta + line breaks). Max 200 chars before hashtags.",
  "hashtags": ["#niche1","#niche2","#niche3","#niche4","#niche5","#niche6","#broad1"],
  "image_prompt": "A detailed visual prompt for an AI image generator. Style: cinematic, dramatic lighting, ultra-HD. Describe scene, mood, color palette. Do NOT include text.",
  "color_scheme": {{
    "primary": "#FFFFFF",
    "accent": "#FFD700",
    "shadow": "#000000"
  }}
}}

Rules:
- hashtags: exactly 5-6 niche tags + 1 broad tag. All lowercase with #.
- hook: must be intriguing. Use power words.
- image_prompt: must be evocative, painterly, or photorealistic. Avoid clichés.
- color_scheme: choose colors that match the topic's emotional tone.
"""

IMAGE_PROMPT_REFINER = """
Refine this AI image generation prompt to be more vivid and specific for a {style} aesthetic.
Make it highly detailed, specify lighting, composition, and mood. Max 120 words.

Original: {prompt}

Return ONLY the refined prompt text, nothing else.
"""


class IntelligenceEngine:
    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY environment variable not set.")
        self.client = genai.Client(api_key=api_key)

    async def generate_content(self, topic: str, post_type: str = "reel") -> dict:
        """
        Main content generation. Returns structured dict with all content fields.
        Gemini calls: 1 (combined prompt to save quota)
        """
        prompt = CONTENT_PROMPT.format(topic=topic, post_type=post_type)

        log.info(f"Calling Gemini for topic='{topic}'")
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.9,
                top_p=0.95,
                max_output_tokens=1024,
            )
        )
        raw = response.text.strip()

        # Strip potential markdown code fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error(f"Gemini returned non-JSON: {raw[:200]}")
            raise ValueError(f"Failed to parse Gemini response: {e}")

        # Build final caption with hashtags
        hashtags_str = " ".join(data.get("hashtags", []))
        full_caption = f"{data['caption']}\n\n{hashtags_str}"

        # Build text layers for video renderer
        text_layers = self._build_text_layers(
            hook=data["hook"],
            body=data["body"],
            cta=data["cta"],
            color_scheme=data.get("color_scheme", {}),
            post_type=post_type,
        )

        return {
            "hook": data["hook"],
            "body": data["body"],
            "cta": data["cta"],
            "caption": full_caption,
            "hashtags": data.get("hashtags", []),
            "image_prompt": data["image_prompt"],
            "color_scheme": data.get("color_scheme", {"primary": "#FFFFFF", "accent": "#FFD700", "shadow": "#000000"}),
            "text_layers": text_layers,
        }

    def _build_text_layers(
        self,
        hook: str,
        body: str,
        cta: str,
        color_scheme: dict,
        post_type: str,
    ) -> list[dict]:
        """
        Returns a list of text layer defs consumed by VideoRenderer.
        Each layer: {text, y_position, font_size, color, shadow_color, appear_at, duration}
        """
        primary = color_scheme.get("primary", "#FFFFFF")
        accent = color_scheme.get("accent", "#FFD700")
        shadow = color_scheme.get("shadow", "#000000")

        # Wrap body text for readability
        wrapped_body = "\n".join(textwrap.wrap(body, width=32))

        if post_type == "reel":
            # 9:16 layout (1080×1920)
            return [
                {
                    "text": hook.upper(),
                    "y_position": 0.15,   # 15% from top
                    "font_size": 72,
                    "color": accent,
                    "shadow_color": shadow,
                    "appear_at": 0.5,
                    "duration": 99,
                    "bold": True,
                },
                {
                    "text": wrapped_body,
                    "y_position": 0.50,   # Centre
                    "font_size": 48,
                    "color": primary,
                    "shadow_color": shadow,
                    "appear_at": 1.5,
                    "duration": 99,
                    "bold": False,
                },
                {
                    "text": f"↓ {cta}",
                    "y_position": 0.85,   # Near bottom
                    "font_size": 52,
                    "color": accent,
                    "shadow_color": shadow,
                    "appear_at": 3.0,
                    "duration": 99,
                    "bold": True,
                },
            ]
        else:
            # 4:5 layout (1080×1350) — Feed post
            return [
                {
                    "text": hook.upper(),
                    "y_position": 0.12,
                    "font_size": 68,
                    "color": accent,
                    "shadow_color": shadow,
                    "appear_at": 0.3,
                    "duration": 99,
                    "bold": True,
                },
                {
                    "text": wrapped_body,
                    "y_position": 0.48,
                    "font_size": 46,
                    "color": primary,
                    "shadow_color": shadow,
                    "appear_at": 0.3,
                    "duration": 99,
                    "bold": False,
                },
                {
                    "text": cta,
                    "y_position": 0.82,
                    "font_size": 50,
                    "color": accent,
                    "shadow_color": shadow,
                    "appear_at": 0.3,
                    "duration": 99,
                    "bold": True,
                },
            ]
