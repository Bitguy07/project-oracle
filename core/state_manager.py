"""
core/state_manager.py
Persistence layer using GitHub Gist as a zero-cost key-value store.
Falls back to local config.json for development/testing.
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("oracle.state")

# ── Schema ────────────────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "quota": {
        "gemini_daily_limit": 1500,        # Gemini Flash free tier: 1500 req/day
        "gemini_used_today": 0,
        "images_generated_today": 0,
        "image_daily_limit": 50,
        "reset_date": "",                  # ISO date string, e.g. "2024-01-15"
    },
    "posts": [],                           # Last 50 posts [{topic, caption, ig_id, ts}]
    "topic_queue": [],                     # [{id, topic, type, added_at}]
    "last_updated": "",
}

MAX_POSTS_HISTORY = 50
RECENT_TOPIC_HOURS = 24                    # Don't re-post same topic within 24h


class StateManager:
    """
    Thin wrapper around a JSON state object.
    Persistence backends (in priority order):
      1. GitHub Gist  (GIST_ID + GITHUB_TOKEN env vars set)
      2. Local config.json  (fallback / dev mode)
    """

    def __init__(self):
        self.gist_id = os.environ.get("GIST_ID")
        self.github_token = os.environ.get("GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.local_path = Path("config.json")
        self._state: dict = self._load()
        self._reset_quota_if_new_day()

    # ── Public API ─────────────────────────────────────────────────────────────

    def has_quota(self) -> bool:
        q = self._state["quota"]
        gemini_ok = q["gemini_used_today"] < q["gemini_daily_limit"]
        image_ok = q["images_generated_today"] < q["image_daily_limit"]
        return gemini_ok and image_ok

    def decrement_quota(self, gemini_calls: int = 3, images: int = 1):
        """Called after each successful post (1 post ≈ 3 Gemini calls + 1 image)."""
        q = self._state["quota"]
        q["gemini_used_today"] += gemini_calls
        q["images_generated_today"] += images
        self._save()

    def was_recently_posted(self, topic: str) -> bool:
        cutoff = time.time() - (RECENT_TOPIC_HOURS * 3600)
        for post in self._state["posts"]:
            if post.get("topic", "").lower() == topic.lower():
                if post.get("ts", 0) > cutoff:
                    return True
        return False

    def record_post(self, topic: str, caption: str, ig_id: Optional[str]):
        post = {
            "id": str(uuid.uuid4())[:8],
            "topic": topic,
            "caption": caption[:100],
            "ig_id": ig_id,
            "ts": time.time(),
            "date": datetime.now(timezone.utc).isoformat(),
        }
        self._state["posts"].insert(0, post)
        # Trim history
        self._state["posts"] = self._state["posts"][:MAX_POSTS_HISTORY]
        self._save()

    def get_topic_queue(self) -> list:
        return list(self._state["topic_queue"])

    def add_to_queue(self, topic: str, post_type: str = "reel") -> str:
        item_id = str(uuid.uuid4())[:8]
        self._state["topic_queue"].append({
            "id": item_id,
            "topic": topic,
            "type": post_type,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        self._save()
        log.info(f"Added to queue: [{item_id}] {topic} ({post_type})")
        return item_id

    def remove_from_queue(self, item_id: str):
        self._state["topic_queue"] = [
            i for i in self._state["topic_queue"] if i["id"] != item_id
        ]
        self._save()

    def get_stats(self) -> dict:
        q = self._state["quota"]
        return {
            "gemini_used": q["gemini_used_today"],
            "gemini_limit": q["gemini_daily_limit"],
            "images_used": q["images_generated_today"],
            "image_limit": q["image_daily_limit"],
            "queue_length": len(self._state["topic_queue"]),
            "total_posts": len(self._state["posts"]),
            "last_post": self._state["posts"][0] if self._state["posts"] else None,
            "reset_date": q["reset_date"],
        }

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.gist_id and self.github_token:
            try:
                return self._load_from_gist()
            except Exception as e:
                log.warning(f"Gist load failed ({e}), falling back to local file.")

        if self.local_path.exists():
            with open(self.local_path) as f:
                data = json.load(f)
                # Merge missing keys from DEFAULT_STATE
                for key, val in DEFAULT_STATE.items():
                    data.setdefault(key, val)
                return data

        log.info("No existing state found. Initialising with defaults.")
        return json.loads(json.dumps(DEFAULT_STATE))  # deep copy

    def _save(self):
        self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
        if self.gist_id and self.github_token:
            try:
                self._save_to_gist()
                return
            except Exception as e:
                log.warning(f"Gist save failed ({e}), saving locally.")

        with open(self.local_path, "w") as f:
            json.dump(self._state, f, indent=2)

    def _load_from_gist(self) -> dict:
        url = f"https://api.github.com/gists/{self.gist_id}"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        files = r.json()["files"]
        # Find our state file (first .json file in the gist)
        for fname, fdata in files.items():
            if fname.endswith(".json"):
                content = fdata.get("content", "{}")
                return json.loads(content)
        raise ValueError("No JSON file found in Gist")

    def _save_to_gist(self):
        url = f"https://api.github.com/gists/{self.gist_id}"
        headers = {
            "Authorization": f"token {self.github_token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
        }
        payload = {
            "files": {
                "oracle_state.json": {
                    "content": json.dumps(self._state, indent=2)
                }
            }
        }
        r = requests.patch(url, headers=headers, json=payload, timeout=10)
        r.raise_for_status()

    # ── Quota Reset ────────────────────────────────────────────────────────────

    def _reset_quota_if_new_day(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._state["quota"]["reset_date"] != today:
            log.info(f"New day detected ({today}). Resetting quota counters.")
            self._state["quota"]["gemini_used_today"] = 0
            self._state["quota"]["images_generated_today"] = 0
            self._state["quota"]["reset_date"] = today
            self._save()
