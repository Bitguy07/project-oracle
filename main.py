"""
Project Oracle — Main Orchestrator

Pipeline modes:
  AUTONOMOUS  — AI picks topic/music/style. Used by cron and /now command.
  MANUAL      — User's free-form input. Used by /now-reel and /now-feed commands.

Both modes use the same pipeline() function.
"""

import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from core.state_manager    import StateManager
from core.intelligence     import IntelligenceEngine
from core.image_generator  import ImageGenerator
from core.audio_fetcher    import AudioFetcher
from core.video_renderer   import VideoRenderer
from core.instagram_publisher import InstagramPublisher
from core.telegram_bot     import TelegramBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("oracle.main")


async def run_pipeline(
    post_type: str = "reel",
    topic_raw: str = "",
    music_raw: str = "",
    mode: str = "autonomous",      # "autonomous" or "manual"
) -> dict:
    """
    Single pipeline function for both autonomous and manual modes.

    autonomous: AI invents everything — pass mode="autonomous"
    manual:     User provides topic_raw/music_raw — pass mode="manual"
    """
    state     = StateManager()
    intel     = IntelligenceEngine()
    img_gen   = ImageGenerator()
    audio_fet = AudioFetcher()
    renderer  = VideoRenderer()
    publisher = InstagramPublisher()

    if not state.has_quota():
        log.warning("Daily quota exhausted.")
        return {"status": "quota_exceeded"}

    # ── 1. Generate content ───────────────────────────────────────────────────
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

    # ── 2. Generate image ─────────────────────────────────────────────────────
    image_path = await img_gen.generate(
        content["image_prompt"],
        post_type,
        color_scheme=content["color_scheme"],
    )
    log.info(f"Image: {image_path}")

    # ── 3. Generate music ─────────────────────────────────────────────────────
    audio_path = await audio_fet.fetch(content["music_prompt"], post_type)
    log.info(f"Audio: {audio_path}")

    # ── 4. Render video ───────────────────────────────────────────────────────
    video_path = renderer.render(
        image_path=image_path,
        audio_path=audio_path,
        text_layers=content["text_layers"],
        post_type=post_type,
        video_style=content.get("video_style", "slow_zoom"),
    )
    log.info(f"Video: {video_path}")

    # ── 5. Review mode check ──────────────────────────────────────────────────
    review_mode = state.get_review_mode()
    log.info(f"Review mode: {review_mode}")

    if review_mode == "review":
        return await _send_for_review(state, content, topic, video_path, post_type)

    # ── 6. Publish ────────────────────────────────────────────────────────────
    result = await publisher.post(
        video_path=video_path,
        caption=content["caption"],
        post_type=post_type,
    )

    state.record_post(topic, content["caption"], result.get("ig_post_id"))
    state.decrement_quota()

    log.info(f"Published! IG ID: {result.get('ig_post_id')}")
    return {"status": "success", "ig_post_id": result.get("ig_post_id"), "topic": topic}


async def _send_for_review(state, content, topic, video_path, post_type):
    """Save pending post and notify via Telegram for review."""
    pending_id = str(uuid.uuid4())[:8]
    state.save_pending_post(
        pending_id=pending_id,
        topic=topic,
        content={**content, "post_type": post_type},
        video_path=str(video_path),
    )
    bot = TelegramBot()
    await bot.send_video_for_review(
        video_path=video_path,
        caption=content["caption"],
        hook=content["hook"],
        pending_id=pending_id,
        topic=topic,
    )
    return {"status": "pending_review", "pending_id": pending_id, "topic": topic}


async def publish_pending(pending_id: str) -> dict:
    """Called by /done <id> — publishes an approved pending video."""
    state = StateManager()
    pending = state.get_pending_post(pending_id)
    if not pending:
        return {"status": "not_found", "message": f"No pending post: {pending_id}"}

    video_path = Path(pending["video_path"])
    if not video_path.exists():
        state.remove_pending_post(pending_id)
        return {"status": "error", "message": "Video file no longer exists on runner."}

    content   = pending["content"]
    post_type = content.get("post_type", "reel")
    publisher = InstagramPublisher()

    result = await publisher.post(
        video_path=video_path,
        caption=content["caption"],
        post_type=post_type,
    )

    state.record_post(pending["topic"], content["caption"], result.get("ig_post_id"))
    state.decrement_quota()
    state.remove_pending_post(pending_id)

    return {"status": "success", "ig_post_id": result.get("ig_post_id"), "topic": pending["topic"]}


async def cron_run():
    """Called by GitHub Actions every 6 hours — fully autonomous."""
    state = StateManager()
    queue = state.get_topic_queue()

    if queue:
        # Drain queued manual topics first
        log.info(f"Queue has {len(queue)} items — processing queue.")
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
        # Autonomous — generate one reel
        log.info("Queue empty — autonomous generation.")
        await run_pipeline(post_type="reel", mode="autonomous")


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