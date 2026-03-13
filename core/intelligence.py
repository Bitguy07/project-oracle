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
from google import genai
from google.genai import types

log = logging.getLogger("oracle.intelligence")

MODEL_NAME = "gemini-2.5-flash"

GENERATION_CONFIG = {
    "temperature": 0.75,
    "top_p": 0.92,
    "max_output_tokens": 1500,
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
- hook: max 10 words — this is the **only** text that appears on the video
- image_prompt: evocative, painterly or photorealistic, no clichés, no text overlays
- hashtags: 5–6 niche + 1 broad, all lowercase with # prefix
- color_scheme: colors must match the emotional tone of the topic
"""


class IntelligenceEngine:

    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY not set.")

        self.client = genai.Client()

    async def generate_content(self, topic: str, post_type: str = "reel") -> dict:
        prompt = CONTENT_PROMPT.format(topic=topic, post_type=post_type)

        log.info(f"Generating content for topic='{topic}' ({post_type})")

        raw = await self._generate_with_retry(prompt)

        cleaned = self._extract_json(raw)

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            log.error(f"JSON parse failed. Raw response:\n{raw[:400]}")
            raise ValueError(f"Gemini returned invalid JSON: {e}")

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

    async def _generate_with_retry(self, prompt: str) -> str:
        """
        Generate Gemini content with automatic retries if response is truncated.
        """
        for attempt in range(3):
            try:
                response = await asyncio.to_thread(
                    self.client.models.generate_content,
                    model=MODEL_NAME,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=GENERATION_CONFIG["temperature"],
                        topP=GENERATION_CONFIG["top_p"],
                        maxOutputTokens=GENERATION_CONFIG["max_output_tokens"],
                        response_mime_type="application/json"
                    )
                )

                raw = response.text.strip()

                raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE | re.IGNORECASE)
                raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE | re.IGNORECASE)

                if raw.count("{") and raw.count("}"):
                    return raw

                raise ValueError("Response appears truncated")

            except Exception as e:
                log.warning(f"Gemini generation attempt {attempt + 1} failed: {e}")

                if attempt == 2:
                    raise

        raise RuntimeError("Gemini generation failed after retries")

    def _extract_json(self, text: str) -> str:
        """
        Extract the first valid JSON object from text.
        Prevents prefix/suffix garbage from breaking parsing.
        """
        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end == -1:
            raise ValueError("No JSON object detected in response")

        return text[start:end + 1]

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

        wrapped = "\n".join(
            textwrap.wrap(hook, width=18, break_long_words=False)
        )

        line_count = wrapped.count("\n") + 1

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
                "y_position": 0.5,
                "font_size": font_size,
                "color": accent,
                "shadow_color": shadow,
                "shadow_offset": (4, 4),
                "shadow_blur": 8,
                "appear_at": 0.6,
                "bold": True,
                "align": "center",
            }
        ]
