"""
core/telegram_bot.py
Telegram Bot — Command & Control (C2) for Project Oracle.

Commands:
  /post <topic>    — Add Reel to queue
  /feed <topic>    — Add Feed post to queue
  /now <topic>     — Run pipeline immediately
  /status          — Show quota, mode, queue
  /queue           — List pending topics
  /clear           — Clear topic queue
  /help            — Show all commands

  Review commands (only appear when a video is sent for review):
  /done <id>       — Approve and publish the video to Instagram
  /no <id>         — Reject and discard the video

  Mode commands:
  /custom          — Show review mode options
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("oracle.telegram")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not self.token:
            raise EnvironmentError("TELEGRAM_BOT_TOKEN not set.")

    # ── Core API ───────────────────────────────────────────────────────────────

    async def send_message(self, text: str, chat_id: Optional[str] = None) -> dict:
        cid = chat_id or self.chat_id
        url = TELEGRAM_API.format(token=self.token, method="sendMessage")
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json={
                "chat_id": cid,
                "text": text,
                "parse_mode": "HTML",
            })
            return r.json()

    async def send_video(
        self,
        video_path: Path,
        caption: str,
        chat_id: Optional[str] = None,
    ) -> dict:
        """Send a video file to Telegram."""
        cid = chat_id or self.chat_id
        url = TELEGRAM_API.format(token=self.token, method="sendVideo")
        video_bytes = video_path.read_bytes()
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                url,
                data={"chat_id": cid, "caption": caption, "parse_mode": "HTML"},
                files={"video": (video_path.name, video_bytes, "video/mp4")},
            )
            return r.json()

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> list:
        url = TELEGRAM_API.format(token=self.token, method="getUpdates")
        async with httpx.AsyncClient(timeout=timeout + 5) as client:
            r = await client.get(url, params={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message"],
            })
            return r.json().get("result", [])

    # ── Video Review ───────────────────────────────────────────────────────────

    async def send_video_for_review(
        self,
        video_path: Path,
        caption: str,
        hook: str,
        pending_id: str,
        topic: str,
    ):
        """
        Send rendered video to Telegram for human review.
        Shows the video with approve/reject instructions.
        """
        review_caption = (
            f"🎬 <b>Review Required</b>\n"
            f"─────────────────────\n"
            f"📌 Topic: <b>{topic}</b>\n"
            f"💬 Hook: <i>{hook[:80]}</i>\n"
            f"─────────────────────\n"
            f"✅ <b>/done {pending_id}</b> — Approve &amp; post to Instagram\n"
            f"❌ <b>/no {pending_id}</b> — Reject &amp; discard\n"
            f"─────────────────────\n"
            f"📝 Caption preview:\n{caption[:200]}..."
        )
        await self.send_video(video_path, review_caption)
        log.info(f"Sent video for review: pending_id={pending_id}")

    # ── Command Handler ────────────────────────────────────────────────────────

    async def handle_update(self, update: dict) -> Optional[str]:
        from core.state_manager import StateManager
        state = StateManager()

        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if not text or not chat_id:
            return None

        if self.chat_id and chat_id != str(self.chat_id):
            log.warning(f"Ignoring unauthorized chat_id: {chat_id}")
            await self.send_message("⛔ Unauthorized.", chat_id)
            return None

        log.info(f"Received command: '{text}'")

        # ── /post <topic> ──────────────────────────────────────────────────────
        if text.startswith("/post "):
            topic = text[6:].strip()
            if not topic:
                reply = "❌ Usage: /post <topic>"
            else:
                item_id = state.add_to_queue(topic, "reel")
                reply = f"✅ Added to Reel queue!\nTopic: <b>{topic}</b>\nID: <code>{item_id}</code>"

        # ── /feed <topic> ──────────────────────────────────────────────────────
        elif text.startswith("/feed "):
            topic = text[6:].strip()
            if not topic:
                reply = "❌ Usage: /feed <topic>"
            else:
                item_id = state.add_to_queue(topic, "feed")
                reply = f"✅ Added to Feed queue!\nTopic: <b>{topic}</b>\nID: <code>{item_id}</code>"

        # ── /now <topic> ───────────────────────────────────────────────────────
        elif text.startswith("/now "):
            topic = text[5:].strip()
            if not topic:
                reply = "❌ Usage: /now <topic>"
            else:
                mode = state.get_review_mode()
                mode_note = "📤 Will send for your review first." if mode == "review" else "🚀 Auto-posting to Instagram."
                reply = f"🚀 Starting pipeline for: <b>{topic}</b>\n{mode_note}\nCheck back in ~3 minutes..."
                await self.send_message(reply, chat_id)
                await self._run_and_notify(topic, chat_id)
                return reply

        # ── /done <id> — Approve pending video ────────────────────────────────
        elif text.startswith("/done"):
            parts = text.split()
            if len(parts) < 2:
                # List all pending posts
                pending = state.get_all_pending_posts()
                if not pending:
                    reply = "📭 No pending videos to approve."
                else:
                    lines = ["📋 <b>Pending Videos:</b>"]
                    for p in pending:
                        lines.append(
                            f"• <b>{p['topic']}</b> — "
                            f"/done {p['id']} | /no {p['id']}"
                        )
                    reply = "\n".join(lines)
            else:
                pending_id = parts[1].strip()
                reply = await self._handle_done(pending_id, chat_id, state)

        # ── /no <id> — Reject pending video ───────────────────────────────────
        elif text.startswith("/no"):
            parts = text.split()
            if len(parts) < 2:
                pending = state.get_all_pending_posts()
                if not pending:
                    reply = "📭 No pending videos to reject."
                else:
                    lines = ["📋 <b>Pending Videos:</b>"]
                    for p in pending:
                        lines.append(
                            f"• <b>{p['topic']}</b> — "
                            f"/done {p['id']} | /no {p['id']}"
                        )
                    reply = "\n".join(lines)
            else:
                pending_id = parts[1].strip()
                pending = state.get_pending_post(pending_id)
                if not pending:
                    reply = f"❌ No pending post found with ID: <code>{pending_id}</code>"
                else:
                    state.remove_pending_post(pending_id)
                    reply = (
                        f"🗑️ Rejected &amp; discarded.\n"
                        f"Topic: <b>{pending['topic']}</b>"
                    )

        # ── /custom — Set review mode ──────────────────────────────────────────
        elif text == "/custom":
            current = state.get_review_mode()
            reply = (
                f"⚙️ <b>Review Mode Settings</b>\n"
                f"─────────────────────────\n"
                f"Current mode: <b>{current.upper()}</b>\n\n"
                f"<b>AUTO</b> — Posts go directly to Instagram\n"
                f"<b>REVIEW</b> — Videos sent to you first, you approve with /done\n\n"
                f"To change:\n"
                f"• /custom auto — Switch to auto-post\n"
                f"• /custom review — Switch to review-first"
            )

        elif text == "/custom auto":
            state.set_review_mode("auto")
            reply = (
                "✅ Mode set to <b>AUTO</b>\n"
                "Videos will be posted directly to Instagram without review."
            )

        elif text == "/custom review":
            state.set_review_mode("review")
            reply = (
                "✅ Mode set to <b>REVIEW</b>\n"
                "Each video will be sent to you here first.\n"
                "Use <b>/done &lt;id&gt;</b> to approve or <b>/no &lt;id&gt;</b> to reject."
            )

        # ── /status ────────────────────────────────────────────────────────────
        elif text == "/status":
            stats = state.get_stats()
            last = stats.get("last_post")
            last_str = (
                f"\n🕐 Last: <b>{last['topic']}</b> ({last['date'][:10]})"
                if last else ""
            )
            mode_emoji = "👁️" if stats["review_mode"] == "review" else "🤖"
            reply = (
                f"📊 <b>Project Oracle Status</b>\n"
                f"{'─'*28}\n"
                f"🤖 Gemini: {stats['gemini_used']}/{stats['gemini_limit']} calls\n"
                f"🖼️ Images: {stats['images_used']}/{stats['image_limit']}\n"
                f"📋 Queue: {stats['queue_length']} topic(s)\n"
                f"⏳ Pending review: {stats['pending_count']}\n"
                f"📸 Total posts: {stats['total_posts']}"
                f"{last_str}\n"
                f"🔄 Resets: {stats['reset_date']}\n"
                f"{mode_emoji} Mode: <b>{stats['review_mode'].upper()}</b>"
            )

        # ── /queue ─────────────────────────────────────────────────────────────
        elif text == "/queue":
            queue = state.get_topic_queue()
            if not queue:
                reply = "📭 Queue is empty."
            else:
                lines = [f"📋 <b>Topic Queue ({len(queue)} items)</b>"]
                for i, item in enumerate(queue, 1):
                    lines.append(
                        f"{i}. [{item['type']}] {item['topic']} "
                        f"<code>({item['id']})</code>"
                    )
                reply = "\n".join(lines)

        # ── /clear ─────────────────────────────────────────────────────────────
        elif text == "/clear":
            state._state["topic_queue"] = []
            state._save()
            reply = "🗑️ Queue cleared."

        # ── /help ──────────────────────────────────────────────────────────────
        elif text == "/help":
            reply = (
                "🤖 <b>Project Oracle Commands</b>\n"
                "─────────────────────────\n"
                "/post &lt;topic&gt; — Add Reel to queue\n"
                "/feed &lt;topic&gt; — Add Feed post to queue\n"
                "/now &lt;topic&gt; — Post immediately\n"
                "/status — Show quota, mode &amp; stats\n"
                "/queue — View pending topics\n"
                "/clear — Clear the queue\n"
                "─────────────────────────\n"
                "<b>Review commands:</b>\n"
                "/done &lt;id&gt; — Approve video → post to Instagram\n"
                "/no &lt;id&gt; — Reject &amp; discard video\n"
                "─────────────────────────\n"
                "<b>Mode settings:</b>\n"
                "/custom — View/change review mode\n"
                "/custom auto — Auto-post (no review)\n"
                "/custom review — Review before posting"
            )

        else:
            reply = "❓ Unknown command. Try /help"

        await self.send_message(reply, chat_id)
        return reply

    async def _handle_done(
        self,
        pending_id: str,
        chat_id: str,
        state,
    ) -> str:
        """Approve a pending video and publish it to Instagram."""
        pending = state.get_pending_post(pending_id)
        if not pending:
            return f"❌ No pending post found with ID: <code>{pending_id}</code>"

        await self.send_message(
            f"⏳ Publishing <b>{pending['topic']}</b> to Instagram...",
            chat_id,
        )

        try:
            import main as oracle_main
            result = await oracle_main.publish_pending(pending_id)

            if result["status"] == "success":
                return (
                    f"✅ <b>Posted to Instagram!</b>\n"
                    f"Topic: <b>{pending['topic']}</b>\n"
                    f"IG ID: <code>{result.get('ig_post_id')}</code>"
                )
            else:
                return f"⚠️ Publish result: {result.get('status')} — {result.get('message', '')}"

        except Exception as e:
            return f"❌ Publish failed: {str(e)[:200]}"

    async def _run_and_notify(self, topic: str, chat_id: str):
        """Run pipeline and notify via Telegram when done."""
        import main as oracle_main
        try:
            result = await oracle_main.run_pipeline(topic)
            if result["status"] == "success":
                msg = (
                    f"✅ <b>Posted!</b>\n"
                    f"Topic: <b>{topic}</b>\n"
                    f"IG ID: <code>{result.get('ig_post_id')}</code>"
                )
            elif result["status"] == "pending_review":
                msg = (
                    f"👆 <b>Video ready for review!</b>\n"
                    f"Topic: <b>{topic}</b>\n"
                    f"Check the video above and use:\n"
                    f"✅ /done {result['pending_id']}\n"
                    f"❌ /no {result['pending_id']}"
                )
            else:
                msg = f"⚠️ Result: {result['status']} for <b>{topic}</b>"
        except Exception as e:
            msg = f"❌ Pipeline error: {str(e)[:200]}"

        await self.send_message(msg, chat_id)

    # ── Polling ────────────────────────────────────────────────────────────────

    async def start_polling(self):
        log.info("Starting Telegram long-polling...")
        offset = 0
        while True:
            try:
                updates = await self.get_updates(offset=offset, timeout=30)
                for update in updates:
                    offset = update["update_id"] + 1
                    await self.handle_update(update)
            except Exception as e:
                log.error(f"Polling error: {e}")
                await asyncio.sleep(5)