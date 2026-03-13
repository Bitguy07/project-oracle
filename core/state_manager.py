"""
Persistence layer using GitHub Gist as a zero-cost key-value store.
Falls back to local config.json for development/testing.

New in this version:
  - review_mode: "auto" | "review"
  - pending_posts: videos waiting for /done or /no approval
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

DEFAULT_STATE = {
    "quota": {
        "gemini_daily_limit": 1500,
        "gemini_used_today": 0,
        "images_generated_today": 0,
        "image_daily_limit": 50,
        "reset_date": "",
    },
    "posts": [],
    "topic_queue": [],
    "pending_posts": [],        # Videos awaiting /done or /no review
    "review_mode": "auto",      # "auto" or "review"
    "last_updated": "",
}

MAX_POSTS_HISTORY = 50
RECENT_TOPIC_HOURS = 24


class StateManager:
    def __init__(self):
        self.gist_id = os.environ.get("GIST_ID")
        self.github_token = os.environ.get("GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self.local_path = Path("config.json")
        self._state: dict = self._load()
        self._reset_quota_if_new_day()

    # ── Quota ──────────────────────────────────────────────────────────────────

    def has_quota(self) -> bool:
        q = self._state["quota"]
        return (
            q["gemini_used_today"] < q["gemini_daily_limit"]
            and q["images_generated_today"] < q["image_daily_limit"]
        )

    def decrement_quota(self, gemini_calls: int = 3, images: int = 1):
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
        self._state["posts"] = self._state["posts"][:MAX_POSTS_HISTORY]
        self._save()

    # ── Queue ──────────────────────────────────────────────────────────────────

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
        return item_id

    def remove_from_queue(self, item_id: str):
        self._state["topic_queue"] = [
            i for i in self._state["topic_queue"] if i["id"] != item_id
        ]
        self._save()

    # ── Review Mode ────────────────────────────────────────────────────────────

    def get_review_mode(self) -> str:
        """Returns 'auto' or 'review'."""
        return self._state.get("review_mode", "auto")

    def set_review_mode(self, mode: str):
        """Set to 'auto' or 'review'."""
        if mode not in ("auto", "review"):
            raise ValueError(f"Invalid mode: {mode}. Use 'auto' or 'review'.")
        self._state["review_mode"] = mode
        self._save()
        log.info(f"Review mode set to: {mode}")

    # ── Pending Posts (Review Queue) ───────────────────────────────────────────

    def save_pending_post(
        self,
        topic: str,
        post_type: str,
        video_path: str,
        caption: str,
    ) -> str:
        """Save a rendered video awaiting human review. Returns pending_id."""
        pending_id = str(uuid.uuid4())[:8]
        self._state.setdefault("pending_posts", []).append({
            "id": pending_id,
            "topic": topic,
            "post_type": post_type,
            "video_path": video_path,
            "caption": caption,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ts": time.time(),
        })
        self._save()
        log.info(f"Saved pending post: {pending_id} ({topic})")
        return pending_id

    def get_pending_post(self, pending_id: str) -> Optional[dict]:
        """Retrieve a pending post by ID."""
        for p in self._state.get("pending_posts", []):
            if p["id"] == pending_id:
                return p
        return None

    def get_all_pending_posts(self) -> list:
        return list(self._state.get("pending_posts", []))

    def remove_pending_post(self, pending_id: str):
        """Remove a pending post (after /done or /no)."""
        self._state["pending_posts"] = [
            p for p in self._state.get("pending_posts", [])
            if p["id"] != pending_id
        ]
        self._save()
        log.info(f"Removed pending post: {pending_id}")

    # ── Stats ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        q = self._state["quota"]
        return {
            "gemini_used": q["gemini_used_today"],
            "gemini_limit": q["gemini_daily_limit"],
            "images_used": q["images_generated_today"],
            "image_limit": q["image_daily_limit"],
            "queue_length": len(self._state["topic_queue"]),
            "pending_count": len(self._state.get("pending_posts", [])),
            "total_posts": len(self._state["posts"]),
            "last_post": self._state["posts"][0] if self._state["posts"] else None,
            "reset_date": q["reset_date"],
            "review_mode": self.get_review_mode(),
        }

    # ── Persistence ────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.gist_id and self.github_token:
            try:
                return self._load_from_gist()
            except Exception as e:
                log.warning(f"Gist load failed ({e}), falling back to local.")

        if self.local_path.exists():
            with open(self.local_path) as f:
                data = json.load(f)
                for key, val in DEFAULT_STATE.items():
                    data.setdefault(key, val)
                return data

        log.info("No existing state. Initialising with defaults.")
        return json.loads(json.dumps(DEFAULT_STATE))

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
        for fname, fdata in files.items():
            if fname.endswith(".json"):
                return json.loads(fdata.get("content", "{}"))
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

    def _reset_quota_if_new_day(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._state["quota"]["reset_date"] != today:
            log.info(f"New day detected ({today}). Resetting quota counters.")
            self._state["quota"]["gemini_used_today"] = 0
            self._state["quota"]["images_generated_today"] = 0
            self._state["quota"]["reset_date"] = today
            self._save()