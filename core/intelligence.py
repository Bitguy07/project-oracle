"""
Gemini Intelligence Engine.
Generates: quote, image prompt, caption, hashtags, text layers.

Video design philosophy:
  - ONE quote, centered, properly wrapped, nothing else.
  - Body + CTA go in the Instagram CAPTION only, not on the video.
  - Clean, cinematic, minimal.
"""

import asyncio
import json
import logging
import os
import re
import textwrap

import google.generativeai as genai

log = logging.getLogger("oracle.intelligence")

MODEL_NAME = "gemini-2.5-flash-lite"
GENERATION_CONFIG = {
    "temperature": 0.9,
    "top_p": 0.95,
    "max_output_tokens": 1024,
}

CONTENT_PROMPT = """
You are a viral Instagram content strategist. Generate content for a {post_type} about: "{topic}"

Respond ONLY with valid JSON — no markdown, no explanation. Schema:
{{
  "hook": "A powerful quote. Max 10 words. Punchy, thought-provoking. NO colons.",
  "body": "2-3 sentences expanding the idea. Max 35 words. For caption only.",
  "cta": "One short CTA like 'Save this.' or 'Tag someone who needs this.' For caption only.",
  "caption": "Full Instagram caption combining hook + body + cta naturally. Max 200 chars before hashtags.",
  "hashtags": ["#niche1","#niche2","#niche3","#niche4","#niche5","#broad1"],
  "image_prompt": "Detailed cinematic scene for AI image generation. Dramatic lighting, ultra-HD, specific mood and color palette. No text in image.",
  "color_scheme": {{
    "primary": "#FFFFFF",
    "accent": "#FFD700",
    "shadow": "#000000"
  }}
}}

Rules:
- hook: max 10 words. This is the ONLY text shown on the video. Make it count.
- image_prompt: painterly or photorealistic, evocative. No clichés.
- hashtags: 5-6 niche + 1 broad, all lowercase.
- color_scheme: match the topic's emotional tone.
"""


class IntelligenceEngine:
    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY not set.")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(
            MODEL_NAME,
            generation_config=GENERATION_CONFIG,
        )

    async def generate_content(self, topic: str, post_type: str = "reel") -> dict:
        prompt = CONTENT_PROMPT.format(topic=topic, post_type=post_type)

        log.info(f"Calling Gemini for topic='{topic}'")
        response = await asyncio.to_thread(self.model.generate_content, prompt)
        raw = response.text.strip()

        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error(f"Gemini returned non-JSON: {raw[:200]}")
            raise ValueError(f"Failed to parse Gemini response: {e}")

        hashtags_str = " ".join(data.get("hashtags", []))
        full_caption = f"{data['caption']}\n\n{hashtags_str}"

        color_scheme = data.get("color_scheme", {
            "primary": "#FFFFFF",
            "accent":  "#FFD700",
            "shadow":  "#000000",
        })

        text_layers = self._build_text_layers(
            hook=data["hook"],
            color_scheme=color_scheme,
            post_type=post_type,
        )

        return {
            "hook":         data["hook"],
            "body":         data["body"],
            "cta":          data["cta"],
            "caption":      full_caption,
            "hashtags":     data.get("hashtags", []),
            "image_prompt": data["image_prompt"],
            "color_scheme": color_scheme,
            "text_layers":  text_layers,
        }

    def _build_text_layers(
        self,
        hook: str,
        color_scheme: dict,
        post_type: str,
    ) -> list[dict]:
        """
        Returns a single centered quote layer for the video.
        Body and CTA go in the caption only — NOT on the video.

        Word-wrap at 18 chars/line so nothing gets cut off at 1080px width.
        """
        accent = color_scheme.get("accent", "#FFD700")
        shadow = color_scheme.get("shadow", "#000000")

        # Wrap at 18 chars so text fits within 1080px at large font size
        wrapped = "\n".join(textwrap.wrap(hook, width=18))

        # Count lines to adjust font size and vertical position
        line_count = wrapped.count("\n") + 1
        font_size  = 90 if line_count <= 2 else 76 if line_count == 3 else 64

        return [
            {
                "text":         wrapped,
                "y_position":   0.42,   # Slightly above center — visually balanced
                "font_size":    font_size,
                "color":        accent,
                "shadow_color": shadow,
                "appear_at":    0.5,
                "bold":         True,
            }
        ]