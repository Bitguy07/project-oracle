"""
core/state_manager.py
Single source of truth: oracle_state.json on GitHub Gist.
Uses GIST_TOKEN (gist+repo scope). Falls back to local config.json.
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

GIST_FILENAME = "oracle_state.json"

DEFAULT_STATE = {
    "quota": {
        "gemini_daily_limit":     1500,
        "gemini_used_today":      0,
        "images_generated_today": 0,
        "image_daily_limit":      50,
        "reset_date":             "",
    },
    "posts":         [],
    "topic_queue":   [],
    "topic_history": [],
    "pending_posts": [],
    "review_mode":   "auto",
    "last_updated":  "",
}

MAX_POSTS_HISTORY  = 50
MAX_TOPIC_HISTORY  = 30
RECENT_TOPIC_HOURS = 24


class StateManager:

    def __init__(self):
        self.gist_id      = os.environ.get("GIST_ID")
        self.github_token = (
            os.environ.get("GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
        )
        self.local_path = Path("config.json")
        self._state: dict = self._load()
        self._reset_quota_if_new_day()

    # ── Quota ──────────────────────────────────────────────────────────────

    def has_quota(self) -> bool:
        q = self._state["quota"]
        return (
            q["gemini_used_today"]      < q["gemini_daily_limit"] and
            q["images_generated_today"] < q["image_daily_limit"]
        )

    def decrement_quota(self, gemini_calls: int = 2, images: int = 1):
        q = self._state["quota"]
        q["gemini_used_today"]      += gemini_calls
        q["images_generated_today"] += images
        self._save()

    # ── Posts ──────────────────────────────────────────────────────────────

    def was_recently_posted(self, topic: str) -> bool:
        cutoff = time.time() - RECENT_TOPIC_HOURS * 3600
        return any(
            p.get("topic", "").lower() == topic.lower() and p.get("ts", 0) > cutoff
            for p in self._state.get("posts", [])
        )

    def record_post(self, topic: str, caption: str, ig_id: Optional[str]):
        post = {
            "id":      str(uuid.uuid4())[:8],
            "topic":   topic,
            "caption": caption[:100],
            "ig_id":   ig_id,
            "ts":      time.time(),
            "date":    datetime.now(timezone.utc).isoformat(),
        }
        posts = self._state.setdefault("posts", [])
        posts.insert(0, post)
        self._state["posts"] = posts[:MAX_POSTS_HISTORY]

        hist = self._state.setdefault("topic_history", [])
        if topic not in hist:
            hist.insert(0, topic)
        self._state["topic_history"] = hist[:MAX_TOPIC_HISTORY]

        self._save()

    def get_topic_history(self) -> list:
        return list(self._state.get("topic_history", []))

    # ── Queue ──────────────────────────────────────────────────────────────

    def get_topic_queue(self) -> list:
        return list(self._state.get("topic_queue", []))

    def add_to_queue(self, topic: str, post_type: str = "reel") -> str:
        item_id = str(uuid.uuid4())[:8]
        self._state.setdefault("topic_queue", []).append({
            "id":       item_id,
            "topic":    topic,
            "type":     post_type,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        self._save()
        return item_id

    def remove_from_queue(self, item_id: str):
        self._state["topic_queue"] = [
            i for i in self._state.get("topic_queue", []) if i["id"] != item_id
        ]
        self._save()

    # ── Review mode ────────────────────────────────────────────────────────

    def get_review_mode(self) -> str:
        return self._state.get("review_mode", "auto")

    def set_review_mode(self, mode: str):
        self._state["review_mode"] = mode
        self._save()

    # ── Pending posts ──────────────────────────────────────────────────────

    def save_pending_post(
        self,
        pending_id: str,
        topic: str,
        content: dict,
        video_path: str,
        video_repo_path: str = "",
        video_repo_sha:  str = "",
    ):
        """
        Save a pending post to Gist.
        content must already be JSON-serializable (sanitized in main.py).
        video_path      = raw.githubusercontent.com URL (for downloading)
        video_repo_path = temp/xxx.mp4 path in repo (for deletion)
        video_repo_sha  = sha of the file (for deletion)
        """
        # Final safety check — ensure serializable
        try:
            json.dumps(content)
        except Exception as e:
            log.error(f"Content not serializable, stripping to basics: {e}")
            content = {
                "caption":   str(content.get("caption", "")),
                "post_type": str(content.get("post_type", "reel")),
                "topic":     str(topic),
            }

        self._state.setdefault("pending_posts", []).append({
            "id":              pending_id,
            "topic":           topic,
            "content":         content,
            "video_path":      video_path,
            "video_repo_path": video_repo_path,
            "video_repo_sha":  video_repo_sha,
            "ts":              time.time(),
        })
        self._save()
        log.info(f"Pending post saved: {pending_id} ({topic})")

    def get_pending_post(self, pending_id: str) -> Optional[dict]:
        return next(
            (p for p in self._state.get("pending_posts", []) if p["id"] == pending_id),
            None,
        )

    def get_all_pending_posts(self) -> list:
        return list(self._state.get("pending_posts", []))

    def remove_pending_post(self, pending_id: str):
        self._state["pending_posts"] = [
            p for p in self._state.get("pending_posts", []) if p["id"] != pending_id
        ]
        self._save()

    def clear_all_pending(self):
        """Called by /clear — wipes all pending posts."""
        self._state["pending_posts"] = []
        self._save()

    # ── Stats ──────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        q = self._state["quota"]
        posts = self._state.get("posts", [])
        return {
            "gemini_used":   q["gemini_used_today"],
            "gemini_limit":  q["gemini_daily_limit"],
            "images_used":   q["images_generated_today"],
            "image_limit":   q["image_daily_limit"],
            "queue_length":  len(self._state.get("topic_queue", [])),
            "pending_count": len(self._state.get("pending_posts", [])),
            "total_posts":   len(posts),
            "last_post":     posts[0] if posts else None,
            "reset_date":    q["reset_date"],
            "review_mode":   self._state.get("review_mode", "auto"),
        }

    # ── Persistence ────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if self.gist_id and self.github_token:
            try:
                data = self._load_from_gist()
                for key, val in DEFAULT_STATE.items():
                    data.setdefault(key, val)
                for key, val in DEFAULT_STATE["quota"].items():
                    data["quota"].setdefault(key, val)
                return data
            except Exception as e:
                log.warning(f"Gist load failed: {e} — using local.")

        if self.local_path.exists():
            with open(self.local_path) as f:
                data = json.load(f)
            for key, val in DEFAULT_STATE.items():
                data.setdefault(key, val)
            return data

        return json.loads(json.dumps(DEFAULT_STATE))

    def _save(self):
        self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
        if self.gist_id and self.github_token:
            try:
                self._save_to_gist()
                log.info("Gist saved OK")
                return
            except Exception as e:
                log.error(f"Gist save FAILED: {e} — saving locally only!")
        with open(self.local_path, "w") as f:
            json.dump(self._state, f, indent=2)

    def _load_from_gist(self) -> dict:
        r = requests.get(
            f"https://api.github.com/gists/{self.gist_id}",
            headers={
                "Authorization": f"token {self.github_token}",
                "Accept":        "application/vnd.github.v3+json",
            },
            timeout=15,
        )
        r.raise_for_status()
        files = r.json()["files"]
        if GIST_FILENAME in files:
            return json.loads(files[GIST_FILENAME].get("content", "{}"))
        for fname, fdata in files.items():
            if fname.endswith(".json"):
                return json.loads(fdata.get("content", "{}"))
        raise ValueError("No JSON file in Gist")

    def _save_to_gist(self):
        # Verify serializable before sending
        content_str = json.dumps(self._state, indent=2)

        last_exc = None
        for attempt in range(3):
            try:
                r = requests.patch(
                    f"https://api.github.com/gists/{self.gist_id}",
                    headers={
                        "Authorization": f"token {self.github_token}",
                        "Content-Type":  "application/json",
                        "Accept":        "application/vnd.github.v3+json",
                    },
                    json={"files": {GIST_FILENAME: {"content": content_str}}},
                    timeout=30,
                )
                r.raise_for_status()
                return
            except Exception as e:
                last_exc = e
                log.warning(f"Gist save attempt {attempt+1}/3: {e}")
                if attempt < 2:
                    import time as _t
                    _t.sleep(2)
        raise last_exc

    def _reset_quota_if_new_day(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._state["quota"].get("reset_date") != today:
            self._state["quota"]["gemini_used_today"]      = 0
            self._state["quota"]["images_generated_today"] = 0
            self._state["quota"]["reset_date"]             = today
            self._save()