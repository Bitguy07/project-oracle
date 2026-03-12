"""
core/audio_fetcher.py
Fetches royalty-free background music via Pixabay API.
 
Pixabay License: Free for commercial use, no attribution required.
Uses your existing PIXABAY_API_KEY secret — no new signup needed.
Searches by topic-based query for relevant music.
Falls back to silent audio if API fails.
 
Correct endpoint: https://pixabay.com/api/
  Parameter: media_type=music
"""
 
import hashlib
import logging
import os
import random
import subprocess
from pathlib import Path
 
import httpx
 
log = logging.getLogger("oracle.audio")
 
ASSETS_DIR = Path("assets/audio")
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
 
AUDIO_DURATION = {"reel": 30, "feed": 15}
 
PIXABAY_API = "https://pixabay.com/api/"
 
# Topic → search query for Pixabay music
TOPIC_QUERY_MAP = {
    "stoicism":    "meditation calm peaceful",
    "philosophy":  "ambient instrumental calm",
    "motivation":  "inspiring uplifting energetic",
    "mindfulness": "meditation relaxing peaceful",
    "success":     "inspiring motivational upbeat",
    "nature":      "nature ambient peaceful",
    "space":       "cinematic ambient electronic",
    "technology":  "electronic ambient futuristic",
    "fitness":     "energetic upbeat workout",
    "wisdom":      "meditation calm ambient",
    "life":        "inspiring ambient peaceful",
}
 
 
class AudioFetcher:
    def __init__(self):
        self.pixabay_key = os.environ.get("PIXABAY_API_KEY")
 
    async def fetch(self, topic: str, post_type: str = "reel") -> Path:
        query = self._get_query(topic)
        cache_key = hashlib.md5(f"{topic}{query}".encode()).hexdigest()[:8]
 
        # Check cache
        cached = list(ASSETS_DIR.glob(f"{cache_key}_*.mp3"))
        if cached:
            log.info(f"Audio cache hit: {cached[0]}")
            return cached[0]
 
        if not self.pixabay_key:
            log.warning("PIXABAY_API_KEY not set — using silent fallback.")
            return self._generate_silent(post_type)
 
        # Try full query first, then fallback to single word
        queries_to_try = [query, query.split()[0]]
        for q in queries_to_try:
            try:
                path = await self._fetch_pixabay(q, cache_key)
                return path
            except Exception as e:
                log.warning(f"Pixabay query '{q}' failed: {e}")
 
        log.info("All Pixabay queries failed. Generating silent fallback.")
        return self._generate_silent(post_type)
 
    async def _fetch_pixabay(self, query: str, cache_key: str) -> Path:
        log.info(f"Searching Pixabay music: query='{query}'")
 
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(PIXABAY_API, params={
                "key": self.pixabay_key,
                "q": query,
                "media_type": "music",
                "per_page": 20,
                "safesearch": "true",
                "order": "popular",
            })
 
        log.info(f"Pixabay response: HTTP {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
 
        data = r.json()
        hits = data.get("hits", [])
        log.info(f"Pixabay returned {len(hits)} results for '{query}'")
 
        if not hits:
            raise RuntimeError(f"No results for query='{query}'")
 
        # Pick a random track
        track = random.choice(hits)
        track_id = track.get("id", "unknown")
 
        # Extract audio URL — Pixabay music tracks have a direct 'audio' field
        audio = track.get("audio")
        if isinstance(audio, dict):
            audio_url = audio.get("mp3") or audio.get("url")
        else:
            audio_url = audio or track.get("url")
 
        if not audio_url:
            log.warning(f"Track {track_id} keys: {list(track.keys())}")
            raise RuntimeError(f"No audio URL in track {track_id}")
 
        log.info(f"Downloading track id={track_id}: {str(audio_url)[:80]}")
 
        output_path = ASSETS_DIR / f"{cache_key}_{track_id}.mp3"
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            r = await client.get(audio_url)
            if r.status_code != 200:
                raise RuntimeError(f"Download HTTP {r.status_code}")
            if len(r.content) < 10_000:
                raise RuntimeError(f"File too small: {len(r.content)} bytes")
 
        output_path.write_bytes(r.content)
        log.info(f"Audio saved: {output_path} ({len(r.content)//1024}KB)")
        return output_path
 
    def _get_query(self, topic: str) -> str:
        topic_lower = topic.lower()
        for keyword, query in TOPIC_QUERY_MAP.items():
            if keyword in topic_lower:
                return query
        return "ambient instrumental calm"
 
    def _generate_silent(self, post_type: str) -> Path:
        duration = AUDIO_DURATION.get(post_type, 30)
        output_path = ASSETS_DIR / f"silent_{duration}s.mp3"
        if output_path.exists():
            return output_path
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t", str(duration),
            "-q:a", "9",
            "-acodec", "libmp3lame",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr}")
        return output_path