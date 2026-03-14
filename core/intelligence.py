"""
core/intelligence.py

Two modes, one pipeline:
  AUTONOMOUS  — AI generates everything from scratch, checks history to avoid repeats.
  MANUAL      — User provides free-form topic/music hints in any language/style.

Both modes return identical structured output so downstream code is unchanged.
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

MODEL = "gemini-2.5-flash"
MAX_TOKENS = 8192

AUTONOMOUS_PROMPT = """You are a viral Instagram content creator and psychologist.
Your reels make people stop scrolling, feel something deep, and save or share the post.

HISTORY (topics already used — do NOT repeat these themes):
{history}

Your task: Invent a completely original topic and create content for a {post_type}.
Use psychological principles — curiosity gap, emotional resonance, pattern interruption.
The hook must make someone stop mid-scroll.

Return ONLY a single valid JSON object, nothing else:
{{
  "topic": "2-4 word topic label for history tracking",
  "hook": "One punch-in-the-gut sentence. Max 8 words. No colons.",
  "body": "2 sentences expanding the idea. Max 20 words. Caption only.",
  "cta": "One action. Max 6 words.",
  "caption": "Max 100 chars. Natural hook + body + cta.",
  "hashtags": ["#tag1","#tag2","#tag3","#tag4","#tag5","#broad"],
  "image_prompt": "Cinematic scene. Max 20 words. Dramatic lighting. No text in image.",
  "music_prompt": "Descriptive MusicGen sentence. E.g.: dark cinematic ambient piano, slow tempo, no vocals, contemplative.",
  "video_style": "One of: slow_zoom, static, pulse, fade_drift",
  "color_scheme": {{
    "primary": "#FFFFFF",
    "accent": "#FFD700",
    "shadow": "#000000"
  }}
}}"""

MANUAL_PROMPT = """You are a viral Instagram content creator and psychologist.

The user gave this free-form instruction (may be Hinglish, broken English, emotional):
TOPIC DESCRIPTION: "{topic_raw}"
MUSIC HINT: "{music_raw}"

Interpret their intent deeply. If music_raw is empty, derive the perfect music style
from the emotional tone of the topic. Create scroll-stopping content.

Return ONLY a single valid JSON object, nothing else:
{{
  "topic": "2-4 word topic label",
  "hook": "One punch-in-the-gut sentence. Max 8 words. No colons.",
  "body": "2 sentences. Max 20 words. Caption only.",
  "cta": "One action. Max 6 words.",
  "caption": "Max 100 chars. Natural hook + body + cta.",
  "hashtags": ["#tag1","#tag2","#tag3","#tag4","#tag5","#broad"],
  "image_prompt": "Cinematic scene matching topic emotion. Max 20 words. No text in image.",
  "music_prompt": "Descriptive MusicGen sentence matching the mood. No vocals.",
  "video_style": "One of: slow_zoom, static, pulse, fade_drift",
  "color_scheme": {{
    "primary": "#FFFFFF",
    "accent": "#FFD700",
    "shadow": "#000000"
  }}
}}"""

_FALLBACK = {
    "topic": "inner silence",
    "hook": "Silence speaks what words cannot.",
    "body": "In stillness, answers emerge.",
    "cta": "Save this.",
    "caption": "Silence speaks what words cannot. In stillness, answers emerge. Save this.",
    "image_prompt": "Dark cinematic landscape, storm clouds, single beam of light.",
    "music_prompt": "dark cinematic ambient piano, slow tempo, no vocals, contemplative mood",
    "video_style": "slow_zoom",
    "color_scheme": {"primary": "#FFFFFF", "accent": "#FFD700", "shadow": "#000000"},
    "hashtags": ["#mindset", "#wisdom", "#motivation", "#clarity", "#growth", "#inspiration"],
}


class IntelligenceEngine:

    def __init__(self):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise EnvironmentError("GEMINI_API_KEY not set.")
        self.client = genai.Client(api_key=api_key)

    async def generate_autonomous(self, post_type: str, history: list[str]) -> dict:
        """AI invents everything. history = last N topic strings."""
        history_str = "\n".join(f"- {t}" for t in history[-10:]) if history else "None yet."
        prompt = AUTONOMOUS_PROMPT.format(history=history_str, post_type=post_type)
        return await self._run(prompt, post_type)

    async def generate_manual(self, topic_raw: str, music_raw: str, post_type: str) -> dict:
        """Interprets user's free-form input in any language/style."""
        prompt = MANUAL_PROMPT.format(
            topic_raw=topic_raw,
            music_raw=music_raw or "",
            post_type=post_type,
        )
        return await self._run(prompt, post_type)

    async def _run(self, prompt: str, post_type: str) -> dict:
        data = None
        for attempt in range(2):
            try:
                raw = await self._call_gemini(prompt)
                data = json.loads(self._extract_json(raw))
                log.info(f"Content OK — hook='{data.get('hook','')[:50]}'")
                break
            except Exception as e:
                log.warning(f"Attempt {attempt+1}/2 failed: {e}")
                if attempt == 0:
                    await asyncio.sleep(2)

        if data is None:
            log.error("Both attempts failed — using fallback.")
            data = dict(_FALLBACK)

        data = self._normalize(data)
        hashtags = data.get("hashtags", [])
        full_caption = f"{data['caption'].strip()}\n\n{' '.join(hashtags)}".strip()

        return {
            "topic":        data["topic"],
            "hook":         data["hook"],
            "body":         data["body"],
            "cta":          data["cta"],
            "caption":      full_caption,
            "hashtags":     hashtags,
            "image_prompt": data["image_prompt"],
            "music_prompt": data["music_prompt"],
            "video_style":  data.get("video_style", "slow_zoom"),
            "color_scheme": data["color_scheme"],
            "text_layers":  self._build_text_layers(data["hook"], data["color_scheme"]),
        }

    async def _call_gemini(self, prompt: str) -> str:
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.85,
                topP=0.92,
                maxOutputTokens=MAX_TOKENS,
                response_mime_type="application/json",
            ),
        )
        raw = ""
        try:
            raw = response.text or ""
        except Exception:
            pass
        if not raw:
            for c in (getattr(response, "candidates", None) or []):
                parts = getattr(getattr(c, "content", None), "parts", []) or []
                raw = "".join(p.text for p in parts if hasattr(p, "text") and p.text)
                if raw:
                    break
        raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        if not raw:
            raise ValueError("Empty Gemini response")
        return raw.strip()

    def _extract_json(self, text: str) -> str:
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            raise ValueError(f"No JSON: {text[:200]!r}")
        return text[s:e+1]

    def _normalize(self, data: dict) -> dict:
        for k in ("topic", "hook", "body", "cta", "caption", "image_prompt", "music_prompt"):
            if not data.get(k):
                data[k] = _FALLBACK[k]
        tags = data.get("hashtags") or []
        if not isinstance(tags, list) or not tags:
            tags = list(_FALLBACK["hashtags"])
        data["hashtags"] = [("#" + t.lower().strip().lstrip("#")) for t in tags]
        cs = data.get("color_scheme")
        if not isinstance(cs, dict):
            data["color_scheme"] = dict(_FALLBACK["color_scheme"])
        else:
            for k, v in _FALLBACK["color_scheme"].items():
                cs.setdefault(k, v)
        if data.get("video_style") not in ("slow_zoom", "static", "pulse", "fade_drift"):
            data["video_style"] = "slow_zoom"
        return data

    def _build_text_layers(self, hook: str, color_scheme: dict) -> list[dict]:
        accent = color_scheme.get("accent", "#FFF653")
        shadow = color_scheme.get("shadow", "#000000")
        wrapped = "\n".join(textwrap.wrap(hook, width=18, break_long_words=False))
        line_count = wrapped.count("\n") + 1
        font_size = {1: 96, 2: 84, 3: 72}.get(line_count, 64)
        return [{
            "text": wrapped, "y_position": 0.30, "font_size": font_size,
            "color": accent, "shadow_color": shadow, "appear_at": 0.6, "bold": True,
        }]