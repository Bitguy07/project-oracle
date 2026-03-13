"""
Gemini Intelligence Engine.
Generates: quote, image prompt, caption, hashtags, text layers.

Video design philosophy (MUST be followed):
  - ONE quote (hook), centered, properly wrapped, nothing else on video.
  - Body + CTA go in the Instagram CAPTION only — NOT rendered on video.
  - Clean, cinematic, minimal aesthetic.
"""

import asyncio
import json
import logging
import os
import re
import textwrap
import google.generativeai as genai

log = logging.getLogger("oracle.intelligence")

MODEL_NAME = "gemini-1.5-flash"           # or "gemini-2.0-flash" / "gemini-2.5-flash" if available & stable
# MODEL_NAME = "gemini-2.5-flash-lite"    # ← switch back only if this version becomes reliable again

GENERATION_CONFIG = {
    "temperature": 0.75,    # lowered from 0.9 — less hallucination / instruction ignoring
    "top_p": 0.92,
    "max_output_tokens": 1024,
}

CONTENT_PROMPT = """You are a viral Instagram content creator.

Rules you MUST follow exactly — do NOT mention them in the output:
- Return **ONLY** valid JSON — nothing else.
- NO explanation, NO preamble, NO markdown, NO ``` fences, NO comments, NO extra text.
- Do NOT repeat the instructions or philosophy.
- Do NOT add any text before or after the JSON object.

Task: Generate content for a {post_type} about: "{topic}"

Return exactly this JSON structure and nothing else:

{{
  "hook": "A powerful quote. Max 10 words. Punchy, thought-provoking. NO colons.",
  "body": "2-3 sentences expanding the idea. Max 35 words total. For caption only.",
  "cta": "One short CTA like 'Save this.' or 'Tag a friend who needs this.' For caption only.",
  "caption": "Full Instagram caption combining hook + body + cta naturally. Max 200 chars before hashtags.",
  "hashtags": ["#niche1","#niche2","#niche3","#niche4","#niche5","#broad1"],
  "image_prompt": "Detailed cinematic scene for AI image generation. Dramatic lighting, ultra-HD, specific mood and color palette. No text in image.",
  "color_scheme": {{
    "primary": "#FFFFFF",
    "accent": "#FFD700",
    "shadow": "#000000"
  }}
}}

Constraints (apply silently):
- hook:      max 10 words — this is the **only** text that appears on the video
- image_prompt:  evocative, painterly or photorealistic, no clichés, no text overlays
- hashtags:  5–6 niche + 1 broad, all lowercase with # prefix
- color_scheme:  colors must match the emotional tone of the topic
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
        log.info(f"Generating content for topic='{topic}' ({post_type})")

        try:
            response = await asyncio.to_thread(self.model.generate_content, prompt)
            raw = response.text.strip()
        except Exception as e:
            log.error(f"Gemini API call failed: {e}")
            raise

        # ── Aggressive cleaning ───────────────────────────────────────────────
        # Remove common violations
        raw = re.sub(r'^.*?(\{.*)$', r'\1', raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r'^(.*?\})\s*.*$', r'\1', raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE | re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE | re.IGNORECASE)
        raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error(f"JSON parse failed. First 300 chars of raw:\n{raw[:300]}")
            raise ValueError(f"Gemini did not return valid JSON: {e}\nRaw:\n{raw[:400]}...")

        # Default fallback color scheme
        color_scheme = data.get("color_scheme", {
            "primary": "#FFFFFF",
            "accent": "#FFD700",
            "shadow": "#000000",
        })

        text_layers = self._build_text_layers(
            hook=data["hook"],
            color_scheme=color_scheme,
        )

        hashtags_list = data.get("hashtags", [])
        hashtags_str = " ".join(hashtags_list)
        full_caption = f"{data['caption'].strip()}\n\n{hashtags_str}".strip()

        return {
            "hook": data["hook"],
            "body": data["body"],
            "cta": data["cta"],
            "caption": full_caption,
            "hashtags": hashtags_list,
            "image_prompt": data["image_prompt"],
            "color_scheme": color_scheme,
            "text_layers": text_layers,
        }

    def _build_text_layers(
        self,
        hook: str,
        color_scheme: dict,
    ) -> list[dict]:
        """
        Returns ONE centered text layer containing only the hook/quote.
        Word-wrap + dynamic font size to fit safely in 1080 px width.
        """
        accent = color_scheme.get("accent", "#FFD700")
        shadow = color_scheme.get("shadow", "#000000")

        # Wrap at ~16–18 chars depending on punctuation density
        wrapped = "\n".join(textwrap.wrap(hook, width=18, break_long_words=False))
        line_count = wrapped.count("\n") + 1

        # Scale font size down gracefully
        if line_count <= 1:
            font_size = 96
        elif line_count == 2:
            font_size = 84
        elif line_count == 3:
            font_size = 72
        else:
            font_size = 64

        return [
            {
                "text": wrapped,
                "y_position": 0.5,          # true center
                "font_size": font_size,
                "color": accent,
                "shadow_color": shadow,
                "shadow_offset": (4, 4),    # optional — many renderers support it
                "shadow_blur": 8,
                "appear_at": 0.6,
                "bold": True,
                "align": "center",
            }
        ]