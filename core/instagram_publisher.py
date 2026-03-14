"""
core/instagram_publisher.py

Publishing flow:
  1. Upload MP4 to GitHub repo at temp/{timestamp}.mp4
  2. Use raw.githubusercontent.com URL — direct CDN, ZERO redirects
  3. POST /{ig_user_id}/media with video_url → container_id
  4. Poll status until FINISHED
  5. POST /{ig_user_id}/media_publish → ig_post_id
  6. DELETE temp file from GitHub repo (only when appropriate — see main.py)
"""

import asyncio
import base64
import json as json_lib
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("oracle.publisher")

GRAPH_BASE    = "https://graph.facebook.com/v19.0"
GH_API        = "https://api.github.com"
GH_RAW        = "https://raw.githubusercontent.com"
REPO          = "Bitguy07/Project-Oracle"
BRANCH        = "main"
POLL_INTERVAL = 10
MAX_POLLS     = 30


class InstagramPublisher:

    def __init__(self):
        self.ig_token   = os.environ["IG_ACCESS_TOKEN"]
        self.ig_user_id = os.environ["IG_USER_ID"]
        self.gh_token   = os.environ.get("GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
        self._temp_path: Optional[str] = None
        self._temp_sha:  Optional[str] = None

    # Expose last upload info so main.py can store it in Gist
    def _get_last_repo_path(self) -> str:
        return self._temp_path or ""

    def _get_last_sha(self) -> str:
        return self._temp_sha or ""

    # ── Public entry point ─────────────────────────────────────────────────
    async def post(self, video_path: Path, caption: str, post_type: str = "reel") -> dict:
        log.info(f"Publishing {post_type} to Instagram…")
        try:
            video_url    = await self._github_upload(video_path)
            container_id = await self._create_container(video_url, caption, post_type == "reel")
            log.info(f"Container created: {container_id}")
            await self._wait_for_container(container_id)
            ig_post_id   = await self._publish(container_id)
            log.info(f"✅ Published! IG post ID: {ig_post_id}")
            return {"ig_post_id": ig_post_id, "container_id": container_id}
        finally:
            # Always clean up the upload used for this publish
            await self._github_delete()

    # ── Upload ─────────────────────────────────────────────────────────────
    async def _github_upload(self, video_path: Path) -> str:
        if not self.gh_token:
            raise EnvironmentError("GIST_TOKEN with repo scope is required.")

        size_mb = video_path.stat().st_size / 1_048_576
        log.info(f"Uploading {size_mb:.1f} MB to GitHub repo…")

        repo_path   = f"temp/{int(time.time())}_{video_path.name}"
        content_b64 = base64.b64encode(video_path.read_bytes()).decode()

        headers = {
            "Authorization": f"token {self.gh_token}",
            "Accept":        "application/vnd.github.v3+json",
            "User-Agent":    "ProjectOracle/1.0",
        }

        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.put(
                f"{GH_API}/repos/{REPO}/contents/{repo_path}",
                headers=headers,
                json={
                    "message": f"temp upload {int(time.time())}",
                    "content": content_b64,
                    "branch":  BRANCH,
                },
            )

        data = r.json()
        if "content" not in data:
            raise RuntimeError(f"GitHub upload failed ({r.status_code}): {data}")

        self._temp_path = repo_path
        self._temp_sha  = data["content"]["sha"]

        raw_url = f"{GH_RAW}/{REPO}/{BRANCH}/{repo_path}"
        log.info(f"Raw URL: {raw_url}")
        return raw_url

    # ── Container ──────────────────────────────────────────────────────────
    async def _create_container(self, video_url: str, caption: str, is_reel: bool) -> str:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{GRAPH_BASE}/{self.ig_user_id}/media",
                params={
                    "access_token": self.ig_token,
                    "video_url":    video_url,
                    "caption":      caption,
                    "media_type":   "REELS" if is_reel else "VIDEO",
                },
            )
        data = r.json()
        log.info(f"Container response: {data}")
        self._raise_if_error(data, "create_container")
        return data["id"]

    # ── Poll ───────────────────────────────────────────────────────────────
    async def _wait_for_container(self, container_id: str):
        for attempt in range(1, MAX_POLLS + 1):
            await asyncio.sleep(POLL_INTERVAL)
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(
                    f"{GRAPH_BASE}/{container_id}",
                    params={"fields": "status_code,status", "access_token": self.ig_token},
                )
            data   = r.json()
            status = data.get("status_code", "UNKNOWN")
            log.info(f"Poll [{attempt}/{MAX_POLLS}] {status} — {data.get('status','')}")
            if status == "FINISHED":
                return
            if status == "ERROR":
                raise RuntimeError(f"Container failed: {data.get('status')}")

        raise TimeoutError(f"Container {container_id} never finished.")

    # ── Publish ────────────────────────────────────────────────────────────
    async def _publish(self, container_id: str) -> str:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{GRAPH_BASE}/{self.ig_user_id}/media_publish",
                params={"creation_id": container_id, "access_token": self.ig_token},
            )
        data = r.json()
        log.info(f"Publish response: {data}")
        self._raise_if_error(data, "publish")
        return data["id"]

    # ── Delete ─────────────────────────────────────────────────────────────
    async def _github_delete(self):
        if not self._temp_path or not self._temp_sha:
            return
        try:
            headers = {
                "Authorization": f"token {self.gh_token}",
                "Accept":        "application/vnd.github.v3+json",
                "User-Agent":    "ProjectOracle/1.0",
                "Content-Type":  "application/json",
            }
            body = json_lib.dumps({
                "message": "delete temp upload",
                "sha":     self._temp_sha,
                "branch":  BRANCH,
            }).encode()
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.delete(
                    f"{GH_API}/repos/{REPO}/contents/{self._temp_path}",
                    headers=headers,
                    content=body,
                )
            if r.status_code == 200:
                log.info(f"Deleted: {self._temp_path}")
            else:
                log.warning(f"Delete returned {r.status_code}: {r.text[:120]}")
        except Exception as e:
            log.warning(f"Could not delete temp file: {e}")
        finally:
            self._temp_path = None
            self._temp_sha  = None

    @staticmethod
    def _raise_if_error(data: dict, stage: str):
        if "error" in data:
            e = data["error"]
            raise RuntimeError(f"IG API [{stage}] code={e.get('code')} → {e.get('message')}")