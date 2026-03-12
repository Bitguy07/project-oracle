"""
Project Oracle — Main Orchestrator
Headless, zero-cost Instagram Reel/Feed Factory.
Entry point for both GitHub Actions CRON runs and Telegram webhook triggers.

Review Modes:
  - "auto"    → Post directly to Instagram without review
  - "review"  → Send video to Telegram first, wait for /done or /no
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from core.state_manager import StateManager
from core.intelligence import IntelligenceEngine
from core.image_generator import ImageGenerator
from core.audio_fetcher import AudioFetcher
from core.video_renderer import VideoRenderer
from core.instagram_publisher import InstagramPublisher
from core.telegram_bot import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("oracle.main")


async def run_pipeline(topic: str, post_type: str = "reel") -> dict:
    """
    Full end-to-end pipeline for a single post.
    Respects the review_mode setting from state:
      - "auto"   → publish directly
      - "review" → send to Telegram for approval first
    """
    state = StateManager()
    intel = IntelligenceEngine()
    img_gen = ImageGenerator()
    audio = AudioFetcher()
    renderer = VideoRenderer()

    # ── 1. Quota Guard ─────────────────────────────────────────────────────
    if not state.has_quota():
        log.warning("Daily quota exhausted. Exiting gracefully.")
        return {"status": "quota_exceeded", "topic": topic}

    # ── 2. Duplicate Guard ─────────────────────────────────────────────────
    if state.was_recently_posted(topic):
        log.info(f"Topic '{topic}' was recently used. Skipping.")
        return {"status": "duplicate_skipped", "topic": topic}

    log.info(f"Starting pipeline for topic='{topic}' type='{post_type}'")

    # ── 3. Generate Content via Gemini ─────────────────────────────────────
    content = await intel.generate_content(topic, post_type)
    log.info(f"Generated content: hook='{content['hook'][:50]}...'")

    # ── 4. Generate Image ──────────────────────────────────────────────────
    image_path = await img_gen.generate(content["image_prompt"], post_type)
    log.info(f"Image saved to {image_path}")

    # ── 5. Fetch Background Audio ──────────────────────────────────────────
    audio_path = await audio.fetch(topic)
    log.info(f"Audio saved to {audio_path}")

    # ── 6. Render Video with FFmpeg ────────────────────────────────────────
    video_path = renderer.render(
        image_path=image_path,
        audio_path=audio_path,
        text_layers=content["text_layers"],
        post_type=post_type,
    )
    log.info(f"Video rendered to {video_path}")

    # ── 7. Check review mode ───────────────────────────────────────────────
    review_mode = state.get_review_mode()
    log.info(f"Review mode: {review_mode}")

    if review_mode == "review":
        # Send video to Telegram for approval — don't post yet
        result = await _send_for_review(
            state=state,
            video_path=video_path,
            content=content,
            topic=topic,
            post_type=post_type,
        )
        return result
    else:
        # Auto mode — post directly
        result = await _publish(
            video_path=video_path,
            content=content,
            topic=topic,
            post_type=post_type,
            state=state,
        )
        return result


async def _send_for_review(
    state: StateManager,
    video_path: Path,
    content: dict,
    topic: str,
    post_type: str,
) -> dict:
    """
    Send rendered video to Telegram for human review.
    Saves pending post to state — waits for /done or /no command.
    """
    bot = TelegramBot()

    # Save pending post to state so /done can retrieve it later
    pending_id = state.save_pending_post(
        topic=topic,
        post_type=post_type,
        video_path=str(video_path),
        caption=content["caption"],
    )

    log.info(f"Sending video for review (pending_id={pending_id})")

    # Send the video file to Telegram
    await bot.send_video_for_review(
        video_path=video_path,
        caption=content["caption"],
        hook=content["hook"],
        pending_id=pending_id,
        topic=topic,
    )

    return {
        "status": "pending_review",
        "pending_id": pending_id,
        "topic": topic,
    }


async def _publish(
    video_path: Path,
    content: dict,
    topic: str,
    post_type: str,
    state: StateManager,
) -> dict:
    """Publish video directly to Instagram."""
    publisher = InstagramPublisher()

    result = await publisher.post(
        video_path=video_path,
        caption=content["caption"],
        post_type=post_type,
    )

    state.record_post(topic, content["caption"], result.get("ig_post_id"))
    state.decrement_quota()

    log.info(f"Post published! IG ID: {result.get('ig_post_id')}")
    return {
        "status": "success",
        "ig_post_id": result.get("ig_post_id"),
        "topic": topic,
    }


async def publish_pending(pending_id: str) -> dict:
    """
    Called when user sends /done <pending_id>.
    Retrieves the pending post and publishes it to Instagram.
    """
    state = StateManager()
    pending = state.get_pending_post(pending_id)

    if not pending:
        return {"status": "error", "message": f"No pending post found: {pending_id}"}

    video_path = Path(pending["video_path"])
    if not video_path.exists():
        return {"status": "error", "message": "Video file no longer exists (GitHub Actions runner reset)"}

    result = await _publish(
        video_path=video_path,
        content={"caption": pending["caption"]},
        topic=pending["topic"],
        post_type=pending["post_type"],
        state=state,
    )

    # Remove from pending
    state.remove_pending_post(pending_id)
    return result


async def continuous_run():
    """
    Drain the topic queue, posting until quota is exhausted.
    Called by GitHub Actions CRON.
    """
    state = StateManager()
    queue = state.get_topic_queue()

    if not queue:
        log.info("Topic queue is empty. Nothing to do.")
        return

    log.info(f"Queue has {len(queue)} topic(s). Starting continuous run...")
    results = []

    for item in queue:
        if not state.has_quota():
            log.warning("Quota hit. Stopping batch run.")
            break

        result = await run_pipeline(
            topic=item["topic"],
            post_type=item.get("type", "reel"),
        )
        results.append(result)
        state.remove_from_queue(item["id"])

        if result["status"] == "success":
            await asyncio.sleep(30)

    log.info(f"Batch complete. Results: {json.dumps(results, indent=2)}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "cron"

    if mode == "cron":
        asyncio.run(continuous_run())
    elif mode == "webhook":
        bot = TelegramBot()
        asyncio.run(bot.start_polling())
    elif mode == "single" and len(sys.argv) >= 3:
        topic = sys.argv[2]
        post_type = sys.argv[3] if len(sys.argv) > 3 else "reel"
        asyncio.run(run_pipeline(topic, post_type))
    elif mode == "publish_pending" and len(sys.argv) >= 3:
        pending_id = sys.argv[2]
        asyncio.run(publish_pending(pending_id))
    else:
        print("Usage: python main.py [cron|webhook|single <topic> [reel|feed]|publish_pending <id>]")