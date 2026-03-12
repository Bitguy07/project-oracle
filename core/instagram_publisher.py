"""
core/instagram_publisher.py
Publishes Reels and Feed videos to Instagram via Graph API v19.0

Correct flow for video/reels (2026):
  1. POST /{ig-user-id}/media with video_url (NOT resumable upload)
     → Returns container_id
  2. Poll container status until FINISHED
  3. POST /{ig-user-id}/media_publish with creation_id
     → Returns ig_post_id

Note: Instagram requires a PUBLIC video URL.
We upload the video to a temporary file host first,
then pass the URL to Instagram.
We use file.io (free, no auth, auto-deletes after download).
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("oracle.publisher")

GRAPH_API_BASE = "https://graph.facebook.com/v19.0"
POLL_INTERVAL = 10
MAX_POLL_ATTEMPTS = 30


class InstagramPublisher:
    def __init__(self):
        self.access_token = os.environ.get("IG_ACCESS_TOKEN")
        self.ig_user_id = os.environ.get("IG_USER_ID")

        if not self.access_token or not self.ig_user_id:
            raise EnvironmentError(
                "IG_ACCESS_TOKEN and IG_USER_ID must be set."
            )

    async def post(
        self,
        video_path: Path,
        caption: str,
        post_type: str = "reel",
    ) -> dict:
        log.info(f"Publishing {post_type} to Instagram...")

        # ── Step 1: Upload video to temporary public host ──────────────────
        video_url = await self._upload_to_temp_host(video_path)
        log.info(f"Video hosted at: {video_url}")

        # ── Step 2: Create Instagram media container ───────────────────────
        container_id = await self._create_container(
            video_url=video_url,
            caption=caption,
            is_reel=(post_type == "reel"),
        )
        log.info(f"Container created: {container_id}")

        # ── Step 3: Poll until ready ───────────────────────────────────────
        await self._wait_for_container(container_id)
        log.info(f"Container ready: {container_id}")

        # ── Step 4: Publish ────────────────────────────────────────────────
        ig_post_id = await self._publish_container(container_id)
        log.info(f"Published! IG Post ID: {ig_post_id}")
        # Clean up temp release
        await self._cleanup_temp_release()

        return {"ig_post_id": ig_post_id, "container_id": container_id}

    async def _upload_to_temp_host(self, video_path: Path) -> str:
        """
        Upload to transfer.sh — returns direct CDN URL, no redirects.
        Instagram can fetch it directly.
        """
        video_bytes = video_path.read_bytes()
        file_size_mb = len(video_bytes) / (1024 * 1024)
        log.info(f"Uploading {file_size_mb:.1f}MB to transfer.sh...")

        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.put(
                f"https://transfer.sh/{video_path.name}",
                content=video_bytes,
                headers={
                    "Content-Type": "video/mp4",
                    "Max-Days": "1",
                },
            )

        if r.status_code != 200:
            raise RuntimeError(
                f"transfer.sh failed: HTTP {r.status_code} — {r.text[:200]}"
            )

        url = r.text.strip()
        log.info(f"Video URL: {url}")

        if not url.startswith("https://"):
            raise RuntimeError(f"Invalid URL: {url}")

        return url

    async def _cleanup_temp_release(self):
        """Delete the temporary GitHub release after Instagram has fetched the video."""
        if not hasattr(self, "_temp_release_id"):
            return
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.delete(
                    f"https://api.github.com/repos/{self._gh_repo}/releases/{self._temp_release_id}",
                    headers=self._gh_headers,
                )
            log.info(f"Deleted temp release {self._temp_release_id}")
        except Exception as e:
            log.warning(f"Failed to delete temp release: {e}")
    
    async def _create_container(
        self,
        video_url: str,
        caption: str,
        is_reel: bool,
    ) -> str:
        """Create Instagram media container with public video URL."""
        endpoint = f"{GRAPH_API_BASE}/{self.ig_user_id}/media"

        params = {
            "access_token": self.access_token,
            "caption": caption,
            "video_url": video_url,
        }

        if is_reel:
            params["media_type"] = "REELS"
        else:
            params["media_type"] = "VIDEO"

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(endpoint, params=params)
            data = r.json()

        log.info(f"Container response: {data}")
        self._check_error(data, "create_container")
        return data["id"]

    async def _wait_for_container(self, container_id: str):
        """Poll container status until FINISHED."""
        endpoint = f"{GRAPH_API_BASE}/{container_id}"
        params = {
            "fields": "status_code,status",
            "access_token": self.access_token,
        }

        for attempt in range(MAX_POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL)

            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(endpoint, params=params)
                data = r.json()

            status = data.get("status_code", "")
            log.info(f"Container status [{attempt+1}/{MAX_POLL_ATTEMPTS}]: {status}")

            if status == "FINISHED":
                return
            elif status == "ERROR":
                raise RuntimeError(f"Container error: {data}")

        raise TimeoutError(f"Container {container_id} never finished.")

    async def _publish_container(self, container_id: str) -> str:
        """Publish the ready container."""
        endpoint = f"{GRAPH_API_BASE}/{self.ig_user_id}/media_publish"

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                endpoint,
                params={
                    "creation_id": container_id,
                    "access_token": self.access_token,
                },
            )
            data = r.json()

        log.info(f"Publish response: {data}")
        self._check_error(data, "publish")
        return data["id"]

    @staticmethod
    def _check_error(data: dict, stage: str):
        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                f"IG API error at '{stage}': "
                f"[{err.get('code')}] {err.get('message')}"
            )