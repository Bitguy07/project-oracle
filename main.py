"""
Project Oracle — Main Orchestrator

Pipeline modes:
  AUTONOMOUS — AI picks topic/music/style. Used by cron and /now command.
  MANUAL     — User's free-form input. Used by /now-reel and /now-feed commands.

Review modes:
  auto   — Publish directly to Instagram without review
  review — Upload video to GitHub, send to Telegram, wait for /done or /no
           Video URL stored in Gist — survives runner shutdown between jobs.

Key design:
  - Videos in review mode are uploaded to GitHub repo and NEVER deleted until:
      a) /done <id>  → published to Instagram then deleted from GitHub
      b) /no <id>    → deleted from GitHub immediately
      c) /clear      → wipes all pending posts and their GitHub files
  - pending_posts in Gist is the single source of truth
"""

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

import httpx

from core.state_manager       import StateManager
from core.intelligence        import IntelligenceEngine
from core.image_generator     import ImageGenerator
from core.audio_fetcher       import AudioFetcher
from core.video_renderer      import VideoRenderer
from core.instagram_publisher import InstagramPublisher
from core.telegram_bot        import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("oracle.main")


def _sanitize_for_json(obj):
    """Recursively convert any non-JSON-serializable objects to strings."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(i) for i in obj]
    elif isinstance(obj, Path):
        return str(obj)
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        return str(obj)


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def run_pipeline(
    post_type: str = "reel",
    topic_raw: str = "",
    music_raw: str = "",
    mode: str = "autonomous",
) -> dict:
    state     = StateManager()
    intel     = IntelligenceEngine()
    img_gen   = ImageGenerator()
    audio_fet = AudioFetcher()
    renderer  = VideoRenderer()

    if not state.has_quota():
        log.warning("Daily quota exhausted.")
        return {"status": "quota_exceeded"}

    # ── 1. Generate content ────────────────────────────────────────────────
    history = state.get_topic_history()

    if mode == "manual" and topic_raw:
        log.info(f"Manual mode | post_type={post_type} | topic='{topic_raw[:60]}'")
        content = await intel.generate_manual(
            topic_raw=topic_raw,
            music_raw=music_raw,
            post_type=post_type,
        )
    else:
        log.info(f"Autonomous mode | post_type={post_type} | history={len(history)} topics")
        content = await intel.generate_autonomous(
            post_type=post_type,
            history=history,
        )

    topic = content["topic"]
    log.info(f"Topic: '{topic}' | Hook: '{content['hook']}'")

    if state.was_recently_posted(topic):
        log.info(f"Topic '{topic}' recently posted — skipping.")
        return {"status": "duplicate_skipped", "topic": topic}

    # ── 2. Generate image ──────────────────────────────────────────────────
    image_path = await img_gen.generate(
        content["image_prompt"],
        post_type,
        color_scheme=content["color_scheme"],
    )
    log.info(f"Image: {image_path}")

    # ── 3. Generate music ──────────────────────────────────────────────────
    audio_path = await audio_fet.fetch(content["music_prompt"], post_type)
    log.info(f"Audio: {audio_path}")

    # ── 4. Render video ────────────────────────────────────────────────────
    video_path = renderer.render(
        image_path=image_path,
        audio_path=audio_path,
        text_layers=content["text_layers"],
        post_type=post_type,
        video_style=content.get("video_style", "slow_zoom"),
    )
    log.info(f"Video: {video_path}")

    # ── 5. Review mode check ───────────────────────────────────────────────
    review_mode = state.get_review_mode()
    log.info(f"Review mode: {review_mode}")

    if review_mode == "review":
        return await _send_for_review(state, content, topic, video_path, post_type)

    return await _publish_video(
        video_path=video_path,
        content=content,
        topic=topic,
        post_type=post_type,
        state=state,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Review mode
# ─────────────────────────────────────────────────────────────────────────────

async def _send_for_review(
    state: StateManager,
    content: dict,
    topic: str,
    video_path: Path,
    post_type: str,
) -> dict:
    publisher  = InstagramPublisher()
    pending_id = str(uuid.uuid4())[:8]

    log.info(f"Uploading video to GitHub for review (pending_id={pending_id})")
    video_url = await publisher._github_upload(video_path)
    # Clear temp tracking on publisher — we do NOT want auto-delete here
    # The file must persist until /done or /no
    publisher._temp_path = None
    publisher._temp_sha  = None
    log.info(f"Video stored at: {video_url}")

    # Sanitize content so it's always JSON-serializable before saving to Gist
    safe_content = _sanitize_for_json({**content, "post_type": post_type})

    # Verify it's serializable before saving
    try:
        json.dumps(safe_content)
    except Exception as e:
        log.error(f"Content not JSON-serializable even after sanitize: {e}")
        safe_content = {
            "caption":   content.get("caption", ""),
            "post_type": post_type,
            "topic":     content.get("topic", topic),
        }

    state.save_pending_post(
        pending_id=pending_id,
        topic=topic,
        content=safe_content,
        video_path=video_url,
        video_repo_path=publisher._get_last_repo_path(),
        video_repo_sha=publisher._get_last_sha(),
    )
    log.info(f"Pending post saved to Gist: {pending_id}")

    bot = TelegramBot()
    await bot.send_video_for_review(
        video_path=video_path,
        caption=content["caption"],
        hook=content["hook"],
        pending_id=pending_id,
        topic=topic,
    )

    return {"status": "pending_review", "pending_id": pending_id, "topic": topic}


# ─────────────────────────────────────────────────────────────────────────────
# /done <id>
# ─────────────────────────────────────────────────────────────────────────────

async def publish_pending(pending_id: str) -> dict:
    state   = StateManager()
    pending = state.get_pending_post(pending_id)

    if not pending:
        return {"status": "not_found", "message": f"No pending post: {pending_id}"}

    video_url     = pending["video_path"]
    content       = pending["content"]
    topic         = pending["topic"]
    post_type     = content.get("post_type", "reel")
    repo_path     = pending.get("video_repo_path")
    repo_sha      = pending.get("video_repo_sha")

    # Download video from GitHub URL
    tmp = Path(f"assets/output/pending_{pending_id}.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Downloading video: {video_url}")
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(video_url)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        tmp.write_bytes(r.content)
        log.info(f"Video downloaded: {tmp} ({tmp.stat().st_size // 1024} KB)")
    except Exception as e:
        state.remove_pending_post(pending_id)
        return {"status": "error", "message": f"Could not download video: {e}"}

    # Publish to Instagram
    result = await _publish_video(
        video_path=tmp,
        content=content,
        topic=topic,
        post_type=post_type,
        state=state,
    )

    # Cleanup
    try:
        tmp.unlink()
    except Exception:
        pass

    state.remove_pending_post(pending_id)

    # Delete from GitHub repo now that it's published
    if repo_path and repo_sha:
        try:
            publisher = InstagramPublisher()
            publisher._temp_path = repo_path
            publisher._temp_sha  = repo_sha
            await publisher._github_delete()
        except Exception as e:
            log.warning(f"Could not delete GitHub file after publish: {e}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# /no <id> — delete video from GitHub and remove from pending
# ─────────────────────────────────────────────────────────────────────────────

async def reject_pending(pending_id: str) -> dict:
    state   = StateManager()
    pending = state.get_pending_post(pending_id)

    if not pending:
        return {"status": "not_found"}

    repo_path = pending.get("video_repo_path")
    repo_sha  = pending.get("video_repo_sha")

    # Delete from GitHub repo
    if repo_path and repo_sha:
        try:
            publisher = InstagramPublisher()
            publisher._temp_path = repo_path
            publisher._temp_sha  = repo_sha
            await publisher._github_delete()
            log.info(f"Deleted rejected video from GitHub: {repo_path}")
        except Exception as e:
            log.warning(f"Could not delete GitHub file on reject: {e}")

    state.remove_pending_post(pending_id)
    return {"status": "rejected", "topic": pending["topic"]}


# ─────────────────────────────────────────────────────────────────────────────
# Shared publish helper
# ─────────────────────────────────────────────────────────────────────────────

async def _publish_video(
    video_path: Path,
    content: dict,
    topic: str,
    post_type: str,
    state: StateManager,
) -> dict:
    publisher = InstagramPublisher()
    result = await publisher.post(
        video_path=video_path,
        caption=content["caption"],
        post_type=post_type,
    )
    state.record_post(topic, content["caption"], result.get("ig_post_id"))
    state.decrement_quota()
    log.info(f"Published! IG ID: {result.get('ig_post_id')}")
    return {
        "status":     "success",
        "ig_post_id": result.get("ig_post_id"),
        "topic":      topic,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cron
# ─────────────────────────────────────────────────────────────────────────────

async def cron_run():
    state = StateManager()
    queue = state.get_topic_queue()

    if queue:
        log.info(f"Queue has {len(queue)} items.")
        for item in queue:
            if not state.has_quota():
                break
            result = await run_pipeline(
                post_type=item.get("type", "reel"),
                topic_raw=item.get("topic", ""),
                music_raw="",
                mode="manual",
            )
            state.remove_from_queue(item["id"])
            if result["status"] == "success":
                await asyncio.sleep(30)
    else:
        log.info("Queue empty — autonomous generation.")
        await run_pipeline(post_type="reel", mode="autonomous")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "cron"

    if mode == "cron":
        asyncio.run(cron_run())
    elif mode == "now":
        asyncio.run(run_pipeline(post_type="reel", mode="autonomous"))
    elif mode == "webhook":
        bot = TelegramBot()
        asyncio.run(bot.start_polling())
    elif mode == "publish_pending" and len(sys.argv) >= 3:
        asyncio.run(publish_pending(sys.argv[2]))
    elif mode == "single" and len(sys.argv) >= 3:
        asyncio.run(run_pipeline(
            post_type=sys.argv[3] if len(sys.argv) > 3 else "reel",
            topic_raw=sys.argv[2],
            mode="manual",
        ))
    else:
        print("Usage: python main.py [cron|now|webhook|single <topic> [reel|feed]|publish_pending <id>]")